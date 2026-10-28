#!/usr/bin/env bash
# Acme 일일 리스트업 파이프라인 (매일 15:00 cron)
# ⚠️ leads_raw.json 동시 쓰기 = 데이터 손실. commit_leads(leads_store.py)로 저장하는 스카우트만
#    run_scout_par로 병렬, 나머지(통째 덮어쓰기·기존 리드 수정)는 run_scout(with_lock) 순차.
set -u

ROOT="$HOME/lead-pipeline"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
LOG="$ROOT/data/daily_pipeline.log"

cd "$ROOT" || exit 1

# keychain unlock — cron에서 claude/gspread 인증 접근용 (비번 파일 있을 때만)
[ -f "$ROOT/data/.kc_pw" ] && security unlock-keychain -p "$(cat "$ROOT/data/.kc_pw")" ~/Library/Keychains/login.keychain-db 2>/dev/null

# leads_raw 동시쓰기 방지 — autofill과 공통 lock 배타 (race 방지)
LOCKF="$ROOT/data/autofill.lock"
if [ -f "$LOCKF" ] && [ $(( $(date +%s) - $(stat -f %m "$LOCKF") )) -lt 14400 ]; then
  echo "[$(date '+%m-%d %H:%M')] autofill/파이프라인 lock 존재 — daily_pipeline 스킵" >> "$LOG"
  exit 0
fi
echo $$ > "$LOCKF"
trap 'rm -f "$LOCKF"' EXIT

echo "" >> "$LOG"
echo "════════════════════════════════════════════" >> "$LOG"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') 파이프라인 시작 ===" >> "$LOG"
echo "════════════════════════════════════════════" >> "$LOG"

# 0. 바운스 메일 동기화 (Gmail IMAP → bounce_list.json)
echo "[0/8] 바운스 메일 동기화..." >> "$LOG"
$PYTHON scripts/sync_bounces.py >> "$LOG" 2>&1 || true

# 0a. 리터치/팔로업 탭 바운스 마킹 (GAS 발송기엔 바운스 필터 없음 → 여기서 거름)
echo "[0a/8] 리터치/팔로업 바운스 마킹..." >> "$LOG"
$PYTHON scripts/mark_retouch_bounces.py >> "$LOG" 2>&1 || true

# 0b. 대시보드 가입자 sync
echo "[0b/8] 대시보드 가입자 sync..." >> "$LOG"
$PYTHON scripts/sync_acme_dashboard.py >> "$LOG" 2>&1 || true

# ── 동결 채널 스킵 러너 (2026-09-02) ────────────────────────────────────────
# 왜: 55개 스크래퍼를 매일 돌렸는데 그중 대부분이 "역대 파트너 가입 0" 채널이었다.
#     발송 대부분이 그 계열로 채워져 모집이 정지했고, 시간·토큰도 같이 태웠다.
# 어떻게: channel_freeze.json(정책 동결 + 통계 동결: 발송300건↑ 가입0)에 있는 채널은 건너뛴다.
#     목록은 pipeline_health.py가 매일 갱신하고, channel_policy.json에서 지우면 즉시 해제된다.
FREEZE_LIST=$($PYTHON - <<'PYEOF_FREEZE'
import json,pathlib
try:
    d=json.loads(pathlib.Path("~/lead-pipeline/data/channel_freeze.json").expanduser().read_text(encoding="utf-8"))
    print(" ".join(sorted(d.get("frozen",{}))))
except Exception:
    print("")
PYEOF_FREEZE
)
echo "[freeze] 동결 채널: $(echo $FREEZE_LIST | wc -w | tr -d ' ')개" >> "$LOG"

is_frozen() {   # $1=scripts/xxx_scraper.py → 동결이면 로그 남기고 0
  local base ch
  base=$(basename "$1" .py)
  ch=$(echo "$base" | sed -E 's/_(scraper|instructor_scraper|center_scraper|api_scraper|from_agencies|icp_harvest|v2_scraper)$//')
  for f in $FREEZE_LIST; do
    if [ "$f" = "$ch" ]; then
      echo "  [skip:frozen] $base" >> "$LOG"
      return 0
    fi
  done
  return 1
}

run_scout() {   # $1=scripts/xxx_scraper.py, 나머지=인자 — with_lock 순차(통째 덮어쓰기형)
  local script="$1"; shift
  is_frozen "$script" && return 0
  $PYTHON scripts/with_lock.py "$script" "$@" >> "$LOG" 2>&1 || true
}

# 병렬 러너(2026-09-30): commit_leads로 "저장 몇 초만" 락 잡는 스크래퍼 전용 — with_lock 없이 백그라운드.
# ⚠️ 여기 넣기 전 스크래퍼가 commit_leads로 저장하는지 확인(selftest가 검사). 통째 덮어쓰기형을 넣으면 유실.
# 동시 4개 상한: 대부분 Naver 검색을 쓰는데 한 IP 몰아치기 = 403(rate-limit, naver_fetch.py 참고).
PAR_MAX=3  # 2026-09-30 첫 병렬 실행 — 네이버 검색 HTML 동시요청 403 방지로 3부터. 로그 403 없으면 4로
PAR_LOGDIR="$ROOT/data/scout_par_logs"
mkdir -p "$PAR_LOGDIR"
run_scout_par() {
  local script="$1"; shift
  is_frozen "$script" && return 0
  while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "$PAR_MAX" ]; do sleep 5; done   # bash 3.2라 wait -n 없음
  $PYTHON "$script" "$@" > "$PAR_LOGDIR/$(basename "$script" .py).log" 2>&1 &
}

