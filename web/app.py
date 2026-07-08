"""Lokální web: python -m uvicorn web.app:app --port 8000 (z kořene projektu)."""

import pathlib
import sqlite3
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lol import db, prostats, stats

ROOT = pathlib.Path(__file__).parent.parent
app = FastAPI(title="LoL Tracker")
app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "web" / "templates")


def cz_date(value: str) -> str:
    """
    @brief Format a Leaguepedia-style date string as a Czech date.

    @param value ISO-ish date/datetime string, e.g. "2025-02-20 18:00:00" or "2025-02-20".
    @return Date string in `D.M.YYYY` form, or the original value if it can't be parsed.
    """
    if not value:
        return ""
    try:
        y, m, d = value.split(" ")[0].split("-")
        return f"{int(d)}.{int(m)}.{int(y)}"
    except (ValueError, AttributeError):
        return value


templates.env.filters["cz_date"] = cz_date

DDRAGON_VERSION = "latest"
RUNE_ICONS: dict[int, str] = {}   # perk id -> ikona URL
RUNE_NAMES: dict[int, str] = {}
STYLE_ICONS: dict[int, str] = {}  # strom (Precision, Domination…)
STYLE_NAMES: dict[int, str] = {}
ITEM_NAMES: dict[int, str] = {}
COMPLETED_ITEMS: set[int] = set()  # dokončené itemy (pro build order)
CHAMP_KEYS: dict[str, str] = {}   # display jméno ("Miss Fortune") -> ddragon klíč


@app.on_event("startup")
async def load_ddragon():
    global DDRAGON_VERSION
    cdn = "https://ddragon.leagueoflegends.com/cdn"
    async with httpx.AsyncClient(timeout=15) as http:
        DDRAGON_VERSION = (await http.get(
            "https://ddragon.leagueoflegends.com/api/versions.json")).json()[0]
        runes = (await http.get(
            f"{cdn}/{DDRAGON_VERSION}/data/en_US/runesReforged.json")).json()
        items = (await http.get(
            f"{cdn}/{DDRAGON_VERSION}/data/en_US/item.json")).json()["data"]
        champs = (await http.get(
            f"{cdn}/{DDRAGON_VERSION}/data/en_US/champion.json")).json()["data"]
    for key, c in champs.items():
        CHAMP_KEYS[c["name"]] = key  # "Wukong" -> "MonkeyKing", "Bel'Veth" -> "Belveth"
    for tree in runes:
        STYLE_ICONS[tree["id"]] = f"{cdn}/img/" + tree["icon"]
        STYLE_NAMES[tree["id"]] = tree["name"]
        for slot in tree["slots"]:
            for perk in slot["runes"]:
                RUNE_ICONS[perk["id"]] = f"{cdn}/img/" + perk["icon"]
                RUNE_NAMES[perk["id"]] = perk["name"]
    for iid, it in items.items():
        ITEM_NAMES[int(iid)] = it["name"]
        if not it.get("into") and it.get("gold", {}).get("total", 0) >= 1100:
            COMPLETED_ITEMS.add(int(iid))


def item_icon(item_id: str) -> str:
    return (f"https://ddragon.leagueoflegends.com/cdn/{DDRAGON_VERSION}"
            f"/img/item/{item_id}.png")


def con() -> sqlite3.Connection:
    return db.connect(str(ROOT / "lol.db"))


def champ_icon(champion: str) -> str:
    key = CHAMP_KEYS.get(
        champion, champion.replace(" ", "").replace("'", "").replace(".", ""))
    return (f"https://ddragon.leagueoflegends.com/cdn/{DDRAGON_VERSION}"
            f"/img/champion/{key}.png")


def latest_ranks(c: sqlite3.Connection, puuid: str) -> dict:
    """Poslední snapshot pro každou queue."""
    rows = c.execute(
        "SELECT * FROM rank_snapshots WHERE puuid = ? AND taken_at ="
        " (SELECT MAX(taken_at) FROM rank_snapshots r2"
        "  WHERE r2.puuid = rank_snapshots.puuid AND r2.queue = rank_snapshots.queue)",
        (puuid,)).fetchall()
    return {r["queue"]: dict(r) for r in rows}


def rolling_winrate(games: list[dict], window: int = 10) -> list[dict]:
    """Klouzavý winrate přes `window` her, chronologicky."""
    games = list(reversed(games))  # nejstarší první
    out = []
    for i in range(window, len(games) + 1):
        chunk = games[i - window:i]
        out.append({"i": i, "wr": 100 * sum(g["win"] for g in chunk) / window,
                    "when": stats._when(chunk[-1]["game_creation"])})
    return out


