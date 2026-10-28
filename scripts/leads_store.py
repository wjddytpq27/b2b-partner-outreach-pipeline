#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""leads_raw.json 공통 저장 함수 (2026-09-30).

배경: 스크래퍼마다 leads_raw를 시작 시 읽고 끝에 통째로 덮어써서, 동시에 돌면 나중 저장이
앞 저장을 지웠다(2026-08-26 openads 123건 유실). 그래서 with_lock.py가 스크래퍼 "전체"를
잠갔는데, 네트워크 수집(수십 분)까지 잠겨 스카우트가 한 줄로 밀렸다.

이 함수는 "저장하는 몇 초"만 잠근다:
  락 획득 → 디스크의 최신 leads_raw 재읽기 → 이메일·handle 신규만 append → tmp+os.replace → 락 해제
그래서 수집은 병렬로 돌려도 유실이 없다.

with_lock.py 아래에서 돌면(KEEPGROW_LEADS_LOCK_HELD=1) 이미 락을 쥐고 있으므로 다시 잡지 않는다
(같은 파일을 자식이 또 flock하면 부모 락에 막혀 교착).

사용:
    from leads_store import commit_leads
    added = commit_leads(new_leads)
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

RAW_PATH = Path("~/lead-pipeline/data/leads_raw.json").expanduser()
LOCKFILE = "/tmp/acme_leads_write.lock"  # with_lock.py와 같은 파일
LOCK_HELD_ENV = "KEEPGROW_LEADS_LOCK_HELD"


