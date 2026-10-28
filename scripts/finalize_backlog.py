#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""
finalize_backlog.py
===================
pending → qualify → draft → backlog 전체 파이프라인을 순차 실행.
파일 충돌 없이 한 번에 처리.

사용:
  python3.14 scripts/finalize_backlog.py
"""
import os
import json
import re
import csv
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_blog_common import is_persona  # noqa: E402  수익형 블로거 페르소나 개인화 보호

ROOT = Path("~/lead-pipeline").expanduser()
LEADS = ROOT / "data" / "leads_raw.json"
CREDS = os.path.expanduser("~/.config/lead-pipeline/credentials.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
SHEET_ID = "YOUR_SEND_SHEET_ID"
KST = timezone(timedelta(hours=9))
CLAUDE_BIN = "/opt/homebrew/bin/claude"  # 구독요금제 headless 호출 (API 키 불필요)

PERSONAL_DOMAINS = {"gmail.com", "naver.com", "daum.net", "hanmail.net", "hotmail.com",
                    "yahoo.com", "outlook.com", "icloud.com", "kakao.com", "nate.com"}
# ── 대행사 직원·개인 마케터 채널: 개인 도달만 허용 (2026-09-02 운영자 지시) ──
# 왜: 대행사 "회사 메일"로 파트너스 제안을 보내면 대표·상사가 같이 보게 되고
#     "일에나 집중하지 남의 파트너스로 일한다고?"가 되어 리드를 죽인다.
#     그래서 이 채널들은 개인 메일(naver/gmail/daum/kakao 등)로만 보낸다.
# 어떻게: 회사 도메인·role 계정(info@·marketing@ 등)은 무조건 disqualified.
#     PERSONAL_OK_CHANNELS(개인메일 허용)의 반대 방향 게이트다.
STAFF_PERSONAL_ONLY_CHANNELS = {"openads", "iboss", "disquiet", "mobiinside", "agency_staff"}

SKIP_EMAIL_PREFIXES = {"info", "contact", "admin", "support", "help", "service",
                       "hello", "official", "press", "sales", "marketing"}
PERSONAL_OK_CHANNELS = {"lighting_maker", "rug_maker", "frame_maker", "phonecase_maker", "towel_maker", "curtain_maker", "shoe_maker", "kmong", "webagency", "shopify_partner", "cafe24_app", "titanclass", "maily", "sixshop_seller", "naver_blog", "youtube", "stibee", "maily", "linktree", "eventers",
                         "iboss", "disquiet", "brunch", "coloso",
                         "cafe24_designer", "fastcampus", "class101", "inflearn",
                         "imweb_expert", "taling", "liveklass", "funnelmoa",
                         "didisam", "inclass", "godomall_edu", "sixshop_partner",
                         "builder_studio", "oopy", "creatorlink", "ezcampus",
                         "imweb_site", "teachable", "tistory", "littly", "inpock",
                         "latpeed", "openads", "slashpage", "framer", "wix", "webflow",
                         "notefolio", "godo_designer", "indie_edu", "onoffmix",
                         "ecommerce_agency", "photo_studio", "globalselling",
                         "detailpage_studio", "livecommerce_agency",
                         "crowdfunding_agency", "video_studio", "review_agency",
                         "fulfillment_agency", "branding_studio", "websearch",
                         "sourcing_agency", "translation_agency", "cs_agency",
                         "packaging_agency", "gonggu_agency", "linkmix",
                         "export_agency", "influencer_agency", "tax_agency",
                         "cert_agency", "promo_agency", "oem_agency",
                         "trademark_agency", "model_agency", "qc_agency",
                         "insert_agency", "sns_agency", "container_agency",
                         "retouch_studio", "coldchain_agency", "goods_maker",
                         "label_maker", "packmaterial_agency", "customs_agency",
                         "sticker_maker", "foodprocess_agency", "delivery_agency",
                         "labelsupply_agency", "giftbox_agency", "productlabel_agency",
                         "trim_agency", "craft_supply", "fabric_supply", "shoppingbag_agency",
                         "cushionfoam_agency", "apparel_print", "prototype_maker",
                         "embroidery_maker", "jewelry_maker", "cylinder_pack",
                         "agri_pack", "pet_oem", "stationery_maker", "health_oem",
                         "furniture_maker", "coffee_oem", "ceramic_maker", "leather_maker",
                         "woodcraft_maker", "pouch_agency", "candle_oem", "cap_maker", "socks_maker",
                         "dessert_oem", "tea_oem", "bag_maker", "cosmetic_oem", "bedding_maker",
                         "soap_oem", "plush_maker", "confection_oem", "pet_apparel_maker"}
BLOCKLIST_PATH = ROOT / "data" / "blocklist.json"
CONTACTED_PATH = ROOT / "data" / "contacted_list.json"

# 명백한 공공/기관/조직 — 사람 아닌 계정. 디렉토리 등록 빌더는 예외(보호).
# 정밀 판정은 lead-validator(LLM)가 담당. 여기선 키워드로 명백한 것만 1차 차단.
ORG_HARD_KW = ("협동조합", "평생교육원", "진흥원", "공단", "위원회", "주민센터",
               "행정복지센터", "시청", "군청", "구청", "도청", "교육청",
               "청년센터", "청년꿈터", "문화재단", "사회적협동조합")
DIRECTORY_PROTECTED = {"cafe24_expert", "cafe24_designer", "imweb_expert",
                       "builder_studio", "photo_studio", "detailpage_studio"}

# 콜드메일 본문(운영자 2026-09-30 확정): 합쇼체만, em dash 금지, 문장마다 줄바꿈.
# 흐름 = 자기소개 → 제품소개 → 왜 당신(개인화) → 파트너 조건 → 회신/접수 CTA.
PRODUCT_INTRO = """저희는 쇼핑몰용 간편가입 SaaS를 운영하고 있습니다.
(공개용: 제품 소개 문구는 비공개)"""

COMMON_BODY = """파트너에 대해 간략히 설명드리면, 파트너 전용 링크를 발급받아 자사몰 사장님께 소개해 주시면
그 링크로 무료 설치가 완료될 때마다 1건당 5만원을 드립니다.
사장님은 무료로 설치하시고, 파트너를 통해 오시면 2개월간 무료로 이용하실 수 있습니다.
리워드는 매월 정산해 드리며, 이미 매달 꾸준히 정산받고 계신 파트너분들도 계십니다.