@app.get("/")
async def index(request: Request):
    c = con()
    players = []
    for p in c.execute("SELECT * FROM players"):
        last = stats.recent_games(c, p["riot_id"], 20)
        ranks = latest_ranks(c, p["puuid"])
        live = c.execute("SELECT * FROM live_games WHERE puuid = ?",
                         (p["puuid"],)).fetchone()
        solo = ranks.get("RANKED_SOLO_5x5")
        players.append({
            "riot_id": p["riot_id"], "platform": p["platform"],
            "solo": dict(solo) if solo else None,
            "wins20": sum(g["win"] for g in last), "n20": len(last),
            "form": [g["win"] for g in last],  # nejnovější první
            "live": dict(live) if live else None,
            "main": last[0]["champion"] if last else None,
        })
    c.close()
    return templates.TemplateResponse(request, "index.html", {
        "players": players, "champ_icon": champ_icon})


QUEUE_NAMES = {420: "Ranked Solo", 440: "Ranked Flex", 400: "Draft",
               430: "Blind", 450: "ARAM", 490: "Quickplay",
               900: "URF", 1700: "Arena", 1750: "Arena", 1820: "Swarm", 1830: "Swarm"}

# HUD/minimap objective icons (CommunityDragon, updates with each patch via "latest").
CDRAGON_MINIMAP_ICONS = "https://raw.communitydragon.org/latest/game/assets/ux/minimap/icons"
OBJECTIVE_ICONS = {
    "towers": f"{CDRAGON_MINIMAP_ICONS}/tower.png",
    "dragons": f"{CDRAGON_MINIMAP_ICONS}/dragon.png",
    "barons": f"{CDRAGON_MINIMAP_ICONS}/baron.png",
    "heralds": f"{CDRAGON_MINIMAP_ICONS}/riftherald.png",
    "inhibitors": f"{CDRAGON_MINIMAP_ICONS}/inhibitor.png",
}


def obj_icon(kind: str) -> str | None:
    """
    @brief Look up the CommunityDragon HUD icon URL for a match objective type.

    @param kind One of "towers", "dragons", "barons", "heralds", "inhibitors".
    @return Icon URL, or None if the objective type has no icon (e.g. "kills").
    """
    return OBJECTIVE_ICONS.get(kind)


def _season(season: str):
    return int(season) if season.isdigit() else None


def _empty_or_404(request, c, riot_id, message):
    """Hráč existuje, jen filtr nemá hry -> 200 s prázdnou stránkou (statický export
    potřebuje 200, jinak wget odkaz nestáhne). Neznámý hráč -> 404."""
    known = stats._puuid(c, riot_id)
    c.close()
    if not known:
        raise HTTPException(404, f"Žádná data pro {riot_id}")
    return templates.TemplateResponse(request, "empty.html",
                                      {"riot_id": riot_id, "message": message})


@app.get("/player/{riot_id}")
async def player(request: Request, riot_id: str, mode: str = "All", season: str = ""):
    if mode not in stats.QUEUE_GROUPS:
        mode = "All"
    queues, sez = stats.QUEUE_GROUPS[mode], _season(season)
    c = con()
    s = stats.summary(c, riot_id, queues, sez)
    if not s:
        return _empty_or_404(request, c, riot_id,
                             f"Žádné hry pro filtr {mode} / {season or 'všechny roky'}.")
    p = c.execute("SELECT * FROM players WHERE riot_id = ?", (riot_id,)).fetchone()
    ranks = latest_ranks(c, p["puuid"]) if p else {}
    games = stats.recent_games(c, riot_id, 20, queues, sez)
    history = stats.recent_games(c, riot_id, 120, queues, sez)
    champs = c.execute(
        "SELECT champion, COUNT(*) games, SUM(win) wins,"
        " SUM(kills) k, SUM(deaths) d, SUM(assists) a"
        " FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ?{stats._filters(queues, sez)}"
        " GROUP BY champion ORDER BY games DESC LIMIT 10",
        (stats._puuid(c, riot_id),)).fetchall()
    me_puuid = stats._puuid(c, riot_id)
    recs = stats.records(c, riot_id, queues, sez)
    mups = stats.matchups(c, riot_id, 3, queues, sez)
    all_seasons = stats.seasons(c)
    c.close()
    return templates.TemplateResponse(request, "player.html", {
        "riot_id": riot_id, "mode": mode, "modes": list(stats.QUEUE_GROUPS),
        "season": season, "seasons": all_seasons, "me_puuid": me_puuid,
        "summary": s, "ranks": ranks, "games": games,
        "winrate_series": rolling_winrate(history),
        "champs": [dict(x) for x in champs], "records": recs,
        "matchups_best": mups[:6], "matchups_worst": mups[-6:][::-1] if len(mups) > 6 else [],
        "champ_icon": champ_icon, "item_icon": item_icon, "rune_icons": RUNE_ICONS,
        "fmt_duration": stats.fmt_duration,
        "fmt_int": stats.fmt_int, "when": stats._when,
        "queue_names": QUEUE_NAMES,
    })


