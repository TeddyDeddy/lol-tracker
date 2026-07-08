#!/usr/bin/env bash
# Statický export webu do docs/ pro GitHub Pages.
# Použití: bash scripts/export_static.sh  (z kořene projektu)
set -euo pipefail
cd "$(dirname "$0")/.."

PORT=8000
STARTED=0
if ! curl_check=$(python3 -c "import urllib.request;urllib.request.urlopen('http://localhost:$PORT/',timeout=3)" 2>&1); then
  echo "Web neběží — spouštím uvicorn na :$PORT…"
  .venv/bin/python -m uvicorn web.app:app --port $PORT >/tmp/lol-export-web.log 2>&1 &
  WEB_PID=$!
  STARTED=1
  sleep 3
fi

rm -rf docs
mkdir -p docs

# --convert-links: relativní odkazy (funguje na subpath *.github.io/lol-tracker/)
# --adjust-extension + --restrict-file-names=windows: ?query a %23 v riot_id -> bezpečná jména souborů
# --domains localhost: ddragon ikony zůstávají hot-link na CDN
wget --mirror --convert-links --adjust-extension --no-parent \
     --restrict-file-names=windows --domains localhost -nH \
     --no-verbose -P docs "http://localhost:$PORT/" 2>&1 | tail -3 || true
# wget vrací 8 při jakémkoli 404 v běhu — nechceme kvůli tomu spadnout

touch docs/.nojekyll

if [ "$STARTED" = 1 ]; then kill "$WEB_PID"; fi

echo "Hotovo: $(find docs -name '*.html' | wc -l) HTML stránek, $(du -sh docs | cut -f1)"
