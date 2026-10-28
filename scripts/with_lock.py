#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""leads_raw 동시쓰기 방지 글로벌 락 래퍼 (2026-08-27).

배경: 스크래퍼마다 leads_raw.json을 각자 read-modify-write 한다(공통 저장함수 없음).
두 스크래퍼가 동시에 돌면 나중 저장이 먼저 저장을 덮어써 리드가 유실된다
(2026-08-26 openads 123건 유실 사고 — autofill 스크래퍼 vs 별도 channel-scout 스크래퍼).

동작: 자물쇠(flock)를 잡은 뒤 대상 스크립트를 서브프로세스로 그대로 실행한다.
  - 스크래퍼 원본은 수정 불필요 — 여기서 대신 락을 잡아준다.
  - 동시에 여러 개가 붙으면 순서대로(블로킹) 실행 → 절대 겹쳐 쓰지 않음.
  - 대상이 죽거나 kill되면 OS가 flock을 자동 해제(스톨 방지).

사용:
  python3 scripts/with_lock.py <대상스크립트.py> [인자...]
  # 예: python3 scripts/with_lock.py scripts/openads_scraper.py --max 200

⚠️ 모든 스크래퍼는 이 래퍼를 거쳐 실행할 것(autofill은 자동 적용). 직접 실행하면 락을 안 잡아 사고 재발 가능.
"""
import fcntl
import os
import subprocess
import sys

LOCKFILE = "/tmp/acme_leads_write.lock"


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: with_lock.py <script.py> [args...]")
    target = sys.argv[1]
    rest = sys.argv[2:]
    with open(LOCKFILE, "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)  # 블로킹: 다른 스크래퍼가 끝날 때까지 대기
        try:
            # 자식이 leads_store.commit_leads로 저장해도 같은 락을 다시 잡지 않게(교착 방지)
            env = {**os.environ, "KEEPGROW_LEADS_LOCK_HELD": "1"}
            proc = subprocess.run([sys.executable, target, *rest], env=env)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
