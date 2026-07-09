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


_MATCH_COLS = ("match_id", "overview_page", "round", "tab", "best_of", "team1",
               "team2", "team1_score", "team2_score", "winner", "date",
               "group_name", "n_tab_in_page", "n_match_in_tab", "n_page")


def _match(con, op=OP, group_name=None, n_tab_in_page=None,
          n_match_in_tab=None, n_page=None, **kwargs):
    """Insert one pro_matches row. Bracket-ordinal fields default to None,
    matching today's real (un-migrated) data unless a test opts in."""
    row = {"overview_page": op, "round": "", "tab": "", "best_of": 5,
           "team1_score": 0, "team2_score": 0, "winner": None, "date": "",
           "group_name": group_name, "n_tab_in_page": n_tab_in_page,
           "n_match_in_tab": n_match_in_tab, "n_page": n_page}
    row.update(kwargs)
    con.execute(
        f"INSERT INTO pro_matches ({', '.join(_MATCH_COLS)}) VALUES"
        f" ({', '.join('?' * len(_MATCH_COLS))})",
        tuple(row[c] for c in _MATCH_COLS))


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


OP_MIX = "LTA North/2025 Season/Split 3"


def test_tournament_phases_splits_season_and_playoff_without_authoritative_fields():
    """Type C shape (LTA-style): 3 round-robin weeks + a bracket in one
    overview_page, with NO group_name/n_tab_in_page set — matching today's
    real DB state (no live re-ingest has populated those fields yet). Must
    still produce exactly two clean phases without Week/Round interleaving,
    and the structural (matches-per-team) heuristic must tell them apart."""
    con = db.connect(":memory:")
    # Week 1-3: round-robin, each team plays a different opponent each week.
    _match(con, op=OP_MIX, match_id="W1a", tab="Week 1", team1="A", team2="B",
           winner="A", team1_score=2, team2_score=0, date="2025-01-01")
    _match(con, op=OP_MIX, match_id="W1b", tab="Week 1", team1="C", team2="D",
           winner="C", team1_score=2, team2_score=1, date="2025-01-01")
    _match(con, op=OP_MIX, match_id="W2a", tab="Week 2", team1="A", team2="C",
           winner="C", team1_score=0, team2_score=2, date="2025-01-08")
    _match(con, op=OP_MIX, match_id="W2b", tab="Week 2", team1="B", team2="D",
           winner="B", team1_score=2, team2_score=1, date="2025-01-08")
    _match(con, op=OP_MIX, match_id="W3a", tab="Week 3", team1="A", team2="D",
           winner="A", team1_score=2, team2_score=0, date="2025-01-15")
    _match(con, op=OP_MIX, match_id="W3b", tab="Week 3", team1="B", team2="C",
           winner="B", team1_score=2, team2_score=1, date="2025-01-15")
    # Round 1 + Finals: elimination bracket, each team plays exactly one
    # match per round — the signal that distinguishes it from the weeks above.
    _match(con, op=OP_MIX, match_id="R1a", tab="Round 1", team1="A", team2="D",
           winner="A", team1_score=3, team2_score=1, date="2025-02-01")
    _match(con, op=OP_MIX, match_id="R1b", tab="Round 1", team1="B", team2="C",
           winner="B", team1_score=3, team2_score=2, date="2025-02-01")
    _match(con, op=OP_MIX, match_id="F1", tab="Finals", team1="A", team2="B",
           winner="A", team1_score=3, team2_score=2, date="2025-02-08")
    con.commit()

    phases = prostats.tournament_phases(con, OP_MIX)
    assert [p["kind"] for p in phases] == ["standings", "bracket"]
    season, playoff = phases
    assert season["label"] == "Základní část"
    assert [r["round"] for r in season["rounds"]] == ["Week 1", "Week 2", "Week 3"]
    assert playoff["label"] == "Play-off"
    assert [r["round"] for r in playoff["rounds"]] == ["Round 1", "Finals"]

    # A: won wk1 (vs B), lost wk2 (vs C), won wk3 (vs D) -> 2-1 in the season phase
    standings = {s["team"]: s for s in season["standings"]}
    assert standings["A"]["wins"] == 2 and standings["A"]["losses"] == 1
    assert round(standings["A"]["winrate"], 1) == round(200 / 3, 1)
    assert playoff["standings"] == []


