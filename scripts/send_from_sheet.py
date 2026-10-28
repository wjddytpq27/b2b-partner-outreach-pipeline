#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""
send_from_sheet.py
==================
Google Sheets 시트1에서 발송완료 안 된 행을 Gmail SMTP로 발송.
발송 후 시트에 "발송완료" 마킹.

cron (화·수·목 10:00):
  0 10 * * 2,3,4 python3.14 send_from_sheet.py --max 80

사용:
  python3.14 scripts/send_from_sheet.py --max 80
  python3.14 scripts/send_from_sheet.py --max 80 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import socket
import sys
import time
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.utils import formataddr
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

ROOT = Path("~/lead-pipeline").expanduser()
DATA = ROOT / "data"
CREDS = os.path.expanduser("~/.config/lead-pipeline/credentials.json")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_ID = "YOUR_SEND_SHEET_ID"
PARTNER_SHEET = "YOUR_PARTNER_SHEET_ID"
# 활성화 탭은 가입 파트너에게 의도적으로 보내는 것 → 파트너 제외 안 함
PARTNER_TARGET_TABS = {"partner_activation", "partner_content", "partner_campaign",
                       "partner_minigame_builder", "partner_minigame_nonbuilder"}

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
KST = timezone(timedelta(hours=9))
SEND_START_HOUR = 10
SEND_CUTOFF_HOUR = 17
SEND_DELAY = 2.0  # 발송 간격(초)

SENDER_NAME = "Acme 담당자"


def _load_env() -> tuple[str, str]:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    return os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_APP_PASSWORD", "")


def _get_sheet_rows(ws) -> tuple[list, int, int, int, int, int]:
    """시트1 헤더 파싱. (rows, name_col, email_col, subj_col, body_col, done_col)"""
    all_vals = ws.get_all_values()
    if not all_vals:
        return [], -1, -1, -1, -1, -1
    header = all_vals[0]
    def col(name):
        try:
            return header.index(name)
        except ValueError:
            return -1
    return (
        all_vals,
        col("이름"),
        col("이메일"),
        col("제목"),
        col("본문"),
        col("발송완료"),
    )


import re as _re

# 발송 전 검증 — 하나라도 걸리면 그 건은 발송 거부(차단·로그)
EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_BODY_LEN = 40  # 정상 콜드메일 본문 최소 길이
# 스크래퍼가 이미지/파일명·경로를 이메일로 오인해 긁은 쓰레기 주소 (예: becky-thumb@2x.webp)
BAD_TLDS = {"webp", "jpg", "jpeg", "png", "gif", "bmp", "svg", "ico", "tif", "tiff",
            "pdf", "uf", "css", "js", "html", "htm", "php", "json", "xml", "mp4", "webm", "zip"}
# 메이저 웹메일은 정확한 TLD만 허용 — naver.co(→.com 잘림)·daum.ne(→.net) 같은 오타/잘림 차단
# (someone@naver.co 가 3일째 timed out 반송된 사고 방지)
MAJOR_STRICT = {"naver": "naver.com", "gmail": "gmail.com", "hanmail": "hanmail.net",
                "daum": "daum.net", "nate": "nate.com", "kakao": "kakao.com",
                "hotmail": "hotmail.com", "outlook": "outlook.com", "yahoo": "yahoo.com"}


try:
    import dns.resolver as _dnsr  # dnspython 있으면 정식 MX 조회
except Exception:
    _dnsr = None

_MX_CACHE: dict[str, bool] = {}


def _domain_deliverable(domain: str) -> bool:
    """도메인이 메일 받을 수 있는지 확인. 확실히 없으면 False, 확인불가/에러면 True(fail-open).
    죽은 도메인(335.co·tklab.kr 등) 사전 차단용 — 2026-07-14 반송 31% 사고 방어."""
    domain = (domain or "").lower().strip()
    if not domain or "." not in domain:
        return True  # 형식검증이 따로 잡음
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]
    ok = True
    try:
        if _dnsr is not None:
            try:
                ans = _dnsr.resolve(domain, "MX", lifetime=5)
                ok = len(ans) > 0
            except _dnsr.NoAnswer:
                ok = _has_addr(domain)          # MX 없어도 A레코드 있으면 수신 가능(RFC fallback)
            except (_dnsr.NXDOMAIN, _dnsr.NoNameservers):
                ok = False                       # 도메인 자체가 없음 → 죽은 도메인
            except Exception:
                ok = True                        # 타임아웃 등 → 통과(오차단 방지)
        else:
            ok = _has_addr(domain)               # dnspython 없으면 A레코드 존재로 대체 판정
    except Exception:
        ok = True
    _MX_CACHE[domain] = ok
    return ok


