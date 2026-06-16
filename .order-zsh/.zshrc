cd "$HYPER_ORDER_DIR" || exit 1
. ./aliases

start_trail_worker_loop() {
  if [[ -n "$TRAIL_WORKER_LOOP_PID" ]] && kill -0 "$TRAIL_WORKER_LOOP_PID" 2>/dev/null; then
    return 0
  fi
  local monitor_was_on=0
  [[ -x "./trail-worker-loop" ]] || return 0
  [[ -o monitor ]] && monitor_was_on=1
  setopt no_monitor
  ./trail-worker-loop &
  TRAIL_WORKER_LOOP_PID=$!
  (( monitor_was_on )) && setopt monitor
}

start_trail_worker_loop
trap '[[ -n "$TRAIL_WORKER_LOOP_PID" ]] && kill "$TRAIL_WORKER_LOOP_PID" 2>/dev/null' EXIT HUP INT TERM

command_not_found_handler() {
  local symbol="$1"
  shift

  case "$symbol" in
    -*|"" )
      echo "Unknown command: $symbol"
      return 127
      ;;
  esac

  ./order "$symbol" "$@"
}

echo "Hyper order terminal ready."
echo "Trail worker: every 10s while this terminal stays open."
echo "Examples:"
echo "  query"
echo "  BTC buy --dry-run"
echo "  GOLD buy"
echo "  GOLD --cancel"
echo "  order BTC buy   # still works too"
echo ""

PROMPT='hyper-order %1~ %# '
