#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""
selftest_pipeline.py — 2026-09-02에 실제로 터진 사고들의 회귀 테스트.

각 테스트는 "그때 무엇이 잘못됐는지"를 이름과 메시지에 남긴다. 실패하면 exit 1.
읽기 전용(leads_raw 안 씀) → 락 불필요. daily_pipeline 앞단·수동 점검에서 돌린다.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
DATA = Path("~/lead-pipeline/data").expanduser()
fails: list[str] = []
oks: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (oks if cond else fails).append(f"{name}{(' — ' + detail) if detail else ''}")


# ── 1. validator: 판정 불가를 review로 삼키지 않는가 ─────────────────────────
# 사고: claude CLI가 빈 출력을 내면 리드를 review로 격리 → 1,529건이 조용히 묶였다.
src = (SCRIPTS / "llm_validator_batch.py").read_text(encoding="utf-8")
check("validator: 파싱실패를 review로 반환하지 않음",
      'return "review", "llm_parse_error"' not in src,
      "파싱 실패가 다시 review로 새면 대량 격리가 재발한다")
check("validator: 판정불가 전용 신호 존재", '__llm_error__' in src)
check("validator: 연속 실패 시 런 중단", 'LLM_ERROR_ABORT_AFTER' in src and 'aborted = True' in src)
check("validator: 중단 시 비정상 종료", 'sys.exit(1)' in src)

# ── 2. 발송 순도 상한이 코드로 강제되는가 ────────────────────────────────────
# 사고: 수량 KPI 때문에 9월 발송 대부분이 '역대 가입 0' 채널로 채워졌다.
pull = (SCRIPTS / "pull_from_backlog.py").read_text(encoding="utf-8")
m = re.search(r"TIER2_MAX_RATIO\s*=\s*([0-9.]+)", pull)
check("pull: 공급버티컬 상한 상수 존재", bool(m))
if m:
    check("pull: 상한이 20% 이하", float(m.group(1)) <= 0.2, f"현재 {m.group(1)}")
check("pull: 상한을 실제 선별 로직에 적용", "tier2_cap" in pull and "channel_tier(r) >= 2" in pull)

# ── 3. dedup 판정식이 단일 모듈로 존재하는가 ─────────────────────────────────
# 사고: 잔여 재고를 handle만으로 세어 "207개 남음"이라 오진(실제 net-new 0).
sys.path.insert(0, str(SCRIPTS))
try:
    from lead_dedup import Universe
    u = Universe()
    check("dedup: 공용 판정 모듈 로드", True)
    # 이미 컨택한 이메일은 반드시 새 리드가 아니어야 한다
    if u.contacted:
        sample = next(iter(u.contacted))
        ok, why = u.verdict(sample, "완전히새로운handle_xyz")
        check("dedup: 컨택이력 이메일은 handle이 새것이어도 재발송 대상 아님",
              (not ok) and why == "already_contacted", f"판정={why}")
    ok2, why2 = u.verdict("definitely-not-in-db-9f3a@example.com", "brand_new_handle_9f3a")
    check("dedup: 미지의 이메일은 new", ok2 and why2 == "new", f"판정={why2}")
    # 사고: 격리 리드 복원 때 verdict()(=새 리드인가)를 써서 전부 email_in_leads_db로
    #      걸러지고 복원 0건이 나왔다. 질문이 다르면 술어도 달라야 한다.
    check("dedup: sendable() 술어 분리 존재", hasattr(u, "sendable"))
    if u.lead_emails:
        e = next(iter(u.lead_emails - u.contacted - u.blocked), None)
        if e:
            v_new, _ = u.verdict(e)
            v_send, why3 = u.sendable(e)
            check("dedup: DB에 있고 미컨택인 리드는 new는 아니지만 sendable",
                  (not v_new) and v_send, f"sendable 판정={why3}")
    rq = (SCRIPTS / "restore_quarantined.py").read_text(encoding="utf-8")
    check("restore: 복원은 sendable()로 판정", "u.sendable(email)" in rq,
          "verdict()를 쓰면 복원이 항상 0건이 된다")
except Exception as e:
    check("dedup: 공용 판정 모듈 로드", False, str(e))

