#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""naver_blog 스크래퍼 공용 헬퍼 (2026-09-30).

1) blog_search()     — 네이버 블로그 검색. Open API(display=100, start≤1000)로 키워드당 수십~수백 블로그.
                       search.naver.com HTML은 start= 페이지네이션이 안 먹어 키워드당 8~10개만 나왔다.
                       API 키가 없거나 API가 실패하면 HTML 1페이지로 폴백.
2) profile_email()   — 블로그 프로필 소개(RSS channel description)의 이메일. 가입자 다수가
                       "협업 문의 xxx@naver.com"을 글 본문이 아니라 프로필 소개에 둔다. 이메일이 딱 1개일 때만 채택.
3) 수익형 블로거 페르소나(monetize_blogger) — 태그 상수, 판정 술어, 언론·기업·공공 차단 술어.
   검수 3곳(validate_leads / llm_validator_batch / finalize_backlog 개인화)이 이 술어를 공유한다.
4) 페르소나 키워드 풀 — data/monetize_keyword_pool.json 에서 안 쓴 키워드 N개를 꺼내고
   data/monetize_keyword_usage.json 에 사용 이력을 남긴다. 소진되면 경고 후 가장 오래 전에 쓴 것부터 재사용
   (API sort=date라 재사용해도 그 사이 새로 올라온 글이 잡힌다).

403/429 방어는 전부 naver_fetch.naver_search_get(pacing + 지수 백오프) 경유.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from naver_fetch import naver_search_get

ROOT = Path("~/lead-pipeline").expanduser()
DATA = ROOT / "data"
POOL_PATH = DATA / "monetize_keyword_pool.json"
USAGE_PATH = DATA / "monetize_keyword_usage.json"

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
POST_RE = re.compile(r"blog\.naver\.com/([A-Za-z0-9_]{2,30})/(\d{6,15})")
_MAJOR_FIX = {"naver.co": "naver.com", "naver.c": "naver.com", "gmail.co": "gmail.com",
              "gmail.c": "gmail.com", "daum.ne": "daum.net", "daum.n": "daum.net",
              "hanmail.ne": "hanmail.net", "nate.co": "nate.com"}
SKIP_DOMAINS = {"example.com", "test.com", "email.com", "gov.kr", "go.kr", "kakao.com", "naver.me"}
SKIP_PREFIXES = {"info", "contact", "admin", "support", "help", "service",
                 "hello", "official", "press", "sales", "marketing", "noreply", "no-reply"}

PERSONA = "monetize_blogger"


def _env() -> dict:
    out = {}
    try:
        for line in (ROOT / ".env").read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


def fix_truncated_email(em: str) -> str:
    local, _, dom = em.rpartition("@")
    return f"{local}@{_MAJOR_FIX[dom.lower()]}" if dom.lower() in _MAJOR_FIX else em


def clean_emails(text: str) -> list[str]:
    out = []
    for e in dict.fromkeys(fix_truncated_email(x) for x in EMAIL_RE.findall(text or "")):
        local, _, dom = e.lower().rpartition("@")
        if dom in SKIP_DOMAINS or local in SKIP_PREFIXES:
            continue
        out.append(e)
    return out


# ── 1. 검색 ──────────────────────────────────────────────────────────────────
def _api_page(query: str, start: int, sort: str, env: dict) -> list[dict] | None:
    r = naver_search_get(
        "https://openapi.naver.com/v1/search/blog.json",
        headers={"X-Naver-Client-Id": env["NAVER_CLIENT_ID"],
                 "X-Naver-Client-Secret": env["NAVER_CLIENT_SECRET"]},
        params={"query": query, "display": 100, "start": start, "sort": sort}, timeout=10)
    if r is None or r.status_code != 200:
        return None
    return r.json().get("items", [])


def _html_page(query: str) -> list[tuple[str, str]]:
    r = naver_search_get("https://search.naver.com/search.naver",
                         params={"where": "blog", "query": query}, timeout=12)
    if r is None or r.status_code != 200:
        return []
    return list(dict.fromkeys(POST_RE.findall(r.text)))


