"""Leaguepedia (Cargo API) — ingest historie pro-zápasů.

CLI:
    python -m lol.leaguepedia leagues [rok]    # průzkum názvů lig
    python -m lol.leaguepedia ingest [2023]    # turnaje + hry od daného roku
    python -m lol.leaguepedia update           # dotáhne běžící/nové turnaje

Data: CC BY-SA, zdroj lol.fandom.com — atribuce na webu.
S FANDOM_USER/FANDOM_BOT_PASSWORD v .env se klient přihlásí (vyšší rate
limity, throttle 1 s); anonymně platí 3 s + backoff.
"""

import datetime
import os
import pathlib
import sys
import time
import tomllib

import httpx

from lol import db

ROOT = pathlib.Path(__file__).parent.parent
API = "https://lol.fandom.com/api.php"
HEADERS = {"User-Agent": "lol-tracker-hobby-project (Teddy, tadeasww22@gmail.com)"}

# Přesné hodnoty pole League v tabulce Tournaments (ověřeno 2026-07-07).
MAJOR_LEAGUES = [
    "LoL EMEA Championship",                            # LEC
    "LoL Champions Korea",                              # LCK
    "Tencent LoL Pro League",                           # LPL
    "League of Legends Championship of The Americas",   # LTA (od 2025)
    "League of Legends Championship of The Americas North",
    "League of Legends Championship of The Americas South",
    "League of Legends Championship Series",            # LCS (do 2024)
    "Circuito Brasileiro de League of Legends",         # CBLOL (do 2024)
    "League of Legends Championship Pacific",           # LCP (od 2025)
    "Pacific Championship Series",                      # PCS (do 2024)
    "Mid-Season Invitational",
    "World Championship",
    "First Stand",
]

# Zkratky pro web (delší názvy jsou nečitelné)
LEAGUE_SHORT = {
    "LoL EMEA Championship": "LEC",
    "LoL Champions Korea": "LCK",
    "Tencent LoL Pro League": "LPL",
    "League of Legends Championship of The Americas": "LTA",
    "League of Legends Championship of The Americas North": "LTA North",
    "League of Legends Championship of The Americas South": "LTA South",
    "League of Legends Championship Series": "LCS",
    "Circuito Brasileiro de League of Legends": "CBLOL",
    "League of Legends Championship Pacific": "LCP",
    "Pacific Championship Series": "PCS",
    "Mid-Season Invitational": "MSI",
    "World Championship": "Worlds",
    "First Stand": "First Stand",
}

_client = httpx.Client(headers=HEADERS, timeout=30)
_logged_in = False
_throttle = 3.0
_last_request = 0.0


def login():
    """Přihlášení bot passwordem (Special:BotPasswords) — vyšší limity."""
    global _logged_in, _throttle
    user = os.environ.get("FANDOM_USER")
    password = os.environ.get("FANDOM_BOT_PASSWORD")
    if not user or not password:
        print("FANDOM_USER/FANDOM_BOT_PASSWORD nenastaveno — jedu anonymně (pomalu).",
              flush=True)
        return False
    token = _client.get(API, params={
        "action": "query", "meta": "tokens", "type": "login", "format": "json",
    }).json()["query"]["tokens"]["logintoken"]
    r = _client.post(API, data={
        "action": "login", "lgname": user, "lgpassword": password,
        "lgtoken": token, "format": "json",
    }).json()
    if r.get("login", {}).get("result") != "Success":
        print(f"Fandom login selhal: {r.get('login')} — jedu anonymně.", flush=True)
        return False
    _logged_in = True
    _throttle = 1.0
    print(f"Fandom login OK ({user.split('@')[0]}).", flush=True)
    return True


