#!/usr/bin/env python3
"""
lead-validator: LLM 기반 리드 검수 스크립트
- pending 리드 전체 검수
- ng_keywords.json 기준 키워드 1차 필터
- LLM 판단으로 2차 정밀 검수
- cafe24_expert/cafe24_designer/imweb_expert/iboss/disquiet 출처 보호 규칙 적용
"""

import os
import json
import re
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.expanduser("~/lead-pipeline/scripts"))
from naver_blog_common import PERSONA_HARD_NG_CATEGORIES, is_persona, persona_org_ng  # noqa: E402

CLAUDE_BIN = "/opt/homebrew/bin/claude"

LEADS_PATH = os.path.expanduser("~/lead-pipeline/data/leads_raw.json")
NG_KEYWORDS_PATH = os.path.expanduser("~/lead-pipeline/data/ng_keywords.json")

# ─── 타겟 정의 ─────────────────────────────────────────────────────────────────
# KEEP 기준:
#   - 셀러(쇼핑몰 운영자/온라인 셀러)를 청중으로 가진 개인 크리에이터
#   - 셀러를 클라이언트로 가진 개인 전문가 (디자이너, 마케터, 컨설턴트)
#   - 이커머스 분야 1인기업·1인사업가·프리랜서
#
# DISQUALIFY 기준:
#   - 회사 공식 발송 (공식 블로그, 회사 뉴스레터)
#   - 공공기관·협회·재단·진흥원
#   - 이커머스 무관 업종 (학원, 세무/법무, 부동산, 의료, 음식점, 종교, 관광)
#   - 메타 텍스트 오류 (Instagram, Email 등)
#   - 자사몰 운영자 본인 (CX팀 별도)
#   - disquiet: SaaS/AI 개발자, 스타트업 창업자 (이커머스 무관)
#   - 이커머스와 무관한 일반 개발자/디자이너/PM

# 보호 출처: 이 플랫폼 리드는 NG 키워드로 자르지 않음 (LLM만 판단)
PROTECTED_SOURCES = {"cafe24_expert", "cafe24_designer", "imweb_expert", "cafe24_app_store",
                     "photo_studio", "detailpage_studio", "godo_designer"}

# 전상법 사업자정보(footer)로 수집된 '셀러(쇼핑몰 사장님)에게 서비스를 파는 개인 제작자' 채널.
# 스크래퍼가 이미 개인사업자·이커머스 순도(corp 필터·전상법·업종 신호)를 걸러둠.
# LLM(haiku)이 얇은 bio의 "○○스튜디오 대표"를 대기업 법인으로 오독해 대량 disqualify 하던 것 방지
# (2026-07-13 photo_studio 90/98 오판 사고). 이 채널은 KEEP 기본, 명백한 대기업/B2C만 컷.
# godo_designer는 cafe24_designer의 고도몰 쌍둥이 채널인데 이 보호목록에서 누락돼 동일
# 오판(주식회사/help@ 도메인메일=법인 컷)으로 4건 전량 disqualified — 2026-09-23 추가.
MAKER_DIRECTORY_SOURCES = {"builder_studio", "photo_studio", "cafe24_designer", "imweb_expert",
                           "detailpage_studio", "godo_designer"}