# 1. 스카우트 순차 실행 (병렬 금지 — leads_raw.json race condition)
echo "[1/8] 스카우트 실행 (병렬 → 순차)..." >> "$LOG"
# 1-a. 병렬: commit_leads 전환 스카우트(검증채널 naver_blog 복구분 포함, 2026-09-09)
run_scout_par scripts/youtube_api_scraper.py
run_scout_par scripts/naver_blog_new_keywords.py --target 200
run_scout_par scripts/coloso_instructor_scraper.py
run_scout_par scripts/liveklass_scraper.py --max 250
run_scout_par scripts/funnelmoa_scraper.py --max 120
run_scout_par scripts/didisam_scraper.py --max 120
run_scout_par scripts/inclass_scraper.py --max 120
run_scout_par scripts/godomall_edu_scraper.py --max 120
run_scout_par scripts/creatorlink_scraper.py --max 120
run_scout_par scripts/branding_studio_scraper.py --max 120
run_scout_par scripts/webagency_scraper.py --commit --max-parse 600
run_scout_par scripts/retouch_studio_scraper.py --max 120
run_scout_par scripts/apparel_print_scraper.py --max 120   # 자체제작 의류/패션 브랜드 셀러에게 프린팅/데코레이션(나염·DTF·전사·커스텀 의류 소량제작) 납품 (fashion 버티컬 원단→봉제→부자재→라벨 다음 "프린팅" 축, 보조, 전상법 footer 개인메일 86.5%, 2026-08-06 발굴)
wait
for f in "$PAR_LOGDIR"/*.log; do [ -f "$f" ] && { echo "── $(basename "$f")"; cat "$f"; } >> "$LOG"; done
rm -f "$PAR_LOGDIR"/*.log
# 1-b. 순차: 통째 덮어쓰기형·기존 리드 수정형(with_lock)
# 동결 채널(data/channel_freeze.json)은 run_scout이 건너뜀. 채널 발굴 근거는 docs/channels.md.
# 공급 버티컬 38개 스크래퍼는 2026-09-30 scripts/_archive_frozen/으로 이동(발송 대비 가입 0, 정책동결).
run_scout scripts/imweb_expert_scraper.py
run_scout scripts/cafe24_designer_center_scraper.py
run_scout scripts/naver_blog_email_enricher.py --max 400
run_scout scripts/kmong_scraper.py --commit --max 150
echo "[1/8] 스카우트 완료" >> "$LOG"

# 2a. lead-validator 1차 키워드 검수
echo "[2a/8] lead-validator 키워드 검수..." >> "$LOG"
$PYTHON scripts/with_lock.py scripts/validate_leads.py >> "$LOG" 2>&1 || true

# 2b. lead-validator LLM 검수
echo "[2b/8] lead-validator LLM 검수..." >> "$LOG"
$PYTHON scripts/with_lock.py scripts/llm_validator_batch.py >> "$LOG" 2>&1 || true

# 3. finalize (qualify → draft → backlog 업로드)
echo "[3/8] finalize_backlog..." >> "$LOG"
$PYTHON scripts/with_lock.py scripts/finalize_backlog.py >> "$LOG" 2>&1 || true
# 3b. 개인화 실패(채널 기본문장) 초안 자동 복구 — claude 인증 살아 있으면 채움, 5연속 실패 시 즉시 중단(2026-10-01)
echo "[3b/8] backlog 재개인화..." >> "$LOG"
$PYTHON scripts/repersonalize_backlog.py --max 150 >> "$LOG" 2>&1 || true

# 4. dedup + sent 이력 자동 반영
echo "[4/8] dedup + sent 마킹..." >> "$LOG"
$PYTHON scripts/dedup_contacted.py --apply >> "$LOG" 2>&1 || true
$PYTHON scripts/dedup_contacted.py --mark-sent >> "$LOG" 2>&1 || true

# 5. 시트 → contacted_list 즉시 sync (발송완료 반영)
echo "[5/8] 시트 sync..." >> "$LOG"
$PYTHON scripts/auto_sync_from_sheets.py >> "$LOG" 2>&1 || true

# 5b. 활성화 발송분 자동 아카이빙 (activation_log 누적)
echo "[5b/8] 활성화 발송 아카이빙..." >> "$LOG"
$PYTHON scripts/archive_activation_sent.py >> "$LOG" 2>&1 || true

# 6. no_email + enrichment_failed → 이메일 발굴 후 pending 복귀
echo "[6/8] 이메일 enrichment (no_email + enrichment_failed)..." >> "$LOG"
bash scripts/run_timeout.sh 900 $PYTHON scripts/enrich_failed_leads.py >> "$LOG" 2>&1 || true  # 15분 타임아웃(2026-08-07 hang 방지)

# 7. 파트너 D+3 활성화 트리거 — 등록 D+3 신규를 세그먼트별 발송 탭에 적재
echo "[7/8] 파트너 D+3 활성화 트리거..." >> "$LOG"
$PYTHON scripts/partner_activation_trigger.py >> "$LOG" 2>&1 || true

echo "=== $(date '+%Y-%m-%d %H:%M:%S') 파이프라인 완료 ===" >> "$LOG"
