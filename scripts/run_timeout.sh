#!/usr/bin/env bash
# run_timeout.sh SECONDS cmd args...
# cmd를 실행하되 SECONDS 초 넘으면 강제 종료(hang 방지). macOS엔 gtimeout이 없어 자체 구현.
# daily_pipeline/autofill에서 스크래퍼 한 개가 멈춰도 전체가 안 서게 감싸는 용도.
secs="$1"; shift
"$@" &
pid=$!
( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) &
watcher=$!
wait "$pid" 2>/dev/null
rc=$?
kill "$watcher" 2>/dev/null
wait "$watcher" 2>/dev/null
if [ "$rc" -ge 128 ]; then
  echo "[run_timeout] '$*' ${secs}s 초과 → 강제 종료(skip)" >&2
fi
exit 0
