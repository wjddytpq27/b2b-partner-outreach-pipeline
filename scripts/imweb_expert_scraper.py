#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""
imweb_expert_scraper.py
=======================
아임웹 전문가 디렉토리 (imweb.me/expert) → 이메일 추출.

전략:
  1. imweb.me/expert에서 Next.js build ID 동적 추출
  2. /_next/data/{build_id}/expert/profile.json?page=N 으로 전문가 목록 수집
  3. 각 전문가 referralCode로 프로필 페이지 방문 → 이메일 직접 추출
     (https://imweb.me/expert/profile/{referralCode} — requests 200 OK 확인)
  4. leads_raw.json 저장

총 1,096명 / 24명 per page = 46페이지 / --pages 5 = 120명/회
이메일 수율 ~30~50% (전문가들이 프로필에 이메일 노출)

사용:
  python3 imweb_expert_scraper.py
  python3 imweb_expert_scraper.py --pages 10
  python3 imweb_expert_scraper.py --reset-progress
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT_DIR = Path("~/lead-pipeline").expanduser()
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
RAW_PATH = DATA_DIR / "leads_raw.json"
PROGRESS_FILE = DATA_DIR / "imweb_expert_progress.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

SKIP_EMAIL_DOMAINS = {
    "example.com", "test.com", "email.com",
    "sentry.io", "wix.com", "shopify.com",
    "imweb.me",  # help@imweb.me 같은 시스템 이메일 제외
    "cafe24.com", "makeshop.co.kr",
    "yourcompany.com", "yourdomain.com",
    "w3schools.com", "schema.org",
    "ftc.go.kr",
}

TOTAL_EXPERTS = 10833  # 2026-08 실측 count (Astro 페이지 totalCount)
PAGE_LIMIT = 24
# 2026-09-30 전수실측: 목록은 포트폴리오 단위, 고유 전문가 2,053명은 p1~86에 다 있고 p87+는 반복.
MAX_PAGES = 90  # 452로 두면 매일 반복 페이지만 돌며 낭비

# 아임웹이 2026-08 Next.js → Astro 이관. 전문가 목록이 /_next/data JSON 대신
# imweb.me/expert?page=N SSR HTML 안에 React Query dehydratedState(&quot; 이스케이프 JSON)로 박힘.
# 리스트 HTML 안에 전문가별 contactEmail·domainCategories·주소가 전부 들어있음(프로필 fetch 불필요, 이메일 노출 100%).
# expert 블록: "referralCode":[0,"CODE"],"name":[0,"NAME"]...,"domainCategories":[1,[...]],
#              "addressSido":[0,"서울"],"addressGungu":[0,"강남구"],"contactPhone":[0,"010..."],"contactEmail":[0,"x@y.com"]
_F0 = r'\[0,"((?:[^"\\]|\\.)*)"\]'  # Astro 스칼라 [0,"값"]
CODE_RE = re.compile(r'"referralCode":' + _F0)
NAME_RE = re.compile(r'"name":' + _F0)
EMAIL_FLD_RE = re.compile(r'"contactEmail":' + _F0)
PHONE_FLD_RE = re.compile(r'"contactPhone":' + _F0)
SIDO_RE = re.compile(r'"addressSido":' + _F0)
GUNGU_RE = re.compile(r'"addressGungu":' + _F0)
DOMCAT_RE = re.compile(r'"domainCategories":\[1,\[(.*?)\]\]\]', re.DOTALL)


def _fetch_experts_page(page: int) -> list[dict]:
    """imweb.me/expert?page=N (Astro SSR) HTML에서 전문가 목록 완전 파싱 (24명/페이지).
    리스트에 이메일·특기·지역이 다 있어 프로필 개별 fetch 불필요."""
    try:
        r = requests.get(
            "https://imweb.me/expert",
            headers={**HEADERS, "Referer": "https://imweb.me/expert"},
            params={"page": page},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        # requests 인코딩 오판 방지 — 한글 깨짐(UTF-8을 Latin1로 읽음) 막으려 강제 UTF-8 디코드
        t = html.unescape(r.content.decode("utf-8", "replace"))
        # 전문가별로 referralCode 위치 기준 윈도우 분할
        starts = [m.start() for m in CODE_RE.finditer(t)]
        seen: set[str] = set()
        out: list[dict] = []
        for i, s in enumerate(starts):
            e = starts[i + 1] if i + 1 < len(starts) else min(len(t), s + 2000)
            win = t[s:e]
            code = CODE_RE.search(win).group(1)
            if code in seen:
                continue
            seen.add(code)
            nm = NAME_RE.search(win)  # 윈도우 첫 name = 전문가명
            em = EMAIL_FLD_RE.search(win)
            ph = PHONE_FLD_RE.search(win)
            sido = SIDO_RE.search(win)
            gungu = GUNGU_RE.search(win)
            # domainCategories 배열 안의 name들 = 전문 분야
            cats: list[str] = []
            dm = DOMCAT_RE.search(win)
            if dm:
                cats = [c for c in NAME_RE.findall(dm.group(1))]
            out.append({
                "referralCode": code,
                "code": code,
                "name": (nm.group(1) if nm else code),
                "email": (em.group(1).strip() if em and em.group(1) else None),
                "phone": (ph.group(1) if ph else ""),
                "domainCategories": [{"name": c} for c in cats],
                "totalPortfolios": 0,
                "addressSido": (sido.group(1) if sido else ""),
                "addressGungu": (gungu.group(1) if gungu else ""),
            })
        return out
    except Exception as e:
        print(f"[imweb] 페이지 {page} 로드 실패: {e}", file=sys.stderr)
        return []


def _scrape_profile_email(referral_code: str) -> str | None:
    """전문가 프로필 페이지에서 이메일 추출.
    https://imweb.me/expert/profile/{referralCode} — requests 200 OK 확인됨.
    """
    url = f"https://imweb.me/expert/profile/{referral_code}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        emails = EMAIL_RE.findall(r.text)
        emails = [
            e for e in dict.fromkeys(emails)
            if e.split("@")[-1].lower() not in SKIP_EMAIL_DOMAINS
            and len(e) < 80
        ]
        return emails[0] if emails else None
    except Exception:
        return None


def _load_existing_handles() -> set[str]:
    if not RAW_PATH.exists():
        return set()
    try:
        d = json.loads(RAW_PATH.read_text())
        leads = d.get("leads", []) if isinstance(d, dict) else d
        return {l.get("handle", "") for l in leads}
    except Exception:
        return set()


def _load_existing_emails() -> set[str]:
    """leads_raw + contacted_list + bounce + blocklist 이메일 합집합. 이미 있는 사람 차단."""
    emails: set[str] = set()
    # leads_raw
    if RAW_PATH.exists():
        try:
            d = json.loads(RAW_PATH.read_text())
            leads = d.get("leads", []) if isinstance(d, dict) else d
            for l in leads:
                e = (l.get("email") or "").lower().strip()
                if e:
                    emails.add(e)
        except Exception:
            pass
    # contacted_list / bounce_list / blocklist
    for fn in ("contacted_list.json", "bounce_list.json", "blocklist.json"):
        p = DATA_DIR / fn
        if not p.exists():
            continue
        try:
            arr = json.loads(p.read_text())
            for it in arr:
                if isinstance(it, dict) and it.get("email"):
                    emails.add(it["email"].lower().strip())
        except Exception:
            pass
    return emails


def _load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_progress(p: dict) -> None:
    PROGRESS_FILE.write_text(json.dumps(p))


def collect(pages: int = 5) -> list[dict]:
    """전문가 목록 수집. pages: 이번 실행에서 처리할 페이지 수 (24명/페이지)."""
    progress = _load_progress()
    next_page = progress.get("last_page", 0) + 1
    existing_handles = _load_existing_handles()
    existing_emails = _load_existing_emails()  # 이메일 중복 방지 (contacted/bounce/blocklist 포함)
    print(f"[imweb] 기존 이메일: {len(existing_emails)}개 (중복 차단용)", file=sys.stderr)

    if next_page > MAX_PAGES:
        print(f"[imweb] 전체 46페이지 완료, 처음부터 재시작", file=sys.stderr)
        next_page = 1
        progress = {}

    end_page = min(next_page + pages - 1, MAX_PAGES)
    print(f"[imweb] 페이지 {next_page}~{end_page} / {MAX_PAGES}", file=sys.stderr)

    results: list[dict] = []

    for page in range(next_page, end_page + 1):
        experts = _fetch_experts_page(page)
        if not experts:
            print(f"[imweb] 페이지 {page}: 결과 없음, 중단", file=sys.stderr)
            break

        print(f"[imweb] 페이지 {page}/{MAX_PAGES}: {len(experts)}명...", file=sys.stderr)
        page_found = 0

        for expert in experts:
            code = str(expert.get("code", ""))
            referral_code = expert.get("referralCode", "")
            name = expert.get("name", code)
            handle = f"imweb_expert_{referral_code or code}"

            if handle in existing_handles:
                continue

            # 이메일은 리스트 HTML에 이미 들어있음(프로필 개별 fetch 불필요)
            email = expert.get("email")
            # 이미 보냈거나 발굴된 이메일이면 skip (handle만 새로 잡힌 거)
            if email and email.lower().strip() in existing_emails:
                continue
            if email:
                page_found += 1
                existing_emails.add(email.lower().strip())

            domain_cats = [d.get("name", "") for d in expert.get("domainCategories", []) if d.get("name")]
            address = f"{expert.get('addressSido', '')} {expert.get('addressGungu', '')}".strip()

            results.append({
                "handle": handle,
                "code": code,
                "referral_code": referral_code,
                "name": name,
                "email": email,
                "domain_categories": domain_cats,
                "address": address,
                "portfolios_count": expert.get("totalPortfolios", 0),
                "profile_url": f"https://imweb.me/expert/profile/{referral_code}" if referral_code else f"https://imweb.me/expert",
            })
            existing_handles.add(handle)

        print(f"[imweb]   → 이메일 {page_found}개 발견", file=sys.stderr)
        progress["last_page"] = page
        _save_progress(progress)
        time.sleep(0.5)

    has_email = sum(1 for r in results if r.get("email"))
    print(
        f"[imweb] 수집 완료: {len(results)}명 "
        f"(이메일 {has_email}명, 없음 {len(results)-has_email}명)",
        file=sys.stderr,
    )
    return results


def save_leads(infos: list[dict]) -> int:
    infos_with_email = [i for i in infos if i.get("email")]

    existing: list[dict] = []
    if RAW_PATH.exists():
        try:
            d = json.loads(RAW_PATH.read_text())
            existing = d.get("leads", existing) if isinstance(d, dict) else d
        except Exception:
            pass

    existing_handles = {l.get("handle", "") for l in existing}

    new_leads = []
    for info in infos_with_email:
        if info["handle"] in existing_handles:
            continue
        cats = ", ".join(info.get("domain_categories", [])) or "Web Design"
        addr = info.get("address", "")
        cnt = info.get("portfolios_count", 0)
        new_leads.append({
            "handle": info["handle"],
            "display_name": info.get("name", ""),
            "platform": "imweb_expert",
            "followers": 0,
            "website": info.get("profile_url"),
            "email": info.get("email"),
            "business_category": cats,
            "country": "KR",
            "language": "ko",
            "bio": f"아임웹 공식 전문가(쇼핑몰·홈페이지 제작) | 분야: {cats} | 지역: {addr or '미상'}".strip(" |"),
            "profile_url": info.get("profile_url", ""),
            "is_business_account": True,
            "source_hashtag": "imweb_expert_directory",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "email_verified": False,
            "outreach_status": "pending",
            "follow_up_count": 0,
            "notes": info.get("profile_url", ""),
        })

    all_leads = existing + new_leads
    RAW_PATH.write_text(json.dumps({
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lead_count": len(all_leads),
        "leads": all_leads,
    }, ensure_ascii=False, indent=2))

    print(f"[imweb] 신규 {len(new_leads)}개 저장 → 총 {len(all_leads)}개", file=sys.stderr)
    return len(new_leads)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=5, help="처리할 페이지 수 (기본 5 = 전문가 ~120명)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reset-progress", action="store_true", help="진행 상황 초기화 후 처음부터")
    args = parser.parse_args()

    if args.reset_progress:
        PROGRESS_FILE.write_text(json.dumps({}))
        print("[imweb] 진행 상황 초기화", file=sys.stderr)

    infos = collect(pages=args.pages)

    if args.dry_run:
        for i in infos[:5]:
            print(json.dumps({k: i[k] for k in ["name", "email", "profile_url", "domain_categories"]}, ensure_ascii=False, indent=2))
        has_email = sum(1 for i in infos if i.get("email"))
        print(f"\n총 {len(infos)}명 | 이메일 {has_email}명 (dry-run)")
    else:
        save_leads(infos)


if __name__ == "__main__":
    main()