@contextmanager
def leads_lock():
    if os.environ.get(LOCK_HELD_ENV) == "1":
        yield
        return
    with open(LOCKFILE, "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


# 스크래퍼가 페이지에서 잘못 긁는 가짜 이메일(2026-09-30 대기열에서 실제 발견:
# info@creativecommons.org=블로그 저작권 footer, _732_348@2x.mp=이미지 파일명)
_EMAIL_OK = re.compile(r"^[a-z0-9][a-z0-9._%+-]*@[a-z0-9.-]+\.[a-z]{2,}$")
_JUNK_EMAIL = re.compile(r"@\d+x\.|\.(png|jpe?g|gif|webp|svg|mp4?|css|js)$"
                         r"|@(creativecommons\.org|example\.com|w3\.org|schema\.org|sentry\.)"
                         r"|^u00[0-9a-f]{2}")  # HTML 이스케이프 찌꺼기(u003e = '>')


def is_junk_email(email: str) -> bool:
    e = (email or "").strip().lower()
    return bool(e) and (not _EMAIL_OK.match(e) or bool(_JUNK_EMAIL.search(e)))


_MAJOR_DOMAINS = {"naver.com", "gmail.com", "daum.net", "hanmail.net", "kakao.com", "nate.com",
                  "hotmail.com", "outlook.com", "icloud.com", "yahoo.com", "me.com"}


def _dead_domain_filter():
    """죽은 도메인(MX·A 레코드 없음) 판정기를 돌려준다. 판정식은 send_from_sheet._domain_deliverable 한 곳
    (확실히 없을 때만 False, 타임아웃 등은 통과). 2026-10-01: 발송 직전에야 13건이 dead_domain으로 거부됨 →
    수집 단계에서 미리 거른다. DNS 자체가 안 되면(맥 슬립·네트워크 단절) 판정 불가 → 필터 없이 통과."""
    try:
        from send_from_sheet import _domain_deliverable
    except Exception:
        return None
    if not _domain_deliverable("naver.com"):
        return None
    return lambda dom: dom in _MAJOR_DOMAINS or _domain_deliverable(dom)


def _key_sets(leads: list[dict]) -> tuple[set[str], set[str]]:
    emails = {(l.get("email") or "").strip().lower() for l in leads if l.get("email")}
    handles = {l.get("handle") for l in leads if l.get("handle")}
    return emails, handles


def commit_leads(new_leads: list[dict], raw_path: Path = RAW_PATH) -> list[dict]:
    """신규 리드를 leads_raw에 안전하게 추가하고, 실제로 추가된 리드를 반환한다."""
    if not new_leads:
        return []
    # 죽은 도메인 사전 차단 — DNS 조회는 네트워크라 락 밖에서
    alive_ok = _dead_domain_filter()
    if alive_ok is not None:
        before = len(new_leads)
        new_leads = [l for l in new_leads
                     if not (l.get("email") or "").strip()
                     or alive_ok((l.get("email") or "").strip().lower().split("@")[-1])]
        if before - len(new_leads):
            print(f"[leads_store] 죽은 도메인(MX·A 없음) {before - len(new_leads)}건 제외")
    with leads_lock():
        doc = json.loads(raw_path.read_text())
        is_list = isinstance(doc, list)
        cur = doc if is_list else doc.get("leads", [])
        emails, handles = _key_sets(cur)
        added: list[dict] = []
        for l in new_leads:
            em = (l.get("email") or "").strip().lower()
            h = l.get("handle")
            if is_junk_email(em) or (em and em in emails) or (h and h in handles):
                continue
            added.append(l)
            if em:
                emails.add(em)
            if h:
                handles.add(h)
        if not added:
            return []
        merged = cur + added
        if is_list:
            out = merged
        else:
            out = {**doc, "leads": merged, "lead_count": len(merged),
                   "generated_at": datetime.now(timezone.utc).isoformat()}
        tmp = raw_path.with_suffix(f".json.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        os.replace(tmp, raw_path)
        _record_personas(added, raw_path.parent / "persona_ledger.json")
    return added


def reserved_emails(path: Path = RAW_PATH.parent / "reserved_1to1.json") -> set[str]:
    """1:1 제휴 트랙(강사 등)에 예약된 이메일. 콜드·재컨택 자동발송 대상에서 제외한다(2026-10-01 운영자 지시)."""
    try:
        return {(x.get("email") or "").strip().lower() for x in json.loads(path.read_text()).get("reserved", [])}
    except Exception:
        return set()


def _record_personas(leads: list[dict], ledger_path: Path) -> None:
    """persona 태그를 이메일 기준 원장에 남긴다. 발송 후 리드가 leads_raw에서 빠져도
    pipeline_health가 페르소나별 가입 전환을 잴 수 있게(contacted_list엔 persona가 안 실림)."""
    tagged = [l for l in leads if l.get("persona") and l.get("email")]
    if not tagged:
        return
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    today = datetime.now(timezone.utc).date().isoformat()
    for l in tagged:
        ledger.setdefault(l["email"].strip().lower(),
                          {"persona": l["persona"], "query": l.get("persona_query", ""), "at": today})
    tmp = ledger_path.with_suffix(f".json.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2))
    os.replace(tmp, ledger_path)


if __name__ == "__main__":
    import tempfile
    d = Path(tempfile.mkdtemp())
    p = d / "leads_raw.json"
    p.write_text(json.dumps({"leads": [{"email": "A@x.com", "handle": "a"}]}))
    os.environ[LOCK_HELD_ENV] = "1"  # 테스트는 전역 락 안 잡음(실제 스크래퍼가 쥐고 있을 수 있음)
    got = commit_leads([{"email": "a@x.com", "handle": "z"},   # 이메일 중복(대소문자)
                        {"email": "b@x.com", "handle": "a"},   # handle 중복
                        {"email": "c@x.com", "handle": "c"},
                        {"email": "c@x.com", "handle": "c2"}], p)  # 같은 런 중복
    assert [l["email"] for l in got] == ["c@x.com"], got
    assert json.loads(p.read_text())["lead_count"] == 2
    assert commit_leads([], p) == []
    for junk in ("info@creativecommons.org", "_732_348@2x.mp", "_junkhandle@naver.com", "logo@2x.png", "u003einfo@ksifinance.com", ".@t.ew"):
        assert is_junk_email(junk), junk
    assert not is_junk_email("someone@gmail.com") and not is_junk_email("")
    commit_leads([{"email": "P@x.com", "handle": "p", "persona": "monetize_blogger", "persona_query": "q"}], p)
    assert json.loads((d / "persona_ledger.json").read_text())["p@x.com"]["persona"] == "monetize_blogger"
    print("ok")