# ── 3-b. 대행사 직원 채널은 개인메일만 나가는가 ──────────────────────────────
# 운영자 지시(2026-09-02): 대행사 회사 메일로 보내면 상사가 보고 부업을 문제시해 리드가 죽는다.
fb = (SCRIPTS / "finalize_backlog.py").read_text(encoding="utf-8")
check("staff: 직원채널 상수 존재", "STAFF_PERSONAL_ONLY_CHANNELS" in fb)
check("staff: 회사도메인 차단 로직", "직원채널_회사도메인" in fb)
check("staff: role계정 차단 로직", "직원채널_role계정" in fb)
try:
    sys.path.insert(0, str(SCRIPTS))
    import finalize_backlog as _fb
    dom_ok = "gmail.com" in _fb.PERSONAL_DOMAINS
    check("staff: 개인 도메인 화이트리스트 유효", dom_ok)
    check("staff: openads가 직원채널로 등록", "openads" in _fb.STAFF_PERSONAL_ONLY_CHANNELS)
except Exception as e:
    check("staff: finalize_backlog 로드", False, str(e))
pullsrc = (SCRIPTS / "pull_from_backlog.py").read_text(encoding="utf-8")
check("staff: 직원채널이 tier1로 흐름", '"openads": 1' in pullsrc,
      "tier2로 두면 발송 상한 20%에 묶여 검증이 안 된다")

# ── 4. 동결 목록이 살아있고 러너가 실제로 참조하는가 ─────────────────────────
try:
    fz = json.loads((DATA / "channel_freeze.json").read_text(encoding="utf-8"))
    check("freeze: 동결 목록 존재", bool(fz.get("frozen")), f"{len(fz.get('frozen', {}))}채널")
except Exception as e:
    check("freeze: 동결 목록 존재", False, str(e))
dp = (DATA / "scripts/daily_pipeline.sh").read_text(encoding="utf-8")
check("freeze: daily_pipeline이 동결을 읽음", "channel_freeze.json" in dp and "run_scout" in dp)
check("freeze: 스카우트가 전부 러너 경유",
      len(re.findall(r"^\$PYTHON scripts/with_lock\.py scripts/(?!validate_leads|llm_validator_batch|finalize_backlog)",
                     dp, re.M)) == 0,  # 검수·초안 단계는 스카우트가 아니라 락만 잡는다(2026-10-02)
      "러너를 우회하면 동결이 무력화된다")

# ── 4-b. autofill도 동결을 반영하는가 ────────────────────────────────────────
# 사고: daily_pipeline만 막아서 autofill이 동결 채널(tax_agency)을 그대로 돌리고 있었다.
af = (SCRIPTS / "backlog_autofill.py").read_text(encoding="utf-8")
check("freeze: autofill이 동결 목록 반영", "frozen_scraper_names" in af and "POOL_BLOCKLIST |=" in af,
      "발굴 경로가 2개(daily_pipeline·autofill)라 둘 다 막아야 동결이 유효하다")
try:
    import backlog_autofill as _af
    fzs = _af.frozen_scraper_names()
    check("freeze: autofill 동결 목록이 비어있지 않음", len(fzs) > 0, f"{len(fzs)}개")
except Exception as e:
    check("freeze: autofill 로드", False, str(e))

# ── 5. 락 규율: 스카우트가 with_lock을 거치는가 ─────────────────────────────
# 사고: 직접 실행으로 leads_raw 동시쓰기 → openads 123건 유실(2026-08-26).
check("lock: 러너가 with_lock 경유", 'with_lock.py "$script"' in dp)
# 병렬 러너(run_scout_par)는 with_lock을 안 잡으므로, 거기 올린 스크래퍼는 반드시 commit_leads로
# 저장해야 한다(통째 덮어쓰기형을 올리면 동시쓰기 유실 재발). 2026-09-30.
_par = re.findall(r"^run_scout_par (scripts/\S+\.py)", dp, re.M)
_bad = [s for s in _par
        if "commit_leads(" not in (DATA / s).read_text(encoding="utf-8")
        or re.search(r"RAW_PATH\.write_text|os\.replace\(", (DATA / s).read_text(encoding="utf-8"))]
check("lock: 병렬 스카우트는 전부 commit_leads 저장", bool(_par) and not _bad,
      f"{len(_par)}개 병렬" + (f" / 위반 {_bad}" if _bad else ""))

# ── 6. 헬스체크가 이상을 종료코드로 알리는가 ─────────────────────────────────
hp = (SCRIPTS / "pipeline_health.py").read_text(encoding="utf-8")
check("health: 이상 시 비정상 종료", "return 1 if alerts else 0" in hp)
check("health: 순도·격리·재고 3종 감시", all(k in hp for k in ("MIN_PURITY", "QUARANTINE_ALERT", "proven_stock")))

