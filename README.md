# lol-tracker

**Live site: https://teddydeddy.github.io/lol-tracker/**

Personal League of Legends tracker + pro-scene database.

- **Friends tracker** — Riot API → SQLite, Discord bot (game notifications, /stats), local web (profiles, game lists, matchups, builds).
- **Pro scene** — Leaguepedia ingest (LEC/LCK/LPL/LTA/LCP/PCS, MSI, Worlds, First Stand): tournaments, brackets, drafts, pick/ban/presence, meta shifts.

## Running locally

```bash
python -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env   # add RIOT_API_KEY (dev key: developer.riotgames.com, expires in 24 h)

.venv/bin/python -m lol.tracker sync                  # download matches of tracked players
.venv/bin/python -m lol.leaguepedia ingest 2026      # pro scene (resumable, slow anonymously)
.venv/bin/python -m uvicorn web.app:app --port 8000  # web at http://localhost:8000
.venv/bin/python -m lol.bot                          # Discord bot (optional)
```

## Public site (GitHub Pages)

The published site is a static snapshot. To update it:

```bash
bash scripts/export_static.sh        # regenerates docs/ (starts the web server itself if needed)
git add -A && git commit -m "update data" && git push
```

## Attribution

- Match data: [Riot Games API](https://developer.riotgames.com). lol-tracker isn't endorsed by Riot Games.
- Pro scene: [Leaguepedia](https://lol.fandom.com) (CC BY-SA 3.0).
