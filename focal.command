#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Focal launcher
#  Double-click in Finder  OR  run from terminal:  bash focal.command
#  Options:
#    --no-browser    don't open Chrome automatically
#    --lm            also open LM Studio
# ─────────────────────────────────────────────────────────────────────────────

# Resolve to wherever this script lives, whatever python3 is on PATH
FOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3)"
if [ -z "$PYTHON" ]; then
  echo "python3 not found — install Python 3 first (https://www.python.org)"
  exit 1
fi
PORT=8080
URL="http://localhost:$PORT"
OPEN_BROWSER=true
OPEN_LM=false

for arg in "$@"; do
  case $arg in
    --no-browser) OPEN_BROWSER=false ;;
    --lm)         OPEN_LM=true ;;
  esac
done

# ── Colours ───────────────────────────────────────────────────────────────────
bold='\033[1m'; dim='\033[2m'; green='\033[32m'; yellow='\033[33m'
red='\033[31m'; reset='\033[0m'

echo ""
echo -e "${bold}  Focal${reset}"
echo -e "${dim}  ──────────────────────────────────${reset}"

# ── Kill any stale instance on port ──────────────────────────────────────────
stale=$(lsof -ti tcp:$PORT 2>/dev/null)
if [ -n "$stale" ]; then
  echo -e "  ${yellow}→${reset} Stopping existing process on :$PORT (PID $stale)…"
  kill "$stale" 2>/dev/null
  sleep 0.8
fi

# ── Start launcher ────────────────────────────────────────────────────────────
echo -e "  ${green}→${reset} Starting launcher…"
cd "$FOCAL_DIR" || { echo -e "  ${red}✗ Could not cd to $FOCAL_DIR${reset}"; exit 1; }
"$PYTHON" focal_launcher.py &
LAUNCHER_PID=$!

# ── Wait until the server responds (max 10 s) ─────────────────────────────────
echo -n "    Waiting for server"
for i in $(seq 1 20); do
  if curl -s --max-time 1 "$URL" > /dev/null 2>&1; then
    echo -e " ${green}ready${reset}"
    break
  fi
  echo -n "."
  sleep 0.5
done

# ── Open LM Studio (optional) ─────────────────────────────────────────────────
if [ "$OPEN_LM" = true ]; then
  echo -e "  ${green}→${reset} Opening LM Studio…"
  open -a "LM Studio"
fi

# ── Open browser ──────────────────────────────────────────────────────────────
if [ "$OPEN_BROWSER" = true ]; then
  echo -e "  ${green}→${reset} Opening $URL…"
  open "$URL"
fi

echo ""
echo -e "  ${bold}App running at $URL${reset}"
echo -e "  ${dim}Ctrl+C to stop${reset}"
echo ""

# ── Stream launcher output; exit when launcher exits ─────────────────────────
wait $LAUNCHER_PID
echo -e "\n  ${yellow}Focal stopped.${reset}\n"
