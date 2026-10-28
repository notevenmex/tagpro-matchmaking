from app.rating import elo_change


def test_even_match_is_25_and_gap_scales_it():
    assert elo_change(1000, 1000, True) == 25 and elo_change(1000, 1000, False) == -25
    assert elo_change(1000, 1200, True) > 25 > elo_change(1200, 1000, True)  # underdog gains more
    assert elo_change(1200, 1000, False) < -25 < elo_change(1000, 1200, False)  # favourite loses more
    assert elo_change(3000, 1000, True) == 1  # always at least 1
    assert elo_change(1000, 1000, True, placement=True) == 38


def test_gap_adjustments():
    from app.rating import adjust
    assert adjust(True, 1000, 1000) == 1 and adjust(False, 1000, 1000) == 1  # at the lobby average: unchanged
    assert adjust(True, 1200, 1400) == 1.25 and adjust(False, 1200, 1400) == 0.75  # 200 below: +25% / -25%
    assert adjust(True, 1000, 1600) == 1.5 and adjust(False, 1000, 1600) == 0.5  # capped at 400 below
    assert adjust(True, 1800, 1400) == 1.25 and adjust(False, 1800, 1400) == 0.75  # 400 above: capped at 25%
    assert adjust(True, 1000, 1600, hot=0.75) == 2.0  # wins never more than double
    assert elo_change(1000, 1000, True, mult=1.5) == 38 and elo_change(1000, 1000, False, mult=0.5) == -12


def test_hot_streak_accelerates_wins_in_higher_rated_lobbies():
    from sqlalchemy import select
    from app.db import Player, Season, SessionLocal
    from app.rating import HOT_MAX, hot_streak, record_match
    with SessionLocal() as db:
        s = db.scalar(select(Season).where(Season.is_active.is_(True)))
        players = []
        for i in range(8):
            p = Player(tagpro_name=f"Hot{i}", position="offense", tags=[])
            db.add(p)
            players.append(p)
        db.flush()
        from app.rating import get_rating
        for i, p in enumerate(players):
            get_rating(db, p, s).elo = 1000 if i == 0 else 1400  # Hot0 keeps playing up
        assert hot_streak(db, players[0], s) == 0
        for _ in range(4):  # Hot0's team wins four games in a row against 1400s
            record_match(db, s, winner="red", reported_by="test",
                         lineup=[(p, "red" if i < 4 else "blue", "offense") for i, p in enumerate(players)])
        assert abs(hot_streak(db, players[0], s) - HOT_MAX) < 1e-9
        m, changes = record_match(db, s, winner="red", reported_by="test",
                                  lineup=[(p, "red" if i < 4 else "blue", "offense") for i, p in enumerate(players)])
        mine = next(c for c in changes if c["name"] == "Hot0")
        assert mine["multiplier"] == 2.6  # gap bonus and hot streak capped at double, then +30% for a 5th win in a row
        db.rollback()


def test_momentum_from_win_and_loss_streaks():
    from app.rating import adjust, momentum
    assert momentum(1, True) == 0 and momentum(2, True) == 0.1  # 3rd win in a row: +10%
    assert abs(momentum(4, True) - 0.3) < 1e-9
    assert abs(momentum(20, True) - 1.9) < 1e-9 and momentum(-20, False) == 0.5  # wins keep growing, losses cap
    assert momentum(-3, False) == momentum(3, True) and momentum(3, False) == 0  # a win snaps a losing streak
    assert abs(adjust(False, 1000, 1000, streak=-3) - 1.2) < 1e-9  # 4th loss in a row costs 20% more
    assert adjust(False, 1000, 1000, streak=-9) == 1.5  # losses never more than 1.5x
    assert abs(adjust(False, 1000, 1400, streak=-9) - 0.75) < 1e-9  # the underdog cushion still applies
    assert abs(adjust(True, 1000, 1600, hot=0.75, streak=9) - 2.0 * 1.8) < 1e-9  # momentum stacks on the 2x cap
    assert abs(adjust(True, 1800, 1000, streak=9) - 1.25 * 1.8) < 1e-9  # and isn't held back at the top


def test_current_streak_counts_results_in_a_row():
    from sqlalchemy import select
    from app.db import Player, Season, SessionLocal
    from app.rating import current_streak, record_match
    with SessionLocal() as db:
        s = db.scalar(select(Season).where(Season.is_active.is_(True)))
        players = [Player(tagpro_name=f"Streak{i}", position="offense", tags=[]) for i in range(8)]
        db.add_all(players)
        db.flush()
        lineup = [(p, "red" if i < 4 else "blue", "offense") for i, p in enumerate(players)]
        for w in ["blue", "red", "red", "red"]:
            record_match(db, s, winner=w, reported_by="test", lineup=lineup)
        assert current_streak(db, players[0], s) == 3 and current_streak(db, players[7], s) == -3
        from app.rating import get_rating
        for p in players:  # level the lobby so only momentum moves the multiplier
            get_rating(db, p, s).elo = 1000
        _, changes = record_match(db, s, winner="red", reported_by="test", lineup=lineup)
        by = {c["name"]: c for c in changes}
        assert by["Streak0"]["multiplier"] == 1.2 and by["Streak7"]["multiplier"] == 1.2  # 4th in a row: +20%
        assert by["Streak0"]["change"] == 45 and by["Streak7"]["change"] == -45  # even teams in placements: 25 * 1.5 * 1.2
        db.rollback()
