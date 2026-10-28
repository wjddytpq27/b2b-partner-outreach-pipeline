#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""
track_conversion.py
====================
파트너 신청자 ↔ 컨택 이력 매칭 → 채널별 전환율 추적.

흐름:
  1. 파트너 신청 시트 읽기 (실제 가입자)
  2. contacted_list와 매칭 (어떤 채널 출신인지)
  3. 채널별 전환율 계산 + 리포트

사용:
  python3 track_conversion.py

cron 등록 가능 (매일):
  매일 신규 가입자 자동 매칭
"""
import os
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

DATA = Path("~/lead-pipeline/data").expanduser()
CREDS = os.path.expanduser("~/.config/lead-pipeline/credentials.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

PARTNER_SHEET_ID = "YOUR_PARTNER_SHEET_ID"


def load_affiliate_emails() -> dict:
    """제휴/직접영입 파트너 명단 (이메일 → source)."""
    path = DATA / "affiliate_partners.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for p in data.get("partners", []):
        em = (p.get("email") or "").lower().strip()
        if em:
            out[em] = p.get("source", "제휴")
    return out


def load_remember_emails() -> set:
    """리멤버 서베이 발송 이메일 (콜드와 별개 경로)."""
    path = DATA / "remember_sent.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {str(e).lower().strip() for e in data if isinstance(e, str) and "@" in str(e)}


def classify_unmatched(channel_text: str) -> str:
    """콜드/제휴/리멤버 어디에도 없을 때, 신청서의 활동채널로 출처 추정."""
    t = (channel_text or "").lower()
    if "크몽" in t or "kmong" in t:
        return "크몽"  # 크몽 플랫폼 메시지로 콜드 소싱 — 이메일 기록이 없어 매칭 실패
    if "youtube" in t or "유튜브" in t or "youtu.be" in t:
        return "유튜브"
    if "blog" in t or "블로그" in t or "blog.naver" in t:
        return "블로그"
    if "instagram" in t or "인스타" in t or "스레드" in t or "threads" in t or "틱톡" in t or "tiktok" in t or "sns" in t:
        return "SNS"
    if "소개" in t or "영업" in t:
        return "지인소개"
    if t.strip():
        return "기타"
    return "organic(불명)"


def main():
    # 1. 파트너 신청자 가져오기
    creds = Credentials.from_service_account_file(CREDS, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(PARTNER_SHEET_ID)
    ws = sh.sheet1
    partners = ws.get_all_records()
    print(f"파트너 신청자: {len(partners)}명")

    partner_emails = {(p.get("이메일") or "").lower().strip() for p in partners if p.get("이메일")}
    partner_emails.discard("")
    print(f"  유효 이메일: {len(partner_emails)}건")

    # 2. contacted_list 가져오기 (채널 정보)
    contacted = json.loads((DATA / "contacted_list.json").read_text())
    email_to_platform = {}
    email_to_date = {}
    for c in contacted:
        if isinstance(c, dict):
            email = (c.get("email") or "").lower().strip()
            if email:
                email_to_platform[email] = c.get("platform", "unknown")
                email_to_date[email] = c.get("contacted_at", "")

    affiliate_emails = load_affiliate_emails()
    remember_emails = load_remember_emails()

    # 3. 출처 분류 — 거의 전부 콜드. 제휴는 명시 명단(소수)뿐, organic은 사실상 0.
    #    핵심: 가입 이메일 ≠ 콜드 보낸 이메일(또는 크몽 등 플랫폼 메시지라 이메일 기록 자체 없음)이라
    #    contacted_list 이메일 매칭은 콜드 기여를 크게 과소계상한다. 매칭 실패해도 신청서에
    #    채널이 적혀 있으면 그 채널에서 우리가 콜드 소싱한 것 → 콜드로 귀속한다.
    cold_matched = []    # 콜드 — contacted_list 이메일 매칭됨 (채널 확실)
    cold_unmatched = []  # 콜드 — 이메일 매칭은 실패했지만 신청서 채널로 콜드 귀속
    affiliate = []       # 제휴/직접영입 (명시 명단)
    remember = []        # 리멤버 서베이
    organic = []         # 채널 단서도 전혀 없는 진짜 불명 (거의 0)
    for p in partners:
        email = (p.get("이메일") or "").lower().strip()
        if not email:
            continue
        base = {
            "email": email,
            "name": p.get("이름", ""),
            "company": p.get("회사명", ""),
            "partner_date": p.get("신청일", "") or p.get("date", ""),
        }
        channel_text = f"{p.get('활동 채널','')} {p.get('대표 활동 채널','')}".strip()
        if email in affiliate_emails:
            affiliate.append({**base, "platform": affiliate_emails[email]})
        elif email in email_to_platform:
            cold_matched.append({**base, "platform": email_to_platform[email],
                                 "contacted_at": email_to_date.get(email, "")})
        elif email in remember_emails:
            remember.append({**base, "platform": "remember"})
        else:
            src = classify_unmatched(channel_text)
            if src == "organic(불명)":
                organic.append({**base, "platform": src})
            else:
                cold_unmatched.append({**base, "platform": src})

    # 4. 통계
    total = len(partner_emails)
    cold_total = len(cold_matched) + len(cold_unmatched)
    print(f"\n=== 출처 분류 ===")
    print(f"파트너 가입자 총: {total}명")
    print(f"  📧 콜드 (이메일 매칭): {len(cold_matched)}명")
    print(f"  📧 콜드 (매칭실패·채널귀속): {len(cold_unmatched)}명")
    print(f"  🤝 제휴: {len(affiliate)}명")
    print(f"  📨 리멤버: {len(remember)}명")
    print(f"  ❓ organic(불명): {len(organic)}명")
    if total:
        print(f"  → 콜드 기여율: {cold_total/total*100:.1f}%  (콜드 {cold_total} / 제휴 {len(affiliate)} / 리멤버 {len(remember)} / organic {len(organic)})")
    if cold_unmatched:
        from collections import Counter as _C
        print("  매칭실패 콜드의 채널귀속 세부:", dict(_C(s["platform"] for s in cold_unmatched)))

    # 5. 채널별 전환 (이메일 매칭된 콜드 기준 — 분모 contacted_list가 채널별로만 있으므로)
    if cold_matched:
        platform_counter = Counter(m["platform"] for m in cold_matched)
        contacted_by_platform = Counter(email_to_platform.values())

        print(f"\n=== 채널별 전환 (이메일 매칭 콜드 기준) ===")
        print(f"{'채널':<25} {'컨택':<8} {'전환':<8} {'전환율':<10}")
        print("-" * 55)
        for platform, conv in sorted(platform_counter.items(), key=lambda x: -x[1]):
            denom = contacted_by_platform.get(platform, 0)
            conv_rate = (conv / denom * 100) if denom > 0 else 0
            print(f"{platform:<25} {denom:<8} {conv:<8} {conv_rate:.2f}%")

    # 6. 매칭 결과 CSV 저장
    import csv
    today = date.today().strftime("%Y%m%d")
    out_path = DATA / f"conversion_report_{today}.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["이메일", "이름", "회사", "채널(출처)", "컨택일", "가입일", "구분"])
        for m in cold_matched:
            writer.writerow([m["email"], m["name"], m["company"], m["platform"], m["contacted_at"], m["partner_date"], "콜드(매칭)"])
        for s in cold_unmatched:
            writer.writerow([s["email"], s["name"], s["company"], s["platform"], "", s["partner_date"], "콜드(채널귀속)"])
        for a in affiliate:
            writer.writerow([a["email"], a["name"], a["company"], a["platform"], "", a["partner_date"], "제휴"])
        for r in remember:
            writer.writerow([r["email"], r["name"], r["company"], r["platform"], "", r["partner_date"], "리멤버"])
        for o in organic:
            writer.writerow([o["email"], o["name"], o["company"], "", "", o["partner_date"], "organic불명"])

    print(f"\n✅ 저장: {out_path}")


if __name__ == "__main__":
    main()
