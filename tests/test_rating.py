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
        assert mine["multiplier"] == 2.0  # gap bonus and hot streak, capped at double
        db.rollback()