문의사항이 있으시면 편하게 회신 주셔도 되고, 아래 링크를 통해 간편하게 파트너 접수도 가능합니다.
https://example.com/partner-apply

감사합니다.
Acme 파트너스 담당자 드림"""


def clean_name(display_name: str) -> str:
    m = re.match(r"^(.*?)\s*\(", display_name or "")
    if m and m.group(1).strip():
        return m.group(1).strip()
    name = re.sub(r"\s*\|.*$", "", display_name or "").strip()
    return name or "담당자"


# 인사말 '{이름}님께'에 도메인/이메일/URL/영문핸들이 박히는 봇티 버그 차단(2026-08-26).
# 예: display_name이 'brewbean.co.kr'이면 'brewbean.co.kr님께'로 나가 스팸신호가 됨.
_NONPERSON_TLD_RE = re.compile(r"\.(com|kr|net|co|io|org|shop|store|me|cc|biz|kr)\b", re.I)


def person_name(raw: str) -> str:
    """인사에 쓸 '실제 사람/상호 이름'만 반환. 도메인·이메일·URL·영문핸들 등
    비-인명이면 '' 반환(→ 인사에서 이름을 아예 생략해 봇티 제거)."""
    n = clean_name(raw)
    if not n or n == "담당자":
        return ""
    low = n.lower().strip()
    if "@" in low or low.startswith("http") or "/" in low:
        return ""
    if "." in low or _NONPERSON_TLD_RE.search(low):        # 도메인류
        return ""
    # 한글이 전혀 없고 영숫자/기호로만 된 토큰 = 핸들/도메인코드 → 이름 취급 안 함
    if not re.search(r"[가-힣]", n) and re.fullmatch(r"[a-z0-9_.\-]+", low):
        return ""
    return n


SELLER_CHANNELS = {"sixshop_seller"}  # 본인이 쇼핑몰 운영 셀러


def _intro_line(name: str, channel: str = "") -> str:
    """개인화 인사가 없을 때 '왜 당신' 자리 문장(합쇼체). bio 없이도 채널로 아는 만큼은 말한다
    (2026-09-30: backlog 440건 중 185건이 무채널 generic 한 줄로 나가던 것 개선)."""
    if channel in BUILDER_CHANNELS:
        why = "쇼핑몰 제작을 하고 계신 것을 보고, 작업하시는 사장님들께 함께 소개하시기 좋은 제안이라 생각해 연락드렸습니다."
    elif channel in SELLER_CHANNELS:
        why = "직접 쇼핑몰을 운영하고 계신 것을 보고, 주변 사장님들께 소개하시기 좋은 제안이라 생각해 연락드렸습니다."
    elif channel in AUDIENCE_CHANNELS:
        why = "온라인 창업과 판매에 관한 콘텐츠를 운영하고 계신 것을 보고, 저희 파트너로 함께하셔도 좋겠다는 생각이 들어 연락드렸습니다."
    else:
        why = "자사몰 사장님들과 함께 일하고 계신 것을 보고, 저희 파트너로 함께하셔도 좋겠다는 생각이 들어 연락드렸습니다."
    return why


# 인사에 절대 들어가면 안 되는 표현(뒤 공통본문과 중복 → 통짜/모순). 포함 문장은 자동 제거.
_BANNED_IN_GREETING = ("5만", "무료 설치", "무료설치", "부담 0", "부담0", "부담이 0",
                       "매월 1일", "매월1일", "세팅", "넣어드리", "넣어 드리", "정산",
                       "제안을 드립니다", "제안드립니다", "제안 드립니다")


def _sanitize_greeting(g: str) -> str:
    """인사에서 정산조건·세팅 표현이 든 문장을 통째로 제거 → '인사만' 100% 보장.
    문장 단위로 쪼개서 금지어 포함 문장을 버리고 나머지만 재조합."""
    parts = re.split(r"(?<=[.!?])\s+", (g or "").strip())
    keep = [p.strip() for p in parts if p.strip() and not any(b in p for b in _BANNED_IN_GREETING)]
    return " ".join(keep).strip()


def personalize_greeting(lead: dict) -> tuple[str | None, str | None]:
    """claude -p --agent personalize-writer로 1건 개인화 인사 생성 (구독요금제, API키 불필요).
    반환: (verdict, greeting). verdict='KEEP'이면 greeting, 'NG_TARGET'이면 ('NG_TARGET','').
    호출 실패/파싱 실패 시 (None, None) → 호출부에서 기존 generic intro fallback."""
    # 이미 세션 Agent로 개인화해둔 인사가 있으면 claude 재호출 없이 그대로 사용
    pre = (lead.get("pre_greeting") or "").strip()
    if pre:
        return "KEEP", pre
    name = clean_name(lead.get("display_name") or lead.get("handle") or "")
    channel = lead.get("channel") or lead.get("platform") or ""
    bio = (lead.get("bio") or "")[:500]
    if is_persona(lead):
        # 수익형 블로거 페르소나 보호(2026-09-30): "일상블로그"로 NG_TARGET 처리하지 않는다.
        # naver_blog 가입자 다수가 이 형태(수익화·N잡·IT/생활꿀팁·리뷰, 프로필에 협업 문의 메일).
        target_rule = (" ※ 이 리드는 '수익형 블로거 페르소나'다. 기본 NG 목록의 '일상 블로그' 규칙은 이 리드에 적용하지 않는다.\n"
                       " - KEEP(기본): 블로그로 부수입을 내는 개인 블로거·인플루언서(주제 무관 — 맛집·여행·육아·IT·리뷰 OK).\n"
                       "   인사는 블로그 운영·수익화 활동을 근거로, 이웃·지인 중 자사몰 하시는 사장님께 소개할 수 있다는 연결로.\n"
                       " - NG_TARGET: 언론/기업·브랜드 공식블로그/공공기관만")
    else:
        target_rule = (" - KEEP: 셀러(쇼핑몰·자사몰 운영자) 청중/고객/수강생을 가진 개인 크리에이터·강사·전문가·쇼핑몰 빌더\n"
                       " - NG_TARGET: 투자/주식/언론/공공기관/회사공식채널/일상블로그/의료/법률/어학/종교 등 셀러 무관")
    prompt = f"""Acme 파트너스 콜드메일 인사 1건만 작성한다.

