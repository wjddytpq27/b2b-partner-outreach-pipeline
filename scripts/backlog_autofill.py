#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""
backlog_autofill.py — 자기강화 재고 보충 (self-reinforcing supply)
==================================================================
콜드 발송 재고(backlog)가 목표 밑으로 떨어지면 스스로 채운다.
  1. backlog 잔량 < FLOOR → 발동 (>= FLOOR면 아무것도 안 함)
  2. scripts/*_scraper.py 자동 발견 → 채널별 72h 쿨다운 로테이션으로
     안 돈(fresh) 채널부터 라운드당 최대 MAX_CHANNELS_PER_ROUND개 수확
  3. 키워드검수 → LLM검수 → finalize(개인화)
  4. 목표(TARGET) 도달 또는 라운드 순증 < MIN_YIELD(소진) 또는
     수확 대기 채널 없음 → 중단 (최대 MAX_ROUNDS)
  ※ 신규 채널 발굴은 주간 channel-scout cron(월 09:00)에 위임.
    (2026-08-30: 소진 채널 무한 grind + 라운드마다 오퍼스 channel-scout 재발동으로
     주말 세션 폭발 → 인라인 scout 제거 + 쿨다운 로테이션으로 재설계)

⚠️ 스카우트는 반드시 순차 (leads_raw.json 동시쓰기 = 데이터 손실).
⚠️ lock으로 daily_pipeline·중복 autofill과 배타.

cron 권장: 발송 전날 밤 (화·수·목 발송이면 월·화·수 22:00)
  0 22 * * 1,2,3 .../python3.14 scripts/backlog_autofill.py >> data/autofill.log 2>&1

수동:
  python3.14 scripts/backlog_autofill.py            # 실제 보충
  python3.14 scripts/backlog_autofill.py --check     # 잔량만 확인(보충 안 함)
  python3.14 scripts/backlog_autofill.py --target 500 --floor 350
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

ROOT = Path("~/lead-pipeline").expanduser()
PY = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
CLAUDE = "/opt/homebrew/bin/claude"
CREDS = os.path.expanduser("~/.config/lead-pipeline/credentials.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
SHEET_ID = "YOUR_SEND_SHEET_ID"
KST = timezone(timedelta(hours=9))

# ── 목표선 (2026-08-31: 하루 100 × 평일 매일 = 주 500 소진으로 전환) ──
DEFAULT_TARGET = 400   # 이만큼 쌓이면 만족 (약 4일치 버퍼)
DEFAULT_FLOOR = 250    # 이 밑이면 보충 발동 (2.5일치 버퍼 — 매일 100 발송 대비 안전마진)
MAX_ROUNDS = 3
MIN_YIELD_PER_ROUND = 20  # 라운드당 이만큼도 못 채우면 = 소진 판단 → 이번 회차 중단

# ── 자동 채널 로테이션 (2026-08-30) ──
# 문제: SCOUTS에 하드코딩된 낡은 10개만 재탕 → 8월 신규 고수율 채널(coffee_oem 등 149개)
#       미편입으로 놀고, 소진 채널만 갈며 토큰 폭발.
# 해결: scripts/*_scraper.py 자동 발견 + 채널별 쿨다운. fresh 우선 수확, 마른 채널 스킵.
CHANNEL_STATE = ROOT / "data" / "autofill_channel_state.json"

# ── 동결 채널 스킵 (2026-09-02) ─────────────────────────────────────────────
# 사고: daily_pipeline만 동결을 반영해서, autofill이 정책 동결 채널(tax_agency 등)을
#       그대로 돌리고 있었다. 로테이션이 자동 발견 방식이라 여기서도 막아야 한다.
# 근거: channel_freeze.json = 정책 동결(공급버티컬) + 통계 동결(발송300↑ 가입0).
def frozen_scraper_names() -> set[str]:
    """동결 채널명 → 해당 스크래퍼 파일명 후보 집합."""
    try:
        fz = json.loads((ROOT / "data" / "channel_freeze.json").read_text(encoding="utf-8"))
    except Exception:
        return set()
    out: set[str] = set()
    for ch in fz.get("frozen", {}):
        out.update({f"{ch}_scraper.py", f"{ch}_center_scraper.py",
                    f"{ch}_instructor_scraper.py", f"{ch}_api_scraper.py"})
    return out
CHANNEL_COOLDOWN_H = 72          # 같은 채널 재수확 최소 간격(시간)
MAX_CHANNELS_PER_ROUND = 6       # 라운드당 최대 채널 수(폭주 상한)
SCRAPER_TIMEOUT = 900            # 채널당 실행 상한(초)
# 자동 로테이션에서 제외: hang·소진확정·기각·비표준(회사 다수)
POOL_BLOCKLIST = {
    "photo_studio_scraper.py",      # 반복 hang(프로빙 무한지연)
    "imweb_expert_scraper.py",      # 소진확정+452p 재크롤 race
    "sixshop_partner_scraper.py",   # net-new 0(기존 빌더채널 완전중복)
    "kmong_builder_scraper.py",     # 폐기(이메일 벽)
    "fund_agency_scraper.py",       # 기각(순도0)
    "juice_oem_scraper.py",         # 기각(corp-heavy)
    "perfume_oem_scraper.py",       # 보류(thin)
    "cylinder_pack_scraper.py",     # 기각(tight시 소진)
    "filling_agency_scraper.py",    # 기각(pos 3)
    "globalselling_scraper.py",     # 기각(순도0)
    "wishket_scraper.py", "wishket_v3_scraper.py",  # 회사 다수(NG 타겟)
    "class101_scraper.py", "class101_instructor_scraper.py", "taling_scraper.py",  # 중개몰(이메일 미노출)
    "cafe24_expert_v2_scraper.py", "cafe24_expert_v3_scraper.py",  # 옛 버전(현행=cafe24_expert_auth)
    "cafe24_expert_pw_scraper.py",  # 옛 버전
}
# 정책·통계 동결 채널을 로테이션 풀에서도 제외(매 실행 시 최신 목록 반영)
POOL_BLOCKLIST |= frozen_scraper_names()


LOCK = ROOT / "data" / "autofill.lock"
LOCK_TTL = 4 * 3600  # 4시간 넘은 lock은 죽은 것으로 간주

# 스카우트: daily_pipeline 검증 인자 + 증량. 순차 실행.
SCOUTS = [
    # 🛠 빌더 우선 — 실측상 판 파트너 다수가 빌더(크몽·카페24·아임웹). 최고 전환 풀이라 맨 앞.
    # [PY, "scripts/imweb_expert_scraper.py", "--pages", "30"],  # 비활성화(2026-08-27): 소진 확정(net-new 0.0%·168샘플). 매 라운드 452p 재크롤로 3h+ 낭비+leads_raw race 유발. 신규 등재 시 수동 실행.
    [PY, "scripts/cafe24_designer_from_agencies.py"],  # elastic 실시간열거 465풀·순도100%·이메일~100% (2026-07-06 주력화)
    # [PY, "scripts/photo_studio_scraper.py", "--max", "200"],  # 비활성화(2026-08-07): 반복 hang(프로빙 무한지연)+포화(net-new~0). 필요시 수동 실행.
    [PY, "scripts/detailpage_studio_scraper.py", "--max", "200"],  # 상세페이지·브랜딩 디자인 프리랜서·net-new 90+ (2026-07-13 channel-scout GO, photo/builder와 disjoint 버티컬)
    # [PY, "scripts/sixshop_partner_scraper.py"],  # 비활성화(2026-08-27): net-new 0(기존 빌더채널과 완전중복). 신규 파트너 등재 시에만 수동 실행.
    # [PY, "scripts/kmong_builder_scraper.py"],  # 폐기 — 이메일 벽(발굴 13%·남의이메일 함정)
    # 강의·기타 — 셀러 청중 보유 가능성(전환 미측정). 빌더 다음 순위, liveklass는 물량 축소.
    [PY, "scripts/liveklass_scraper.py", "--max", "250"],
    [PY, "scripts/funnelmoa_scraper.py", "--max", "200"],
    [PY, "scripts/didisam_scraper.py", "--max", "200"],
    [PY, "scripts/inclass_scraper.py", "--max", "200"],
    [PY, "scripts/godomall_edu_scraper.py", "--max", "200"],
    [PY, "scripts/youtube_api_scraper.py", "--max", "300"],
    [PY, "scripts/coloso_instructor_scraper.py"],
    [PY, "scripts/fabric_supply_scraper.py", "--max", "120"],  # 자체제작 의류/패션 셀러에게 원단 도매·사입 공급·전상법 footer 개인메일·보조 고순도 (2026-08-04 channel-scout 발굴)
]

# 검수 → 개인화 finalize (llm/finalize는 claude 호출 다수 → 넉넉한 timeout)
PIPELINE = [
    ([PY, "scripts/validate_leads.py"], 1800),
    ([PY, "scripts/llm_validator_batch.py"], 10800),
    ([PY, "scripts/finalize_backlog.py"], 10800),
]


def log(msg: str) -> None:
    ts = datetime.now(KST).strftime("%m-%d %H:%M:%S")
    print(f"[autofill {ts}] {msg}", flush=True)


def backlog_count() -> int:
    """backlog에서 '실제로 발송 가능한'(게이트 통과 채널) 행 수. 실패 시 -1.
    2026-09-30: 전체 행 수(427)를 재고로 세서 게이트 드롭될 좀비 367건 때문에 '재고 충분'으로
    오판 → 보충이 멈췄다. 판정식은 send_from_sheet(원장 검증채널 ∪ 테스트후보) 한 곳."""
    from send_from_sheet import CANDIDATE_CHANNELS, _load_proven_channels
    proven = _load_proven_channels()
    ok = (proven | CANDIDATE_CHANNELS) if proven else None
    for attempt in range(5):
        try:
            gc = gspread.authorize(Credentials.from_service_account_file(CREDS, scopes=SCOPES))
            rows = gc.open_by_key(SHEET_ID).worksheet("backlog").get_all_values()[1:]
            if ok is None:  # 원장 못 읽으면 예전처럼 전체 행 수
                return len(rows)
            return sum(1 for r in rows if len(r) > 2 and r[2].strip() in ok)
        except Exception as e:
            if attempt == 4:
                log(f"backlog 조회 실패: {str(e)[:60]}")
                return -1
            time.sleep(3 * (attempt + 1))
    return -1


def run(cmd: list[str], timeout: int = 1800) -> None:
    name = cmd[1].split("/")[-1] if len(cmd) > 1 else cmd[0]
    # leads_raw 동시쓰기 방지(2026-08-27): 스크래퍼/파이프라인을 글로벌 락 래퍼를 거쳐 실행.
    # cmd = [PY, script, args...] → [PY, with_lock.py, script, args...]
    locked = [cmd[0], str(ROOT / "scripts" / "with_lock.py")] + cmd[1:]
    try:
        subprocess.run(locked, cwd=str(ROOT), timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"  ⏱ timeout: {name}")
    except Exception as e:
        log(f"  ✗ 실패 {name}: {str(e)[:50]}")


def _channel_args(name: str) -> list[str]:
    """SCOUTS에 튜닝된 인자가 있으면 유지(youtube --max 등), 없으면 기본(무인자=스크래퍼 기본 수확)."""
    for c in SCOUTS:
        if c[1].split("/")[-1] == name:
            return list(c[2:])
    return []


def _load_channel_state() -> dict:
    try:
        return json.loads(CHANNEL_STATE.read_text())
    except Exception:
        return {}


def _save_channel_state(state: dict) -> None:
    try:
        CHANNEL_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=0))
    except Exception as e:
        log(f"  channel_state 저장 실패: {str(e)[:40]}")


def select_due_channels(state: dict, now_ts: float) -> list[str]:
    """scripts/*_scraper.py 자동 발견 → 블록리스트 제외 → 쿨다운 지난 것만,
    가장 오래 안 돈(=never/least-recent, fresh 우선) 순으로 최대 N개."""
    due = []
    for f in sorted(glob.glob(str(ROOT / "scripts" / "*_scraper.py"))):
        name = os.path.basename(f)
        if name in POOL_BLOCKLIST:
            continue
        last = state.get(name, 0)
        if now_ts - last >= CHANNEL_COOLDOWN_H * 3600:
            due.append((last, f))
    due.sort()  # last_run 오름차순 → 안 돈 것 먼저
    return [f for _, f in due[:MAX_CHANNELS_PER_ROUND]]


def trigger_channel_scout() -> None:
    """채널 소진 신호 → channel-scout 에이전트로 새 채널 발굴 + 스크래퍼 생성 (구독요금제)."""
    log("  ⚡ 채널 소진 감지 → channel-scout 발동 (새 채널·스크래퍼 발굴)")
    prompt = (
        "Acme 파트너 리드 발굴 채널이 소진되고 있다. backlog 재고가 목표에 못 미친다. "
        "channel-scout 역할로 아직 안 쓰는 신규 채널을 발굴하고, 가능하면 스크래퍼(scripts/{채널}_scraper.py)까지 "
        "만들어서 leads_raw에 리드가 유입되게 하라. 셀러 청중을 가진 개인 크리에이터·강사·전문가·쇼핑몰 빌더가 "
        "있는 채널이어야 한다. 완료 후 무엇을 추가했는지 한 줄로 요약."
    )
    try:
        subprocess.run(
            [CLAUDE, "-p", prompt, "--agent", "channel-scout", "--dangerously-skip-permissions"],
            cwd=str(ROOT), timeout=3600,
        )
    except Exception as e:
        log(f"  channel-scout 실패: {str(e)[:50]}")


def acquire_lock() -> bool:
    if LOCK.exists():
        age = time.time() - LOCK.stat().st_mtime
        if age < LOCK_TTL:
            log(f"다른 파이프라인/autofill 실행 중(lock {int(age)}s). 종료.")
            return False
        log(f"오래된 lock({int(age)}s) 무시하고 진행.")
    LOCK.write_text(str(time.time()))
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET)
    ap.add_argument("--floor", type=int, default=DEFAULT_FLOOR)
    ap.add_argument("--check", action="store_true", help="잔량만 확인, 보충 안 함")
    args = ap.parse_args()

    # keychain unlock — cron(비대화 세션)에서 claude/gspread 인증 접근용.
    # 비번은 ~/lead-pipeline/data/data/.kc_pw (심링크). 파일 없으면 조용히 skip.
    import os
    kc = ROOT / "data" / ".kc_pw"
    if kc.exists():
        subprocess.run(
            ["security", "unlock-keychain", "-p", kc.read_text().strip(),
             os.path.expanduser("~/Library/Keychains/login.keychain-db")],
            capture_output=True,
        )
        log("keychain unlock 시도함")

    q = backlog_count()
    log(f"backlog 현재 {q} / floor {args.floor} / target {args.target}")

    if args.check:
        need = max(0, args.target - q)
        log(f"{'보충 필요' if q < args.floor else '재고 충분'} (부족분 {need})")
        return

    if q < 0:
        log("잔량 조회 실패로 중단(무분별 스카우트 방지).")
        return
    if q >= args.floor:
        log(f"재고 충분(>={args.floor}). 보충 불필요.")
        return

    if not acquire_lock():
        return
    try:
        rounds = 0
        ch_state = _load_channel_state()
        while q < args.target and rounds < MAX_ROUNDS:
            rounds += 1
            before = q
            now_ts = time.time()
            due = select_due_channels(ch_state, now_ts)
            if not due:
                log(f"─ 라운드 {rounds}/{MAX_ROUNDS}: 수확 대기 채널 없음"
                    f"(전부 쿨다운 {CHANNEL_COOLDOWN_H}h 중) → 이번 회차 보충 중단.")
                break
            log(f"─ 라운드 {rounds}/{MAX_ROUNDS}: 채널 {len(due)}개 수확 → "
                + ", ".join(os.path.basename(f) for f in due))
            for f in due:
                name = os.path.basename(f)
                run([PY, f] + _channel_args(name), timeout=SCRAPER_TIMEOUT)
                ch_state[name] = now_ts   # 성과 무관 스탬프 → 마른 채널은 쿨다운 동안 스킵
            _save_channel_state(ch_state)
            # finalize/llm은 claude -p 다수 호출 → 직전에 claude 건강 확인·자동복구
            # (자동업뎃이 native 없이 깨뜨리면 generic fallback 발송사고 나므로)
            subprocess.run(["/bin/zsh", str(ROOT / "scripts" / "ensure_claude.sh")],
                           timeout=300)
            log("검수 → 개인화 finalize")
            for cmd, to in PIPELINE:
                run(cmd, timeout=to)
            q = backlog_count()
            if q < 0:
                # 라운드 중 네트워크/조회 실패 → 소진 오판 금지, 조용히 중단(다음 회차가 만회)
                log("라운드 중 backlog 조회 실패(네트워크 추정) → 채널스카우트 없이 중단.")
                break
            gained = q - before
            log(f"라운드 {rounds} 결과: backlog {q} (+{gained})")
            # 주의(2026-08-31): 순증<MIN_YIELD로 break하지 않는다.
            # 로테이션 설계에선 라운드마다 다른 채널을 수확하므로(쿨다운이 재탕 차단),
            # 첫 배치가 저조해도 뒤 라운드에 고수율 fresh 채널이 남아있을 수 있다.
            # 소진 판단은 select_due_channels가 빈 리스트를 반환(=수확 대기 채널 없음)할 때만.
            # 폭주는 MAX_ROUNDS·MAX_CHANNELS_PER_ROUND·쿨다운·target 도달로 이미 상한.
        log(f"완료. 최종 backlog {q} (목표 {args.target})")
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
