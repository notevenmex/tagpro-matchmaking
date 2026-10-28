from app.rating import elo_change, level, level_range


def test_levels_match_faceit_thresholds():
    assert [level(e) for e in (100, 500, 501, 900, 1000, 1050, 1051, 1530, 1531, 2000, 2001, 3500)] == \
        [1, 1, 2, 3, 4, 4, 5, 7, 8, 9, 10, 10]
    assert level_range(4) == (901, 1050) and level_range(10) == (2001, None)


def test_even_match_is_25_and_gap_scales_it():
    assert elo_change(1000, 1000, True) == 25 and elo_change(1000, 1000, False) == -25
    assert elo_change(1000, 1200, True) > 25 > elo_change(1200, 1000, True)  # underdog gains more
    assert elo_change(1200, 1000, False) < -25 < elo_change(1000, 1200, False)  # favourite loses more
    assert elo_change(3000, 1000, True) == 1  # always at least 1
    assert elo_change(1000, 1000, True, placement=True) == 38
