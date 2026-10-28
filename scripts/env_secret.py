#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""비밀값은 .env 한 곳에서만 읽는다 (2026-10-02: 슬랙 봇 토큰이 스크립트 6개에 하드코딩돼 있던 것 이관).
cron은 TCC 때문에 ~/Desktop을 직접 못 읽는 경우가 있어 실경로(~/lead-pipeline/data/.env)를 본다."""
from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path("~/lead-pipeline/data/.env").expanduser()


def get(key: str, default: str = "") -> str:
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return default
