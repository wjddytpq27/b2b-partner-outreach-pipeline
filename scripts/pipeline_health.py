#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""
pipeline_health.py — 모집 파이프라인 단일 진실 리포트 + 이상 감지 게이트.

왜 존재하나 (2026-09-02 사고):
  9월 발송 대부분이 "역대 가입 0" 채널로 채워졌는데 아무도 몰랐다. 운영자가 발송 대기열을
  직접 눈으로 보고 발견했다. 그 사이 validator 파싱 실패로 리드 1,529건이 조용히 격리됐고,
  나는 진행 포인터/로그 tail 같은 대리지표로 "소진"을 오진했다.
  → 사람이 큐를 뒤지지 않아도 매일 같은 형식으로 진실이 나오고, 위험하면 발송을 막는다.

하는 일
  1) 채널별 발송→가입 전환율 (contacted_list ↔ 파트너 명단 조인)
  2) 발송 대기열(backlog + 시트1 미발송) 검증채널 순도
  3) 채널별 미발송 재고 + 격리(review/llm_parse_error) 감지
  4) 0가입 채널 자동 동결 판정 → data/channel_freeze.json
  5) 원장 기록 → data/channel_ledger.json (소진·전환 주장은 이 원장만 근거로 인정)

종료코드: 0 정상 / 1 차단성 이상(발송 전 사람이 봐야 함)
읽기 전용(leads_raw 안 씀) → 글로벌 락 불필요.
사용: python3.14 pipeline_health.py [--json]
"""
from __future__ import annotations
import os
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lead_dedup import Universe, DATA  # noqa: E402

LEADS = DATA / "leads_raw.json"
LEDGER = DATA / "channel_ledger.json"
FREEZE = DATA / "channel_freeze.json"
POLICY = DATA / "channel_policy.json"
PARTNER_SHEET = "YOUR_PARTNER_SHEET_ID"
SEND_SHEET = "YOUR_SEND_SHEET_ID"
CREDS = os.path.expanduser("~/.config/lead-pipeline/credentials.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

# ── 동결 규칙 ──────────────────────────────────────────────────────────────────
# 검증채널 baseline 전환율 ≈1%. 진짜 1%인 채널이 n건 발송에서 가입 0일 확률 = 0.99^n.
#   n=120 → 30%  (아직 판단 유보)
#   n=200 → 13%  (경고)
#   n=300 →  5%  (통계적으로 사망 — 동결)
FREEZE_SENT = 300     # 이 이상 보냈고 가입 0 → 동결(신규 발굴 중단)
WATCH_SENT = 200      # 이 이상 보냈고 가입 0 → 감시
MIN_PURITY = 0.80     # 발송 대기열의 검증채널 최소 순도
QUARANTINE_ALERT = 50  # 격리(llm_parse_error) 이 이상이면 이상 신고

KST = timezone(timedelta(hours=9))

# 이상이 있으면 사람이 찾아오길 기다리지 않고 먼저 알린다
# (운영자가 발송 대기열을 눈으로 보고 드리프트를 발견한 2026-09-02 사고 방지).
# 토큰/채널은 warehouse_to_sheet.py와 동일한 운영 채널을 재사용한다.
def _slack_conf():
    import env_secret
    return env_secret.get("SLACK_BOT_TOKEN"), env_secret.get("SLACK_CHANNEL", "YOUR_SLACK_CHANNEL_ID")


def post_slack(text: str) -> None:
    import urllib.request
    tok, ch = _slack_conf()
    if not tok or not ch:
        return
    try:
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": ch, "text": text}).encode("utf-8"),
            headers={"Authorization": f"Bearer {tok}",
                     "Content-Type": "application/json; charset=utf-8"})
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def _gs():
    import gspread
    from google.oauth2.service_account import Credentials
    return gspread.authorize(Credentials.from_service_account_file(CREDS, scopes=SCOPES))


def _partner_emails(gc) -> set[str]:
    ws = gc.open_by_key(PARTNER_SHEET).worksheet("warehouse_1076_partners")
    v = ws.get_all_values()
    if not v:
        return set()
    h = v[0]
    ei = h.index("이메일") if "이메일" in h else 2
    return {(r[ei] or "").strip().lower() for r in v[1:] if len(r) > ei and r[ei].strip()}


def channel_conversion(partners: set[str]) -> dict:
    """채널별 발송(중복제거) → 가입 전환. 단일 정의: contacted_list의 platform 기준."""
    contacted = json.loads((DATA / "contacted_list.json").read_text(encoding="utf-8"))
    by = defaultdict(set)
    for x in contacted:
        e = (x.get("email") or "").strip().lower()
        if not e or "@" not in e:
            continue
        by[(x.get("platform") or "unknown").strip()].add(e)
    # 페르소나별 전환(채널과 별개 축): leads_store가 수집 시 남긴 원장으로 contacted와 조인.
    # "persona:" 키는 시트 플랫폼 열과 매칭될 일이 없어 순도 판정엔 영향 없음.
    lp = DATA / "persona_ledger.json"
    if lp.exists():
        sent = set().union(*by.values()) if by else set()
        for e, v in json.loads(lp.read_text(encoding="utf-8")).items():
            if e in sent:
                by[f"persona:{v.get('persona')}"].add(e)
    out = {}
    for ch, emails in by.items():
        conv = len(emails & partners)
        out[ch] = {"sent": len(emails), "conv": conv,
                   "rate": round(conv / len(emails) * 100, 2) if emails else 0.0}
    return out


def proven_channels(conv: dict) -> set[str]:
    """'검증채널' = 실측 가입이 1건 이상 나온 채널. 감이 아니라 데이터가 정의한다."""
    return {ch for ch, v in conv.items() if v["conv"] > 0}


def queue_state(gc, proven: set[str]) -> dict:
    sh = gc.open_by_key(SEND_SHEET)
    def rows(tab):
        v = sh.worksheet(tab).get_all_values()
        return v[0], v[1:]
    bh, brows = rows("backlog")
    sh1_h, sh1 = rows("시트1")
    bp = bh.index("플랫폼") if "플랫폼" in bh else 2
    bd = bh.index("발송완료") if "발송완료" in bh else 5
    sp = sh1_h.index("플랫폼") if "플랫폼" in sh1_h else 2
    sd = sh1_h.index("발송완료") if "발송완료" in sh1_h else 5

    def pend(rs, pi, di):
        out = []
        for r in rs:
            r = r + [""] * (max(pi, di) + 1 - len(r))
            if str(r[di]).strip():
                continue
            out.append((r[pi] or "").strip())
        return out
    q = pend(brows, bp, bd) + pend(sh1, sp, sd)
    c = Counter(q)
    pv = sum(n for ch, n in c.items() if ch in proven)
    return {"pending_total": len(q), "proven": pv,
            "purity": round(pv / len(q), 3) if q else 1.0,
            "mix": c.most_common(15)}


def stock_state(u: Universe, proven: set[str]) -> dict:
    raw = json.loads(LEADS.read_text(encoding="utf-8"))
    by = defaultdict(Counter)
    quarantined = 0
    for l in raw.get("leads", []):
        e = (l.get("email") or "").strip().lower()
        if not e or "@" not in e or e in u.contacted or e in u.blocked:
            continue
        st = l.get("outreach_status", "?")
        if st == "disqualified":
            continue
        ch = (l.get("channel") or l.get("platform") or "?")
        by[ch][st] += 1
        if st == "review" and "llm_parse_error" in (l.get("notes") or ""):
            by[ch]["_quarantined"] += 1
            quarantined += 1
    proven_stock = sum(sum(v for k, v in c.items() if not k.startswith("_"))
                       for ch, c in by.items() if ch in proven)
    return {"quarantined": quarantined, "proven_stock": proven_stock,
            "by_channel": {ch: dict(c) for ch, c in by.items()}}


def ops_alerts(gc) -> list[str]:
    """2026-09-30~10-01에 실제로 터진 운영 사고 4종을 매일 감지(사람이 눈으로 봐야 알던 것 제거).
    ① LLM 검수 중단으로 검수 안 된 pending 적체 ② 개인화 실패로 기본문장만 든 초안 비율
    ③ 직전 콜드 발송에 신규 0건 ④ 직전 발송 SMTP 실패."""
    out = []
    try:
        raw = json.loads((DATA / "leads_raw.json").read_text(encoding="utf-8"))
        leads = raw if isinstance(raw, list) else raw.get("leads", [])
        # 이틀 이상 지난 것만: 15:57 점검 땐 당일 파이프라인 검수가 아직 안 돌았을 수 있어 오탐 방지
        cutoff = (datetime.now(KST).date() - timedelta(days=1)).isoformat()
        stuck = [l for l in leads if l.get("outreach_status") == "pending" and not l.get("llm_checked")
                 and (l.get("collected_at") or "9999")[:10] < cutoff]
        if len(stuck) >= 50:
            out.append(f"LLM 검수 안 된 pending {len(stuck)}건 적체 — llm_validator_batch 중단/실패 의심 "
                       f"(daily_pipeline.log에서 '🛑 LLM 판정 실패' 확인)")
    except Exception as e:
        out.append(f"leads_raw 읽기 실패: {str(e)[:60]}")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from finalize_backlog import _intro_line, BUILDER_CHANNELS, AUDIENCE_CHANNELS, SELLER_CHANNELS
        fb = {_intro_line("", c) for c in (next(iter(BUILDER_CHANNELS)), next(iter(AUDIENCE_CHANNELS)),
                                            next(iter(SELLER_CHANNELS)), "_other_")}
        rows = gc.open_by_key(SEND_SHEET).worksheet("backlog").get_all_values()[1:]
        bodies = [r[4] for r in rows if len(r) > 4 and r[4].strip()]
        n_fb = sum(1 for b in bodies if any(x in b for x in fb))
        if len(bodies) >= 30 and n_fb / len(bodies) > 0.5:
            out.append(f"backlog 초안 {len(bodies)}건 중 {n_fb}건이 개인화 실패(채널 기본문장) — claude 호출 실패 의심")
    except Exception as e:
        out.append(f"개인화 비율 점검 실패: {str(e)[:60]}")
    try:
        log = (DATA / "send.log").read_text(encoding="utf-8", errors="ignore").splitlines()
        comp = [x for x in log if x.startswith("발송 구성: 신규 ")]
        if comp and comp[-1].startswith("발송 구성: 신규 0건"):
            out.append(f"직전 콜드 발송에 신규 0건 ({comp[-1]}) — backlog 검증채널 재고/충전 확인")
        summ = [json.loads(x) for x in (DATA / "send_summary.jsonl").read_text().splitlines()[-5:] if x.strip()]
        cold = [s for s in summ if s.get("tab") == "시트1"]
        if cold and cold[-1].get("failed", 0) > 0:
            out.append(f"직전 콜드 발송 SMTP 실패 {cold[-1]['failed']}건 ({cold[-1]['date']}) — 재발송 필요")
    except Exception as e:
        out.append(f"발송 로그 점검 실패: {str(e)[:60]}")
    # claude 인증 만료(2026-10-01 검수 중단 진짜 원인): 최근 24h llm_fail.log에 인증 실패가 있으면 알림
    try:
        cut = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
        fails = [x for x in (DATA / "llm_fail.log").read_text(encoding="utf-8").splitlines() if x[:19] >= cut]
        auth = [x for x in fails if "authenticate" in x or "OAuth" in x]
        if auth:
            out.append(f"claude 인증 만료로 LLM 호출 {len(auth)}건 실패 — 터미널에서 `claude setup-token` 후 "
                       f".env에 CLAUDE_CODE_OAUTH_TOKEN 넣기(자동화 전용 토큰)")
        elif len(fails) >= 10:
            out.append(f"최근 24h LLM 호출 실패 {len(fails)}건 — data/llm_fail.log 확인")
    except FileNotFoundError:
        pass
    except Exception as e:
        out.append(f"llm_fail.log 점검 실패: {str(e)[:60]}")
    # 셀프테스트는 실패해도 exit 1뿐이라 아무도 몰랐다 → 헬스 알림에 합친다
    try:
        import subprocess
        st = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "selftest_pipeline.py")],
                            capture_output=True, text=True, timeout=180)
        if st.returncode != 0:
            fails = [x.strip() for x in st.stdout.splitlines() if x.strip().startswith("❌")]
            out.append(f"셀프테스트 실패 {len(fails)}건: " + " / ".join(fails[:3]))
    except Exception as e:
        out.append(f"셀프테스트 실행 실패: {str(e)[:60]}")
    return out


def main() -> int:
    as_json = "--json" in sys.argv
    gc = _gs()
    partners = _partner_emails(gc)
    conv = channel_conversion(partners)
    proven = proven_channels(conv)
    u = Universe()
    q = queue_state(gc, proven)
    stock = stock_state(u, proven)

    # ── 동결 판정 ──
    freeze, watch = {}, {}
    for ch, v in conv.items():
        if v["conv"] == 0 and v["sent"] >= FREEZE_SENT:
            freeze[ch] = {"sent": v["sent"], "reason": f"발송 {v['sent']}건 가입 0 (p<5%)"}
        elif v["conv"] == 0 and v["sent"] >= WATCH_SENT:
            watch[ch] = {"sent": v["sent"], "reason": f"발송 {v['sent']}건 가입 0 (p<13%)"}
    # 정책 동결(사람이 결정한 중단)과 통계 동결(데이터가 사망 판정)을 합친다.
    policy = []
    try:
        policy = json.loads(POLICY.read_text(encoding="utf-8")).get("policy_frozen", [])
    except Exception:
        pass
    for ch in policy:
        freeze.setdefault(ch, {"sent": conv.get(ch, {}).get("sent", 0),
                               "reason": "정책 동결(공급 버티컬 신규 발굴 중단, 2026-09-02 승인)"})
    # 디렉토리 소진 동결: 전환은 되나 풀이 소진된 프루븐 채널(imweb_expert·cafe24_designer 등).
    # 공급버티컬과 사유가 다르므로 별도 라벨. 스크래핑만 멈추고 발송은 유지.
    exhausted = []
    try:
        exhausted = json.loads(POLICY.read_text(encoding="utf-8")).get("exhausted_frozen", [])
    except Exception:
        pass
    for ch in exhausted:
        freeze.setdefault(ch, {"sent": conv.get(ch, {}).get("sent", 0),
                               "reason": "디렉토리 소진(2026-09-04 프로브 net-new 0, 스크래핑만 중단·발송 유지)"})
    now = datetime.now(KST).isoformat(timespec="seconds")
    FREEZE.write_text(json.dumps({"updated_at": now,
                                  "frozen": freeze, "watch": watch,
                                  "policy_count": len(policy),
                                  "_use": "daily_pipeline.sh / backlog_autofill.py가 이 목록의 채널 스크래퍼를 건너뜀"},
                                 ensure_ascii=False, indent=2), encoding="utf-8")

    ledger = {}
    if LEDGER.exists():
        try:
            ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
        except Exception:
            ledger = {}
    ledger[now] = {"conversion": conv, "queue": {k: v for k, v in q.items() if k != "mix"},
                   "stock": {"quarantined": stock["quarantined"], "proven_stock": stock["proven_stock"]},
                   "frozen": list(freeze), "watch": list(watch)}
    # 원장은 최근 60개 스냅샷만 유지(파일 비대 방지)
    for k in sorted(ledger)[:-60]:
        ledger.pop(k)
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 이상 판정 ──
    alerts = []
    if q["pending_total"] and q["purity"] < MIN_PURITY:
        alerts.append(f"발송 대기열 검증채널 순도 {q['purity']*100:.0f}% < {MIN_PURITY*100:.0f}% "
                      f"(대기 {q['pending_total']}건 중 검증채널 {q['proven']}건)")
    if stock["quarantined"] >= QUARANTINE_ALERT:
        alerts.append(f"격리 리드(llm_parse_error) {stock['quarantined']}건 — validator 환경 점검 필요")
    if stock["proven_stock"] < 140:
        alerts.append(f"검증채널 미발송 재고 {stock['proven_stock']}건 — 1일치({140}) 미달, 발굴 필요")
    alerts += ops_alerts(gc)

    if as_json:
        print(json.dumps({"conversion": conv, "queue": q, "stock": stock,
                          "frozen": freeze, "watch": watch, "alerts": alerts},
                         ensure_ascii=False, indent=2))
    else:
        print(f"══ 파이프라인 헬스 {now} ══")
        print(f"\n[검증채널] 실측 가입이 나온 채널 {len(proven)}개")
        for ch, v in sorted(conv.items(), key=lambda kv: -kv[1]["rate"]):
            if v["conv"]:
                print(f"   {ch:22s} {v['sent']:5d}발송 → {v['conv']:3d}가입 ({v['rate']}%)")
        print(f"\n[발송 대기열] {q['pending_total']}건 · 검증채널 순도 {q['purity']*100:.0f}%")
        for ch, n in q["mix"][:8]:
            print(f"   {ch:22s} {n:4d}{'  ⭐' if ch in proven else ''}")
        print(f"\n[재고] 검증채널 미발송 {stock['proven_stock']}건 · 격리 {stock['quarantined']}건")
        if freeze:
            print(f"\n[자동 동결] {len(freeze)}채널 — 신규 발굴 중단 대상")
            for ch, v in sorted(freeze.items(), key=lambda kv: -kv[1]["sent"]):
                print(f"   {ch:22s} {v['reason']}")
        if watch:
            joined = ", ".join(f"{c}({v['sent']}발송)" for c, v in watch.items())
            print(f"\n[감시] {joined}")
        if alerts:
            print("\n🛑 이상 감지")
            for a in alerts:
                print(f"   - {a}")
        else:
            print("\n✅ 이상 없음")

    if alerts and "--no-slack" not in sys.argv:
        lines = "\n".join(f"- {a}" for a in alerts)
        post_slack(f"[파트너 모집 파이프라인 이상] {now}\n{lines}\n"
                   f"대기열 {q['pending_total']}건·순도 {q['purity']*100:.0f}% / "
                   f"검증채널 재고 {stock['proven_stock']}건")
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
