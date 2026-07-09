"""SQLite schéma a přístup (část 1 — sledovaní hráči)."""

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    puuid TEXT PRIMARY KEY,
    riot_id TEXT NOT NULL,          -- "GameName#TAG"
    platform TEXT NOT NULL,         -- eun1 / euw1
    discord_user_id TEXT,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    region TEXT NOT NULL,
    game_creation INTEGER,          -- epoch ms
    duration INTEGER,               -- sekundy
    queue_id INTEGER,
    patch TEXT,
    raw_json TEXT
);
CREATE TABLE IF NOT EXISTS match_participants (
    match_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    riot_id TEXT,
    champion TEXT,
    kills INTEGER, deaths INTEGER, assists INTEGER,
    win INTEGER,
    role TEXT,
    cs INTEGER,
    gold INTEGER,
    damage INTEGER,
    team_id INTEGER,
    items TEXT,                     -- item idy oddělené mezerou
    keystone INTEGER,               -- perk id hlavní runy
    PRIMARY KEY (match_id, puuid)
);
CREATE TABLE IF NOT EXISTS live_games (
    puuid TEXT PRIMARY KEY,
    game_id INTEGER,
    champion_id INTEGER,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    notified_at TEXT
);
CREATE TABLE IF NOT EXISTS item_events (
    match_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    ts INTEGER NOT NULL,            -- ms od začátku hry
    item_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_events ON item_events (match_id, puuid);
CREATE TABLE IF NOT EXISTS pro_games (
    game_id TEXT PRIMARY KEY,       -- Leaguepedia GameId
    match_id TEXT,                  -- série (BO3/BO5)
    league TEXT NOT NULL,
    tournament TEXT,
    date TEXT,                      -- DateTime_UTC
    patch TEXT,
    team1 TEXT, team2 TEXT,
    winner TEXT,                    -- jméno vítězného týmu
    duration REAL,                  -- minuty
    team1_picks TEXT, team2_picks TEXT,   -- CSV championů
    team1_bans TEXT, team2_bans TEXT,
    players TEXT                    -- "t1p1,...|t2p1,..."
);
CREATE INDEX IF NOT EXISTS idx_pro_games_teams ON pro_games (team1, team2, date);
CREATE TABLE IF NOT EXISTS pro_tournaments (
    overview_page TEXT PRIMARY KEY,  -- Leaguepedia OverviewPage (klíč všude)
    name TEXT,                       -- "LEC 2025 Winter Playoffs"
    league TEXT,
    year INTEGER,
    date_start TEXT, date_end TEXT,  -- YYYY-MM-DD
    is_playoffs INTEGER
);
CREATE TABLE IF NOT EXISTS pro_player_games (
    game_id TEXT NOT NULL,
    player TEXT NOT NULL,            -- Leaguepedia Link (disambiguované jméno)
    team TEXT,
    champion TEXT,
    kills INTEGER, deaths INTEGER, assists INTEGER,
    role TEXT,
    win INTEGER,
    PRIMARY KEY (game_id, player)
);
CREATE INDEX IF NOT EXISTS idx_ppg_player ON pro_player_games (player);
CREATE TABLE IF NOT EXISTS pro_matches (
    match_id TEXT PRIMARY KEY,       -- série (BO3/BO5)
    overview_page TEXT,
    round TEXT, tab TEXT,
    best_of INTEGER,
    team1 TEXT, team2 TEXT,
    team1_score INTEGER, team2_score INTEGER,
    winner TEXT,                     -- jméno týmu
    date TEXT
);
CREATE INDEX IF NOT EXISTS idx_pro_matches_op ON pro_matches (overview_page);
CREATE TABLE IF NOT EXISTS patch_changes (
    patch TEXT NOT NULL,
    champion TEXT NOT NULL,
    kind TEXT,                       -- buff / nerf / adjust
    note TEXT,
    PRIMARY KEY (patch, champion)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS rank_snapshots (
    puuid TEXT NOT NULL,
    queue TEXT NOT NULL,
    tier TEXT, division TEXT, lp INTEGER,
    wins INTEGER, losses INTEGER,
    taken_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(path: str = "lol.db") -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(match_participants)")}
    for col, typ in (("team_id", "INTEGER"), ("items", "TEXT"), ("keystone", "INTEGER"),
                     ("perks", "TEXT"), ("primary_style", "INTEGER"),
                     ("sub_style", "INTEGER")):
        if col not in cols:
            con.execute(f"ALTER TABLE match_participants ADD COLUMN {col} {typ}")
    cols = {r[1] for r in con.execute("PRAGMA table_info(pro_games)")}
    for col, typ in (("overview_page", "TEXT"), ("team1_kills", "INTEGER"),
                     ("team2_kills", "INTEGER")):
        if col not in cols:
            con.execute(f"ALTER TABLE pro_games ADD COLUMN {col} {typ}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pro_games_op ON pro_games (overview_page)")
    # Leaguepedia's own bracket ordinals (Phase/GroupName/N_TabInPage/N_MatchInTab/N_Page) —
    # authoritative round/position data, replacing the old date-order guesswork in bracket().
    cols = {r[1] for r in con.execute("PRAGMA table_info(pro_matches)")}
    for col, typ in (("phase", "TEXT"), ("group_name", "TEXT"),
                     ("n_tab_in_page", "INTEGER"), ("n_match_in_tab", "INTEGER"),
                     ("n_page", "INTEGER")):
        if col not in cols:
            con.execute(f"ALTER TABLE pro_matches ADD COLUMN {col} {typ}")
    return con


def _participant_extras(p: dict) -> tuple:
    items = " ".join(str(p.get(f"item{i}", 0)) for i in range(6) if p.get(f"item{i}"))
    keystone = perks = primary = sub = None
    try:
        styles = p["perks"]["styles"]
        keystone = styles[0]["selections"][0]["perk"]
        perks = " ".join(str(s["perk"]) for st in styles for s in st["selections"])
        primary, sub = styles[0]["style"], styles[1]["style"]
    except (KeyError, IndexError):
        pass
    return p.get("teamId"), items, keystone, perks, primary, sub


def insert_item_events(con: sqlite3.Connection, match_id: str, timeline: dict):
    """Uloží nákupy itemů z timeline (ITEM_PURCHASED eventy)."""
    id_to_puuid = {pt["participantId"]: pt["puuid"]
                   for pt in timeline["info"]["participants"]}
    con.execute("DELETE FROM item_events WHERE match_id = ?", (match_id,))
    for frame in timeline["info"]["frames"]:
        for ev in frame["events"]:
            # participantId 0 = neznámý (Swarm/Arena eventy) -> přeskočit
            if ev["type"] == "ITEM_PURCHASED" and ev["participantId"] in id_to_puuid:
                con.execute(
                    "INSERT INTO item_events VALUES (?,?,?,?)",
                    (match_id, id_to_puuid[ev["participantId"]],
                     ev["timestamp"], ev["itemId"]))
    con.commit()


def insert_match(con: sqlite3.Connection, match: dict, region: str = "europe"):
    info = match["info"]
    con.execute(
        "INSERT OR IGNORE INTO matches VALUES (?,?,?,?,?,?,?)",
        (match["metadata"]["matchId"], region, info["gameCreation"],
         info["gameDuration"], info["queueId"], info["gameVersion"],
         json.dumps(match)),
    )
    for p in info["participants"]:
        con.execute(
            "INSERT OR IGNORE INTO match_participants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (match["metadata"]["matchId"], p["puuid"],
             f"{p.get('riotIdGameName', '')}#{p.get('riotIdTagline', '')}",
             p["championName"], p["kills"], p["deaths"], p["assists"],
             int(p["win"]), p.get("teamPosition", ""),
             p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0),
             p.get("goldEarned", 0),
             p.get("totalDamageDealtToChampions", 0),
             *_participant_extras(p)),
        )
    con.commit()


def insert_rank_snapshot(con: sqlite3.Connection, puuid: str, entry: dict):
    con.execute(
        "INSERT INTO rank_snapshots (puuid, queue, tier, division, lp, wins, losses)"
        " VALUES (?,?,?,?,?,?,?)",
        (puuid, entry["queueType"], entry["tier"], entry["rank"],
         entry["leaguePoints"], entry["wins"], entry["losses"]),
    )
    con.commit()
