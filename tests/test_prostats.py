from lol import db, prostats

OP = "LEC/2025 Season/Winter Playoffs"


def _game(con, gid, mid, team1="G2", team2="FNC", winner="G2",
          picks1="Ambessa,Vi,Taliyah,Corki,Rell", picks2="Rumble,Maokai,Azir,Ezreal,Alistar",
          bans1="Yone,Akali,LeBlanc", bans2="Skarner,Aurora,Kalista",
          date="2025-02-15 17:00:00", patch="25.03"):
    con.execute(
        "INSERT INTO pro_games (game_id, match_id, league, tournament, date, patch,"
        " team1, team2, winner, duration, team1_picks, team2_picks, team1_bans,"
        " team2_bans, players, overview_page, team1_kills, team2_kills)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (gid, mid, "LoL EMEA Championship", "LEC 2025 Winter Playoffs", date, patch,
         team1, team2, winner, 32.5, picks1, picks2, bans1, bans2, "", OP, 20, 9))


def _setup():
    con = db.connect(":memory:")
    con.execute("INSERT INTO pro_tournaments VALUES (?,?,?,?,?,?,?)",
                (OP, "LEC 2025 Winter Playoffs", "LoL EMEA Championship", 2025,
                 "2025-02-15", "2025-03-02", 1))
    _game(con, "G1", "M1")
    _game(con, "G2", "M1", winner="FNC", picks1="Ambessa,Vi,Yone,Corki,Rell",
          picks2="Gnar,Maokai,Azir,Ezreal,Alistar", bans1="Akali,LeBlanc,Zed",
          date="2025-02-15 18:00:00")
    con.commit()
    return con


def test_tournament_champs_presence_and_winrate():
    con = _setup()
    champs = {c["champion"]: c for c in prostats.tournament_champs(con, OP)}
    amb = champs["Ambessa"]
    assert amb["picks"] == 2 and amb["bans"] == 0
    assert amb["presence"] == 100.0
    assert amb["winrate"] == 50.0          # 1 výhra z 2 picků (G2 vyhrálo G1)
    # Yone: 1 ban (G1) + 1 pick (G2, prohraný G2 týmem... pick1 patří G2=team1, vyhrál FNC)
    assert champs["Yone"]["presence"] == 100.0
    assert champs["Yone"]["winrate"] == 0.0


def test_bracket_groups_by_round():
    con = _setup()
    con.execute("INSERT INTO pro_matches (match_id, overview_page, round, tab, best_of,"
                " team1, team2, team1_score, team2_score, winner, date) VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?)",
                ("M1", OP, "Semifinals", "1", 5, "G2", "FNC", 3, 1, "G2",
                 "2025-02-15 17:00:00"))
    con.execute("INSERT INTO pro_matches (match_id, overview_page, round, tab, best_of,"
                " team1, team2, team1_score, team2_score, winner, date) VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?)",
                ("M2", OP, "Finals", "2", 5, "G2", "KC", 3, 2, "G2",
                 "2025-03-01 17:00:00"))
    con.commit()
    b = prostats.bracket(con, OP)
    assert [r["round"] for r in b] == ["Semifinals", "Finals"]
    assert b[0]["matches"][0]["winner"] == "G2"


def test_bracket_feeds_from_team_progression():
    con = _setup()
    rows = [
        ("S1", OP, "Semifinals", "1", 5, "G2", "FNC", 3, 1, "G2", "2025-02-15"),
        ("S2", OP, "Semifinals", "1", 5, "KC", "BDS", 3, 0, "KC", "2025-02-16"),
        ("F1", OP, "Finals", "2", 5, "KC", "G2", 3, 2, "KC", "2025-03-01"),
    ]
    for r in rows:
        con.execute("INSERT INTO pro_matches (match_id, overview_page, round, tab, best_of,"
                " team1, team2, team1_score, team2_score, winner, date) VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?)", r)
    con.commit()
    b = prostats.bracket(con, OP)
    semis = {m["match_id"]: m for m in b[0]["matches"]}
    final = b[1]["matches"][0]
    assert semis["S1"]["feeds_from"] == []
    assert sorted(final["feeds_from"]) == ["S1", "S2"]


