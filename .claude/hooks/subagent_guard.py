#!/usr/bin/env python3
"""서브에이전트의 발송·배포 차단. 메인 세션(agent_id 없음)은 통과."""
import json, re, sys

RISKY = re.compile(r"vercel\b.*(--prod|\balias\b|\bpromote\b)|\bclasp\s+(push|deploy)\b|\bsend_\w*\.py\b")

d = json.load(sys.stdin)
cmd = d.get("tool_input", {}).get("command", "")
if d.get("agent_id") and RISKY.search(cmd):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "서브에이전트는 발송·배포 금지. 파일 수정까지만 하고 메인 세션에 보고할 것.",
    }}, ensure_ascii=False))
