from lol import db
from lol.tracker import live_game_event

MATCH = {
    "metadata": {"matchId": "EUN1_1"},
    "info": {
        "gameCreation": 1700000000000, "gameDuration": 1600,
        "queueId": 420, "gameVersion": "16.13.1",
        "participants": [
            {"puuid": "p1", "riotIdGameName": "Teddy", "riotIdTagline": "TAG",
             "championName": "Jayce", "kills": 8, "deaths": 5, "assists": 11,
             "win": False, "teamPosition": "TOP",
             "totalMinionsKilled": 200, "neutralMinionsKilled": 12,
             "goldEarned": 12000},
            {"puuid": "p2", "riotIdGameName": "Enemy", "riotIdTagline": "X",
             "championName": "Garen", "kills": 5, "deaths": 8, "assists": 2,
             "win": True, "teamPosition": "TOP",
             "totalMinionsKilled": 180, "neutralMinionsKilled": 0,
             "goldEarned": 11000},
        ],
    },
}


def test_insert_match_stores_all_participants():
    con = db.connect(":memory:")
    db.insert_match(con, MATCH)
    db.insert_match(con, MATCH)  # idempotence
    rows = con.execute("SELECT * FROM match_participants ORDER BY puuid").fetchall()
    assert len(rows) == 2
    assert rows[0]["champion"] == "Jayce"
    assert rows[0]["cs"] == 212
    assert con.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1


def test_live_game_event_fires_once_per_game():
    con = db.connect(":memory:")
    live = {"gameId": 42, "participants": [{"puuid": "p1", "championId": 126}]}
    assert live_game_event(con, "p1", live)["champion_id"] == 126
    assert live_game_event(con, "p1", live) is None          # stejná hra -> ticho
    assert live_game_event(con, "p1", None) is None          # dohráno -> smazat
    assert live_game_event(con, "p1", live)["game_id"] == 42  # nová hra -> event