def blog_search(query: str, max_results: int = 1000, sort: str = "sim") -> list[tuple[str, str]]:
    """[(blog_id, post_no)] — 블로그ID 기준 중복 제거, 검색 순위 순. API 실패 시 HTML 1페이지 폴백."""
    env = _env()
    pairs: dict[str, str] = {}
    if env.get("NAVER_CLIENT_ID") and env.get("NAVER_CLIENT_SECRET"):
        for start in range(1, min(max_results, 1000) + 1, 100):   # API 상한 start=1000
            items = _api_page(query, start, sort, env)
            if items is None:
                if start == 1:
                    break          # 첫 페이지부터 실패 → HTML 폴백
                return list(pairs.items())
            for it in items:
                m = POST_RE.search(it.get("link", ""))
                if m:
                    pairs.setdefault(m.group(1), m.group(2))
            if len(items) < 100:
                return list(pairs.items())
        if pairs:
            return list(pairs.items())
    for bid, pno in _html_page(query):
        pairs.setdefault(bid, pno)
    return list(pairs.items())


# ── 2. 프로필 소개 이메일 ─────────────────────────────────────────────────────
def profile_email(blog_id: str) -> tuple[str | None, str]:
    """(이메일|None, 프로필 소개문). 소개문 속 유효 이메일이 딱 1개일 때만 채택(주인 본인 연락처)."""
    r = naver_search_get(f"https://rss.blog.naver.com/{blog_id}.xml", timeout=8)
    if r is None or r.status_code != 200:
        return None, ""
    m = re.search(r"<description>(.*?)</description>", r.text, re.S)
    desc = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip() if m else ""
    ems = clean_emails(desc)
    return (ems[0] if len(ems) == 1 else None), desc


def blog_title(blog_id: str) -> str | None:
    """RSS channel <title> = 블로그 이름. 페이지 <title>(=글 제목)을 이름으로 쓰면 LLM 검수가
    '이름이 상품명/서비스명 → 메타 오류'로 멀쩡한 블로거를 탈락시킨다(2026-10-01 실측)."""
    r = naver_search_get(f"https://rss.blog.naver.com/{blog_id}.xml", timeout=8)
    if r is None or r.status_code != 200:
        return None
    m = re.search(r"<title>(.*?)</title>", r.text, re.S)
    t = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip() if m else ""
    return t[:40] or None


def pick_owner_email(emails: list[str], blog_id: str) -> str | None:
    """본문 이메일 후보 중 블로그 주인 것. 1개면 채택, 여러 개면 blog_id와 연관된 것만(무관하면 None=오발송 방지)."""
    if len(emails) <= 1:
        return emails[0] if emails else None
    bid = blog_id.lower()

    def score(em: str) -> int:
        local = em.split("@")[0].lower()
        if bid in local or local in bid:
            return 2
        return 1 if len(bid) >= 4 and len(local) >= 4 and bid[:4] == local[:4] else 0
    best = max(emails, key=score)
    return best if score(best) > 0 else None


def resolve_email(blog_id: str, body_text: str) -> tuple[str | None, str, str]:
    """(이메일, 출처 'body'|'profile'|'', 프로필소개문). 본문(주인 판별) → 없으면 프로필 소개(1개일 때만).
    ⚠️ 본문만 본다 — 전체 HTML엔 댓글·위젯·이웃 블로그의 '남의 이메일'이 섞인다(kimmink 사고)."""
    em = pick_owner_email(clean_emails(body_text), blog_id)
    if em:
        return em, "body", ""
    pem, desc = profile_email(blog_id)
    return (pem, "profile", desc) if pem else (None, "", desc)


# ── 3. 페르소나 판정 ─────────────────────────────────────────────────────────
# 언론·기업 공식블로그·공공기관은 페르소나여도 계속 거른다(운영자 2026-09-30).
# 이름·소개 앞부분 어디서든 조직 신호
PERSONA_ORG_KW = ("공식블로그", "공식 블로그", "편집국", "(주)", "㈜", "주식회사", "코퍼레이션", "홍보팀",
                  "공단", "재단", "협회", "진흥원", "위원회", "주민센터", "교육청")
# 이름(블로그명)에만 있을 때 조직 신호 — 소개문엔 일반어로 흔해서("뉴스 정리", "1인 법인 후기") 이름만 본다
PERSONA_ORG_NAME_KW = ("뉴스", "신문", "일보", "매거진", "법인", "시청", "구청", "군청", "도청")
PERSONA_ORG_HANDLE = ("news", "press", "media", "official", "techm", "corp", "_kr_official")
PERSONA_ORG_EMAIL = (".go.kr", ".or.kr", ".ac.kr", ".re.kr", ".edu")


