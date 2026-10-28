"""
qualify_pending.py
------------------
pending 리드를 자격심사한다.
- blocklist / bounce_list 필터
- 개인 이메일(naver/gmail/daum/hanmail/nate/kakao/hanmir/hotmail) 제외
- 적합도 스코어링
- outreach_status -> qualified / disqualified
contacted 중복은 dedup_contacted.py --apply 로 선처리됨.
"""
import json
import re
from collections import Counter
from pathlib import Path

DATA = Path(__file__).parent.parent / "data"
RAW = DATA / "leads_raw.json"

PERSONAL_DOMAINS = {
    "naver.com", "gmail.com", "daum.net", "hanmail.net", "nate.com",
    "kakao.com", "hanmir.com", "hotmail.com", "icloud.com", "outlook.com",
    "yahoo.com", "googlemail.com",
}


def norm_email(e):
    return (e or "").lower().strip()


def domain(e):
    e = norm_email(e)
    return e.split("@")[-1] if "@" in e else ""


def load_filter_emails():
    block = set()
    for f, key in [("blocklist.json", None), ("bounce_list.json", None)]:
        d = json.loads((DATA / f).read_text())
        items = d if isinstance(d, list) else d.get("leads", d.get("emails", []))
        for it in items:
            if isinstance(it, dict):
                block.add(norm_email(it.get("email", "")))
            elif isinstance(it, str):
                block.add(norm_email(it))
    block.discard("")
    return block


def score_lead(l):
    """스코어 + 사유 리스트 반환."""
    s = 0
    reasons = []
    email = norm_email(l.get("email"))
    dom = domain(email)
    platform = l.get("platform", "")
    bio = (l.get("bio") or "") + " " + (l.get("business_category") or "")
    followers = l.get("followers", 0) or 0

    is_personal = dom in PERSONAL_DOMAINS
    if email and not is_personal:
        s += 30
        reasons.append("+30 업무이메일")
    elif is_personal:
        s -= 20
        reasons.append("-20 개인이메일")

    # 아임웹/카페24/크몽 전문가
    if platform in ("imweb_expert", "cafe24_expert", "cafe24_designer") or \
       any(k in bio for k in ("아임웹", "카페24", "크몽", "쇼핑몰 구축", "쇼핑몰 제작", "웹사이트")):
        s += 20
        reasons.append("+20 쇼핑몰구축전문가")

    # 팔로워 1000+
    if followers >= 1000:
        s += 20
        reasons.append("+20 팔로워1000+")

    # 강사/튜터
    if any(k in bio for k in ("강의", "강사", "클래스", "수강", "튜터", "교육", "원데이")):
        s += 15
        reasons.append("+15 강사/튜터")

    # 블로거/유튜버
    if any(k in bio for k in ("블로그", "유튜브", "유튜버", "블로거", "채널")):
        s += 10
        reasons.append("+10 블로거/유튜버")

    return s, reasons


def main():
    d = json.loads(RAW.read_text())
    leads = d["leads"]
    block = load_filter_emails()

    stats = Counter()
    dq_reasons = Counter()
    qualified_by_platform = Counter()
    scores = []

    for l in leads:
        if l.get("outreach_status") != "pending":
            continue
        stats["reviewed"] += 1
        email = norm_email(l.get("email"))
        dom = domain(email)

        # 1) 이메일 없음
        if not email:
            l["outreach_status"] = "no_email"
            stats["no_email"] += 1
            dq_reasons["이메일없음"] += 1
            continue
        # 2) blocklist/bounce
        if email in block:
            l["outreach_status"] = "disqualified"
            l["dq_reason"] = "blocklist/bounce"
            stats["disqualified"] += 1
            dq_reasons["blocklist/bounce"] += 1
            continue
        # 3) 개인 이메일만 -> disqualified
        # 단, 블로그/유튜브/뉴스레터 채널은 개인 이메일이 정상 — 예외 허용
        PERSONAL_OK_CHANNELS = {"lighting_maker", "rug_maker", "frame_maker", "phonecase_maker", "towel_maker", "curtain_maker", "shoe_maker", "kmong", "webagency", "shopify_partner", "cafe24_app", "titanclass", "maily", "sixshop_seller", "naver_blog", "youtube", "stibee", "maily", "linktree", "eventers",
                                "iboss", "disquiet", "brunch", "coloso",
                                "cafe24_designer", "fastcampus", "class101", "inflearn",
                                "imweb_expert", "taling", "liveklass", "funnelmoa",
                                "didisam", "inclass", "godomall_edu", "builder_studio", "oopy",
                                "sixshop_partner", "creatorlink", "ezcampus",
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
        lead_channel = l.get("channel") or l.get("platform") or ""
        if dom in PERSONAL_DOMAINS and lead_channel not in PERSONAL_OK_CHANNELS:
            l["outreach_status"] = "disqualified"
            l["dq_reason"] = "개인이메일만"
            stats["disqualified"] += 1
            dq_reasons["개인이메일만"] += 1
            continue

        # 통과 -> 스코어링
        score, reasons = score_lead(l)
        l["qual_score"] = score
        l["qual_reasons"] = reasons
        l["outreach_status"] = "qualified"
        stats["qualified"] += 1
        qualified_by_platform[l.get("platform")] += 1
        scores.append((score, l.get("display_name") or l.get("handle"), l.get("platform"), email))

    RAW.write_text(json.dumps(d, ensure_ascii=False, indent=2))

    print("=== 자격심사 결과 ===")
    print(f"검토: {stats['reviewed']}건")
    print(f"통과(qualified): {stats['qualified']}건")
    print(f"제외(disqualified): {stats['disqualified']}건")
    print(f"이메일없음(no_email): {stats['no_email']}건")
    print()
    print("제외 사유:", dict(dq_reasons))
    print("통과 채널 분포:", dict(qualified_by_platform))
    print()
    scores.sort(reverse=True)
    print("스코어 분포:", dict(Counter(s for s, *_ in scores)))
    print("\n상위 10건:")
    for s, name, plat, em in scores[:10]:
        print(f"  {s:>3}  {plat:14} {name}  <{em}>")


if __name__ == "__main__":
    main()
