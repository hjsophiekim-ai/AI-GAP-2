#!/usr/bin/env bash
# Render Web Service Start Command 래퍼 (2026-10-15).
#
#   bash scripts/render_start_with_logger.sh
#
# 기존 Start Command(docs/deploy_render.md):
#   streamlit run app/ui/streamlit_app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true
# 를 **그대로** foreground 로 실행하고, KIS_LOGGER_ENABLED=1 일 때만 ws_shadow_logger 를 별도 감독 프로세스로
# 붙인다.
#
#   · KIS_LOGGER_ENABLED!=1 : 로거를 시작하지 않고 위 명령으로 exec 한다 (기존 production 과 동일 프로세스 구조).
#   · 로거 장애는 UI 를 죽이지 않는다. 로거가 비정상 종료하면 백오프 후 재시작하고, 종료코드
#     0(정상/비활성) 2(키) 3(디스크) 4(RSS 한도)이면 재시작하지 않는다. UI 는 계속 돈다.
#   · UI(web) 가 죽으면 로거를 정리하고 web 의 종료코드로 끝난다 -> Render 가 서비스를 재시작한다.
#   · SIGTERM/SIGINT 를 받으면 web 과 로거에 SIGTERM 을 전달하고 둘 다 끝날 때까지 기다린다.
#
# 인자를 주면 기본 web 명령 대신 그 명령을 쓴다(테스트용). WS_LOGGER_CMD 로 로거 명령을 바꿀 수 있다(테스트용).
set -u

cd "$(dirname "$0")/.." || exit 1

if [ "$#" -gt 0 ]; then
  WEB_CMD=("$@")
else
  WEB_CMD=(streamlit run app/ui/streamlit_app.py --server.address 0.0.0.0 --server.port "${PORT:-10000}" --server.headless true)
fi
LOGGER_CMD="${WS_LOGGER_CMD:-python -m ws_shadow_logger.runner --live}"
MAX_RESTARTS="${WS_LOGGER_MAX_RESTARTS:-20}"
STOP_GRACE_SEC="${WS_LOGGER_STOP_GRACE_SEC:-15}"
RESTART_DELAY="${WS_LOGGER_RESTART_DELAY:-5}"

log() { echo "[render_start] $(date '+%Y-%m-%dT%H:%M:%S%z') $*"; }

if [ "${KIS_LOGGER_ENABLED:-0}" != "1" ]; then
  log "KIS_LOGGER_ENABLED!=1 — ws_shadow_logger not started; exec web command unchanged"
  exec "${WEB_CMD[@]}"
fi

NICE=""
if command -v nice >/dev/null 2>&1; then NICE="nice -n 10"; fi

logger_supervisor() {
  local child="" rc restarts=0 delay=$RESTART_DELAY started
  trap 'if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null; wait "$child" 2>/dev/null; fi; log "logger supervisor stopped"; exit 0' TERM INT
  while :; do
    started=$SECONDS
    # shellcheck disable=SC2086
    $NICE $LOGGER_CMD &
    child=$!
    log "ws_shadow_logger started pid=$child"
    wait "$child"
    rc=$?
    child=""
    case "$rc" in
      0|2|3|4)
        log "ws_shadow_logger exited rc=$rc (0=ok/disabled 2=key 3=disk 4=rss) — not restarting; UI keeps running"
        return 0 ;;
    esac
    if [ $((SECONDS - started)) -ge 600 ]; then restarts=0; delay=$RESTART_DELAY; fi
    restarts=$((restarts + 1))
    if [ "$restarts" -gt "$MAX_RESTARTS" ]; then
      log "ws_shadow_logger DIED rc=$rc — restart limit ($MAX_RESTARTS) reached, giving up; UI keeps running"
      return 0
    fi
    log "ws_shadow_logger DIED rc=$rc — restart #$restarts in ${delay}s; UI keeps running"
    sleep "$delay" &
    wait $!
    delay=$(( delay * 2 > 300 ? 300 : delay * 2 ))
  done
}

logger_supervisor &
SUP_PID=$!

"${WEB_CMD[@]}" &
WEB_PID=$!
log "web started pid=$WEB_PID; logger supervisor pid=$SUP_PID"

SHUTTING_DOWN=0
shutdown() {
  SHUTTING_DOWN=1
  log "received $1 — stopping web and logger"
  kill -TERM "$WEB_PID" 2>/dev/null
  kill -TERM "$SUP_PID" 2>/dev/null
}
trap 'shutdown SIGTERM' TERM
trap 'shutdown SIGINT' INT

WEB_RC=0
while :; do
  wait "$WEB_PID"
  WEB_RC=$?
  kill -0 "$WEB_PID" 2>/dev/null || break
done
[ "$SHUTTING_DOWN" = 1 ] || log "web exited rc=$WEB_RC — stopping logger so Render can restart the service"

kill -TERM "$SUP_PID" 2>/dev/null
for _ in $(seq 1 "$STOP_GRACE_SEC"); do
  kill -0 "$SUP_PID" 2>/dev/null || break
  sleep 1
done
kill -KILL "$SUP_PID" 2>/dev/null
wait "$SUP_PID" 2>/dev/null
log "exit rc=$WEB_RC"
exit "$WEB_RC"