def _has_addr(domain: str) -> bool:
    try:
        socket.setdefaulttimeout(5)
        socket.getaddrinfo(domain, None)
        return True
    except socket.gaierror:
        return False                             # 도메인 미해석 = 없는 도메인
    except Exception:
        return True                              # 그 외 에러는 통과


def _validate_outgoing(name: str, email: str, subject: str, body: str) -> list[str]:
    """발송 직전 본문/제목/이메일 검증. 문제 사유 리스트 반환(빈 리스트면 통과)."""
    problems: list[str] = []

    # 1. literal escape 잔존 (줄바꿈/탭이 글자로 박힌 경우)
    for token in ("\\n", "\\r", "\\t"):
        if token in body or token in subject:
            problems.append(f"literal_{token.strip(chr(92))}")
            break

    # 2. 치환 안 된 템플릿 토큰 ({이름}, {greeting} 등)
    if _re.search(r"\{[^}]+\}", body) or _re.search(r"\{[^}]+\}", subject):
        problems.append("untemplated_token")

    # 3. 빈/너무 짧은 본문·제목
    if not subject.strip():
        problems.append("empty_subject")
    if len(body.strip()) < MIN_BODY_LEN:
        problems.append("body_too_short")

    # 4. 이메일 형식
    em = email.strip()
    # 4b. HTML엔티티/이스케이프 잔재 오염(u003e>, u003c< 등) — 형식은 유효해 통과하나 100% 반송(2026-07-14)
    if _re.search(r"(u003[ec]|&gt;|&lt;|[<>])", em, _re.I):
        problems.append("malformed_email_junk")
    if not EMAIL_RE.match(em):
        problems.append("bad_email")
    # 5. 파일 확장자가 TLD로 잡힌 쓰레기 주소 (이미지/경로 오인 스크랩)
    elif "." in em.rsplit("@", 1)[-1] and em.rsplit(".", 1)[-1].lower() in BAD_TLDS:
        problems.append("garbage_filetld")
    else:
        # 6. 메이저 웹메일 도메인 잘림/오타 (naver.co, gmail.co, daum.ne 등)
        dom = em.rsplit("@", 1)[-1].lower()
        base = dom.split(".")[0]
        if base in MAJOR_STRICT and dom != MAJOR_STRICT[base]:
            problems.append("truncated_major_domain")
        elif dom in MAJOR_STRICT.values():
            pass  # 메이저 웹메일(naver/gmail/daum 등) = 확실히 살아있음, DNS 조회 생략
        elif not _domain_deliverable(dom):
            problems.append("dead_domain")  # MX/도메인 미존재 → 반송 확정, 사전 차단

    return problems


def _load_blocked_emails() -> set[str]:
    """bounce_list + blocklist 통합 차단 주소 세트."""
    blocked: set[str] = set()
    for fname in ("bounce_list.json", "blocklist.json"):
        p = DATA / fname
        if p.exists():
            for item in json.loads(p.read_text()):
                if isinstance(item, dict) and item.get("email"):
                    blocked.add(item["email"].lower().strip())
    return blocked


def _load_disqualified_emails() -> set[str]:
    """leads_raw에서 disqualified 상태 이메일 — 뒤늦게 탈락한 리드 발송 차단."""
    dq: set[str] = set()
    p = DATA / "leads_raw.json"
    if p.exists():
        data = json.loads(p.read_text())
        leads = data.get("leads", data) if isinstance(data, dict) else data
        for l in leads:
            em = (l.get("email") or "").lower().strip()
            if em and l.get("outreach_status") == "disqualified":
                dq.add(em)
    return dq


def _load_partner_emails(gc) -> set[str]:
    """이미 가입한 파트너 이메일 — 콜드 발송에서 제외(가입자에게 영업메일 재발송 방지)."""
    emails: set[str] = set()
    try:
        recs = gc.open_by_key(PARTNER_SHEET).worksheet("시트1").get_all_records()
        for p in recs:
            em = str(p.get("이메일") or "").lower().strip()
            if em:
                emails.add(em)
    except Exception as e:
        print(f"  (파트너 목록 로드 실패: {e})", file=sys.stderr)
    return emails


