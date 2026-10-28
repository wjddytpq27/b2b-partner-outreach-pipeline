#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""
email_templates.py
==================
HTML 메일 카드 레이아웃 (공개용 축약본: 캠페인별 고정 템플릿은 제외).
이메일 클라이언트 호환 위해 inline 스타일 + 600px 컨테이너 + 시스템 폰트.
send_from_sheet.py --html-template body 에서 사용.
"""
from __future__ import annotations
from html import escape


BLUE = "#2C6BED"
BLUE_D = "#1740A6"
INK = "#1C2A44"
SUB = "#5A6B82"
BG = "#F4F7FC"
BLUE_BG = "#EAF1FF"
GOLD_BG = "#FFF6E5"
GOLD = "#B5731A"
FONT = "'Apple SD Gothic Neo','Malgun Gothic',-apple-system,sans-serif"


def _wrap(inner: str) -> str:
    # 전체를 center 테이블로 감싸 Outlook(max-width 무시)에서도 600px 중앙정렬.
    # 헤더는 background-color(solid) 먼저 → Outlook은 단색, 모던 클라이언트는 gradient.
    return f"""<!-- email --><div style="margin:0;padding:24px 0;background:{BG};font-family:{FONT};">
<table role="presentation" align="center" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #E3E9F2;border-radius:14px;">
  <tr><td style="background-color:{BLUE};background:linear-gradient(135deg,{BLUE},{BLUE_D});padding:22px 28px;border-radius:14px 14px 0 0;">
    <div style="color:#ffffff;font-size:13px;font-weight:700;letter-spacing:.04em;">Acme 파트너스</div>
  </td></tr>
  <tr><td style="padding:28px;color:{INK};font-size:15px;line-height:1.7;">
    {inner}
  </td></tr>
  <tr><td style="padding:18px 28px;background:{BG};color:{SUB};font-size:12px;line-height:1.6;border-top:1px solid #E3E9F2;border-radius:0 0 14px 14px;">
    Acme 파트너스 · 담당자 매니저<br>문의는 이 메일에 그대로 회신 주세요 · partners@example.com
  </td></tr>
</table></div>"""


def _p(t: str) -> str:
    return f'<p style="margin:0 0 14px;">{t}</p>'


def render_body(body: str) -> str:
    """시트 본문(plain) 그대로를 카드 레이아웃 HTML로. 회차마다 내용이 바뀌는 캠페인(매주 한 편·빌더 월간)용.
    고정 템플릿을 쓰면 Gmail이 HTML만 보여줘서 시트 본문(주제·SEO 안내)이 통째로 안 보인다(2026-10-02 사고)."""
    import re as _re
    paras = [b.strip() for b in body.replace("\r", "").split("\n\n") if b.strip()]
    def fmt(t: str) -> str:
        t = escape(t)
        t = _re.sub(r"(https?://[^\s<]+)", rf'<a href="\1" style="color:{BLUE};word-break:break-all;">\1</a>', t)
        return t.replace("\n", "<br>")
    return _wrap("".join(_p(fmt(x)) for x in paras))


RENDERERS = {
    "body": None,  # send_from_sheet가 행 본문으로 render_body 호출
}