def load_data():
    with open(LEADS_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    with open(NG_KEYWORDS_PATH, "r", encoding="utf-8") as f:
        ng = json.load(f)
    return raw, ng


def build_keyword_sets(ng_data):
    """NG 키워드 전체 플랫(flat) 리스트 + false positive 보호 세트"""
    all_ng = []
    for cat, words in ng_data["categories"].items():
        all_ng.extend(words)
    false_positive_safe = set(ng_data.get("false_positive_safe", []))
    ng_platforms = set(ng_data.get("ng_platforms", []))
    ng_bio_patterns = ng_data.get("ng_bio_patterns", [])
    return all_ng, false_positive_safe, ng_platforms, ng_bio_patterns


def persona_hard_keywords(ng_data):
    """수익형 블로거 페르소나에 여전히 적용할 NG 키워드(공공·회사 명시·메타오류·종교정치)만."""
    return [kw for cat, words in ng_data["categories"].items()
            if cat in PERSONA_HARD_NG_CATEGORIES for kw in words]


def keyword_check(lead, all_ng, false_positive_safe, ng_platforms, ng_bio_patterns, persona_hard_ng=None):
    """
    키워드 기반 1차 필터.
    반환: (is_ng: bool, reason: str)
    """
    platform = lead.get("platform", "") or ""
    display_name = lead.get("display_name", "") or ""
    bio = lead.get("bio", "") or ""
    notes = lead.get("notes", "") or ""
    handle = lead.get("handle", "") or ""
    category = lead.get("business_category", "") or ""

    # 플랫폼 NG
    if platform in ng_platforms:
        return True, f"ng_platform:{platform}"

    # bio 패턴 NG
    combined_text = f"{display_name} {bio} {notes} {category}"
    for pattern in ng_bio_patterns:
        if pattern in combined_text:
            return True, f"ng_bio_pattern:{pattern}"

    # 보호 출처 → 키워드 필터 스킵 (LLM만)
    if platform in PROTECTED_SOURCES:
        return False, ""

    # 수익형 블로거 페르소나 보호(2026-09-30, 디렉토리 보호와 같은 방식): 일상·맛집·여행·육아 등 주제 NG는
    # 이 페르소나의 정상 형태(naver_blog 가입자 다수)라 적용 안 함. 조직(언론·기업·공공)만 차단.
    if is_persona(lead):
        org = persona_org_ng(lead)
        if org:
            return True, org
        for kw in (persona_hard_ng or []):
            if isinstance(kw, str) and (kw in display_name or kw in bio or kw in category):
                return True, f"keyword:{kw}"
        return False, ""

    # false positive safe 단어가 있으면 해당 리드는 보호
    for safe_word in false_positive_safe:
        if isinstance(safe_word, str) and safe_word.startswith("_"):
            continue
        if isinstance(safe_word, str) and safe_word in combined_text:
            return False, ""

    # NG 키워드 매칭
    for kw in all_ng:
        if not isinstance(kw, str):
            continue
        if kw in display_name or kw in bio or kw in category:
            return True, f"keyword:{kw}"

    return False, ""


def llm_judge(leads_batch):
    """
    LLM 판단 - Claude 자체 추론으로 직접 판단.
    각 리드에 대해 결정(keep/disqualify/review)과 사유를 반환.
    """
    results = {}

    for lead in leads_batch:
        lid = lead.get("handle") or lead.get("email") or str(id(lead))
        platform = lead.get("platform", "") or ""
        display_name = lead.get("display_name", "") or ""
        bio = lead.get("bio", "") or ""
        notes = lead.get("notes", "") or ""
        handle = lead.get("handle", "") or ""
        email = lead.get("email", "") or ""
        category = lead.get("business_category", "") or ""

        decision, reason = judge_single(platform, display_name, bio, notes, handle, email, category,
                                        persona=is_persona(lead))
        results[lid] = (decision, reason)

    return results


def judge_single(platform, display_name, bio, notes, handle, email, category, persona=False):
    """
    claude CLI subprocess로 실제 LLM 검수.
    반환: (decision: str, reason: str)
    decision: "keep" | "disqualify" | "review"
    """
    # 전상법 수집 '셀러 대상 개인 제작자' 채널 — 스크래퍼가 이미 개인/이커머스 순도 필터.
    # bio가 얇아("○○스튜디오 대표 | 쇼핑몰 제품촬영") haiku가 법인으로 오독하므로 KEEP 기본으로 교정.
    # 수익형 블로거 페르소나 — "셀러 청중 아님"으로 자르지 않는다. 조직만 컷.
    # 근거: naver_blog 가입자 다수이 이커머스가 아닌 수익화·N잡·IT/생활꿀팁·리뷰 블로거(프로필에 협업 문의 메일).
    # 5만원/설치 레퍼럴은 쿠팡파트너스형이라 블로그로 부수입 내는 개인이 반응한다.
    if persona:
        prompt = f"""Acme 파트너스 리드 검수(수익형 블로거 페르소나).
이 리드는 블로그로 부수입을 내는 개인 블로거다(블로그 수익화·쿠팡파트너스·체험단·N잡·IT/생활 꿀팁·리뷰·AI 활용 등).
※ "이름" 칸은 블로거 닉네임이 아니라 수집된 **글 제목**일 수 있다. 제목이 상품명·서비스명처럼 보여도 그것만으로 메타 오류·조직 판정하지 말고 소개(bio)로 판단하라.
주제가 이커머스가 아니어도 된다 — 실제 가입자 대다수가 이 형태다. 맛집·여행·육아·뷰티·일상 주제도 KEEP.

[KEEP — 기본값] 협업/리뷰/협찬 문의를 받는 개인 블로거·인플루언서 (주제 무관)
[DISQUALIFY — 발송 X] 언론사·매거진, 기업/브랜드 공식블로그·홍보 계정, 공공기관/협회/재단, 메타 오류(이름이 "Instagram"/"Email" 등)
[REVIEW] 개인인지 조직인지 정말 불분명할 때만.

플랫폼: {platform}
이름: {display_name or "(없음)"}
소개: {bio or "(없음)"}
이메일: {email}

JSON만 반환: {{"decision": "keep"/"disqualify"/"review", "reason": "이유"}}"""
        # fallback 규칙은 플랫폼별 주제 NG(naver_blog: 맛집/음식 → disqualify)가 있어 페르소나 전용 키로 우회
        return _run_llm(prompt, f"persona:{platform}", display_name, bio, notes, handle, email, category)

    if platform in MAKER_DIRECTORY_SOURCES:
        prompt = f"""Acme 파트너스 리드 검수.
이 리드는 전상법 사업자정보(footer)로 수집된, 쇼핑몰/자사몰 셀러(사장님)에게 서비스를 파는 제작자다
(제품촬영·상세페이지 제작·쇼핑몰 구축·디자인 등). 셀러를 고객으로 두므로, 작업하는 사장님들께
'간편가입 SaaS'을 소개할 수 있는 정타겟이다. bio는 원래 짧다("○○스튜디오 대표 | 제품촬영").

[KEEP — 발송 OK / 기본값]
- 개인사업자·1인 스튜디오·프리랜서 제작자 (촬영/상세페이지/쇼핑몰 제작/디자인)
- bio에 "대표/스튜디오"가 있어도 개인사업자·소규모면 KEEP (이게 정상 형태다. 법인이라 단정 금지)

[DISQUALIFY — 발송 X]
- 명백한 대기업/중견 법인 (규모 큰 주식회사, 대형 대행사 조직)
- 이커머스와 무관한 B2C 사진관 (웨딩/돌스냅/증명사진/가족사진/프로필/바디프로필)
- 셀러 본인(자사몰 운영자), 메타 오류(이름이 "전화"/"번호"/"이미지설정" 등이면 이름 미상일 뿐 제작자 자체는 유효 → REVIEW)

[REVIEW] 법인인지 개인인지 정말 불분명한 경우만.

플랫폼: {platform}
이름: {display_name or "(없음)"}
소개: {bio or "(없음)"}
이메일: {email}

JSON만 반환: {{"decision": "keep"/"disqualify"/"review", "reason": "이유"}}"""
        return _run_llm(prompt, platform, display_name, bio, notes, handle, email, category)

    prompt = f"""Acme 파트너스 리드 검수. 이 리드가 우리 타겟인지 판단하세요.
핵심 질문: "이 사람이 자기 청중·수강생·고객 중 자사몰 운영하는 사장님께 간편가입 SaaS을 소개해줄 만한 개인인가?"

[KEEP — 발송 OK]
- 쇼핑몰/자사몰 셀러를 청중·고객으로 가진 개인 (이커머스 유튜버·뉴스레터·강사, 카페24/아임웹 디자이너, 마케팅 컨설턴트)
- 이커머스 분야 1인사업가/프리랜서
- **비즈니스 인접 개인 크리에이터/강사 (청중 보유) — 돈 버는 법을 가르치는 사람은 수강생 중 셀러가 있을 가능성 높음**:
  마케팅, 창업, 부업, N잡, 1인사업, 디자인, 브랜딩, 콘텐츠/블로그/유튜브 운영, SNS 수익화, 스마트스토어, 광고, 카피, 퍼스널브랜딩

[DISQUALIFY — 발송 X]
- 기업 공식 계정/뉴스레터, 공공기관/협회/재단/진흥원
- **명백히 비즈니스 무관 주제 강사**: 명상, 드로잉/미술, 음악/반주, 요가/운동, 어학(영어·전기 등 자격증), 의료/건강, 종교, 부동산 실거주, 여행, 입시/학원
- 세무/법무, 음식점
- 메타 오류: 이름이 "Instagram", "Email" 등
- 셀러 본인 (자사몰 운영자)
- 이커머스·비즈니스 무관 SaaS/AI 개발자

[REVIEW] 회사인지 1인인지 정말 불분명한 경우만. 개인 + 비즈니스/돈벌이 주제면 REVIEW 말고 KEEP.

플랫폼: {platform}
이름: {display_name or "(없음)"}
소개: {bio or "(없음)"}
이메일: {email}

JSON만 반환: {{"decision": "keep"/"disqualify"/"review", "reason": "이유"}}"""

    return _run_llm(prompt, platform, display_name, bio, notes, handle, email, category)


def _run_llm(prompt, platform, display_name, bio, notes, handle, email, category):
    """claude CLI 호출 → (decision, reason). 실패 시 규칙 fallback."""
    try:
        # 프로젝트 컨텍스트 없이 호출(132k→24k 토큰/건) — 사용량 한도로 ~270건마다 중단되던 원인 제거
        from claude_cli import claude_lean
        result = claude_lean(prompt, "claude-haiku-4-5-20251001", timeout=60)
        text = result.stdout.strip()
        json_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group())
            decision = parsed.get("decision", "review")
            reason = str(parsed.get("reason", "llm"))[:80]
            if decision not in ("keep", "disqualify", "review"):
                decision = "review"
            return decision, reason
        # ⚠️ 파싱 실패를 review로 삼키면 안 된다. 2026-09-02 실측: claude CLI가 빈 출력을 내는
        # 상황(메인세션 중첩·cron keychain 미해제)에서 이 경로가 리드를 통째로 review로 격리했고,
        # review 1,775건 중 1,529건(86%)이 llm_parse_error였다. 검증채널 100건이 여기 묻혀 발송이 끊겼다.
        # → 판정 불가는 판정이 아니다. 호출부가 감지해서 런을 중단할 수 있게 별도 신호로 올린다.
        # 실패 원인을 남긴다(2026-10-01: ~210~270건마다 10연속 실패하는데 원인 문구가 없어 진단 불가였음)
        try:
            with open(os.path.expanduser("~/lead-pipeline/data/llm_fail.log"), "a", encoding="utf-8") as fp:
                fp.write(f"{datetime.now().isoformat(timespec='seconds')} rc={result.returncode} "
                         f"out={text[:200]!r} err={(result.stderr or '')[:300]!r}\n")
        except Exception:
            pass
        return "__llm_error__", f"llm_parse_error(empty_or_unparseable:{len(text)}b)"
    except Exception as e:
        return _judge_fallback(platform, display_name, bio, notes, handle, email, category, str(e))


