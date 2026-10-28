"""FACEIT-style Elo. One rating per player per season; every player on a team moves by the
same amount, based on the two teams' average Elo.

How it maps to FACEIT:
- Everyone starts at 1000 Elo (FACEIT's default for new accounts).
- A match between evenly matched teams is worth about ±25. The underdog gains more and loses
  less; the favourite gains less and loses more. Every result moves you at least 1 point.
- The site shows the Elo number itself (no FACEIT-style levels). Elo can't drop below 100.
- The first 10 matches of a season are placements: they move Elo 1.5x as far, and your Elo
  stays hidden (and you stay off the Top 100) until they're done.
Tune the constants here.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Match, MatchPlayer, Player, Rating, Season

START_ELO = 1000
K = 50                     # an even match is worth K / 2 = 25
PLACEMENT_GAMES = 10       # matches before your Elo shows and you appear on the Top 100
PLACEMENT_MULTIPLIER = 1.5
ELO_FLOOR = 100
MIN_GAMES = PLACEMENT_GAMES

def expected(a: float, b: float) -> float:
    return 1 / (1 + 10 ** ((b - a) / 400))


def elo_change(own_avg: float, opp_avg: float, won: bool, placement: bool = False) -> int:
    """Whole-number Elo change for one player, from the two team averages."""
    k = K * (PLACEMENT_MULTIPLIER if placement else 1)
    e = expected(own_avg, opp_avg)
    d = max(1, round(k * ((1 - e) if won else e)))
    return d if won else -d


def get_rating(db: Session, player: Player, season: Season) -> Rating:
    r = db.scalar(select(Rating).where(Rating.player_id == player.id, Rating.season_id == season.id))
    if not r:
        r = Rating(player_id=player.id, season_id=season.id, elo=START_ELO, games=0, wins=0)
        db.add(r)
        db.flush()
    return r


def rating_out(r: Rating | None) -> dict:
    """What the site shows for a rating. Level is hidden until placements are done."""
    games = r.games if r else 0
    placed = games >= PLACEMENT_GAMES
    elo = r.elo if r else START_ELO
    return {"elo": elo if placed else None,
            "games": games, "wins": r.wins if r else 0,
            "placements_left": max(0, PLACEMENT_GAMES - games)}


def rating_for(db: Session, player: Player, season: Season) -> dict:
    """Rating summary for draft pools and captain picks (includes the hidden Elo as `true_elo`)."""
    r = db.scalar(select(Rating).where(Rating.player_id == player.id, Rating.season_id == season.id))
    return {**rating_out(r), "true_elo": r.elo if r else START_ELO}


def record_match(db: Session, season: Season, *, winner: str, lineup: list[tuple[Player, str, str]],
                 map_id: int | None = None, reported_by: str) -> tuple[Match, list[dict]]:
    """Save a finished match and update everyone's Elo. Doesn't commit; the caller does.

    lineup: (player, team "red" | "blue", position "offense" | "defense") for each player.
    """
    entries = [(p, team, pos, get_rating(db, p, season)) for p, team, pos in lineup]
    avg = {t: sum(r.elo for _, team, _, r in entries if team == t) / sum(1 for e in entries if e[1] == t)
           for t in ("red", "blue")}
    m = Match(season_id=season.id, map_id=map_id, winner=winner, reported_by=reported_by)
    db.add(m)
    db.flush()
    changes = []
    for p, team, pos, r in entries:
        before = r.elo
        won = team == winner
        opp = "blue" if team == "red" else "red"
        r.elo = max(ELO_FLOOR, before + elo_change(avg[team], avg[opp], won, r.games < PLACEMENT_GAMES))
        r.games += 1
        r.wins += int(won)
        db.add(MatchPlayer(match_id=m.id, player_id=p.id, team=team, position=pos,
                           elo_before=before, elo_after=r.elo))
        changes.append({"name": p.tagpro_name, "team": team, "before": before, "after": r.elo,
                        "change": r.elo - before})
    return m, changes
