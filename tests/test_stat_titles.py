from conftest import client

from app import titles
from app.db import Match, Player, PlayerStat, Season, SessionLocal


def seed_stats(season_no, lines):
    """lines: {name: (games, caps, returns, hold, tags, pops) season totals}."""
    with SessionLocal() as db:
        s = Season(number=season_no, is_active=False, banner="", banner_note="")
        db.add(s)
        db.flush()
        for name, (n, caps, rets, hold, tags, pops) in lines.items():
            p = Player(tagpro_name=name, position="offense", tags=[])
            db.add(p)
            db.flush()
            for g in range(n):  # spread the totals over n games, the rest on the first
                m = Match(season_id=s.id, winner="red", reported_by="test")
                db.add(m)
                db.flush()
                first = g == 0
                db.add(PlayerStat(match_id=m.id, player_id=p.id, season_id=s.id, tagpro_eu_id=1,
                                  caps=caps if first else 0, returns=rets if first else 0,
                                  hold=hold if first else 0, tags=tags if first else 0, pops=pops if first else 0))
        db.commit()


def test_stat_leaders_use_season_averages_and_min_games():
    n = titles.STAT_MIN_GAMES
    seed_stats(81, {
        "StatCapper": (n, 3 * n, 1 * n, 20 * n, 10, 10),       # 3 caps/game
        "StatReturner": (n, 1 * n, 9 * n, 10 * n, 40, 10),     # 9 returns/game, K/D 4.0
        "StatHolder": (n * 2, 0, 0, 90 * n * 2, 5, 5),         # 1:30 hold/game
        "StatFewGames": (n - 1, 99 * n, 99 * n, 999 * n, 999, 1),  # best numbers, too few games
    })
    a = client("admin-1")
    got = {t["stat"]: t for t in client().get("/api/titles", params={"season": 81}).json() if t.get("stat")}
    assert got["caps"]["player"] == "StatCapper" and got["caps"]["value"] == "3.00 caps per game"
    assert got["returns"]["player"] == "StatReturner"
    assert got["hold"]["player"] == "StatHolder" and got["hold"]["value"] == "1:30 hold per game"
    assert got["kd"]["player"] == "StatReturner" and got["kd"]["value"] == "4.00 K/D"

    # shown on the profile, with the stat in the hover text
    held = client().get("/api/players/StatCapper").json()["titles"]
    assert {"label": "S81 Cap Leader", "reward": "", "season": 81, "value": "3.00 caps per game"} in held

    # admins rename, set a reward, or turn a title off
    assert client("stat-player").patch("/api/admin/stat-titles/caps", json={"reward": "x"}).status_code == 403
    assert a.patch("/api/admin/stat-titles/caps", json={"title": "Top Capper", "reward": "Red flair"}).status_code == 200
    assert a.patch("/api/admin/stat-titles/kd", json={"enabled": False}).status_code == 200
    assert a.patch("/api/admin/stat-titles/nope", json={"enabled": False}).status_code == 422
    got = {t["stat"]: t for t in client().get("/api/titles", params={"season": 81}).json() if t.get("stat")}
    assert got["caps"]["title"] == "Top Capper" and got["caps"]["reward"] == "Red flair" and "kd" not in got
    cfg = a.get("/api/admin/stat-titles").json()
    assert cfg["min_games"] == n and len(cfg["titles"]) == 4
    a.patch("/api/admin/stat-titles/kd", json={"enabled": True})
    a.patch("/api/admin/stat-titles/caps", json={"title": "Cap Leader", "reward": ""})


def test_active_season_without_stats_says_who_qualifies():
    rows = [t for t in client().get("/api/titles", params={"season": 4}).json() if t.get("stat")]
    assert len(rows) == 4 and all(t["player"] is None and t["value"].startswith("Needs ") for t in rows)