def _judge_fallback(platform, display_name, bio, notes, handle, email, category, error_info=""):
    """API 실패 시 규칙 기반 fallback."""
    combined = f"{display_name} {bio} {notes} {category}".lower()
    dn_lower = display_name.lower()
    bio_lower = bio.lower()
    email_lower = email.lower()

    # ─── disquiet 플랫폼 특별 규칙 ────────────────────────────────────────────
    if platform == "disquiet":
        # disquiet = IT/SaaS/스타트업 플랫폼
        # 우리 타겟: 이커머스 셀러 대상 크리에이터/전문가
        # 대부분 SaaS 개발자, AI 빌더, 스타트업 창업자 → 이커머스 무관

        # 이커머스 관련 키워드
        ecommerce_keywords = [
            "이커머스", "쇼핑몰", "온라인 셀러", "셀러", "카페24", "아임웹",
            "스마트스토어", "쇼핑", "d2c", "물류", "풀필먼트", "상세페이지",
            "마케팅 대행", "퍼포먼스 마케팅", "광고 대행"
        ]
        is_ecommerce = any(kw in combined for kw in ecommerce_keywords)

        # 명확 NG: 이커머스 무관 SaaS/AI/스타트업
        ng_disquiet = [
            "회계법인", "세무", "법무", "법인", "변호사", "법률",
            "igaming", "채용", "hr", "hrd", "hrd 지원",
            "accelerat", "엑셀러레이팅", "대학생", "고등학생",
            "금융", "주식", "코인", "블록체인", "nft",
            "헬스케어", "의료", "병원",
            "교육", "학습",
        ]

        # 회계법인 등 명시 NG
        for kw in ng_disquiet:
            if kw in combined:
                return "disqualify", f"disquiet_ng:{kw}"

        # 이커머스 직접 연관 없고
        # SaaS/AI 개발자 패턴이면 disqualify
        saas_patterns = [
            "saas", "saasbuild", "ai builder", "ai 빌더", "ai 할루", "ai 에이전트",
            "앱 개발", "웹 개발", "서버 개발", "프론트엔드", "백엔드", "개발자",
            "cto", "cpo", "pm ", "product manager", "product designer",
            "스타트업 창업", "예비창업", "창업가", "연쇄창업",
            "saas 빌딩", "마이크로 saas", "1인 메이커", "인디 메이커",
            "vibe cod", "바이브 코더",
            "미국 법인", "글로벌 saas",
            "독립 뮤지션", "뮤직",
            "데이터 분석가", "데이터분석",
            "ux/ui", "product design",
            "외주개발", "웹 에이전시",
        ]
        if not is_ecommerce:
            for kw in saas_patterns:
                if kw in combined:
                    return "disqualify", f"disquiet_saas:{kw}"

        # bio 없거나 매우 짧고, 이커머스 무관이면 review
        if not is_ecommerce and (not bio.strip() or len(bio.strip()) < 5):
            # handle/display_name으로 추가 판단
            if any(kw in dn_lower for kw in ["이커머스", "쇼핑", "셀러", "마케팅"]):
                return "keep", "disquiet_ecommerce_name"
            return "review", "disquiet_no_bio_no_ecommerce"

        if is_ecommerce:
            # ecommerce 연관 있어도 회사/단체 패턴 확인
            if any(kw in combined for kw in ["법인", "주식회사", "회계", "기관", "공기관"]):
                return "review", "disquiet_ecommerce_but_company"
            return "keep", "disquiet_ecommerce"

        # 이커머스 없고, 뚜렷한 NG도 없으면 review
        return "review", "disquiet_ambiguous"

    # ─── iboss 플랫폼 ────────────────────────────────────────────────────────
    if platform == "iboss":
        # iboss = 마케터/사업자 커뮤니티
        # 회사 공식 계정은 NG, 개인 마케터/프리랜서는 KEEP

        # 명확 회사 계정
        company_names_explicit = [
            "채널톡", "datarize", "searchos", "그루컴퍼니",
        ]
        for cn in company_names_explicit:
            if cn in dn_lower:
                return "disqualify", f"iboss_company:{cn}"

        # notes에 company: 패턴 + 역할이 직원이면 review
        if "company:" in notes.lower():
            # 대표/CEO 역할이면 keep 후보
            if any(kw in notes.lower() for kw in ["대표", "ceo", "대표경력"]):
                # 프리랜서/1인 사업자 판단
                return "keep", "iboss_1인대표"
            # 직원 역할이면 disqualify
            role_match = re.search(r"role:(.{1,30})", notes, re.IGNORECASE)
            if role_match:
                role = role_match.group(1).lower()
                if any(kw in role for kw in ["마케팅", "ae", "기획", "디자인"]):
                    return "review", "iboss_employee_marketer"
            return "review", "iboss_company_member"

        # bio가 없거나 자기소개 없는 경우
        if not bio.strip() or "자기소개가 없습니다" in bio:
            if "kakao:" in notes or "company:" in notes:
                return "review", "iboss_no_bio_with_company"
            return "review", "iboss_no_bio"

        # 이커머스/마케팅 키워드
        ec_kw = ["마케팅", "이커머스", "쇼핑몰", "셀러", "광고", "퍼포먼스", "콘텐츠"]
        if any(kw in combined for kw in ec_kw):
            return "keep", "iboss_ecommerce_marketer"

        return "review", "iboss_ambiguous"

    # ─── naver_blog ──────────────────────────────────────────────────────────
    if platform == "naver_blog":
        # notes에 llm_ng 이미 있으면 현재 pending인데 이전 검수 통과된 것
        # 재검수

        # 이미 이전 검수에서 ng 표시된 경우
        if "llm_ng3:job_category" in notes or "llm_ng3:일반_직업단어" in notes:
            # 아임웹/카페24 전문가 bio가 있으면 보호
            if any(kw in combined for kw in ["아임웹", "카페24", "쇼핑몰", "이커머스", "스마트스토어"]):
                return "keep", "naver_blog_imweb_cafe24_expert"
            return "disqualify", "naver_blog_prev_ng_confirmed"

        # 명확 회사 계정
        if any(kw in dn_lower for kw in ["공식", "업체", "기업", "법인", "(주)", "주식회사"]):
            # 단, 아임웹/카페24 전문가라면 개인 에이전시로 보호
            if any(kw in combined for kw in ["아임웹 전문가", "카페24 전문가", "imweb 전문가"]):
                return "keep", "naver_blog_official_but_imweb_expert"
            return "disqualify", "naver_blog_company_name"

        # 이커머스/쇼핑몰 제작 전문가
        if any(kw in combined for kw in [
            "아임웹", "카페24", "imweb", "쇼핑몰 제작", "쇼핑몰 구축",
            "홈페이지 제작", "웹사이트 제작", "온라인 마케팅", "퍼포먼스 마케팅",
            "상세페이지", "이커머스 컨설팅", "스마트스토어"
        ]):
            return "keep", "naver_blog_ecommerce_expert"

        # 이커머스 무관 명시 패턴
        if any(kw in combined for kw in [
            "세무", "세금", "회계", "법무", "부동산", "의원", "병원",
            "학원", "공무원", "요가", "필라테스", "맛집", "음식"
        ]):
            return "disqualify", "naver_blog_irrelevant"

        # NG notes 패턴
        if "NG:공식 홈페이지" in notes:
            return "disqualify", "naver_blog_official_homepage"

        # 마케팅 컨설팅/이커머스 관련
        source_hashtag = combined
        if any(kw in source_hashtag for kw in [
            "이커머스 구축", "이커머스 마케팅", "쇼핑몰 마케팅",
            "아임웹 전문가", "카페24 전문가"
        ]):
            return "keep", "naver_blog_ecommerce_search"

        return "review", "naver_blog_ambiguous"

    # ─── youtube ─────────────────────────────────────────────────────────────
    if platform == "youtube":
        # 자사몰 운영자(셀러 본인)는 CX팀 컨택 → NG
        if any(kw in combined for kw in ["자사몰 운영", "쇼핑몰 운영", "셀러 본인"]):
            if "강의" not in combined and "컨설팅" not in combined and "대행" not in combined:
                return "disqualify", "youtube_self_seller"

        # 명확 NG: 방송사/공공기관
        if any(kw in dn_lower for kw in ["ytn", "tbc", "뉴스", "중진공", "공단", "kbs", "mbc", "sbs"]):
            return "disqualify", "youtube_broadcast_public"

        # 이커머스/마케팅 관련 채널
        ec_yt = [
            "셀러", "이커머스", "쇼핑몰", "마케팅", "창업", "온라인 비즈",
            "스마트스토어", "구매대행", "상세페이지", "d2c", "풀필",
            "네이버 플레이스", "광고", "퍼포먼스", "콘텐츠 마케팅"
        ]
        if any(kw in combined for kw in ec_yt):
            # 단, 자사몰 운영(셀러 본인)은 제외
            return "keep", "youtube_ecommerce_channel"

        # 이메일 패턴
        if email_lower:
            # 공공기관
            if email_lower.endswith(".or.kr") or "kosmes" in email_lower:
                return "disqualify", "youtube_public_email"
            # 방송사
            if any(kw in email_lower for kw in ["ytn.co.kr", "tbc.co.kr"]):
                return "disqualify", "youtube_broadcast_email"

        # 코딩/개발 채널
        if any(kw in combined for kw in ["코딩", "개발", "프로그래밍", "ai lab", "수익화"]):
            # 이커머스 무관이면 review
            return "review", "youtube_coding_not_ecommerce"

        return "review", "youtube_ambiguous"

    # ─── coloso ──────────────────────────────────────────────────────────────
    if platform == "coloso":
        # coloso creator: 강의 분야로 판단
        coloso_creator = ""
        creator_match = re.search(r"coloso creator: ([^\s|]+)", notes)
        if creator_match:
            coloso_creator = creator_match.group(1).lower()

        # 이커머스 관련 강의 분야
        ec_coloso = ["ecommerce", "marketing", "branding", "product", "business", "branddesign"]
        if any(kw in coloso_creator for kw in ec_coloso):
            return "keep", f"coloso_ecommerce:{coloso_creator}"

        # 완전히 무관한 분야
        non_ec_coloso = [
            "illustration", "2d", "3d", "pastry", "bakery", "cooking",
            "music", "dance", "yoga", "photography", "film", "animation"
        ]
        if any(kw in coloso_creator for kw in non_ec_coloso):
            return "disqualify", f"coloso_irrelevant:{coloso_creator}"

        # display_name이나 notes에 이커머스 관련
        if any(kw in combined for kw in ["마케팅", "브랜딩", "이커머스", "쇼핑"]):
            return "keep", "coloso_ecommerce_name"

        return "review", f"coloso_ambiguous:{coloso_creator}"

    # ─── 기타 플랫폼 ─────────────────────────────────────────────────────────
    # 이커머스 관련 키워드 있으면 keep
    ec_general = [
        "이커머스", "쇼핑몰", "셀러", "카페24", "아임웹", "스마트스토어",
        "퍼포먼스 마케팅", "마케팅 대행", "상세페이지", "온라인 마케팅",
        "구매대행", "물류", "풀필먼트"
    ]
    if any(kw in combined for kw in ec_general):
        return "keep", "general_ecommerce"

    return "review", "general_ambiguous"