# ── 7. 2026-09-30~10-01 사고 회귀 ─────────────────────────────────────────
try:
    import importlib, os as _os, tempfile as _tf, json as _json
    sys.path.insert(0, str(SCRIPTS))
    _fbm = importlib.import_module("finalize_backlog")
    _L = [{"outreach_status": "pending", "email": "a@naver.com", "channel": "naver_blog"},
          {"outreach_status": "pending", "email": "b@naver.com", "channel": "naver_blog", "llm_checked": "x"}]
    _fbm.step1_qualify(_L, set(), set())
    check("order: LLM 검수 표시 없는 pending은 qualify 안 됨(9/30 266/526 중단 사고)",
          [l["outreach_status"] for l in _L] == ["pending", "qualified"])
    _H = [{"outreach_status": "qualified", "channel": "zz_unproven"}, {"outreach_status": "qualified", "channel": "naver_blog"}]
    _fbm.hold_gate_ineligible(_H)
    check("gate: 게이트 비대상 채널은 초안 전 hold(좀비 backlog 재발 방지)",
          [l["outreach_status"] for l in _H] == ["hold", "qualified"])
    vb = (SCRIPTS / "llm_validator_batch.py").read_text(encoding="utf-8")
    check("order: validator가 keep에 llm_checked 표시", 'lead["llm_checked"]' in vb)
    sf = (SCRIPTS / "send_from_sheet.py").read_text(encoding="utf-8")
    check("send: 신규 우선 정렬 후 상한 자르기", "RECONTACT_MARKS" in sf and "to_send = to_send[:args.max]" in sf
          and "if len(to_send) >= args.max:\n            break" not in sf)
    check("pull: 게이트 비대상 행 매일 청소", "게이트 비대상 채널" in pull)
    check("autofill: 재고는 게이트 통과분만 셈", "_load_proven_channels" in af)
    _ls = importlib.import_module("leads_store")
    check("store: 가짜 이메일 차단", all(_ls.is_junk_email(x) for x in
          ("info@creativecommons.org", "_732_348@2x.mp", "u003einfo@a.com")) and not _ls.is_junk_email("ab12@gmail.com"))
    check("store: 죽은 도메인 사전 차단 경로 존재", "_dead_domain_filter" in (SCRIPTS / "leads_store.py").read_text())
    check("copy: 콜드 본문에 em dash·해요체 없음", "—" not in _fbm.COMMON_BODY + _fbm.PRODUCT_INTRO
          and "돼요" not in _fbm.COMMON_BODY)
    check("health: 운영사고 4종 감시(검수적체·개인화실패·신규0·SMTP실패)", "def ops_alerts" in hp)
    check("llm: 검수·개인화는 프로젝트 컨텍스트 없이 호출(132k→24k토큰/건)",
          "claude_lean(" in vb and "claude_lean(" in (SCRIPTS / "finalize_backlog.py").read_text()
          and "[CLAUDE_BIN, \"-p\"" not in vb)
    check("llm: 이미 검수된(llm_checked) pending은 재검수 안 함", 'not l.get("llm_checked")' in vb)
    check("llm: 자동화 전용 토큰(.env CLAUDE_CODE_OAUTH_TOKEN) 사용 경로", "_automation_env" in
          (SCRIPTS / "claude_cli.py").read_text())
    check("health: claude 인증 만료 감시", "OAuth" in hp and "setup-token" in hp)
    check("pipeline: 개인화 실패 초안 자동 복구 단계", "repersonalize_backlog.py" in dp)
    check("lock: 파이프라인의 검수·초안(통째 덮어쓰기)도 with_lock 경유",
          all(f"with_lock.py scripts/{x}" in dp for x in ("validate_leads.py", "llm_validator_batch.py", "finalize_backlog.py")))
    check("reserve: 1:1 트랙 예약자는 재컨택·초안·발송 제외",
          "reserved_emails" in (SCRIPTS / "recontact_verified.py").read_text() and "RESERVED_1TO1" in
          (SCRIPTS / "finalize_backlog.py").read_text() and "reserved_emails" in sf)
except Exception as e:
    check("regress 0930: 로드", False, str(e)[:120])

print("══ 파이프라인 셀프테스트 ══")
for o in oks:
    print(f"  ✅ {o}")
for f in fails:
    print(f"  ❌ {f}")
print(f"\n통과 {len(oks)} / 실패 {len(fails)}")
sys.exit(1 if fails else 0)
