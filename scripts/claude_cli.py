#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""파이프라인용 가벼운 claude -p 호출 (2026-10-01).

프로젝트 폴더에서 claude -p를 부르면 매 호출마다 CLAUDE.md·메모리·전역 rules·플러그인 설정을 통째로 싣는다.
실측(haiku 1건): 프로젝트 cwd 132k 토큰·15초·$0.237 → /tmp + --setting-sources "" 24k 토큰·7초·$0.018 (13배).
검수·개인화처럼 프롬프트가 자기완결인 호출은 이걸 쓴다. (9/30~10/1 검수 중단의 진짜 원인은 토큰이 아니라
OAuth 세션 만료·갱신 실패였다 → _automation_env 참고)
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLAUDE_BIN = "/opt/homebrew/bin/claude"
LEAN_CWD = "/tmp"


ENV_FILE = Path("~/lead-pipeline/.env").expanduser()


def _automation_env() -> dict:
    """자동화 전용 장기 토큰(claude setup-token → .env CLAUDE_CODE_OAUTH_TOKEN)이 있으면 그걸로 인증.
    2026-10-01 실측: 데스크탑 앱과 같은 OAuth 세션을 공유하다 'OAuth session expired and could not be
    refreshed'로 1시간쯤마다 전 호출 실패 → 검수 중단의 진짜 원인. 토큰 없으면 기존 로그인 사용."""
    env = dict(os.environ)
    # 다른 Claude Code 세션 안에서 실행돼도 '중첩 세션'으로 막히지 않게(cron엔 원래 없음)
    for k in [k for k in env if k.startswith("CLAUDECODE") or k.startswith("CLAUDE_CODE_")]:
        env.pop(k, None)
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if line.startswith("CLAUDE_CODE_OAUTH_TOKEN=") and line.split("=", 1)[1].strip():
                tok = line.split("=", 1)[1].strip()
                # 터미널에서 복사하면 줄바꿈이 끼어 두 줄로 쪼개진다(10/2 두 번 재발: 79+30자).
                # 다음 줄이 '='·공백 없는 토큰 조각이면 이어 붙인다.
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if nxt and "=" not in nxt and " " not in nxt and not nxt.startswith("#"):
                    tok += nxt
                env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    except Exception:
        pass
    return env


def has_automation_token() -> bool:
    return "CLAUDE_CODE_OAUTH_TOKEN" in _automation_env()


def claude_lean(prompt: str, model: str, system_file: str | Path | None = None,
                timeout: int = 120) -> subprocess.CompletedProcess:
    """프로젝트 컨텍스트 없이 claude -p 실행. system_file(예: 에이전트 .md)은 시스템 프롬프트에 덧붙인다."""
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", model, "--setting-sources", ""]
    if system_file:
        cmd += ["--append-system-prompt-file", str(system_file)]
    return subprocess.run(cmd, cwd=LEAN_CWD, capture_output=True, text=True, timeout=timeout,
                          env=_automation_env())
