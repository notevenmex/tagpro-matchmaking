from app.rating import elo_change


def test_even_match_is_25_and_gap_scales_it():
    assert elo_change(1000, 1000, True) == 25 and elo_change(1000, 1000, False) == -25
    assert elo_change(1000, 1200, True) > 25 > elo_change(1200, 1000, True)  # underdog gains more
    assert elo_change(1200, 1000, False) < -25 < elo_change(1000, 1200, False)  # favourite loses more
    assert elo_change(3000, 1000, True) == 1  # always at least 1
    assert elo_change(1000, 1000, True, placement=True) == 38
