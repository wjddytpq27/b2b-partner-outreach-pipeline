#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""
pull_from_backlog.py
====================
발송일 아침에 backlog에서 N건 꺼내서 시트1에 추가.
backlog에서는 해당 행 삭제.

사용:
  python3.14 pull_from_backlog.py          # 기본 100건
  python3.14 pull_from_backlog.py 200      # 200건
"""
import os
import json
import sys
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

CREDS = os.path.expanduser("~/.config/lead-pipeline/credentials.json")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_ID = "YOUR_SEND_SHEET_ID"
DATA = Path("~/lead-pipeline/data").expanduser()


def load_blocked_emails() -> set[str]:
    """발송 불가 이메일 = leads_raw disqualified + bounce_list + blocklist.
    이걸로 걸러야 죽은 리드가 pull 슬롯을 잡아먹지 않음(2026-07-14: 140칸 중 62칸이 죽은
    리드라 실발송 78로 쪼그라든 사고). send_from_sheet의 '탈락차단'과 동일 기준."""
    blocked: set[str] = set()
    try:
        raw = json.loads((DATA / "leads_raw.json").read_text())
        for l in raw.get("leads", []):
            if l.get("outreach_status") == "disqualified":
                e = (l.get("email") or "").lower().strip()
                if e:
                    blocked.add(e)
    except Exception:
        pass
    for fn in ("bounce_list.json", "blocklist.json"):
        try:
            arr = json.loads((DATA / fn).read_text())
            entries = arr if isinstance(arr, list) else arr.get("blocked", [])
            for it in entries:
                e = (it.get("email") if isinstance(it, dict) else it) or ""
                if e:
                    blocked.add(str(e).lower().strip())
        except Exception:
            pass
    return blocked


# 채널별 파트너 가입 전환율 실측(warehouse 역귀속).
# pull은 원래 backlog FIFO였으나, 검증된 모집 채널이 0-recruit 버티컬 뒤에 묶여
# 발송에서 밀리는 문제 발견 → tier 낮은(=우선) 채널부터 나가게 안정정렬.
# tier 0: 실측 가입 실적 있는 proven 채널 (전환율 순)
# tier 1: 셀러를 직접 상대하는 페르소나 (가입 개연성 높음, 실적 관측 대기)
# tier 2: 공급 버티컬 (역대 가입 0 — 리드 순도는 높으나 모집 전환 미검증)
CHANNEL_TIER = {
    "cafe24_expert": 0, "naver_blog": 0, "imweb_expert": 0, "youtube": 0,
    "cafe24_designer": 0, "webagency": 0, "eventers": 0,
    "builder_studio": 1, "liveklass": 1, "ecommerce_agency": 1,
    "coloso": 1,
    # stibee 제거(2026-09-14): 0% 포화·발송게이트 미통과인데 tier1이라 pull 상위90을 먹고
    # candidate(kmong)를 밀어냄=발송0 버그. DEFAULT_TIER 2로 강등.
    # 대행사 직원·개인 마케터 채널(2026-09-02 운영자 지목 페르소나): 셀러/광고주를 상대하는
    # 실무자 개인. 회사 메일이 아니라 개인 메일로만 도달한다(finalize_backlog의
    # STAFF_PERSONAL_ONLY_CHANNELS 게이트가 회사도메인·role계정을 끊는다).
    # 아직 가입 실측 전이라 tier1 — 300건 발송해서 0이면 통계 동결 규칙이 자동으로 끊는다.
    "openads": 1, "iboss": 1, "mobiinside": 1, "agency_staff": 1,
    # 테스트슬롯 후보(2026-09-10): 디렉토리 등재 개발사 유형 미검증. send_from_sheet
    # 게이트의 CANDIDATE_CHANNELS가 하루 상한(80)만큼 테스트 발송, conv 나오면 원장 승격.
    "cafe24_app": 1, "shopify_partner": 1, "kmong": 1, "titanclass": 1, "maily": 1, "sixshop_seller": 1,
}
DEFAULT_TIER = 2  # 그 외(maker/oem/packaging 등 공급 버티컬)
TIER2_MAX_RATIO = 0.2  # 한 배치에서 공급버티컬이 차지할 수 있는 최대 비율(순도 KPI 80%+)


def channel_tier(row: list) -> int:
    ch = row[2].strip() if len(row) > 2 else ""
    return CHANNEL_TIER.get(ch, DEFAULT_TIER)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100

    creds = Credentials.from_service_account_file(CREDS, scopes=SCOPES)
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(SHEET_ID)
    backlog = ss.worksheet("backlog")
    sheet1 = ss.worksheet("시트1")

    all_rows = backlog.get_all_values()
    header = all_rows[0]
    data = all_rows[1:]

    if not data:
        print("backlog 비어있음")
        return

    # 죽은 리드(disqualified/bounce/block)는 backlog에서 제거 — pull도 remaining도 아님(청소).
    blocked = load_blocked_emails()
    alive = [r for r in data if (r[1].strip().lower() if len(r) > 1 else "") not in blocked]
    dead = len(data) - len(alive)

    # 발송 게이트 비대상 채널 행도 청소(2026-09-30): 어차피 send_from_sheet가 드롭하는 좀비재고.
    # 판정식은 send_from_sheet 한 곳(원장 검증채널 ∪ 테스트후보). 원장 못 읽으면 청소 안 함.
    from send_from_sheet import CANDIDATE_CHANNELS, _load_proven_channels
    proven = _load_proven_channels()
    if proven:
        ok = proven | CANDIDATE_CHANNELS
        before = len(alive)
        alive = [r for r in alive if (r[2].strip() if len(r) > 2 else "") in ok]
        if before - len(alive):
            print(f"  ↳ 게이트 비대상 채널 {before - len(alive)}건 backlog에서 제거")
            dead += before - len(alive)

    # 채널 우선순위 안정정렬: proven 모집 채널(tier0)→셀러직접(tier1)→공급버티컬(tier2).
    # 안정정렬이라 동일 tier 내에서는 기존 backlog(FIFO) 순서 유지.
    alive.sort(key=channel_tier)

    # ── 순도 KPI 강제(2026-09-02 운영자 승인) ──
    # 실측: 전환검증 채널 약 1% / 공급버티컬 0%.
    # "일 350건" 수량 목표를 좇다 9월 발송 대부분이 공급버티컬로 드리프트 → 모집 정지.
    # 규율에 맡기지 않고 코드로 상한을 박는다: tier2(공급버티컬)는 한 배치의 20%까지만.
    # 검증채널 재고가 없으면 배치가 작아지는 게 정답 — 0% 리드로 머릿수를 채우지 않는다.
    tier2_cap = max(1, int(n * TIER2_MAX_RATIO))
    picked, t2 = [], 0
    for r in alive:
        if len(picked) >= n:
            break
        if channel_tier(r) >= 2:
            if t2 >= tier2_cap:
                continue
            t2 += 1
        picked.append(r)
    batch = picked
    picked_ids = set(map(id, picked))
    remaining = [r for r in alive if id(r) not in picked_ids]
    if t2:
        print(f"  ↳ 공급버티컬(tier2) {t2}건 / 상한 {tier2_cap}건 (검증채널 {len(batch)-t2}건)")
    if len(batch) < n:
        print(f"  ⚠️ 검증채널 재고 부족 — {n}건 요청했으나 {len(batch)}건만 발송(0% 리드로 채우지 않음)")

    # 시트1 중복 체크
    s1_rows = sheet1.get_all_values()
    existing_emails = {r[1].strip().lower() for r in s1_rows[1:] if len(r) > 1 and r[1].strip()}

    fresh = [r for r in batch if r[1].strip().lower() not in existing_emails]
    dup = len(batch) - len(fresh)

    # 시트1 append
    if fresh:
        sheet1.append_rows(fresh, value_input_option="USER_ENTERED")

    # backlog 재기록 (헤더 + 남은 것)
    # 안전장치(R7): clear↔append 사이 실패해도 복구되게 remaining 먼저 로컬 백업
    import json as _json
    __import__("pathlib").Path("~/lead-pipeline/data/backlog_safety_backup.json").expanduser().write_text(
        _json.dumps({"header": header, "remaining": remaining}, ensure_ascii=False))
    backlog.clear()
    backlog.append_row(header)
    if remaining:
        backlog.append_rows(remaining, value_input_option="USER_ENTERED")

    print(f"시트1 추가: {len(fresh)}건" + (f" (중복 스킵 {dup}건)" if dup else "")
          + (f" | 죽은 리드 제거 {dead}건" if dead else ""))
    print(f"backlog 잔여: {len(remaining)}건")


if __name__ == "__main__":
    main()
