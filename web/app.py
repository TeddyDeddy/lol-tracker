"""
@brief Local web UI (FastAPI/Jinja2): friend profiles, match detail, pro-scene brackets.

Run: `python -m uvicorn web.app:app --port 8000` from the project root. Also
statically exported to GitHub Pages via `scripts/export_static.sh`.
"""

import json
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
SUMMONER_ICONS: dict[int, str] = {}  # summoner spell id -> ikona URL

MULTIKILL_LABELS = {2: "Double kill", 3: "Triple kill", 4: "Quadra kill", 5: "Penta kill"}


@app.on_event("startup")
async def load_ddragon():
    """
    @brief Fetch the latest Data Dragon version and pre-load icon/name lookup
           tables (champions, items, runes, summoner spells) into module globals.
    """
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
        summoners = (await http.get(
            f"{cdn}/{DDRAGON_VERSION}/data/en_US/summoner.json")).json()["data"]
    for key, c in champs.items():
        CHAMP_KEYS[c["name"]] = key  # "Wukong" -> "MonkeyKing", "Bel'Veth" -> "Belveth"
    for spell in summoners.values():
        SUMMONER_ICONS[int(spell["key"])] = f"{cdn}/{DDRAGON_VERSION}/img/spell/" + spell["image"]["full"]
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
    """@brief Data Dragon icon URL for an item ID."""
    return (f"https://ddragon.leagueoflegends.com/cdn/{DDRAGON_VERSION}"
            f"/img/item/{item_id}.png")


def summoner_icon(spell_id: int) -> str | None:
    """@brief Icon URL for a summoner spell ID, or None if unknown."""
    return SUMMONER_ICONS.get(spell_id)


def con() -> sqlite3.Connection:
    """@brief Open a connection to the project's `lol.db`."""
    return db.connect(str(ROOT / "lol.db"))


def champ_icon(champion: str) -> str:
    """
    @brief Data Dragon icon URL for a champion display name.

    @param champion Champion display name (e.g. "Miss Fortune", "Bel'Veth").
    @return Icon URL, using `CHAMP_KEYS` for names that don't match their
            ddragon key directly, else a punctuation-stripped fallback.
    """
    key = CHAMP_KEYS.get(
        champion, champion.replace(" ", "").replace("'", "").replace(".", ""))
    return (f"https://ddragon.leagueoflegends.com/cdn/{DDRAGON_VERSION}"
            f"/img/champion/{key}.png")


def latest_ranks(c: sqlite3.Connection, puuid: str) -> dict:
    """
    @brief Most recent rank snapshot per queue for a player.

    @param c     Open sqlite3 connection.
    @param puuid Player's PUUID.
    @return Dict of queue -> `rank_snapshots` row dict.
    """
    rows = c.execute(
        "SELECT * FROM rank_snapshots WHERE puuid = ? AND taken_at ="
        " (SELECT MAX(taken_at) FROM rank_snapshots r2"
        "  WHERE r2.puuid = rank_snapshots.puuid AND r2.queue = rank_snapshots.queue)",
        (puuid,)).fetchall()
    return {r["queue"]: dict(r) for r in rows}


@app.get("/")
async def index(request: Request):
    """@brief Home page: every tracked player with their recent form, rank, and live status."""
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


QUEUE_NAMES = stats.QUEUE_NAMES

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


# Arena (Cherry) augment id -> {name, icon, rarity}, pre-fetched via a one-off script
# from CommunityDragon (`cdragon/arena/en_us.json`) into data/arena_augments.json.
try:
    ARENA_AUGMENTS: dict[str, dict] = json.loads(
        (ROOT / "data" / "arena_augments.json").read_text())
except FileNotFoundError:
    ARENA_AUGMENTS = {}


def augment_icon(aug_id: int) -> str | None:
    """
    @brief Look up the small icon URL for an Arena augment.

    @param aug_id Riot `playerAugmentN` id from match participant data.
    @return Icon URL, or None if the id is unknown (e.g. 0 = no augment in that slot).
    """
    a = ARENA_AUGMENTS.get(str(aug_id))
    return a["icon"] if a else None


def augment_name(aug_id: int) -> str:
    """
    @brief Look up the display name for an Arena augment.

    @param aug_id Riot `playerAugmentN` id from match participant data.
    @return Augment name, or a placeholder if the id is unknown.
    """
    a = ARENA_AUGMENTS.get(str(aug_id))
    return a["name"] if a else f"Augment {aug_id}"


def _season(season: str):
    """@brief Parse a season query-string value to an int year, or None if not numeric."""
    return int(season) if season.isdigit() else None


