#!/usr/bin/env bash
# Statický export webu do docs/ pro GitHub Pages.
# Použití: bash scripts/export_static.sh  (z kořene projektu)
set -euo pipefail
cd "$(dirname "$0")/.."

PORT=8000
STARTED=0
if ! python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:$PORT/',timeout=3)" 2>/dev/null; then
  echo "Web neběží — spouštím uvicorn na :$PORT…"
  .venv/bin/python -m uvicorn web.app:app --port $PORT >/tmp/lol-export-web.log 2>&1 &
  WEB_PID=$!
  STARTED=1
  sleep 3
fi

mirror() {
  # --convert-links: relativní odkazy (funguje na subpath *.github.io/lol-tracker/)
  # --adjust-extension + --restrict-file-names=windows: ?query a %23 v riot_id -> bezpečná jména souborů
  # --retry-connrefused: uvicorn hiccup nesmí zahodit URL z fronty (jinak zůstane localhost odkaz)
  # --domains 127.0.0.1: ddragon ikony zůstávají hot-link na CDN
  # --iri=no: wget2 jinak dekóduje %XX (jap. znaky v riot_id) a pošle raw UTF-8 -> uvicorn 400
  wget --mirror --convert-links --adjust-extension --no-parent \
       --restrict-file-names=windows --domains 127.0.0.1 -nH --iri=no \
       --retry-connrefused --tries=3 --waitretry=2 \
       --no-verbose -P docs "http://127.0.0.1:$PORT/" 2>&1 | tail -2 || true
  # wget vrací 8 při jakémkoli 404 v běhu — nechceme kvůli tomu spadnout
}

OK=0
for attempt in 1 2 3; do
  rm -rf docs
  mkdir -p docs
  echo "Mirror pokus $attempt…"
  mirror
  if ! grep -rql "http://127.0.0.1:$PORT" docs; then OK=1; break; fi
  echo "V exportu zbyly localhost odkazy (nekompletní crawl) — opakuji."
done

touch docs/.nojekyll
if [ "$STARTED" = 1 ]; then kill "$WEB_PID"; fi

if [ "$OK" != 1 ]; then
  echo "CHYBA: ani po 3 pokusech není crawl kompletní. Zbylé localhost odkazy:" >&2
  grep -rho "http://127.0.0.1:$PORT[^\"]*" docs 2>/dev/null | sort -u | head -20 >&2 || true
  exit 1
fi

echo "Hotovo: $(find docs -name '*.html' | wc -l) HTML stránek, $(du -sh docs | cut -f1)"
