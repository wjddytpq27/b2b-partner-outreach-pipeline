"""Naver 검색 fetch 공통 헬퍼 — pacing + 403/429 지수 백오프 재시도.

배경: curl_cffi TLS 위장 파일럿 결과, Naver search.naver 403은 TLS 지문 차단이
아니라 순수 IP·요청속도(rate) 기반임이 실측으로 확인됨(맨 requests와 curl_cffi가
동일하게 49/60 차단). 따라서 진짜 처방은 TLS 위장이 아니라 ①호출 간격(pacing)
②403/429 시 지수 백오프 재시도. 이 모듈이 그 둘만 구현한다.

사용:
    from naver_fetch import naver_search_get
    r = naver_search_get(url, headers=HEADERS, timeout=12)
    if r is None or r.status_code != 200:
        # 백오프 재시도까지 실패 → 이 쿼리만 스킵(전체는 계속)
        return []
    soup = BeautifulSoup(r.text, "html.parser")
"""
import sys
import time
import threading

import requests

# 맨 requests 기본 헤더(기존 스크래퍼들과 동일). curl_cffi 미사용.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.naver.com",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MIN_INTERVAL = 0.7        # 검색/페이지 fetch 사이 최소 간격(초) — 연타 방지
BACKOFFS = (10, 20, 40)   # 403/429 시 대기(초). 순서대로 최대 len(BACKOFFS)회 재시도
BLOCK_CODES = {403, 429}

_lock = threading.Lock()
_last_call = [0.0]


def _pace():
    """직전 호출로부터 MIN_INTERVAL 이상 지나도록 대기(프로세스 전역, 스레드 안전)."""
    with _lock:
        now = time.monotonic()
        wait = MIN_INTERVAL - (now - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def naver_search_get(url, headers=None, timeout=12, params=None,
                     max_retries=3, session=None):
    """pacing + 403/429 지수 백오프로 Naver를 GET.

    반환:
      - 성공/비차단 응답: requests.Response (호출측에서 status_code 확인)
      - 백오프 재시도까지 계속 403/429: 마지막 차단 Response (status_code로 감지 가능)
      - 네트워크 예외가 재시도 끝까지 지속: None
    호출측은 (r is None or r.status_code != 200) 이면 그 쿼리를 스킵하고 진행한다.
    """
    hdr = headers if headers is not None else DEFAULT_HEADERS
    getter = session.get if session is not None else requests.get
    last_resp = None

    for attempt in range(max_retries + 1):
        _pace()
        try:
            resp = getter(url, headers=hdr, timeout=timeout, params=params)
        except Exception as e:  # 네트워크 계열 오류 — 백오프 후 재시도
            if attempt < max_retries:
                back = BACKOFFS[min(attempt, len(BACKOFFS) - 1)]
                print(
                    f"[naver_fetch] 네트워크 오류 {type(e).__name__} → {back}s 후 "
                    f"재시도({attempt + 1}/{max_retries})",
                    file=sys.stderr,
                )
                time.sleep(back)
                continue
            print(f"[naver_fetch] 네트워크 오류 지속 → 포기: {e}", file=sys.stderr)
            return None

        last_resp = resp
        if resp.status_code in BLOCK_CODES and attempt < max_retries:
            back = BACKOFFS[min(attempt, len(BACKOFFS) - 1)]
            print(
                f"[naver_fetch] {resp.status_code} 차단 → {back}s 백오프 후 "
                f"재시도({attempt + 1}/{max_retries})",
                file=sys.stderr,
            )
            time.sleep(back)
            continue
        return resp

    return last_resp