def _empty_or_404(request, c, riot_id, message):
    """
    @brief Render an empty-state page, or 404 if the player isn't tracked at all.

    A known player with no games matching the current filter gets 200 + an
    empty page — the static export needs 200 or `wget` won't mirror the
    link. An unknown player gets a real 404.

    @param request Current FastAPI request.
    @param c       Open sqlite3 connection (closed by this function).
    @param riot_id Player being looked up.
    @param message Empty-state message to show.
    @return Rendered `empty.html` template response.
    @throws HTTPException 404 if the player has no `players` row at all.
    """
    known = stats._puuid(c, riot_id)
    c.close()
    if not known:
        raise HTTPException(404, f"Žádná data pro {riot_id}")
    return templates.TemplateResponse(request, "empty.html",
                                      {"riot_id": riot_id, "message": message})


@app.get("/player/{riot_id}")
async def player(request: Request, riot_id: str, mode: str = "All", season: str = ""):
    """
    @brief Player profile page: summary, rank, recent games, champion pool, top records.

    @param request  Current FastAPI request.
    @param riot_id "GameName#TAG" identifier.
    @param mode     QUEUE_GROUPS filter key, falls back to "All" if unrecognized.
    @param season   Optional year filter (query string, may be empty).
    @return Rendered `player.html`, or the empty/404 fallback via `_empty_or_404`.
    """
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
    champs = c.execute(
        "SELECT champion, COUNT(*) games, SUM(win) wins,"
        " SUM(kills) k, SUM(deaths) d, SUM(assists) a"
        " FROM match_participants p JOIN matches m USING (match_id)"
        f" WHERE p.puuid = ?{stats._filters(queues, sez)}"
        " GROUP BY champion ORDER BY games DESC",
        (stats._puuid(c, riot_id),)).fetchall()
    me_puuid = stats._puuid(c, riot_id)
    recs = stats.records(c, riot_id, queues or stats.STANDARD_QUEUES, sez, limit=1)
    all_seasons = stats.seasons(c)
    mode_counts = stats.queue_counts(c, riot_id, sez)
    modes = stats.mode_pills(c, riot_id, sez)
    c.close()
    return templates.TemplateResponse(request, "player.html", {
        "riot_id": riot_id, "mode": mode, "modes": modes,
        "season": season, "seasons": all_seasons, "me_puuid": me_puuid,
        "summary": s, "ranks": ranks, "games": games,
        "mode_counts": mode_counts,
        "champs": [dict(x) for x in champs], "records": recs,
        "champ_icon": champ_icon, "item_icon": item_icon, "rune_icons": RUNE_ICONS,
        "fmt_duration": stats.fmt_duration,
        "fmt_int": stats.fmt_int, "when": stats._when,
        "queue_names": QUEUE_NAMES,
    })


GAMES_PER_PAGE = 25