def test_bracket_sections_splits_double_elim_upper_lower_final():
    """4-team double-elim: A beats B and C beats D in Round 1 (both losers get
    a second life -> upper); B beats D in Round 2 (lower bracket, D truly
    eliminated -> lower); A beats C in Round 3 (upper final, C drops to lower
    -> upper); C beats B in Round 4 (lower final, B truly eliminated ->
    lower); Finals is the sole decider -> forced GF regardless of the loop's
    own tag. Mirrors the real LEC/LPL 2026 playoff shape verified live."""
    con = db.connect(":memory:")
    _match(con, match_id="R1a", tab="Round 1", n_tab_in_page=1, n_match_in_tab=1,
           team1="A", team2="B", winner="A", date="2025-02-01")
    _match(con, match_id="R1b", tab="Round 1", n_tab_in_page=1, n_match_in_tab=2,
           team1="C", team2="D", winner="C", date="2025-02-01")
    _match(con, match_id="R2", tab="Round 2", n_tab_in_page=2, n_match_in_tab=1,
           team1="B", team2="D", winner="B", date="2025-02-02")
    _match(con, match_id="R3", tab="Round 3", n_tab_in_page=3, n_match_in_tab=1,
           team1="A", team2="C", winner="A", date="2025-02-03")
    _match(con, match_id="R4", tab="Round 4", n_tab_in_page=4, n_match_in_tab=1,
           team1="C", team2="B", winner="C", date="2025-02-04")
    _match(con, match_id="GF", tab="Finals", n_tab_in_page=5, n_match_in_tab=1,
           team1="A", team2="C", winner="C", date="2025-02-05")
    con.commit()

    phases = prostats.tournament_phases(con, OP)
    assert len(phases) == 1
    sec = phases[0]["sections"]
    assert sec["double"] is True
    assert [r["round"] for r in sec["upper"]] == ["Round 1", "Round 3"]
    assert [m["match_id"] for r in sec["upper"] for m in r["matches"]] == \
        ["R1a", "R1b", "R3"]
    assert [r["round"] for r in sec["lower"]] == ["Round 2", "Round 4"]
    assert [m["match_id"] for r in sec["lower"] for m in r["matches"]] == \
        ["R2", "R4"]
    assert [m["match_id"] for r in sec["final"] for m in r["matches"]] == ["GF"]
    # spojnice jen uvnitř sekce, nikdy napříč (upper feeds upper, lower feeds lower)
    r3 = sec["upper"][1]["matches"][0]
    assert sorted(r3["feeds_from"]) == ["R1a", "R1b"]
    # R4's other feeder (upper final loser C) isn't tracked: connectors are
    # computed per-section, and C's prior match lives in the upper section's
    # own match list, not lower's — the upper->lower drop-in line is
    # deliberately not drawn (see `_bracket_sections` docstring).
    r4 = sec["lower"][1]["matches"][0]
    assert r4["feeds_from"] == ["R2"]
    assert sec["final"][0]["matches"][0]["feeds_from"] == []


def test_bracket_sections_single_elim_stays_one_tree():
    """No loser ever gets a second life -> not double-elim, sections falls
    back to the plain single-tree shape (sections["rounds"] == input)."""
    con = db.connect(":memory:")
    _match(con, match_id="M1", tab="Round 1", team1="A", team2="B",
           winner="A", date="2025-02-01")
    _match(con, match_id="M2", tab="Finals", team1="A", team2="C",
           winner="A", date="2025-02-08")
    con.commit()
    phases = prostats.tournament_phases(con, OP)
    sec = phases[0]["sections"]
    assert sec["double"] is False
    assert [r["round"] for r in sec["rounds"]] == ["Round 1", "Finals"]