def process_leads():
    raw, ng_data = load_data()
    leads = raw["leads"]
    all_ng, false_positive_safe, ng_platforms, ng_bio_patterns = build_keyword_sets(ng_data)
    persona_hard_ng = persona_hard_keywords(ng_data)

    # 이미 LLM 검수 통과(llm_checked)한 건 재검수하지 않는다(2026-10-01: 매번 앞쪽 ~270건을 재검수하다 사용량 한도에 걸려 뒤쪽이 영영 안 됐다)
    pending = [l for l in leads if l.get("outreach_status") == "pending" and not l.get("llm_checked")]
    print(f"[lead-validator] 검수 대상: {len(pending)}건")

    stats = {"keep": 0, "disqualify": 0, "review": 0, "llm_error": 0}
    disqualify_reasons = {}
    llm_errors = 0        # 연속 LLM 판정 실패 카운터
    aborted = False
    LLM_ERROR_ABORT_AFTER = 10   # 연속 10건 실패면 환경 문제로 보고 중단

    for lead in pending:
        lid = lead.get("handle") or lead.get("email") or "unknown"

        # 1차: 키워드 필터
        is_ng, kw_reason = keyword_check(
            lead, all_ng, false_positive_safe, ng_platforms, ng_bio_patterns, persona_hard_ng
        )
        if is_ng:
            lead["outreach_status"] = "disqualified"
            notes = lead.get("notes") or ""
            lead["notes"] = f"{notes} | llm_ng:{kw_reason}".strip(" |")
            stats["disqualify"] += 1
            cat = kw_reason.split(":")[0]
            disqualify_reasons[cat] = disqualify_reasons.get(cat, 0) + 1
            continue

        # 2차: LLM 판단
        platform = lead.get("platform", "") or ""
        display_name = lead.get("display_name", "") or ""
        bio = lead.get("bio", "") or ""
        notes = lead.get("notes", "") or ""
        handle = lead.get("handle", "") or ""
        email = lead.get("email", "") or ""
        category = lead.get("business_category", "") or ""

        decision, reason = judge_single(platform, display_name, bio, notes, handle, email, category,
                                        persona=is_persona(lead))

        # 판정 불가(LLM 호출·파싱 실패) → status 유지(pending)하고 카운트만.
        # 초반부터 대량 실패면 환경이 깨진 것이므로 리드를 오염시키기 전에 중단한다.
        if decision == "__llm_error__":
            llm_errors += 1
            stats["llm_error"] = llm_errors
            if llm_errors >= LLM_ERROR_ABORT_AFTER:
                print(f"\n🛑 LLM 판정 실패 {llm_errors}건 연속/누적 — 환경이 깨진 것으로 판단해 중단합니다.",
                      file=sys.stderr)
                print("   (리드 status는 pending 그대로 두어 오염시키지 않았습니다)", file=sys.stderr)
                print("   원인 후보: claude CLI 메인세션 중첩 / cron keychain 미해제 / 모델 접근 실패", file=sys.stderr)
                print("   → Agent(lead-validator)로 검수하거나 환경 복구 후 재실행하세요.", file=sys.stderr)
                aborted = True
                break
            continue
        llm_errors = 0  # 성공 1건 나오면 연속 카운터 리셋

        if decision == "disqualify":
            lead["outreach_status"] = "disqualified"
            existing_notes = lead.get("notes") or ""
            lead["notes"] = f"{existing_notes} | llm_ng:{reason}".strip(" |")
            stats["disqualify"] += 1
            cat = reason.split(":")[0]
            disqualify_reasons[cat] = disqualify_reasons.get(cat, 0) + 1
        elif decision == "review":
            lead["outreach_status"] = "review"
            existing_notes = lead.get("notes") or ""
            lead["notes"] = f"{existing_notes} | llm_review:{reason}".strip(" |")
            stats["review"] += 1
        else:
            # keep — status 유지 (pending 그대로) + 검수 통과 표시. finalize는 이 표시가 있어야 qualify한다
            # (2026-10-01: 표시가 없어 '검수 통과'와 '검수 안 거침'을 구분 못 하던 구멍).
            lead["llm_checked"] = datetime.now().date().isoformat()
            stats["keep"] += 1

    # 저장
    raw["generated_at"] = datetime.utcnow().isoformat() + "Z"
    raw["lead_count"] = len(leads)
    with open(LEADS_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    print(f"\n[lead-validator LLM 검수 완료]")
    print(f"검수 대상: {sum(stats.values())}건")
    print(f"keep (pending 유지): {stats['keep']}건")
    print(f"disqualify: {stats['disqualify']}건")
    print(f"review: {stats['review']}건")
    if stats.get("llm_error"):
        print(f"⚠️ LLM 판정 실패(status 미변경): {stats['llm_error']}건")
    if aborted:
        print("🛑 중단됨 — 남은 리드는 pending 유지. 환경 복구 후 재실행 필요.")
    print(f"\n[disqualify 사유 분포]")
    for reason, count in sorted(disqualify_reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count}건")

    return aborted


if __name__ == "__main__":
    if process_leads():
        sys.exit(1)   # LLM 판정 불가로 중단 → cron/파이프라인이 실패를 알아채도록 비정상 종료