@app.get("/player/{riot_id}/games")
async def games_list(request: Request, riot_id: str, mode: str = "All",
                     season: str = "", page: int = 1, champion: str = ""):
    """
    @brief Paginated full match history for a player, with optional champion filter.

    The champion filter is applied server-side against the player's whole
    history, not just the current page.

    @param request  Current FastAPI request.
    @param riot_id  "GameName#TAG" identifier.
    @param mode     QUEUE_GROUPS filter key, falls back to "All" if unrecognized.
    @param season   Optional year filter.
    @param page     1-based page number, clamped to the valid range.
    @param champion Optional exact champion name filter.
    @return Rendered `games.html`, or the empty/404 fallback via `_empty_or_404`.
    """
    if mode not in stats.QUEUE_GROUPS:
        mode = "All"
    queues, sez = stats.QUEUE_GROUPS[mode], _season(season)
    c = con()
    s = stats.summary(c, riot_id, queues, sez, champion or None)
    if not s:
        filt = f"{mode} / {season or 'všechny roky'}"
        if champion:
            filt += f" / {champion}"
        return _empty_or_404(request, c, riot_id, f"Žádné hry pro filtr {filt}.")
    pages = max(1, -(-s["games"] // GAMES_PER_PAGE))
    page = max(1, min(page, pages))
    games = stats.recent_games(c, riot_id, GAMES_PER_PAGE, queues, sez,
                               offset=(page - 1) * GAMES_PER_PAGE,
                               champion=champion or None)
    rosters = stats.match_rosters(c, [g["match_id"] for g in games])
    # puuid -> aktuální jméno (roster drží jména z doby zápasu)
    tracked = {r["puuid"]: r["riot_id"]
               for r in c.execute("SELECT puuid, riot_id FROM players")}
    me_puuid = stats._puuid(c, riot_id)
    all_seasons = stats.seasons(c)
    champ_list = stats.champion_list(c, riot_id)
    c.close()
    return templates.TemplateResponse(request, "games.html", {
        "riot_id": riot_id, "mode": mode, "modes": list(stats.QUEUE_GROUPS),
        "season": season, "seasons": all_seasons, "champion": champion,
        "champ_list": champ_list,
        "games": games, "rosters": rosters, "tracked": tracked,
        "me_puuid": me_puuid,
        "page": page, "pages": pages, "total": s["games"],
        "champ_icon": champ_icon, "item_icon": item_icon, "rune_icons": RUNE_ICONS,
        "fmt_duration": stats.fmt_duration, "when": stats._when,
        "queue_names": QUEUE_NAMES,
    })


@app.get("/player/{riot_id}/records")
async def player_records(request: Request, riot_id: str, mode: str = "All", season: str = ""):
    """
    @brief Full records page for a player: top-10 per record category, in table form.

    @param request Current FastAPI request.
    @param riot_id "GameName#TAG" identifier.
    @param mode     QUEUE_GROUPS filter key, falls back to "All" if unrecognized.
    @param season   Optional year filter.
    @return Rendered `records.html`, or the empty/404 fallback via `_empty_or_404`.
    """
    if mode not in stats.QUEUE_GROUPS:
        mode = "All"
    queues, sez = stats.QUEUE_GROUPS[mode], _season(season)
    c = con()
    recs = stats.records(c, riot_id, queues or stats.STANDARD_QUEUES, sez, limit=10)
    if not recs:
        return _empty_or_404(request, c, riot_id,
                             f"Žádné rekordy pro filtr {mode} / {season or 'všechny roky'}.")
    me_puuid = stats._puuid(c, riot_id)
    all_seasons = stats.seasons(c)
    c.close()
    return templates.TemplateResponse(request, "records.html", {
        "riot_id": riot_id, "mode": mode, "modes": list(stats.QUEUE_GROUPS),
        "season": season, "seasons": all_seasons, "me_puuid": me_puuid,
        "records": recs, "queue_names": QUEUE_NAMES,
        "fmt_duration": stats.fmt_duration, "fmt_int": stats.fmt_int, "when": stats._when,
    })


@app.get("/player/{riot_id}/champ/{champion}")
async def champion_detail(request: Request, riot_id: str, champion: str,
                          mode: str = "All", season: str = ""):
    """
    @brief Per-champion detail page: matchups, rune pages, item builds, build orders.

    @param request  Current FastAPI request.
    @param riot_id  "GameName#TAG" identifier.
    @param champion Champion to show stats for.
    @param mode     QUEUE_GROUPS filter key, falls back to "All" if unrecognized.
    @param season   Optional year filter.
    @return Rendered `champion.html`, or the empty/404 fallback via `_empty_or_404`.
    """
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

    @param request  Current FastAPI request.
    @param match_id Riot match id, e.g. "EUN1_3626538195".
    @param focus riot_id or puuid to highlight in the player table (threaded through from
                 profile/game-list links so the viewer's own row stands out).
    @return Rendered `match.html` (or `arena.html` for Arena games) template response.
    """
    c = con()
    row = c.execute("SELECT raw_json FROM matches WHERE match_id = ?",
                    (match_id,)).fetchone()
    tracked = {r["riot_id"] for r in c.execute("SELECT riot_id FROM players")}
    c.close()
    if not row:
        raise HTTPException(404, f"Zápas {match_id} není v DB")
    info = json.loads(row["raw_json"])["info"]

    if info.get("gameMode") == "CHERRY":
        return _arena_match_detail(request, match_id, info, tracked, focus)

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

    duration_min = max(info.get("gameDuration", 0) / 60, 1)
    teams: dict[int, list] = {100: [], 200: []}
    for p in info["participants"]:
        rid = f"{p.get('riotIdGameName', '?')}#{p.get('riotIdTagline', '')}"
        try:
            styles = p["perks"]["styles"]
            sub_style = styles[1]["style"]
            keystone = styles[0]["selections"][0]["perk"]
        except (KeyError, IndexError):
            sub_style = keystone = None
        team_kills = objectives.get(p["teamId"], {}).get("kills", 0)
        kills, deaths, assists = p["kills"], p["deaths"], p["assists"]
        cs = p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)
        gold = p.get("goldEarned", 0)
        teams.setdefault(p["teamId"], []).append({
            "riot_id": rid, "tracked": rid in tracked,
            "focus": focus in (rid, p.get("puuid")),
            "champion": p["championName"], "level": p.get("champLevel", 0),
            "kills": kills, "deaths": deaths, "assists": assists,
            "kda": round((kills + assists) / max(deaths, 1), 2),
            "kp": round(100 * (kills + assists) / team_kills) if team_kills else 0,
            "cs": cs, "cs_per_min": round(cs / duration_min, 1),
            "gold": gold, "gold_per_min": round(gold / duration_min),
            "damage": p.get("totalDamageDealtToChampions", 0),
            "damage_taken": p.get("totalDamageTaken", 0),
            "vision": p.get("visionScore", 0),
            "items": [p.get(f"item{i}", 0) for i in range(6) if p.get(f"item{i}")],
            "keystone": keystone, "sub_style": sub_style,
            "summoner1": summoner_icon(p.get("summoner1Id")),
            "summoner2": summoner_icon(p.get("summoner2Id")),
            "multikill": MULTIKILL_LABELS.get(p.get("largestMultiKill", 0)),
            "first_blood": bool(p.get("firstBloodKill") or p.get("firstBloodAssist")),
        })

    all_players = [pl for tm in teams.values() for pl in tm]
    max_damage = max((pl["damage"] for pl in all_players), default=1) or 1
    max_damage_taken = max((pl["damage_taken"] for pl in all_players), default=1) or 1
    best = {stat: max((pl[stat] for pl in all_players), default=0)
           for stat in ("kills", "kda", "cs", "gold", "vision")}
    return templates.TemplateResponse(request, "match.html", {
        "match_id": match_id, "teams": teams, "objectives": objectives,
        "queue": QUEUE_NAMES.get(info.get("queueId"), f"queue {info.get('queueId')}"),
        "duration": stats.fmt_duration(info.get("gameDuration", 0)),
        "when": stats._when(info.get("gameCreation", 0)),
        "patch": ".".join(str(info.get("gameVersion", "")).split(".")[:2]),
        "max_damage": max_damage, "max_damage_taken": max_damage_taken,
        "best": best, "focus": focus,
        "champ_icon": champ_icon, "item_icon": item_icon, "obj_icon": obj_icon,
        "rune_icons": RUNE_ICONS, "style_icons": STYLE_ICONS,
        "fmt_int": stats.fmt_int,
    })


def _arena_match_detail(request: Request, match_id: str, info: dict,
                         tracked: set[str], focus: str):
    """
    @brief Render an Arena (Cherry) game as ranked duo/trio subteams.

    Arena has no fixed 5v5 sides — 1700 groups 16 players into 8 duos, 1750 groups
    18 players into 6 trios (`playerSubteamId`). Every player on a subteam already
    carries the team's final standing in `placement`, so no extra ranking pass is needed.

    @param request  Current FastAPI request.
    @param match_id Riot match id.
    @param info Riot API match `info` object (already confirmed `gameMode == "CHERRY"`).
    @param tracked Set of tracked riot_ids, for linking to friend profiles.
    @param focus riot_id or puuid to highlight in the player table.
    @return Rendered `arena.html` template response.
    """
    subteams: dict[int, dict] = {}
    for p in info["participants"]:
        rid = f"{p.get('riotIdGameName', '?')}#{p.get('riotIdTagline', '')}"
        try:
            keystone = p["perks"]["styles"][0]["selections"][0]["perk"]
        except (KeyError, IndexError):
            keystone = None
        sid = p.get("playerSubteamId", 0)
        team = subteams.setdefault(sid, {
            "placement": p.get("placement", 0), "players": []})
        augments = [p[f"playerAugment{i}"] for i in range(1, 7)
                    if p.get(f"playerAugment{i}")]
        team["players"].append({
            "riot_id": rid, "tracked": rid in tracked,
            "focus": focus in (rid, p.get("puuid")),
            "champion": p["championName"], "level": p.get("champLevel", 0),
            "kills": p["kills"], "deaths": p["deaths"], "assists": p["assists"],
            "damage": p.get("totalDamageDealtToChampions", 0),
            "damage_taken": p.get("totalDamageTaken", 0),
            "items": [p.get(f"item{i}", 0) for i in range(6) if p.get(f"item{i}")],
            "keystone": keystone, "augments": augments,
        })
    ordered = sorted(subteams.values(), key=lambda t: t["placement"] or 99)
    max_damage = max(
        (pl["damage"] for t in ordered for pl in t["players"]), default=1) or 1
    return templates.TemplateResponse(request, "arena.html", {
        "match_id": match_id, "subteams": ordered,
        "duration": stats.fmt_duration(info.get("gameDuration", 0)),
        "when": stats._when(info.get("gameCreation", 0)),
        "patch": ".".join(str(info.get("gameVersion", "")).split(".")[:2]),
        "max_damage": max_damage, "focus": focus,
        "champ_icon": champ_icon, "item_icon": item_icon,
        "augment_icon": augment_icon, "augment_name": augment_name,
        "rune_icons": RUNE_ICONS, "fmt_int": stats.fmt_int,
    })


# ---------- pro scéna (Leaguepedia, CC BY-SA) ----------

def _pro_common():
    """@brief Template context shared by every pro-scene page (short names, icons, formatting)."""
    return {"short": prostats.short, "champ_icon": champ_icon,
            "fmt_int": stats.fmt_int}


@app.get("/pro")
async def pro_index(request: Request, year: int = 0):
    """
    @brief Pro-scene landing page: tournaments for one year, grouped into
           international events vs. regional leagues.

    @param request Current FastAPI request.
    @param year Year to show; defaults to the latest year with any ingested
           games, falling back to the newest known year if none has data yet.
    @return Rendered `pro_index.html`.
    """
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
    """
    @brief One pro series (BO3/BO5): per-game results and rosters.

    @param request  Current FastAPI request.
    @param match_id Leaguepedia series match ID.
    @return Rendered `pro_match.html`, or an empty-state page if the series
            exists in the bracket but its games aren't ingested yet.
    @throws HTTPException 404 if the series isn't in the DB at all.
    """
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


def _sibling_tournament(c: sqlite3.Connection, op: str) -> dict | None:
    """
    @brief Best-effort lookup of a tournament's season/playoffs sibling page,
           using Leaguepedia's dominant "X" / "X Playoffs" OverviewPage naming
           convention (season and playoffs are usually two separate pages).

    @param c  Open sqlite3 connection.
    @param op This tournament's OverviewPage.
    @return {overview_page, name} of the sibling page if found, else None.
    """
    sibling_op = op[: -len(" Playoffs")] if op.endswith(" Playoffs") else op + " Playoffs"
    row = c.execute("SELECT overview_page, name FROM pro_tournaments"
                    " WHERE overview_page = ?", (sibling_op,)).fetchone()
    return dict(row) if row else None


@app.get("/pro/t/{op:path}")
async def pro_tournament(request: Request, op: str):
    """
    @brief Render one tournament's page: pick/ban meta, teams, and its
           bracket/standings phases (see `prostats.tournament_phases`).
    """
    c = con()
    t = c.execute("SELECT * FROM pro_tournaments WHERE overview_page = ?",
                  (op,)).fetchone()
    if not t:
        c.close()
        raise HTTPException(404, f"Turnaj {op} není v DB")
    champs = prostats.tournament_champs(c, op)
    teams = prostats.tournament_teams(c, op)
    phases = prostats.tournament_phases(c, op)
    sibling = _sibling_tournament(c, op)
    forms = {}
    if t["is_playoffs"]:
        for team in {x["team"] for x in teams}:
            forms[team] = prostats.team_form(c, team, before=t["date_start"])
    c.close()
    return templates.TemplateResponse(request, "pro_tournament.html", {
        "t": dict(t), "champs": champs, "teams": teams, "phases": phases,
        "sibling": sibling, "forms": forms, **_pro_common()})


@app.get("/pro/player/{player}")
async def pro_player(request: Request, player: str):
    """
    @brief Pro player page: per-tournament games/WR/KDA and champion pool.

    @param request Current FastAPI request.
    @param player Leaguepedia player Link (disambiguated name).
    @return Rendered `pro_player.html`. If no exact match, shows name
            suggestions instead of a 404 (Leaguepedia names are easy to typo).
    """
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
    """
    @brief Buff/nerf meta shift page for an event: champion presence vs. the
           baseline main-league period before it.

    @param request Current FastAPI request.
    @param op Event tournament's Leaguepedia OverviewPage.
    @return Rendered `pro_meta.html`.
    @throws HTTPException 404 if the event isn't in the DB or has no games.
    """
    c = con()
    shift = prostats.event_meta_shift(c, op)
    c.close()
    if not shift:
        raise HTTPException(404, f"Event {op} není v DB (nebo nemá hry)")
    return templates.TemplateResponse(request, "pro_meta.html", {
        **shift, "op": op, **_pro_common()})


@app.get("/api/live")
async def api_live():
    """@brief JSON API: every currently in-progress live game among tracked players."""
    c = con()
    rows = [dict(r) for r in c.execute(
        "SELECT l.puuid, l.champion_id, l.started_at, p.riot_id"
        " FROM live_games l JOIN players p USING (puuid)")]
    c.close()
    return JSONResponse(rows)
