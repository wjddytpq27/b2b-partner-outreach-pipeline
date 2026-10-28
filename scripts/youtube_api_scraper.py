#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""
youtube_api_scraper.py
======================
YouTube Data API v3로 강의팔이 채널 대량 수집.

전략:
  1. 키워드별 search.list (type=channel) → 채널 ID 모음
  2. channels.list로 채널 상세 (description 포함) batch 조회
  3. description / about 페이지에서 이메일 추출
  4. blocklist + contacted + leads_raw 중복 검증

비용: 무료 (일 quota 10,000)
  - search: 100 quota/회
  - channels (50 batch): 1 quota/회
  - 60 키워드 × 100 = 6,000 quota (여유 있음)

사용:
  python3 youtube_api_scraper.py --max 300
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lead_dedup import Universe  # noqa: E402
from leads_store import commit_leads  # 저장 몇 초만 락(수집은 병렬 OK)

ROOT = Path("~/lead-pipeline").expanduser()
DATA = ROOT / "data"
RAW_PATH = DATA / "leads_raw.json"

# .env 로드
ENV = {}
with open(ROOT / ".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            ENV[k.strip()] = v.strip()

# 키 로테이션: .env의 YouTube_Data_API_v3* / YOUTUBE_API_KEY* 전부 사용(쿼터 소진 시 다음 키).
# ⚠️ 쿼터는 GCP "프로젝트" 단위라 같은 프로젝트의 키 여러 개는 쿼터가 안 늘어난다.
API_KEYS = list(dict.fromkeys(
    v for k, v in ENV.items() if v and (k.startswith("YouTube_Data_API_v3") or k.startswith("YOUTUBE_API_KEY"))
))
if not API_KEYS:
    print("YouTube API 키 없음. .env 확인", file=sys.stderr)
    sys.exit(1)
_key_idx = 0

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# 메이저 도메인 잘림 보정: 크리에이터 스팸방지 난독화("id@naver.co m")가 공백서 끊겨 .co로 잘림
_MAJOR_FIX = {"naver.co": "naver.com", "naver.c": "naver.com", "gmail.co": "gmail.com",
              "gmail.c": "gmail.com", "daum.ne": "daum.net", "daum.n": "daum.net",
              "hanmail.ne": "hanmail.net", "nate.co": "nate.com"}
def fix_truncated_email(em: str) -> str:
    d = em.rsplit("@", 1)[-1].lower()
    return em.rsplit("@", 1)[0] + "@" + _MAJOR_FIX[d] if d in _MAJOR_FIX else em
SKIP_DOMAINS = {
    "youtube.com", "youtu.be", "google.com",
    "kakao.com", "example.com", "apple.com",
    "instagram.com", "facebook.com", "twitter.com",
    "tiktok.com", "ftc.go.kr",
}

# 강의팔이/이커머스 강사 타겟 키워드
KEYWORDS = [
    # 이커머스/쇼핑몰 강의
    "이커머스 강의", "스마트스토어 강의", "쇼핑몰 창업", "온라인 셀러",
    # 셀러를 '고객'으로 둔 사람들 (도매/사입/유통 — 청중 전체가 셀러)
    "도매 사입", "동대문 도매", "쇼핑몰 사입", "사입 노하우", "위탁판매 셀러",
    "온라인 유통 벤더", "쇼핑몰 도매처", "셀러 교육", "쇼핑몰 사장 노하우",
    "스마트스토어 도매", "사입 대행", "셀러 코칭",
    "카페24 쇼핑몰", "아임웹 쇼핑몰", "자사몰 운영", "셀러 전략",
    "쇼핑몰 제작 강의", "아임웹 제작", "카페24 제작",
    "스마트스토어 마케팅", "셀러 창업", "이커머스 마케팅",
    # 마케팅 강의
    "디지털 마케팅 강의", "온라인마케팅 강의", "퍼포먼스 마케팅 강의",
    "메타 광고 강의", "카카오 광고 강의", "네이버 광고 강의",
    "구글 광고 강의", "SNS 마케팅 강의", "인스타그램 마케팅",
    "유튜브 마케팅", "브랜드 마케팅",
    # 강의팔이 / 컨설팅 / 멘토링
    "이커머스 컨설팅", "쇼핑몰 컨설팅", "스마트스토어 컨설팅",
    "이커머스 멘토", "쇼핑몰 멘토", "스마트스토어 멘토",
    "온라인 부업", "스마트스토어 부업", "쇼핑몰 부업",
    "온라인 창업 강의", "쇼핑몰 운영 노하우", "이커머스 노하우",
    "쇼핑몰 노하우", "스마트스토어 후기", "쇼핑몰 운영 후기",
    "월매출 1000만원", "월매출 5000만원", "1인 창업",
    "재택 사업", "온라인 수익화", "수익형 부업",
    "셀러 성공기", "스마트스토어 성공기",
    "이커머스 전문가", "쇼핑몰 전문가",
    "온라인 사업가", "1인 기업가",
    "쇼핑몰 디자인 강의", "상세페이지 강의",
    # 제품/카테고리
    "의류 쇼핑몰", "화장품 브랜드 창업", "패션 브랜드 런칭",
    "핸드메이드 쇼핑몰", "퍼스널브랜딩",
    "네이버 쇼핑 광고", "쿠팡 판매 전략",
    "자사몰 구축", "스마트스토어 셀러",
    # 신규 추가 키워드 (2026-06-15)
    "자사몰 만들기", "카페24 사용법", "아임웹 쇼핑몰",
    "스마트스토어 운영", "쿠팡 파트너스", "온라인 쇼핑몰 창업",
    "D2C 브랜드", "쇼핑몰 마케팅", "이커머스 컨설팅",
    "셀러 마케팅", "네이버 스마트스토어",
    # 신규 앵글 클러스터 (2026-07-16 channel-scout — 미채굴 버티컬)
    "구매대행 창업", "중국 구매대행", "해외구매대행 셀러", "공동구매 창업",
    "무재고 판매", "무재고 셀러", "위탁판매 노하우", "틱톡샵 셀러", "틱톡 커머스",
    "아마존 셀러", "아마존 창업", "쇼피 셀러", "글로벌 셀러", "이베이 셀러",
    "쿠팡 셀러", "쿠팡 로켓그로스", "쿠팡 윙", "스토어팜 판매", "스토어팜 창업",
    "도매매", "오너클랜", "도매꾹", "리셀 창업", "해외직구 창업",
    "반려동물 쇼핑몰", "뷰티 셀러", "식품 온라인판매", "패션 도매 쇼핑몰",
    "릴스 마케팅", "숏폼 마케팅", "라이브커머스 판매", "라방 셀러",
    "무자본 창업 스토어", "부업 스토어", "1인 쇼핑몰",
]

# 롱테일 (2026-09-30) — innertube는 검색 쿼터 0이라 키워드를 크게 늘려도 비용 없음.
# 같은 헤드 키워드 반복은 매일 같은 채널만 돌려줌(9/28: 1,876채널 중 net-new 8) → 롱테일로 깊이 확장.
LONGTAIL_KEYWORDS = [
    # 스마트스토어 실무
    "스마트스토어 상위노출", "스마트스토어 키워드", "스마트스토어 상품등록", "스마트스토어 초보",
    "스마트스토어 매출", "스마트스토어 광고", "스마트스토어 리뷰", "스마트스토어 수수료",
    "스마트스토어 사업자등록", "스마트스토어 대량등록", "스마트스토어 브랜드스토어", "스마트스토어 쇼핑라이브",
    "스마트스토어 정산", "스마트스토어 반품", "스마트스토어 소싱", "스마트스토어 1년",
    "네이버쇼핑 SEO", "네이버 쇼핑 상위노출", "네이버 가격비교", "상품명 짓는법",
    # 쿠팡/오픈마켓
    "쿠팡 상위노출", "쿠팡 광고", "쿠팡 로켓배송 입점", "오픈마켓 창업", "오픈마켓 셀러",
    "11번가 판매", "지마켓 판매", "옥션 셀러", "위메프 판매", "에이블리 셀러", "지그재그 입점",
    "무신사 입점", "29cm 입점", "텐바이텐 입점", "아이디어스 작가",
    # 카페24/아임웹/자사몰
    "카페24 디자인", "카페24 스킨", "카페24 쇼핑몰 만들기", "카페24 앱", "카페24 마케팅",
    "아임웹 홈페이지 만들기", "아임웹 사용법", "아임웹 디자인", "식스샵 쇼핑몰", "고도몰",
    "메이크샵", "자사몰 마케팅", "자사몰 매출", "자사몰 전환율", "자사몰 회원가입",
    "쇼핑몰 홈페이지 제작", "쇼핑몰 리뉴얼", "쇼피파이 한국", "쇼피파이 강의", "워드프레스 쇼핑몰",
    # 상세페이지/디자인/촬영
    "상세페이지 만들기", "상세페이지 디자인", "상세페이지 기획", "상세페이지 카피",
    "썸네일 만드는법 쇼핑몰", "제품 촬영", "제품사진 촬영 팁", "누끼 따기", "쇼핑몰 모델 촬영",
    "미리캔버스 상세페이지", "포토샵 상세페이지", "피그마 상세페이지",
    # 마케팅/광고 실무
    "메타 광고 세팅", "페이스북 광고 쇼핑몰", "인스타 광고 쇼핑몰", "GFA 광고", "네이버 검색광고 쇼핑",
    "구글 쇼핑 광고", "ROAS", "퍼포먼스 마케터", "그로스 해킹", "CRM 마케팅", "카카오톡 채널 마케팅",
    "리텐션 마케팅", "이메일 마케팅", "체험단 마케팅", "블로그 마케팅", "인플루언서 마케팅",
    "브랜딩 전략", "마케팅 대행사 대표", "광고 대행사 운영",
    # 창업/부업/사입
    "온라인 쇼핑몰 부업", "직장인 부업 스토어", "해외구매대행 부업", "구매대행 대량등록",
    "1688 사입", "타오바오 사입", "중국 사입", "알리바바 소싱", "위탁판매 사이트", "도매 사이트",
    "브랜드 런칭", "PB 상품 만들기", "OEM 제조 창업", "제조 창업", "와디즈 펀딩", "크라우드펀딩 성공",
    "온라인 사업 실패", "쇼핑몰 폐업", "이커머스 트렌드", "이커머스 창업", "D2C 마케팅",
    "셀러 브이로그", "쇼핑몰 브이로그", "쇼핑몰 사장 브이로그", "스토어 운영 브이로그",
    "사업가 브이로그", "1인 사업자", "개인사업자 세금", "쇼핑몰 세무",
]


# 관련성 게이트 (2026-09-30): innertube는 키워드당 수백 채널을 주지만 롱테일일수록 주제가 샌다.
# 실측: 게이트 없이 1,000건 스테이징 → 수동 검수 KEEP 32(3.2%)·REVIEW 16·NG 952(연예/게임/주식/부동산 등).
# KEEP 32 중 28이 아래 신호를(NG 952 중 69만 통과) 제목/설명에 포함 → 검수 전에 컷해서 validator 토큰·DB 오염을 줄인다.
RELEVANCE_RE = re.compile(
    r"쇼핑몰|스마트스토어|이커머스|셀러|쿠팡|카페24|아임웹|자사몰|상세페이지|구매대행|사입|도매|위탁판매|"
    r"온라인\s?(판매|유통|사업|쇼핑몰)|라이브커머스|쇼핑라이브|라방|아마존|쇼피|쇼피파이|드랍쉬핑|드롭쉬핑|"
    r"펀딩|마케팅\s?대행|광고\s?대행|중국\s?무역|이우|1688|타오바오", re.I)


def _api_get(endpoint: str, params: dict) -> dict:
    global _key_idx
    while _key_idx < len(API_KEYS):
        params["key"] = API_KEYS[_key_idx]
        url = f"https://www.googleapis.com/youtube/v3/{endpoint}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            if e.code in (403, 429) and ("quota" in body.lower() or e.code == 429):
                print(f"  [API] 키#{_key_idx} 쿼터 소진 → 다음 키", file=sys.stderr)
                _key_idx += 1
                continue
            print(f"  [API 오류] {endpoint}: {e}", file=sys.stderr)
            return {}
        except Exception as e:
            print(f"  [API 오류] {endpoint}: {e}", file=sys.stderr)
            return {}
    print(f"  [API] 모든 키({len(API_KEYS)}개) 쿼터 소진", file=sys.stderr)
    return {}


# ── innertube 검색 (쿼터 0) ────────────────────────────────────────────────
# search.list(100 quota/회, 25건 고정)를 대체. youtube.com 웹이 쓰는 내부 검색을 continuation으로
# 페이지 넘겨 키워드당 수백 채널을 뽑는다. 채널 상세는 channels.list(1 quota/50채널)로 그대로.
# 실측(2026-09-30): '스마트스토어' 10페이지 → 채널 260개, verdict net-new 12
#   vs API 121검색(12,100 quota) → 1,876채널, net-new 8 (2026-09-28 로그).
_IT_URL = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
_IT_CTX = {"client": {"clientName": "WEB", "clientVersion": "2.20250101.00.00", "hl": "ko", "gl": "KR"}}
# type=channel + 필터없음(영상 결과의 업로더). type=video 파라미터(EgIQAQ)는 2026-09-30 실측 403이라 제외.
_IT_FILTERS = ("EgIQAg%3D%3D", None)


def _it_post(body: dict) -> dict:
    req = urllib.request.Request(_IT_URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _it_walk(o, ids: set, toks: list) -> None:
    if isinstance(o, dict):
        if "channelRenderer" in o:
            ids.add(o["channelRenderer"].get("channelId", ""))
        for run in o.get("videoRenderer", {}).get("ownerText", {}).get("runs", []):
            b = run.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId", "")
            if b.startswith("UC"):
                ids.add(b)
        t = (o.get("continuationItemRenderer", {}).get("continuationEndpoint", {})
             .get("continuationCommand", {}).get("token"))
        if t:
            toks.append(t)
        for v in o.values():
            _it_walk(v, ids, toks)
    elif isinstance(o, list):
        for v in o:
            _it_walk(v, ids, toks)


_IT_BLOCKED = 0  # innertube 403/429 횟수 — 막히면 API 검색으로 대체(2026-10-01)


def innertube_search(keyword: str, pages: int = 10) -> set[str]:
    ids: set[str] = set()
    for flt in _IT_FILTERS:
        toks: list = []
        body = {"context": _IT_CTX, "query": keyword, **({"params": flt} if flt else {})}
        for _ in range(pages):
            try:
                _it_walk(_it_post(body), ids, toks)
            except Exception as e:
                global _IT_BLOCKED
                if "403" in str(e) or "429" in str(e):
                    _IT_BLOCKED += 1
                print(f"  [innertube 오류] '{keyword}': {e}", file=sys.stderr)
                break
            if not toks:
                break
            body = {"context": _IT_CTX, "continuation": toks.pop()}
            toks = []
            time.sleep(0.3)
    ids.discard("")
    return ids


def search_channels(keyword: str, max_results: int = 25) -> list[str]:
    """키워드로 채널 검색 → 채널 ID 리스트."""
    data = _api_get("search", {
        "part": "snippet",
        "q": keyword,
        "type": "channel",
        "maxResults": max_results,
        "regionCode": "KR",
        "relevanceLanguage": "ko",
    })
    return [item.get("snippet", {}).get("channelId", "") or item.get("id", {}).get("channelId", "")
            for item in data.get("items", []) if item.get("snippet")]


def get_channel_details(channel_ids: list[str]) -> list[dict]:
    """채널 ID 50개씩 batch로 상세 정보."""
    all_channels = []
    for i in range(0, len(channel_ids), 50):
        batch = [c for c in channel_ids[i:i+50] if c]
        if not batch:
            continue
        data = _api_get("channels", {
            "part": "snippet,statistics,brandingSettings",
            "id": ",".join(batch),
        })
        all_channels.extend(data.get("items", []))
    return all_channels


def extract_email(channel: dict) -> str | None:
    """채널 description + brandingSettings에서 이메일 추출."""
    snippet = channel.get("snippet", {})
    branding = channel.get("brandingSettings", {}).get("channel", {})
    text = " ".join([
        snippet.get("description", ""),
        branding.get("description", ""),
        branding.get("title", ""),
    ])
    emails = [fix_truncated_email(e) for e in EMAIL_RE.findall(text)]
    for e in emails:
        domain = e.split("@")[-1].lower()
        if domain not in SKIP_DOMAINS and 6 < len(e) < 60:
            return e.lower()
    return None


def fetch_channel_about(channel_id: str) -> str | None:
    """채널 about 페이지에서 이메일 추출 (백업)."""
    try:
        req = urllib.request.Request(
            f"https://www.youtube.com/channel/{channel_id}/about",
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ko-KR,ko"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="ignore")
        m = re.search(r"ytInitialData\s*=\s*({.{1,300000}?});", html)
        if m:
            emails = [fix_truncated_email(e) for e in EMAIL_RE.findall(m.group(1))]
            for e in emails:
                domain = e.split("@")[-1].lower()
                if domain not in SKIP_DOMAINS and 6 < len(e) < 60:
                    return e.lower()
    except Exception:
        pass
    return None


def _load_filters():
    contacted = json.loads((DATA / "contacted_list.json").read_text())
    contacted_emails = {(c.get("email") or "").lower() for c in contacted if isinstance(c, dict)}

    blocklist = json.loads((DATA / "blocklist.json").read_text())
    blocked = set()
    for b in blocklist:
        for k in ("name", "alias", "email", "instagram"):
            v = b.get(k, "")
            if v:
                blocked.add(v.lower().strip())
    return contacted_emails, blocked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keywords-limit", type=int, default=0, help="키워드 일부만 (테스트용)")
    parser.add_argument("--api-search", action="store_true",
                        help="구방식: search.list(100 quota/회) 사용. 기본은 innertube(쿼터 0)")
    parser.add_argument("--pages", type=int, default=10, help="innertube 키워드당 페이지(필터별)")
    parser.add_argument("--min-subs", type=int, default=300,
                        help="구독자 하한 — 구독자 수십 명짜리 셀러 본인 채널(청중 없음) 컷")
    parser.add_argument("--no-relevance", action="store_true", help="관련성 게이트 끄기(구방식 재현용)")
    args = parser.parse_args()

    contacted_emails, blocked = _load_filters()
    raw = json.loads(RAW_PATH.read_text())
    existing = raw.get("leads", [])
    existing_emails = {(l.get("email") or "").lower() for l in existing if l.get("email")}
    existing_handles = {l.get("handle", "") for l in existing}

    all_kw = list(dict.fromkeys(KEYWORDS + LONGTAIL_KEYWORDS))
    keywords = all_kw[: args.keywords_limit] if args.keywords_limit else all_kw
    # innertube가 유튜브에 막혀 있으면(403/429, 10/1 실측: 1.6만 건 수집 후 차단) 공식 API 검색으로 대체.
    # 쿼터 1만 = 검색 약 90회 → 날짜 기준으로 키워드를 돌려가며 90개만.
    if not args.api_search:
        innertube_search(keywords[0], 1)
        if _IT_BLOCKED:
            import random
            rnd = random.Random(datetime.now().strftime("%Y%m%d"))
            keywords = rnd.sample(keywords, min(90, len(keywords)))
            args.api_search = True
            print(f"[yt-api] ⚠️ innertube 차단 감지 → API 검색 대체(키워드 {len(keywords)}개)", file=sys.stderr)
    mode = "api" if args.api_search else "innertube"
    print(f"[yt-api] 키워드 {len(keywords)}개 검색 시작 (mode={mode})", file=sys.stderr)

    all_channel_ids = set()
    if args.api_search:
        results = ((kw, search_channels(kw, max_results=25)) for kw in keywords)
    else:
        # ponytail: 스레드 4개 고정. innertube가 막히면(429/빈응답) workers 줄이기.
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=4)
        results = zip(keywords, pool.map(lambda k: innertube_search(k, args.pages), keywords))
    for i, (kw, cids) in enumerate(results, 1):
        new = [c for c in cids if c not in all_channel_ids]
        all_channel_ids.update(new)
        print(f"  [{i}/{len(keywords)}] '{kw}': +{len(new)} (누적 {len(all_channel_ids)})", file=sys.stderr)

    print(f"[yt-api] 총 {len(all_channel_ids)}개 채널 발견", file=sys.stderr)

    # 채널 상세 batch
    print("[yt-api] 채널 상세 조회 중...", file=sys.stderr)
    channels = get_channel_details(list(all_channel_ids))
    print(f"[yt-api] {len(channels)}개 채널 상세 받음", file=sys.stderr)

    new_leads = []
    stats = {"total": 0, "with_email": 0, "dup": 0, "contacted": 0, "blocked": 0, "added": 0,
             "low_subs": 0, "off_topic": 0}
    uni = Universe()  # 판정식 단일화(lead_dedup.verdict) — 이메일+handle+컨택+바운스+블록

    for ch in channels:
        if len(new_leads) >= args.max:
            break
        stats["total"] += 1
        snippet = ch.get("snippet", {})
        stats_data = ch.get("statistics", {})
        cid = ch.get("id", "")
        title = snippet.get("title", "")
        desc = snippet.get("description", "")
        subs = int(stats_data.get("subscriberCount", 0) or 0)

        email = extract_email(ch)
        if not email and args.api_search:
            # about 페이지 = description과 같은 텍스트라 추가 수율 거의 0 → innertube 대량모드에선 생략
            email = fetch_channel_about(cid)
            time.sleep(0.3)

        if not email:
            continue
        stats["with_email"] += 1

        if not args.no_relevance and not RELEVANCE_RE.search(f"{title} {desc}"):
            stats["off_topic"] += 1
            continue

        if subs < args.min_subs and not stats_data.get("hiddenSubscriberCount"):
            stats["low_subs"] += 1
            continue

        handle = f"youtube_{cid}"
        ok, why = uni.verdict(email, handle)
        if not ok or email in existing_emails or handle in existing_handles:
            stats["contacted" if why in ("already_contacted", "blocked_or_bounced") else "dup"] += 1
            continue

        check_text = f"{title} {desc} {email}".lower()
        if any(b and b in check_text for b in blocked):
            stats["blocked"] += 1
            continue

        new_leads.append({
            "handle": handle,
            "display_name": title,
            "platform": "youtube",
            "followers": subs,
            "website": f"https://www.youtube.com/channel/{cid}",
            "email": email,
            "business_category": "이커머스 강사/유튜버",
            "country": "KR", "language": "ko",
            "bio": desc[:200],
            "profile_url": f"https://www.youtube.com/channel/{cid}",
            "is_business_account": True,
            "source_hashtag": f"youtube_{mode}",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "email_verified": False,
            "outreach_status": "pending",
            "follow_up_count": 0,
            "notes": f"youtube | subs:{subs}",
        })
        existing_emails.add(email)
        existing_handles.add(handle)
        stats["added"] += 1
        print(f"  ✓ {title[:30]} | {email} | subs:{subs}", file=sys.stderr)

    print(f"\n=== 결과 ===", file=sys.stderr)
    print(f"  채널 처리: {stats['total']}", file=sys.stderr)
    print(f"  이메일 있는 채널: {stats['with_email']}", file=sys.stderr)
    print(f"  leads_raw 중복: {stats['dup']}", file=sys.stderr)
    print(f"  contacted 중복: {stats['contacted']}", file=sys.stderr)
    print(f"  blocklist: {stats['blocked']}", file=sys.stderr)
    print(f"  구독자<{args.min_subs} 컷: {stats['low_subs']}", file=sys.stderr)
    print(f"  관련성 게이트 컷: {stats['off_topic']}", file=sys.stderr)
    print(f"  API 쿼터 사용 추정: {(len(keywords) * 100 if args.api_search else 0) + len(channels) // 50 + 1}",
          file=sys.stderr)
    print(f"  ✅ 신규 추가: {stats['added']}", file=sys.stderr)

    if args.dry_run:
        return

    added = commit_leads(new_leads, RAW_PATH)
    print(f"저장 완료: +{len(added)}건", file=sys.stderr)


if __name__ == "__main__":
    main()
