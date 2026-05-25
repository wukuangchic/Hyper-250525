cd "$HYPER_ORDER_DIR" || exit 1
. ./aliases

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
echo "Examples:"
echo "  query"
echo "  BTC buy --dry-run"
echo "  GOLD buy"
echo "  GOLD --cancel"
echo "  order BTC buy   # still works too"
echo ""

PROMPT='hyper-order %1~ %# '