GAMES_PER_PAGE = 25


@app.get("/player/{riot_id}/games")
async def games_list(request: Request, riot_id: str, mode: str = "All",
                     season: str = "", page: int = 1):
    if mode not in stats.QUEUE_GROUPS:
        mode = "All"
    queues, sez = stats.QUEUE_GROUPS[mode], _season(season)
    c = con()
    s = stats.summary(c, riot_id, queues, sez)
    if not s:
        return _empty_or_404(request, c, riot_id,
                             f"Žádné hry pro filtr {mode} / {season or 'všechny roky'}.")
    pages = max(1, -(-s["games"] // GAMES_PER_PAGE))
    page = max(1, min(page, pages))
    games = stats.recent_games(c, riot_id, GAMES_PER_PAGE, queues, sez,
                               offset=(page - 1) * GAMES_PER_PAGE)
    rosters = stats.match_rosters(c, [g["match_id"] for g in games])
    # puuid -> aktuální jméno (roster drží jména z doby zápasu)
    tracked = {r["puuid"]: r["riot_id"]
               for r in c.execute("SELECT puuid, riot_id FROM players")}
    me_puuid = stats._puuid(c, riot_id)
    all_seasons = stats.seasons(c)
    c.close()
    return templates.TemplateResponse(request, "games.html", {
        "riot_id": riot_id, "mode": mode, "modes": list(stats.QUEUE_GROUPS),
        "season": season, "seasons": all_seasons,
        "games": games, "rosters": rosters, "tracked": tracked,
        "me_puuid": me_puuid,
        "page": page, "pages": pages, "total": s["games"],
        "champ_icon": champ_icon, "item_icon": item_icon, "rune_icons": RUNE_ICONS,
        "fmt_duration": stats.fmt_duration, "when": stats._when,
        "queue_names": QUEUE_NAMES,
    })


@app.get("/player/{riot_id}/champ/{champion}")
async def champion_detail(request: Request, riot_id: str, champion: str,
                          mode: str = "All", season: str = ""):
    if mode not in stats.QUEUE_GROUPS:
        mode = "All"
    queues, sez = stats.QUEUE_GROUPS[mode], _season(season)
    c = con()
    games = stats.recent_games(c, riot_id, 1000, queues, sez)
    champ_games = [g for g in games if g["champion"] == champion]
    if not champ_games:
        return _empty_or_404(request, c, riot_id,
                             f"Žádné hry na {champion} pro filtr {mode} / {season or 'všechny roky'}.")
    wins = sum(g["win"] for g in champ_games)
    k = sum(g["kills"] for g in champ_games)
    d = sum(g["deaths"] for g in champ_games)
    a = sum(g["assists"] for g in champ_games)
    mups = stats.champ_matchups(c, riot_id, champion, 2, queues, sez)
    runes = stats.top_rune_pages(c, riot_id, champion, queues, sez)
    items, item_total = stats.top_final_items(c, riot_id, champion, queues, sez)
    orders, order_total = stats.item_orders(c, riot_id, champion,
                                            COMPLETED_ITEMS, queues, sez)
    all_seasons = stats.seasons(c)
    c.close()
    return templates.TemplateResponse(request, "champion.html", {
        "riot_id": riot_id, "champion": champion, "mode": mode,
        "modes": list(stats.QUEUE_GROUPS),
        "season": season, "seasons": all_seasons,
        "n": len(champ_games), "wins": wins,
        "winrate": 100 * wins / len(champ_games),
        "kda": (k + a) / max(d, 1), "k": k, "d": d, "a": a,
        "matchups": mups, "runes": runes,
        "items": items, "item_total": item_total,
        "orders": orders, "order_total": order_total,
        "champ_icon": champ_icon, "item_icon": item_icon,
        "rune_icons": RUNE_ICONS, "rune_names": RUNE_NAMES,
        "style_icons": STYLE_ICONS, "style_names": STYLE_NAMES,
        "item_names": ITEM_NAMES,
    })


@app.get("/match/{match_id}")
async def match_detail(request: Request, match_id: str, focus: str = ""):
    """
    @brief Render the full per-player stat breakdown for one Summoner's Rift/ARAM/etc. game.

    @param match_id Riot match id, e.g. "EUN1_3626538195".
    @param focus riot_id or puuid to highlight in the player table (threaded through from
                 profile/game-list links so the viewer's own row stands out).
    @return Rendered `match.html` template response.
    """
    import json as _json
    c = con()
    row = c.execute("SELECT raw_json FROM matches WHERE match_id = ?",
                    (match_id,)).fetchone()
    tracked = {r["riot_id"] for r in c.execute("SELECT riot_id FROM players")}
    c.close()
    if not row:
        raise HTTPException(404, f"Zápas {match_id} není v DB")
    info = _json.loads(row["raw_json"])["info"]

    objectives = {}
    for t in info.get("teams", []):
        o = t.get("objectives", {})
        objectives[t["teamId"]] = {
            "win": t.get("win"),
            "kills": o.get("champion", {}).get("kills", 0),
            "towers": o.get("tower", {}).get("kills", 0),
            "dragons": o.get("dragon", {}).get("kills", 0),
            "barons": o.get("baron", {}).get("kills", 0),
            "heralds": o.get("riftHerald", {}).get("kills", 0),
            "inhibitors": o.get("inhibitor", {}).get("kills", 0),
        }

    teams: dict[int, list] = {100: [], 200: []}
    for p in info["participants"]:
        rid = f"{p.get('riotIdGameName', '?')}#{p.get('riotIdTagline', '')}"
        try:
            keystone = p["perks"]["styles"][0]["selections"][0]["perk"]
        except (KeyError, IndexError):
            keystone = None
        team_kills = objectives.get(p["teamId"], {}).get("kills", 0)
        kills, assists = p["kills"], p["assists"]
        teams.setdefault(p["teamId"], []).append({
            "riot_id": rid, "tracked": rid in tracked,
            "focus": focus in (rid, p.get("puuid")),
            "champion": p["championName"], "level": p.get("champLevel", 0),
            "kills": kills, "deaths": p["deaths"], "assists": assists,
            "kp": round(100 * (kills + assists) / team_kills) if team_kills else 0,
            "cs": p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0),
            "gold": p.get("goldEarned", 0),
            "damage": p.get("totalDamageDealtToChampions", 0),
            "damage_taken": p.get("totalDamageTaken", 0),
            "vision": p.get("visionScore", 0),
            "items": [p.get(f"item{i}", 0) for i in range(6) if p.get(f"item{i}")],
            "keystone": keystone,
        })

    for tm in teams.values():
        team_damage = sum(pl["damage"] for pl in tm) or 1
        for pl in tm:
            pl["dmg_share"] = round(100 * pl["damage"] / team_damage)

    max_damage = max((pl["damage"] for tm in teams.values() for pl in tm), default=1) or 1
    max_damage_taken = max(
        (pl["damage_taken"] for tm in teams.values() for pl in tm), default=1) or 1
    return templates.TemplateResponse(request, "match.html", {
        "match_id": match_id, "teams": teams, "objectives": objectives,
        "queue": QUEUE_NAMES.get(info.get("queueId"), f"queue {info.get('queueId')}"),
        "duration": stats.fmt_duration(info.get("gameDuration", 0)),
        "when": stats._when(info.get("gameCreation", 0)),
        "patch": ".".join(str(info.get("gameVersion", "")).split(".")[:2]),
        "max_damage": max_damage, "max_damage_taken": max_damage_taken, "focus": focus,
        "champ_icon": champ_icon, "item_icon": item_icon, "obj_icon": obj_icon,
        "rune_icons": RUNE_ICONS, "fmt_int": stats.fmt_int,
    })


# ---------- pro scéna (Leaguepedia, CC BY-SA) ----------

def _pro_common():
    return {"short": prostats.short, "champ_icon": champ_icon,
            "fmt_int": stats.fmt_int}


@app.get("/pro")
async def pro_index(request: Request, year: int = 0):
    c = con()
    years = [r[0] for r in c.execute(
        "SELECT DISTINCT year FROM pro_tournaments ORDER BY year DESC")]
    if not years:
        c.close()
        return templates.TemplateResponse(request, "pro_index.html", {
            "years": [], "year": 0, "leagues": [], "events": [], "short": prostats.short})
    if year not in years:
        # default: poslední rok, který má data; jinak nejnovější
        row = c.execute(
            "SELECT MAX(t.year) FROM pro_tournaments t"
            " JOIN pro_games g ON g.overview_page = t.overview_page").fetchone()
        year = row[0] if row and row[0] else years[0]
    rows = [dict(r) for r in c.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM pro_games g"
        "  WHERE g.overview_page = t.overview_page) games"
        " FROM pro_tournaments t WHERE year = ? ORDER BY date_start", (year,))]
    with_data = c.execute(
        "SELECT COUNT(DISTINCT overview_page), MAX(date) FROM pro_games").fetchone()
    c.close()
    events, leagues = [], {}
    for t in rows:
        if t["league"] in ("Mid-Season Invitational", "World Championship",
                           "First Stand", "Esports World Cup"):
            events.append(t)
        else:
            leagues.setdefault(t["league"], []).append(t)
    return templates.TemplateResponse(request, "pro_index.html", {
        "years": years, "year": year, "events": events,
        "leagues": sorted(leagues.items(),
                          key=lambda kv: (-sum(t["games"] for t in kv[1]), kv[0])),
        "short": prostats.short,
        "n_with_data": with_data[0], "n_total": len(rows),
        "last_data": (with_data[1] or "")[:10]})