def cargo_query(tables: str, fields: str, where: str, join_on: str = "",
                order_by: str = "", limit: int = 500, offset: int = 0) -> list[dict]:
    """Jeden cargoquery request s throttlingem a backoffem na rate limit."""
    global _last_request
    params = {"action": "cargoquery", "format": "json", "tables": tables,
              "fields": fields, "where": where, "limit": limit, "offset": offset}
    if join_on:
        params["join_on"] = join_on
    if order_by:
        params["order_by"] = order_by
    for attempt in range(6):
        wait = _throttle - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        r = _client.get(API, params=params)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            info = data["error"].get("info", "")
            if "rate limit" in info.lower():
                pause = 20 * (attempt + 1)
                print(f"rate limit, čekám {pause} s…", flush=True)
                time.sleep(pause)
                continue
            raise RuntimeError(f"cargoquery: {info}")
        return [row["title"] for row in data.get("cargoquery", [])]
    raise RuntimeError("cargoquery: rate limit nepovolil ani po 6 pokusech")


def cargo_query_all(tables: str, fields: str, where: str, **kw) -> list[dict]:
    """Stránkuje po 500 dokud chodí plné stránky."""
    out, offset = [], 0
    while True:
        page = cargo_query(tables, fields, where, offset=offset, **kw)
        out += page
        if len(page) < 500:
            return out
        offset += 500
        print(f"  …{len(out)} řádků", flush=True)


def _esc(s: str) -> str:
    return s.replace("'", "''")


def list_leagues(year: int = 2025):
    """Průzkum: vypíše Primary ligy pro daný rok."""
    rows = cargo_query("Tournaments", "League,TournamentLevel",
                       f"Year={year}", order_by="League")
    for league in sorted({r["League"] for r in rows
                          if r.get("TournamentLevel") == "Primary" and r["League"]}):
        mark = "✔" if league in MAJOR_LEAGUES else " "
        print(f" {mark} {league}")


# ---------- ingest ----------

def ingest_tournaments(con, since_year: int) -> list[dict]:
    """Turnaje hlavních lig od roku → pro_tournaments. Vrací řádky z DB."""
    leagues = ",".join(f"'{_esc(x)}'" for x in MAJOR_LEAGUES)
    rows = cargo_query_all(
        "Tournaments", "Name,OverviewPage,League,Year,DateStart,Date",
        f"League IN ({leagues}) AND Year >= {since_year}",
        order_by="DateStart")
    for t in rows:
        if not t.get("OverviewPage") or not t.get("DateStart"):
            continue
        con.execute(
            "INSERT OR REPLACE INTO pro_tournaments VALUES (?,?,?,?,?,?,?)",
            (t["OverviewPage"], t.get("Name"), t.get("League"),
             int(t["Year"]) if t.get("Year") else None,
             t.get("DateStart"), t.get("Date"),
             int("playoff" in (t.get("Name") or "").lower())))
    con.commit()
    return [dict(r) for r in con.execute(
        "SELECT * FROM pro_tournaments ORDER BY date_start")]


GAME_FIELDS = ("GameId,MatchId,Tournament,DateTime_UTC,Patch,"
               "Team1,Team2,WinTeam,Gamelength_Number,"
               "Team1Picks,Team2Picks,Team1Bans,Team2Bans,"
               "Team1Players,Team2Players,Team1Kills,Team2Kills")

PLAYER_FIELDS = "GameId,Link,Team,Champion,Kills,Deaths,Assists,Role,PlayerWin"

MATCH_FIELDS = ("MatchId,Team1,Team2,Winner,Team1Score,Team2Score,"
                "DateTime_UTC,Round,Tab,BestOf")