def test_bracket_group_stage_vs_bracket_and_authoritative_order():
    """MSI-like mix: a round-robin group stage (Play-In) followed by an
    elimination bracket, both under one overview_page. Play-In's tab name has
    no digit/quarter/semi/final keyword, so the old name-only heuristic would
    sort it LAST — n_tab_in_page must override that and put it first. Within
    the bracket round, n_match_in_tab must override date order too."""
    con = _setup()
    cols = ("match_id", "overview_page", "round", "tab", "best_of", "team1", "team2",
            "team1_score", "team2_score", "winner", "date", "group_name",
            "n_tab_in_page", "n_match_in_tab", "n_page")
    rows = [
        dict(match_id="P1", overview_page=OP, round="", tab="Play-In",
             best_of=1, team1="G2", team2="FNC", team1_score=1, team2_score=0,
             winner="G2", date="2025-02-10", group_name="Group A",
             n_tab_in_page=1, n_match_in_tab=1, n_page=1),
        dict(match_id="P2", overview_page=OP, round="", tab="Play-In",
             best_of=1, team1="KC", team2="BDS", team1_score=1, team2_score=0,
             winner="KC", date="2025-02-11", group_name="Group A",
             n_tab_in_page=1, n_match_in_tab=2, n_page=1),
        # n_match_in_tab (1,2) deliberately conflicts with date order (later, earlier)
        # to prove the authoritative field wins over the date fallback.
        dict(match_id="B2", overview_page=OP, round="", tab="Bracket Round 1",
             best_of=5, team1="KC", team2="BDS", team1_score=3, team2_score=1,
             winner="KC", date="2025-02-19", group_name=None,
             n_tab_in_page=2, n_match_in_tab=2, n_page=1),
        dict(match_id="B1", overview_page=OP, round="", tab="Bracket Round 1",
             best_of=5, team1="G2", team2="FNC", team1_score=3, team2_score=0,
             winner="G2", date="2025-02-20", group_name=None,
             n_tab_in_page=2, n_match_in_tab=1, n_page=1),
    ]
    for r in rows:
        con.execute(
            f"INSERT INTO pro_matches ({', '.join(cols)}) VALUES"
            f" ({', '.join('?' * len(cols))})", tuple(r[c] for c in cols))
    con.commit()

    b = prostats.bracket(con, OP)
    assert [r["round"] for r in b] == ["Play-In", "Bracket Round 1"]
    play_in, bracket_round = b
    assert play_in["is_bracket"] is False
    assert bracket_round["is_bracket"] is True
    assert [m["match_id"] for m in bracket_round["matches"]] == ["B1", "B2"]


def test_series_games_with_players():
    con = _setup()
    for player, champ, role in (("Caps", "Taliyah", "Mid"), ("BrokenBlade", "Ambessa", "Top")):
        con.execute("INSERT INTO pro_player_games VALUES (?,?,?,?,?,?,?,?,?)",
                    ("G1", player, "G2", champ, 3, 1, 7, role, 1))
    con.commit()
    games = prostats.series_games(con, "M1")
    assert len(games) == 2
    assert [p["player"] for p in games[0]["players"]] == ["BrokenBlade", "Caps"]


def test_event_meta_shift_uses_baseline_and_patch_changes():
    con = _setup()
    # baseline hra mimo event, 30 dní před startem
    con.execute(
        "INSERT INTO pro_games (game_id, match_id, league, date, patch, team1,"
        " team2, winner, team1_picks, team2_picks, team1_bans, team2_bans,"
        " overview_page) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("B1", "BM1", "LoL Champions Korea", "2025-01-20 08:00:00", "25.01",
         "T1", "GEN", "T1", "Rumble,A,B,C,D", "E,F,G,H,I", "", "", "LCK/x"))
    con.execute("INSERT INTO patch_changes VALUES (?,?,?,?)",
                ("25.03", "Ambessa", "nerf", "Q damage down"))
    con.commit()
    shift = prostats.event_meta_shift(con, OP)
    assert shift["baseline_games"] == 1 and shift["event_games"] == 2
    rows = {r["champion"]: r for r in shift["rows"]}
    assert rows["Ambessa"]["kind"] == "nerf"
    assert rows["Ambessa"]["delta"] == 100.0        # 0 % před, 100 % na eventu
    assert rows["Rumble"]["before"] == 100.0 and rows["Rumble"]["at"] == 50.0
    assert rows["Rumble"]["delta"] == -50.0