def test_tournament_phases_keeps_play_in_separate_from_main_bracket():
    """Play-In (gauntlet: losers CAN reappear, but its last round has TWO
    parallel deciders promoting two different teams -> no single champion to
    converge on -> single tree, not upper/lower) must stay its own phase, not
    merge into the main bracket that follows — a Play-In loss must not carry
    over as a bracket-loss into the main bracket's upper/lower
    reconstruction. Regression for the real LPL/LCK 2026 shape verified live."""
    con = db.connect(":memory:")
    _match(con, match_id="P1a", tab="Play-In Round 1", n_tab_in_page=1,
           n_match_in_tab=1, team1="E", team2="F", winner="E", date="2025-02-01")
    _match(con, match_id="P1b", tab="Play-In Round 1", n_tab_in_page=1,
           n_match_in_tab=2, team1="I", team2="J", winner="I", date="2025-02-01")
    _match(con, match_id="P2a", tab="Play-In Round 2", n_tab_in_page=2,
           n_match_in_tab=1, team1="G", team2="H", winner="H", date="2025-02-02")
    _match(con, match_id="P2b", tab="Play-In Round 2", n_tab_in_page=2,
           n_match_in_tab=2, team1="K", team2="L", winner="L", date="2025-02-02")
    _match(con, match_id="P3a", tab="Play-In Round 3", n_tab_in_page=3,
           n_match_in_tab=1, team1="F", team2="H", winner="F", date="2025-02-03")
    _match(con, match_id="P3b", tab="Play-In Round 3", n_tab_in_page=3,
           n_match_in_tab=2, team1="J", team2="L", winner="J", date="2025-02-03")
    _match(con, match_id="B1", tab="Round 1", n_tab_in_page=4, n_match_in_tab=1,
           team1="A", team2="E", winner="A", date="2025-02-10")
    _match(con, match_id="B2", tab="Finals", n_tab_in_page=5, n_match_in_tab=1,
           team1="A", team2="F", winner="F", date="2025-02-15")
    con.commit()

    phases = prostats.tournament_phases(con, OP)
    assert [p["label"] for p in phases] == ["Play-In", "Play-off"]
    assert all(p["kind"] == "bracket" for p in phases)
    play_in, playoff = phases
    assert play_in["sections"]["double"] is False
    assert [r["round"] for r in play_in["sections"]["rounds"]] == \
        ["Play-In Round 1", "Play-In Round 2", "Play-In Round 3"]
    assert [r["round"] for r in playoff["rounds"]] == ["Round 1", "Finals"]


def test_tournament_phases_orders_by_page_before_tab_ordinal():
    """First Stand shape: `n_tab_in_page` RESETS per Leaguepedia page break,
    so a group-stage round on page 1 and a bracket round on page 2 can share
    the same ordinal (both "tab 1") and must not tie/interleave — page must
    lead the sort. Regression for the real fragmentation bug found live
    (5 phases instead of 2) before `n_page` was added to `_round_sort_key`."""
    con = db.connect(":memory:")
    _match(con, match_id="G1", tab="Groups Day 1", n_page=1, n_tab_in_page=1,
           n_match_in_tab=1, team1="A", team2="B", winner="A", date="2025-03-16")
    _match(con, match_id="G2", tab="Groups Day 2", n_page=1, n_tab_in_page=2,
           n_match_in_tab=1, team1="C", team2="D", winner="C", date="2025-03-17")
    _match(con, match_id="S1", tab="Semifinals", n_page=2, n_tab_in_page=1,
           n_match_in_tab=1, team1="A", team2="C", winner="A", date="2025-03-20")
    _match(con, match_id="F1", tab="Finals", n_page=2, n_tab_in_page=2,
           n_match_in_tab=1, team1="A", team2="E", winner="A", date="2025-03-22")
    con.commit()

    phases = prostats.tournament_phases(con, OP)
    assert [p["kind"] for p in phases] == ["standings", "bracket"]
    groups, playoff = phases
    assert [r["round"] for r in groups["rounds"]] == ["Groups Day 1", "Groups Day 2"]
    assert [r["round"] for r in playoff["rounds"]] == ["Semifinals", "Finals"]


def test_tournament_phases_single_shape_is_one_phase():
    """A plain playoff-only page (Type B) must stay a single bracket phase —
    no spurious splitting when there's nothing to split."""
    con = db.connect(":memory:")
    _match(con, op=OP, match_id="M1", tab="Round 1", team1="G2", team2="FNC",
           winner="G2", team1_score=3, team2_score=1, date="2025-02-15")
    _match(con, op=OP, match_id="M2", tab="Finals", team1="G2", team2="KC",
           winner="KC", team1_score=2, team2_score=3, date="2025-03-01")
    con.commit()
    phases = prostats.tournament_phases(con, OP)
    assert len(phases) == 1
    assert phases[0]["kind"] == "bracket"
    assert phases[0]["label"] == "Play-off"