채널: {channel}
이름: {name}
bio: {bio or "(없음)"}

1) 타겟 검증:
{target_rule}
2) KEEP이면 **인사 2~3문장만** (인사는 인사만 — 뒤에 공통본문이 정산조건을 전부 설명하므로 인사에서 반복 금지):
 - 이 인사는 자기소개·제품소개 "다음"에 들어간다. "안녕하세요"·자기소개·제품 설명은 쓰지 말 것.
 - 1문장: bio 근거로 본인 활동을 구체적으로 (정확히, 거짓 친근감 금지). 예: "쿠팡파트너스 수익 후기와 블로그 운영 노하우 글을 인상 깊게 보았습니다."
 - 2문장: "이런 활동을 하시는 분이라면 저희 파트너로 함께하셔도 좋겠다는 생각이 들어 연락드렸습니다." 식으로 마무리
 - 문체: 반드시 합쇼체(-습니다/-됩니다). "-요"로 끝나는 해요체 금지. em dash(—) 금지.
 ❌ 절대 금지: 정산조건("5만원"·"무료 설치"·"부담 0"·"매월 1일"·"정산"), "세팅/설치해 주시면/함께 넣어드리면" 같은 표현, "제안을 드립니다"로 완결짓기. 파트너가 하는 일은 '셀러에게 링크를 소개'하는 것이지 직접 설치가 아니므로 '세팅' 표현 쓰지 말 것.
 - 이름이 사람이 아니라 상호/채널명이면 "OOO님께"처럼 어색하게 쓰지 말 것.