def _load_proven_channels() -> set[str]:
    """검증채널 = channel_ledger.json 최신 스냅샷에서 가입 1건 이상 나온 채널.
    감·문서가 아니라 원장 근거(pipeline_health가 매 사이클 갱신)."""
    try:
        ledger = json.loads((DATA / "channel_ledger.json").read_text(encoding="utf-8"))
        latest = ledger[sorted(ledger)[-1]]
        conv = latest.get("conversion", {})
        return {ch for ch, v in conv.items() if isinstance(v, dict) and v.get("conv", 0) >= 1}
    except Exception:
        return set()


# ── 테스트슬롯(2026-09-10) ──────────────────────────────────────────────
# 검증채널 8개가 물리적으로 소진(하루 20-40 상한)이라 발송량이 굶는다. 유망 미검증 후보
# (셀러에게 납품하는 '디렉토리 등재 개발사' 유형 — cafe24_designer/webagency와 동형)를
# 배치에 얹어 테스트 발송 → track_conversion이 conv>0 감지 시 원장 반영돼 자동 검증채널 승격.
# ❌ 반증된 계열 제외: builder_studio(886발송 0)·kmong(460발송 0)·공급버티컬(2201발송 0)·콘텐츠(800발송 0).
CANDIDATE_CHANNELS = {"cafe24_app", "shopify_partner", "kmong", "titanclass", "maily", "sixshop_seller"}  # sixshop_seller=식스샵 자사몰 셀러 본인(2026-09-21)
CANDIDATE_DAILY_MAX = 80  # 하루 테스트 발송 상한(검증채널 재고에 얹어 총 ~100 목표)
RECONTACT_MARKS = ("다시 한번 연락드립니다", "놓치셨을까")  # recontact_verified.py 본문 식별


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=80)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tab", default="시트1", help="발송할 워크시트 탭 (default 시트1)")
    parser.add_argument("--attach", default="", help="첨부할 파일 경로 (PDF 등)")
    parser.add_argument("--html-template", default="",
                        choices=["", "body"],
                        help="HTML 디자인 템플릿 (body=시트 본문 그대로 카드HTML, 나머지=고정템플릿)")
    args = parser.parse_args()

    html_render = None
    if args.html_template:
        from email_templates import RENDERERS
        html_render = RENDERERS[args.html_template]
        if args.html_template == "body":
            from email_templates import render_body
            html_render = lambda name, link, _b=None: None  # 자리표시 — 실제는 _build_msg에서 행 본문으로
        print(f"HTML 템플릿: {args.html_template}")

    attach_bytes = None
    attach_name = ""
    if args.attach:
        ap = Path(args.attach)
        if not ap.exists():
            print(f"오류: 첨부파일 없음 — {args.attach}", file=sys.stderr)
            sys.exit(1)
        attach_bytes = ap.read_bytes()
        attach_name = ap.name
        print(f"첨부: {attach_name} ({len(attach_bytes)//1024}KB)")

    now_kst = datetime.now(KST)
    if not args.dry_run:
        if now_kst.hour < SEND_START_HOUR or now_kst.hour >= SEND_CUTOFF_HOUR:
            print(f"[발송 중단] {now_kst.strftime('%H:%M')} KST — 발송 허용 시간: {SEND_START_HOUR}~{SEND_CUTOFF_HOUR}시")
            sys.exit(0)
        # 공휴일 가드 — 한국 공휴일이면 발송 중단
        hol_path = DATA / "kr_holidays.json"
        if hol_path.exists():
            holidays = json.loads(hol_path.read_text())
            tday = now_kst.strftime("%Y-%m-%d")
            if tday in holidays:
                print(f"[발송 중단] {tday} 공휴일({holidays[tday]}) — 발송 안 함")
                sys.exit(0)

    gmail_user, gmail_pw = _load_env()
    if not gmail_user or not gmail_pw:
        print("오류: .env에 GMAIL_USER, GMAIL_APP_PASSWORD 필요", file=sys.stderr)
        sys.exit(1)

    blocked_emails = _load_blocked_emails()
    if args.tab in PARTNER_TARGET_TABS:
        # 파트너 캠페인 제외 명단(설치완료·부정제출·수신거부 등) — 어떤 충전 스크립트가 채웠든 발송 직전에 막는다
        p = DATA / "partner_activation_exclude.json"
        if p.exists():
            blocked_emails |= {(it.get("email") or "").lower().strip() for it in json.loads(p.read_text()) if isinstance(it, dict)}
    # 파트너 탭엔 disqualified-리드 필터 미적용: '옛날 콜드 리드로 탈락'과 '지금 가입한 파트너'는 별개다.
    # (bounce/수신거부 차단은 파트너에게도 유지 — blocked_emails는 그대로 적용)
    disqualified_emails = set() if args.tab in PARTNER_TARGET_TABS else _load_disqualified_emails()
    from leads_store import reserved_emails  # 1:1 제휴 트랙 예약자 자동발송 제외(2026-10-01)
    reserved = set() if args.tab in PARTNER_TARGET_TABS else reserved_emails()
    print(f"차단 목록: {len(blocked_emails)}건 (bounce + block) / disqualified: {len(disqualified_emails)}건")

    creds = Credentials.from_service_account_file(CREDS, scopes=SCOPES)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SHEET_ID).worksheet(args.tab)

    # 콜드/일반 탭이면 가입 파트너 제외 (활성화 탭은 파트너 대상이라 제외 안 함)
    partner_emails: set[str] = set()
    if args.tab not in PARTNER_TARGET_TABS:
        partner_emails = _load_partner_emails(gc)
        print(f"가입 파트너 제외 목록: {len(partner_emails)}건")

    all_vals, nc, ec, sc, bc, dc = _get_sheet_rows(ws)
    if dc == -1:
        print("오류: 시트1에 '발송완료' 컬럼 없음", file=sys.stderr)
        sys.exit(1)
    lc = all_vals[0].index("전용링크") if all_vals and "전용링크" in all_vals[0] else -1
    sdc = all_vals[0].index("발송일") if all_vals and "발송일" in all_vals[0] else -1  # 발송일 스탬프 컬럼(있으면)
    pc = all_vals[0].index("플랫폼") if all_vals and "플랫폼" in all_vals[0] else -1  # 채널(플랫폼) 컬럼 — 순도 게이트용

    # 발송 대기 행 추출 (발송완료 빈 것만)
    to_send = []
    rejected = 0
    for i, row in enumerate(all_vals[1:], start=2):  # 행 번호(1-based + 헤더)
        if len(row) <= dc:
            row = row + [""] * (dc - len(row) + 1)
        if str(row[dc]).strip():
            continue  # 이미 발송완료
        email = row[ec].strip() if ec >= 0 and len(row) > ec else ""
        # raw 상태 그대로 읽어서 검증 (절대 조용히 고치지 않는다)
        subj = row[sc].strip() if sc >= 0 and len(row) > sc else ""
        body = row[bc].strip() if bc >= 0 and len(row) > bc else ""
        name = row[nc].strip() if nc >= 0 and len(row) > nc else ""
        if not email or not subj or not body:
            continue
        if email.lower() in blocked_emails:
            ws.update_cell(i, dc + 1, "바운스차단")
            print(f"  [차단] {email}")
            continue
        if email.lower() in reserved:
            print(f"  [1:1트랙 예약·자동발송 제외] {email}")
            continue
        if email.lower() in disqualified_emails:
            ws.update_cell(i, dc + 1, "탈락차단")
            print(f"  [탈락차단] {email}")
            continue
        if partner_emails and email.lower() in partner_emails:
            ws.update_cell(i, dc + 1, "파트너전환")
            print(f"  [파트너전환·제외] {email}")
            continue
        # ── 발송 전 검증: 깨진 본문은 발송 거부 + 셀 마킹 + 로그 (조용히 고치지 않음) ──
        problems = _validate_outgoing(name, email, subj, body)
        if problems:
            ws.update_cell(i, dc + 1, "검증실패:" + ",".join(problems))
            print(f"  [검증실패·발송거부] {email} — {', '.join(problems)}", file=sys.stderr)
            rejected += 1
            continue
        link = row[lc].strip() if lc >= 0 and len(row) > lc else ""
        channel = row[pc].strip() if pc >= 0 and len(row) > pc else ""
        to_send.append({"row": i, "name": name, "email": email, "subject": subj, "body": body, "link": link, "channel": channel})
        # 상한 자르기는 게이트·신규우선 정렬 뒤에 한다(2026-09-30): 여기서 먼저 자르면
        # 시트 위쪽 재컨택이 자리를 다 먹고, 게이트 드롭분만큼 발송이 비었다.

    if rejected:
        print(f"⚠️  검증실패로 발송 거부된 건: {rejected}건 (시트에 '검증실패:사유' 마킹됨 — 원인 고치고 재발송)", file=sys.stderr)

    # ── 순도 하드 게이트 (2026-09-04): 콜드 탭 발송이 검증채널 80% 미만이면 비검증 건을 드롭 ──
    # 경보만 내고 발송은 나가던 gap을 실제 차단으로. 검증채널이 하나도 없으면 그 사이클은 아무것도 안 보낸다
    # (KPI: 재고 없으면 물량을 줄인다 — 0% 헛발송으로 도메인 reputation 태우지 않기). 활성화 탭은 파트너 대상이라 제외.
    if args.tab not in PARTNER_TARGET_TABS and to_send:
        proven = _load_proven_channels()
        if proven:
            prov = [r for r in to_send if r.get("channel") in proven]
            cand = [r for r in to_send if r.get("channel") in CANDIDATE_CHANNELS]
            # 발송 = 검증채널 전부 + 테스트슬롯 후보(하루 상한). 나머지(0% 반증계열)는 드롭.
            allowed = prov + cand[:CANDIDATE_DAILY_MAX]
            allowed_ids = {id(r) for r in allowed}
            dropped_rows = [r for r in to_send if id(r) not in allowed_ids]
            if dropped_rows:
                from collections import Counter
                mix = Counter(r.get("channel") or "?" for r in dropped_rows)
                print(f"🛑 게이트: 검증채널 {len(prov)}건 + 테스트슬롯 후보 "
                      f"{min(len(cand), CANDIDATE_DAILY_MAX)}건 발송 / 비검증(반증계열) {len(dropped_rows)}건 드롭.",
                      file=sys.stderr)
                print(f"   드롭된 채널 top: {dict(mix.most_common(10))}", file=sys.stderr)
            if len(cand) > CANDIDATE_DAILY_MAX:
                print(f"   ⚠️ 테스트슬롯 후보 {len(cand)}건 중 {CANDIDATE_DAILY_MAX}건만 발송(하루 상한).",
                      file=sys.stderr)
            to_send = allowed
        else:
            print("⚠️ 순도 게이트: 원장에서 검증채널을 못 읽어 게이트 skip (pipeline_health 먼저 확인).", file=sys.stderr)

    # 신규 콜드 우선(운영자 2026-09-30 "신규 콜이 더 중요"): 재컨택은 남는 자리에만. 같은 그룹 안에선 시트 순서 유지.
    if args.tab not in PARTNER_TARGET_TABS:
        to_send.sort(key=lambda r: any(m in r["body"] for m in RECONTACT_MARKS))
        n_re = sum(1 for r in to_send[:args.max] if any(m in r["body"] for m in RECONTACT_MARKS))
        print(f"발송 구성: 신규 {min(len(to_send), args.max) - n_re}건 + 재컨택 {n_re}건 (상한 {args.max})")
    to_send = to_send[:args.max]

    if not to_send:
        print("발송 대기 없음." + (f" (검증실패 {rejected}건 거부)" if rejected else ""))
        return

    print(f"발송 대기: {len(to_send)}건")

    if args.dry_run:
        for r in to_send:
            print(f"  [DRY] {r['name']} <{r['email']}> — {r['subject'][:40]}")
        return

    sent = 0
    failed = 0
    # 시트 쓰기는 건당 update_cell(→ 분당 쓰기 쿼터 429) 대신 누적 후 batch_update 1회.
    # a1 셀 주소로 모아 발송 성공/실패 마킹을 한 번에 반영(429 방지).
    from gspread.utils import rowcol_to_a1
    pending_updates: list[dict] = []

    def _queue(row: int, col0: int, value: str) -> None:
        pending_updates.append({"range": rowcol_to_a1(row, col0 + 1), "values": [[value]]})

    def _build_msg(r):
        if html_render is not None:
            body_part = MIMEMultipart("alternative")
            body_part.attach(MIMEText(r["body"], "plain", "utf-8"))
            html = render_body(r["body"]) if args.html_template == "body" else html_render(r["name"], r["link"])
            body_part.attach(MIMEText(html, "html", "utf-8"))
        else:
            body_part = MIMEText(r["body"], "plain", "utf-8")
        if attach_bytes is not None:
            msg = MIMEMultipart("mixed")
            msg.attach(body_part)
            part = MIMEApplication(attach_bytes, _subtype="pdf")
            part.add_header("Content-Disposition", "attachment", filename=attach_name)
            msg.attach(part)
        else:
            msg = body_part
        msg["Subject"] = r["subject"]
        msg["From"] = formataddr((SENDER_NAME, gmail_user))
        msg["To"] = r["email"]
        msg["Reply-To"] = gmail_user
        return msg

    def _connect():
        s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
        s.login(gmail_user, gmail_pw)
        return s

    # ⚠️ Gmail은 연결당 메시지 수/시간 한도가 있어 대량 발송 중 소켓을 끊는다.
    # 예전엔 연결 1개로 전량 발송 → 중간에 끊기면 나머지 전부 "please run connect() first"로 실패.
    # → 끊김 감지 시 1회 재연결 재시도 + 50건마다 선제 재연결로 방지.
    RECONNECT_EVERY = 50
    CONN_ERRORS = (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError, TimeoutError)
    smtp = _connect()
    for idx, r in enumerate(to_send):
        if idx > 0 and idx % RECONNECT_EVERY == 0:  # 선제 재연결
            try:
                smtp.quit()
            except Exception:
                pass
            smtp = _connect()
        msg = _build_msg(r)
        try:
            smtp.sendmail(gmail_user, [r["email"]], msg.as_bytes())
        except CONN_ERRORS as e:
            # 연결 끊김류 → 재연결 후 1회 재시도
            try:
                try:
                    smtp.quit()
                except Exception:
                    pass
                smtp = _connect()
                smtp.sendmail(gmail_user, [r["email"]], msg.as_bytes())
            except Exception as e2:
                _queue(r["row"], dc, f"오류:{e2}")
                failed += 1
                print(f"  ✗ {r['email']}: {e2}", file=sys.stderr)
                continue
        except Exception as e:
            # 수신자 거부 등 이 주소만의 문제 → 오류 마킹, 연결 유지
            _queue(r["row"], dc, f"오류:{e}")
            failed += 1
            print(f"  ✗ {r['email']}: {e}", file=sys.stderr)
            continue
        _queue(r["row"], dc, "발송완료")
        if sdc >= 0:
            _queue(r["row"], sdc, now_kst.strftime("%Y-%m-%d"))
        sent += 1
        print(f"  ✓ {r['name']} <{r['email']}>")
        time.sleep(SEND_DELAY)
    try:
        smtp.quit()
    except Exception:
        pass

    # 시트 마킹 일괄 반영 (429 방지). 실패해도 발송은 이미 끝났으므로 경고만.
    if pending_updates:
        try:
            ws.batch_update(pending_updates, value_input_option="USER_ENTERED")
        except Exception as e:
            print(f"⚠️ 시트 발송완료 마킹 실패(발송은 성공): {str(e)[:80]}", file=sys.stderr)

    print(f"\n발송 완료: {sent}건 / 실패: {failed}건")

    # 일일 보고용 발송 요약 기록 (날짜·탭·건수)
    try:
        summary = {
            "date": now_kst.strftime("%Y-%m-%d"),
            "ts": now_kst.strftime("%H:%M"),
            "tab": args.tab,
            "sent": sent,
            "failed": failed,
            "rejected": rejected,
        }
        with open(DATA / "send_summary.jsonl", "a") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"  (요약 기록 실패: {e})", file=sys.stderr)

    # ── 재발방지: 발송 실패가 많으면 슬랙 경고 + 비정상 종료 ──
    # 예전엔 실패 125건에도 exit 0이라 retry_wrapper가 성공으로 오인 → 조용히 증발.
    # 이제 실패 5건↑ 또는 실패율 20%↑면 슬랙 알림 + exit 1(retry_wrapper도 감지·재시도).
    total = sent + failed
    if failed >= 5 or (total > 0 and failed / total >= 0.2):
        msg = (f"🚨 콜드발송 실패 {failed}건 (성공 {sent}, 탭 {args.tab}, "
               f"{now_kst.strftime('%m-%d %H:%M')}). 연결끊김/발송한도 의심 — "
               f"data/send.log·시트1 '오류:' 칸 확인. 오류칸 비우면 재발송됨.")
        try:
            import urllib.request as _u
            _u.urlopen(_u.Request(
                "https://slack.com/api/chat.postMessage",
                data=json.dumps({"channel": "YOUR_SLACK_CHANNEL_ID", "text": msg}).encode("utf-8"),
                headers={"Authorization": "Bearer " + __import__("env_secret").get("SLACK_BOT_TOKEN"),
                         "Content-Type": "application/json; charset=utf-8"}), timeout=15)
        except Exception:
            pass
        print(f"⚠️ 실패 {failed}건 → 슬랙 경고 발송, 비정상 종료(exit 1)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