def ingest_tournament(con, t: dict) -> int:
    """Hry + hráči + série jednoho turnaje (klíč OverviewPage)."""
    op = t["overview_page"]
    where = f"OverviewPage='{_esc(op)}'"

    games = cargo_query_all("ScoreboardGames", GAME_FIELDS, where,
                            order_by="DateTime_UTC")
    n = 0
    for g in games:
        if not g.get("GameId") or not g.get("WinTeam"):
            continue
        con.execute(
            "INSERT OR REPLACE INTO pro_games"
            " (game_id, match_id, league, tournament, date, patch, team1, team2,"
            "  winner, duration, team1_picks, team2_picks, team1_bans, team2_bans,"
            "  players, overview_page, team1_kills, team2_kills)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (g["GameId"], g.get("MatchId"), t["league"], g.get("Tournament"),
             g.get("DateTime UTC"), g.get("Patch"),
             g.get("Team1"), g.get("Team2"), g.get("WinTeam"),
             float(g.get("Gamelength Number") or 0),
             g.get("Team1Picks"), g.get("Team2Picks"),
             g.get("Team1Bans"), g.get("Team2Bans"),
             f"{g.get('Team1Players', '')}|{g.get('Team2Players', '')}",
             op,
             int(g["Team1Kills"]) if g.get("Team1Kills") else None,
             int(g["Team2Kills"]) if g.get("Team2Kills") else None))
        n += 1

    for p in cargo_query_all("ScoreboardPlayers", PLAYER_FIELDS, where):
        if not p.get("GameId") or not p.get("Link"):
            continue
        con.execute(
            "INSERT OR REPLACE INTO pro_player_games VALUES (?,?,?,?,?,?,?,?,?)",
            (p["GameId"], p["Link"], p.get("Team"), p.get("Champion"),
             int(p["Kills"] or 0), int(p["Deaths"] or 0), int(p["Assists"] or 0),
             p.get("Role"), int(p.get("PlayerWin") == "Yes")))

    for m in cargo_query_all("MatchSchedule", MATCH_FIELDS, where,
                             order_by="DateTime_UTC"):
        if not m.get("MatchId") or not m.get("Team1"):
            continue
        winner = {"1": m.get("Team1"), "2": m.get("Team2")}.get(m.get("Winner"))
        con.execute(
            "INSERT OR REPLACE INTO pro_matches VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (m["MatchId"], op, m.get("Round"), m.get("Tab"),
             int(m["BestOf"]) if m.get("BestOf") else None,
             m.get("Team1"), m.get("Team2"),
             int(m["Team1Score"] or 0), int(m["Team2Score"] or 0),
             winner, m.get("DateTime UTC")))

    con.commit()
    return n


def load_patch_changes(con):
    """Ručně kurátorovaný data/patch_changes.toml → tabulka patch_changes."""
    path = ROOT / "data" / "patch_changes.toml"
    if not path.exists():
        return 0
    data = tomllib.loads(path.read_text())
    n = 0
    for ch in data.get("change", []):
        con.execute("INSERT OR REPLACE INTO patch_changes VALUES (?,?,?,?)",
                    (ch["patch"], ch["champion"], ch.get("kind"), ch.get("note")))
        n += 1
    con.commit()
    return n


def _is_done(con, t: dict) -> bool:
    """Ukončený turnaj s daty v DB se nepřestahovává."""
    today = datetime.date.today().isoformat()
    if not t["date_end"] or t["date_end"] >= today:
        return False
    return con.execute("SELECT 1 FROM pro_games WHERE overview_page = ? LIMIT 1",
                       (t["overview_page"],)).fetchone() is not None


def ingest(since_year: int = 2023, only: str | None = None):
    con = db.connect(str(ROOT / "lol.db"))
    login()
    print(f"patch_changes: {load_patch_changes(con)} záznamů", flush=True)
    tournaments = ingest_tournaments(con, since_year)
    print(f"turnajů celkem: {len(tournaments)}", flush=True)
    for t in tournaments:
        if only and only not in t["overview_page"]:
            continue
        if _is_done(con, t):
            continue
        print(f"== {t['name']} ==", flush=True)
        try:
            print(f"   uloženo {ingest_tournament(con, t)} her", flush=True)
        except Exception as e:
            print(f"   CHYBA: {e}", flush=True)
    total = con.execute("SELECT COUNT(*) FROM pro_games").fetchone()[0]
    print(f"HOTOVO — pro_games: {total} her", flush=True)
    con.close()


def update():
    """Dotáhne jen neukončené/nové turnaje (od loňska, kvůli přelomu roku)."""
    ingest(datetime.date.today().year - 1)


if __name__ == "__main__":
    from lol.verify import load_env
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "leagues":
        login()
        list_leagues(int(sys.argv[2]) if len(sys.argv) > 2 else 2025)
    elif cmd == "ingest":
        ingest(int(sys.argv[2]) if len(sys.argv) > 2 else 2023,
               only=sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "update":
        update()
    else:
        sys.exit("Použití: python -m lol.leaguepedia <leagues|ingest [rok] [filtr]|update>")