JSON만 출력: {{"verdict":"KEEP" 또는 "NG_TARGET","greeting":"인사텍스트(NG면 빈문자열)"}}"""
    try:
        # 개인화=품질 민감 → sonnet 고정. 프로젝트 컨텍스트 없이(13배 가벼움) 에이전트 지침만 시스템 프롬프트로.
        from claude_cli import claude_lean
        r = claude_lean(prompt, "claude-sonnet-4-6",
                        system_file=ROOT / "data" / "prompts" / "personalize-writer.md", timeout=120)
        m = re.search(r"\{.*\}", r.stdout, re.DOTALL)
        if not m:  # 실패 원인을 남긴다 (10/14 191건 전부 기본문장 사고 때 원인 로그가 없었음)
            with open(ROOT / "data" / "llm_fail.log", "a") as fl:
                fl.write(f"{datetime.now().isoformat(timespec='seconds')} personalize rc={r.returncode} out={r.stdout[:150]!r} err={r.stderr[:150]!r}\n")
        if m:
            d = json.loads(m.group())
            v = d.get("verdict", "KEEP")
            g = (d.get("greeting") or "").strip()
            if v == "NG_TARGET":
                return "NG_TARGET", ""
            if g:
                return "KEEP", g
    except Exception as e:
        print(f"    (personalize 실패 → generic fallback: {str(e)[:50]})")
    return None, None


# 고정 자기소개 — 모든 콜드메일 첫 줄. 개인화 인사는 이 뒤에 줄바꿈해서 붙는다.
FIXED_INTRO = "안녕하세요, Acme 파트너스 담당자 선임입니다."


WHY_YOU_CLOSING = "이런 활동을 하시는 분이라면 저희 파트너로 함께하셔도 좋겠다는 생각이 들어 연락드렸습니다."


def with_closing(g: str) -> str:
    """개인화 인사가 활동 묘사로만 끝나면 '그래서 파트너 제안' 연결 문장을 붙인다."""
    return g if "연락드" in g else f"{g}\n{WHY_YOU_CLOSING}"


def _format_greeting(g: str) -> str:
    """개인화 인사를 문장마다 줄바꿈. 자기소개 중복·해요체(-요) 문장은 빼고, em dash는 쉼표로."""
    g = (g or "").replace(" — ", ", ").replace("—", ", ")
    sents = re.split(r"(?<=[.!?])\s+", g.strip())
    body = [s.strip() for s in sents if s.strip()
            and "안녕하세요" not in s and not ("담당자" in s and "입니다" in s)
            and not re.search(r"요[.!?]?$", s.strip())]
    return "\n".join(body)


# 3그룹 라우팅: 빌더(쇼핑몰 제작자) / 청중형(독자·구독자) / 공급·대행(셀러 거래처)
BUILDER_CHANNELS = {
    "kmong", "builder_studio", "webagency", "imweb_expert", "imweb_site",
    "cafe24_designer", "cafe24_expert", "cafe24_expert_v2", "cafe24_expert_v3",
    "sixshop_partner", "godo_designer", "detailpage_studio", "photo_studio",
    "branding_studio", "retouch_studio", "framer", "wix", "webflow",
}
AUDIENCE_CHANNELS = {  # 고객사가 아니라 "청중"(독자/구독자/팔로워)을 가진 콘텐츠형
    "naver_blog", "youtube", "stibee", "maily", "linktree", "coloso", "eventers",
    "disquiet", "iboss", "brunch", "tistory", "oopy", "littly", "inpock", "linkmix",
    "onoffmix", "latpeed", "slashpage", "notefolio", "teachable", "ezcampus",
    "fastcampus", "inflearn", "liveklass", "class101", "taling", "globalselling",
    "funnelmoa", "didisam", "inclass", "godomall_edu", "creatorlink", "indie_edu",
}


def make_draft(lead: dict) -> tuple[str, str | None]:
    channel = lead.get("channel") or lead.get("platform") or "unknown"
    name = person_name(lead.get("display_name") or lead.get("handle") or "")
    intro = _intro_line(name, channel)   # 개인화 실패 시 채널별 "왜 당신" 문장

    # 제목: 개인화·업무 톤(운영자 2026-09-17) — 스팸형 "돈 안 들어요" 폐기, 오픈율↑
    if name and len(name) <= 6:
        subject = f"[Acme] {name}님께 간편가입 SaaS 파트너 제안드립니다"
    else:
        subject = "[Acme] 간편가입 SaaS 파트너 제안드립니다"

    # 개인화 인사 시도 (claude -p --agent personalize-writer, 구독요금제).
    # NG_TARGET → body=None 신호로 호출부에서 disqualify. 실패 → generic intro 유지.
    verdict, greeting = personalize_greeting(lead)
    if verdict == "NG_TARGET":
        return subject, None
    if greeting:
        g2 = _sanitize_greeting(greeting)
        g3 = _format_greeting(g2)
        if len(g3) >= 15:   # 인사가 남으면 사용, 부실하면 기본 문장 유지
            intro = with_closing(g3)
    body = f"{FIXED_INTRO}\n\n{PRODUCT_INTRO}\n\n{intro}\n\n{COMMON_BODY}"
    return subject, body


from leads_store import reserved_emails as _reserved_emails
RESERVED_1TO1 = _reserved_emails()


def step1_qualify(leads: list[dict], blocklist: set, contacted: set) -> tuple[int, int]:
    reviewed = qualified = disqualified = 0
    unchecked = 0
    for l in leads:
        if l.get("outreach_status") != "pending":
            continue
        # 검수 순서 강제(2026-10-01): LLM 검수(llm_validator_batch)가 keep 판정한 리드만 qualify.
        # 9/30 밤 LLM 검수가 266/526에서 중단됐는데 finalize가 나머지 260건을 그대로 통과시켜 backlog에 올렸다.
        if not l.get("llm_checked"):
            unchecked += 1
            continue
        reviewed += 1
        email = (l.get("email") or "").strip().lower()
        if not email:
            l["outreach_status"] = "no_email"
            continue
        if email in RESERVED_1TO1:
            l["outreach_status"] = "hold"
            l["hold_reason"] = "reserved_1to1"
            continue
        if email in blocklist or email in contacted:
            l["outreach_status"] = "disqualified"
            l["dq_reason"] = "blocklist/bounce"
            disqualified += 1
            continue
        dom = email.split("@")[-1].lower()
        lead_channel = l.get("channel") or l.get("platform") or ""
        # 직원 채널은 개인 도달만 — 회사 메일/role 계정이면 여기서 끊는다.
        if lead_channel in STAFF_PERSONAL_ONLY_CHANNELS:
            pfx = email.split("@")[0].lower()
            if dom not in PERSONAL_DOMAINS:
                l["outreach_status"] = "disqualified"
                l["dq_reason"] = "직원채널_회사도메인(개인메일만 허용)"
                disqualified += 1
                continue
            if any(pfx.startswith(x) for x in SKIP_EMAIL_PREFIXES):
                l["outreach_status"] = "disqualified"
                l["dq_reason"] = "직원채널_role계정"
                disqualified += 1
                continue
        if dom in PERSONAL_DOMAINS and lead_channel not in PERSONAL_OK_CHANNELS:
            l["outreach_status"] = "disqualified"
            l["dq_reason"] = "개인이메일만"
            disqualified += 1
            continue
        prefix = email.split("@")[0].lower()
        if any(prefix.startswith(p) for p in SKIP_EMAIL_PREFIXES) and lead_channel not in PERSONAL_OK_CHANNELS:
            l["outreach_status"] = "disqualified"
            l["dq_reason"] = "generic_prefix"
            disqualified += 1
            continue
        # linktree/maily: 이름+bio 없으면 발송 불가 → disqualify
        if lead_channel in ("linktree", "maily"):
            dn = (l.get("display_name") or l.get("handle") or "").strip()
            bio_txt = (l.get("bio") or "").strip()
            if not dn and not bio_txt:
                l["outreach_status"] = "disqualified"
                l["dq_reason"] = "no_identity"
                disqualified += 1
                continue

        # 명백한 공공/기관/조직 1차 차단 (디렉토리 등록 빌더는 보호) — 정밀 판정은 LLM validator
        ident = (l.get("display_name") or "") + " " + (l.get("bio") or "")
        if any(k in ident for k in ORG_HARD_KW) and lead_channel not in DIRECTORY_PROTECTED:
            l["outreach_status"] = "disqualified"
            l["dq_reason"] = "공공/기관/조직"
            disqualified += 1
            continue

        l["outreach_status"] = "qualified"
        l["qual_score"] = 30
        qualified += 1
    if unchecked:
        print(f"    ⏸ LLM 검수 미완료 pending {unchecked}건 — qualify 보류(다음 검수 후 처리)")
    return qualified, disqualified


def send_eligible_channels() -> set[str] | None:
    """발송 게이트가 통과시키는 채널 = 원장 검증채널 ∪ 테스트슬롯 후보. 판정식은 send_from_sheet 한 곳.
    원장을 못 읽으면 None(판정 불가는 판정이 아니다 → 아무것도 hold하지 않음)."""
    from send_from_sheet import CANDIDATE_CHANNELS, _load_proven_channels
    proven = _load_proven_channels()
    return (proven | CANDIDATE_CHANNELS) if proven else None


def hold_gate_ineligible(leads: list[dict]) -> int:
    ok = send_eligible_channels()
    if ok is None:
        print("    ⚠️ 원장 못 읽음 → 게이트 hold 건너뜀")
        return 0
    n = 0
    for l in leads:
        if l.get("outreach_status") not in ("qualified", "draft_ready"):
            continue
        if (l.get("channel") or l.get("platform") or "") not in ok:
            l["outreach_status"] = "hold"
            l["hold_reason"] = "gate_ineligible"
            n += 1
    return n


def step2_draft(leads: list[dict]) -> list[list]:
    rows = []
    for l in leads:
        if l.get("outreach_status") != "qualified":
            continue
        subject, body = make_draft(l)
        # personalize-writer가 NG_TARGET 판정 → body=None → 발송 후보 제외
        if body is None:
            l["outreach_status"] = "disqualified"
            l["dq_reason"] = "NG_TARGET(개인화검수)"
            continue
        # draft_body가 이름 없이 "담당자님께"로 시작하는 경우 draft 생략
        name = clean_name(l.get("display_name") or l.get("handle") or "")
        channel = l.get("channel") or l.get("platform") or ""
        if name == "담당자" and channel in ("linktree", "maily"):
            l["outreach_status"] = "disqualified"
            l["dq_reason"] = "no_identity"
            continue
        l["draft_subject"] = subject
        l["draft_body"] = body
        l["outreach_status"] = "draft_ready"
        rows.append([name, (l.get("email") or "").strip(), channel, subject, body, ""])
    return rows


def step3_push_to_backlog(all_draft_rows: list[list], backlog_ws, sheet1_emails: set) -> int:
    existing = backlog_ws.get_all_values()
    existing_emails = {r[1].strip().lower() for r in existing[1:] if len(r) > 1 and r[1].strip()}

    fresh = []
    for r in all_draft_rows:
        em = r[1].strip().lower()
        if em and em not in existing_emails and em not in sheet1_emails:
            fresh.append(r)
            existing_emails.add(em)

    if fresh:
        backlog_ws.append_rows(fresh, value_input_option="USER_ENTERED")
    return len(fresh)


def main():
    blob = json.loads(LEADS.read_text())
    leads = blob["leads"]

    # new → pending 자동 변환 (스크래퍼들이 new 상태로 저장하는 경우)
    new_converted = sum(1 for l in leads if l.get("outreach_status") == "new")
    for l in leads:
        if l.get("outreach_status") == "new":
            l["outreach_status"] = "pending"
    if new_converted:
        print(f"[전처리] new → pending 변환: {new_converted}건")

    # Load blocklist / contacted
    blocklist: set[str] = set()
    if BLOCKLIST_PATH.exists():
        try:
            bl = json.loads(BLOCKLIST_PATH.read_text())
            for item in (bl if isinstance(bl, list) else bl.get("emails", [])):
                if isinstance(item, str):
                    blocklist.add(item.lower())
                elif isinstance(item, dict):
                    blocklist.add((item.get("email") or "").lower())
        except Exception as e:
            raise RuntimeError(f"blocklist 로드 실패 — 빈 set 발송사고 방지 위해 중단: {e}")
    contacted: set[str] = set()
    if CONTACTED_PATH.exists():
        try:
            cl = json.loads(CONTACTED_PATH.read_text())
            for item in (cl if isinstance(cl, list) else []):
                contacted.add((item.get("email") or "").lower())
        except Exception as e:
            raise RuntimeError(f"contacted_list 로드 실패 — 5천+명 재발송 방지 위해 중단: {e}")

    # Step 1: Qualify
    pending_count = sum(1 for l in leads if l.get("outreach_status") == "pending")
    print(f"\n[1] Qualify — pending: {pending_count}건")
    q, dq = step1_qualify(leads, blocklist, contacted)
    print(f"    qualified: {q}건 / disqualified: {dq}건")

    # Step 1.5: 발송 게이트가 어차피 드롭할 채널은 초안·개인화·backlog 적재를 하지 않고 hold (2026-09-30).
    # backlog 427건 중 367건이 게이트 비대상으로 쌓여 순도 분모만 키우던 좀비재고 재발 방지.
    held = hold_gate_ineligible(leads)
    if held:
        print(f"\n[1.5] 게이트 비대상 채널 hold: {held}건 (검증채널·테스트후보 아님 → 초안 안 만듦)")

    # Step 2: Draft
    qualified_count = sum(1 for l in leads if l.get("outreach_status") == "qualified")
    print(f"\n[2] Draft — qualified: {qualified_count}건")
    new_rows = step2_draft(leads)
    print(f"    drafted: {len(new_rows)}건")

    # Save JSON
    blob["lead_count"] = len(leads)
    LEADS.write_text(json.dumps(blob, ensure_ascii=False, indent=2))
    print(f"    leads_raw.json 저장 완료")

    # Step 3: Push all draft_ready to backlog
    all_draft_ready = [
        [clean_name(l.get("display_name") or l.get("handle") or ""),
         (l.get("email") or "").strip(),
         l.get("channel") or l.get("platform") or "",
         l.get("draft_subject") or "",
         l.get("draft_body") or "",
         ""]
        for l in leads
        if l.get("outreach_status") == "draft_ready" and (l.get("email") or "").strip()
    ]
    print(f"\n[3] Backlog 업로드 — draft_ready: {len(all_draft_ready)}건")

    gc = gspread.authorize(Credentials.from_service_account_file(CREDS, scopes=SCOPES))
    sheet = gc.open_by_key(SHEET_ID)
    try:
        backlog = sheet.worksheet("backlog")
    except Exception:
        backlog = sheet.add_worksheet("backlog", rows=5000, cols=10)
        backlog.append_row(["이름", "이메일", "채널", "제목", "본문", "발송여부"])
    try:
        sheet1 = sheet.worksheet("시트1")
        sheet1_vals = sheet1.get_all_values()
        sheet1_emails = {r[1].strip().lower() for r in sheet1_vals[1:] if len(r) > 1}
    except Exception:
        sheet1_emails = set()

    added = step3_push_to_backlog(all_draft_ready, backlog, sheet1_emails)
    backlog_total = len(backlog.get_all_values()) - 1
    print(f"    신규 업로드: {added}건")
    print(f"\n✅ backlog 최종: {backlog_total}건")
    print(f"   시트1: {len(sheet1_emails)}건 (발송 예정)")

    # Summary
    final = Counter(l.get("outreach_status") for l in leads)
    print(f"\n최종 현황:")
    for k, v in sorted(final.items(), key=lambda x: -x[1]):
        print(f"  {(k or 'pending'):25s} {v}")


if __name__ == "__main__":
    main()