@app.get("/pro/m/{match_id:path}")
async def pro_match(request: Request, match_id: str):
    c = con()
    m = c.execute("SELECT * FROM pro_matches WHERE match_id = ?",
                  (match_id,)).fetchone()
    games = prostats.series_games(c, match_id)
    op = (m["overview_page"] if m else None) or (
        games[0]["overview_page"] if games else "")
    t = c.execute("SELECT * FROM pro_tournaments WHERE overview_page = ?",
                  (op,)).fetchone()
    c.close()
    if not games:
        if not m:
            raise HTTPException(404, f"Série {match_id} není v DB")
        # série existuje v pavouku, ale hry nejsou stažené -> 200 (statický export)
        return templates.TemplateResponse(request, "empty.html", {
            "riot_id": f"{m['team1']} vs {m['team2']}",
            "message": "Hry této série zatím nejsou stažené.",
            "back_href": f"/pro/t/{quote(op, safe='')}" if op else "/pro",
            "back_label": "← zpět na turnaj",
        })
    return templates.TemplateResponse(request, "pro_match.html", {
        "m": dict(m) if m else None, "games": games, "op": op,
        "tournament": dict(t) if t else None,
        "split": prostats._split, **_pro_common()})


@app.get("/pro/t/{op:path}")
async def pro_tournament(request: Request, op: str):
    c = con()
    t = c.execute("SELECT * FROM pro_tournaments WHERE overview_page = ?",
                  (op,)).fetchone()
    if not t:
        c.close()
        raise HTTPException(404, f"Turnaj {op} není v DB")
    champs = prostats.tournament_champs(c, op)
    teams = prostats.tournament_teams(c, op)
    rounds = prostats.bracket(c, op)
    forms = {}
    if t["is_playoffs"]:
        for team in {x["team"] for x in teams}:
            forms[team] = prostats.team_form(c, team, before=t["date_start"])
    c.close()
    return templates.TemplateResponse(request, "pro_tournament.html", {
        "t": dict(t), "champs": champs, "teams": teams, "rounds": rounds,
        "forms": forms, **_pro_common()})


@app.get("/pro/player/{player}")
async def pro_player(request: Request, player: str):
    c = con()
    tournaments = prostats.player_summary(c, player)
    if not tournaments:
        suggestions = prostats.search_players(c, player)
        c.close()
        return templates.TemplateResponse(request, "pro_player.html", {
            "player": player, "tournaments": [], "suggestions": suggestions,
            **_pro_common()})
    c.close()
    return templates.TemplateResponse(request, "pro_player.html", {
        "player": player, "tournaments": tournaments, "suggestions": [],
        **_pro_common()})


@app.get("/pro/meta/{op:path}")
async def pro_meta(request: Request, op: str):
    c = con()
    shift = prostats.event_meta_shift(c, op)
    c.close()
    if not shift:
        raise HTTPException(404, f"Event {op} není v DB (nebo nemá hry)")
    return templates.TemplateResponse(request, "pro_meta.html", {
        **shift, "op": op, **_pro_common()})


@app.get("/api/live")
async def api_live():
    c = con()
    rows = [dict(r) for r in c.execute(
        "SELECT l.puuid, l.champion_id, l.started_at, p.riot_id"
        " FROM live_games l JOIN players p USING (puuid)")]
    c.close()
    return JSONResponse(rows)