def is_persona(lead: dict) -> bool:
    return (lead.get("persona") or "") == PERSONA


def persona_org_ng(lead: dict) -> str | None:
    """페르소나 리드 중 사람이 아닌 조직(언론/기업/공공) → 사유, 아니면 None."""
    name = lead.get("display_name") or ""
    bio = lead.get("bio") or ""
    handle = (lead.get("handle") or "").lower()
    email = (lead.get("email") or "").lower()
    if any(email.endswith(d) for d in PERSONA_ORG_EMAIL):
        return "persona_org:public_email"
    if any(k in handle for k in PERSONA_ORG_HANDLE):
        return "persona_org:handle"
    for k in PERSONA_ORG_KW:
        if k in name or k in bio[:120]:
            return f"persona_org:{k}"
    for k in PERSONA_ORG_NAME_KW:
        if k in name:
            return f"persona_org:{k}"
    return None


# 페르소나 리드에 여전히 적용할 NG 키워드 카테고리(ng_keywords.json). 일상·맛집·여행·육아·뷰티 등
# "주제" 카테고리는 이 페르소나의 정상 형태라 적용하지 않는다(가입자 다수가 이 형태).
PERSONA_HARD_NG_CATEGORIES = {"공공기관_협회", "회사_뉴스레터_명시NG", "페이지_메타_오류", "종교_정치"}


# ── 4. 키워드 풀 ─────────────────────────────────────────────────────────────
def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_atomic(p: Path, obj) -> None:
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def take_keywords(n: int = 50, pool_path: Path = POOL_PATH, usage_path: Path = USAGE_PATH) -> list[str]:
    """안 쓴 키워드 n개. 모자라면 stderr 경고 후 가장 오래 전에 쓴 것으로 채운다. 이력은 mark_used()로."""
    pool = _read(pool_path, {}).get("keywords", [])
    used = _read(usage_path, {})
    fresh = [k for k in pool if k not in used]
    if len(fresh) < n:
        print(f"[keyword_pool] ⚠️ 소진 임박/소진: 미사용 {len(fresh)}개 < 요청 {n}개 (풀 {len(pool)}) "
              f"— 가장 오래 전에 쓴 키워드 재사용. 시드/수식어 확장 필요: scripts/build_monetize_keyword_pool.py",
              file=sys.stderr)
        oldest = sorted((k for k in pool if k in used), key=lambda k: used[k].get("last", ""))
        fresh += oldest[: n - len(fresh)]
    return fresh[:n]


def mark_used(keywords: list[str], hits: dict[str, int] | None = None,
              usage_path: Path = USAGE_PATH) -> None:
    used = _read(usage_path, {})
    today = date.today().isoformat()
    for k in keywords:
        rec = used.get(k, {"first": today, "runs": 0, "hits": 0})
        rec["last"] = today
        rec["runs"] = rec.get("runs", 0) + 1
        rec["hits"] = rec.get("hits", 0) + (hits or {}).get(k, 0)
        used[k] = rec
    _write_atomic(usage_path, used)


if __name__ == "__main__":
    import tempfile
    # 페르소나 술어
    assert is_persona({"persona": PERSONA}) and not is_persona({})
    assert persona_org_ng({"display_name": "[테크M] 공식블로그", "handle": "naver_blog_techm_kr"})
    assert persona_org_ng({"email": "x@city.go.kr"}) == "persona_org:public_email"
    assert persona_org_ng({"display_name": "쩝쩝박사 숩니", "bio": "맛집·여행 협업문의 a@naver.com",
                           "handle": "naver_blog_bysoopni", "email": "a@naver.com"}) is None
    # 이메일 정리
    assert clean_emails("협업 문의 abc@naver.com / info@x.com") == ["abc@naver.com"]
    assert clean_emails("문의 abc@naver.co") == ["abc@naver.com"]
    # 키워드 풀: 미사용 우선 → 소진 시 오래된 것 재사용
    d = Path(tempfile.mkdtemp())
    pp, up = d / "pool.json", d / "usage.json"
    pp.write_text(json.dumps({"keywords": ["a", "b", "c"]}))
    assert take_keywords(2, pp, up) == ["a", "b"]
    mark_used(["a", "b"], {"a": 3}, up)
    assert take_keywords(2, pp, up) == ["c", "a"]          # c 미사용 + 가장 오래된 a
    assert json.loads(up.read_text())["a"]["hits"] == 3
    print("ok")
