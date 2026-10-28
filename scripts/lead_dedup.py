#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""
lead_dedup.py — "이 리드는 새것인가"를 판정하는 **단일 술어**.

왜 존재하나 (2026-09-02 사고):
  cafe24_designer 잔여를 프로브할 때 agencyId(handle) 중복만 봐서 "207개 남음"이라 보고했는데,
  실제 스크래퍼는 handle + **이메일** 양쪽을 보므로 net-new는 0이었다. 같은 질문에 대해
  두 개의 서로 다른 판정식이 존재했던 게 원인. 판정식을 여기 하나로 모으고
  프로브·리포트·신규 스크래퍼는 전부 이걸 import 해서 쓴다.

핵심 규칙: **발송 가능 여부는 이메일이 최종 키다.** handle이 새것이어도 이메일이
이미 DB/컨택이력/바운스/블록에 있으면 새 리드가 아니다.
"""
from __future__ import annotations
import json
from pathlib import Path

DATA = Path("~/lead-pipeline/data").expanduser()
LEADS = DATA / "leads_raw.json"


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


class Universe:
    """기존에 아는 모든 이메일/handle. 한 번 만들어 재사용."""

    def __init__(self) -> None:
        raw = _read_json(LEADS, {"leads": []})
        leads = raw.get("leads", [])
        self.lead_emails = {
            (l.get("email") or "").strip().lower() for l in leads if l.get("email")
        }
        self.handles = {
            (l.get("handle") or "").strip().lower() for l in leads if l.get("handle")
        }
        self.contacted = set()
        self.blocked = set()
        for fn, bucket in (("contacted_list.json", self.contacted),
                           ("bounce_list.json", self.blocked),
                           ("blocklist.json", self.blocked)):
            for it in _read_json(DATA / fn, []) or []:
                if isinstance(it, dict) and it.get("email"):
                    bucket.add(it["email"].strip().lower())
                elif isinstance(it, str) and "@" in it:
                    bucket.add(it.strip().lower())
        self.all_emails = self.lead_emails | self.contacted | self.blocked

    # ── 판정 ────────────────────────────────────────────────────────────────
    def verdict(self, email: str | None, handle: str | None = None) -> tuple[bool, str]:
        """(새 리드인가, 이유). 이유는 리포트/로그에 그대로 찍어 근거를 남긴다."""
        e = (email or "").strip().lower()
        h = (handle or "").strip().lower()
        if not e or "@" not in e:
            return False, "no_email"
        if e in self.blocked:
            return False, "blocked_or_bounced"
        if e in self.contacted:
            return False, "already_contacted"
        if e in self.lead_emails:
            return False, "email_in_leads_db"
        if h and h in self.handles:
            return False, "handle_in_leads_db"
        return True, "new"

    def is_new(self, email: str | None, handle: str | None = None) -> bool:
        return self.verdict(email, handle)[0]

    # ── 질문이 다르면 술어도 달라야 한다 ────────────────────────────────────
    # verdict()  = "이 리드를 DB에 새로 추가할 수 있나"  (스크래퍼·프로브용)
    # sendable() = "이미 DB에 있는 이 리드를 지금 보낼 수 있나" (재고·복원용)
    # 2026-09-02: 격리 리드 복원 스크립트에서 verdict()를 쓰는 바람에 전부
    # "email_in_leads_db"로 걸러져 복원 0건이 나왔다. 같은 함수로 두 질문에
    # 답하려 한 게 오늘 낸 오진들과 정확히 같은 실수다.
    def sendable(self, email: str | None) -> tuple[bool, str]:
        e = (email or "").strip().lower()
        if not e or "@" not in e:
            return False, "no_email"
        if e in self.blocked:
            return False, "blocked_or_bounced"
        if e in self.contacted:
            return False, "already_contacted"
        return True, "sendable"

    def sendable_stock(self, channel: str | None = None) -> list[dict]:
        """leads_raw에 있으나 아직 발송되지 않은 재고(= 즉시 활용 가능 자산)."""
        raw = _read_json(LEADS, {"leads": []})
        out = []
        for l in raw.get("leads", []):
            e = (l.get("email") or "").strip().lower()
            if not e or "@" not in e:
                continue
            if e in self.contacted or e in self.blocked:
                continue
            if l.get("outreach_status") == "disqualified":
                continue
            if channel and (l.get("channel") or l.get("platform")) != channel:
                continue
            out.append(l)
        return out


def summarize(rows: list[tuple[str, str]]) -> dict:
    """[(email, handle)] → 판정 사유별 집계. 프로브가 근거를 함께 보고하도록 강제."""
    u = Universe()
    agg: dict[str, int] = {}
    fresh: list[str] = []
    for email, handle in rows:
        ok, why = u.verdict(email, handle)
        agg[why] = agg.get(why, 0) + 1
        if ok:
            fresh.append((email or "").strip().lower())
    return {"total": len(rows), "new": len(fresh), "by_reason": agg, "fresh_emails": fresh}


if __name__ == "__main__":
    u = Universe()
    print(f"leads DB 이메일 {len(u.lead_emails)} · handle {len(u.handles)}")
    print(f"컨택이력 {len(u.contacted)} · 차단/바운스 {len(u.blocked)}")
    print(f"판정 대상 전체 이메일 유니버스 {len(u.all_emails)}")
    print(f"미발송 재고(전 채널) {len(u.sendable_stock())}건")
