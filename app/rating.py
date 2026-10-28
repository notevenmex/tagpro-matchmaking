"""FACEIT-style Elo. One rating per player per season; every player on a team moves by the
same amount, based on the two teams' average Elo.

How it maps to FACEIT:
- Everyone starts at 1000 Elo (FACEIT's default for new accounts).
- A match between evenly matched teams is worth about ±25. The underdog gains more and loses
  less; the favourite gains less and loses more. Every result moves you at least 1 point.
- The site shows the Elo number itself (no FACEIT-style levels). Elo can't drop below 100.
- The first 10 matches of a season are placements: they move Elo 1.5x as far, and your Elo
  stays hidden (and you stay off the Top 100) until they're done.

On top of that, each player's own change is scaled (see adjust()):
- Below the lobby's average Elo: wins count for more and losses for less, so underrated
  players climb faster (up to +/-50% at 400 below the average).
- Above the lobby's average Elo: same idea but smaller (up to +/-25% at 400 above), so strong
  players aren't punished for playing in lower-rated games.
- Hot streak: if at least 4 of your last 10 matches were in lobbies rated above you and you won
  60%+ of those, wins count for more (up to +75% at a 100% win rate).
Wins are capped at 2x and losses at 0.5x of the normal amount. Tune the constants here.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Match, MatchPlayer, Player, Rating, Season

START_ELO = 1000
K = 50                     # an even match is worth K / 2 = 25
PLACEMENT_GAMES = 10       # matches before your Elo shows and you appear on the Top 100
PLACEMENT_MULTIPLIER = 1.5
ELO_FLOOR = 100
MIN_GAMES = PLACEMENT_GAMES

GAP_SCALE = 400            # Elo away from the lobby average that earns the full gap adjustment
UNDER_MAX = 0.5            # below the average: wins up to +50%, losses up to -50%
OVER_MAX = 0.25            # above the average: wins up to +25%, losses up to -25%
HOT_GAMES = 10             # recent matches checked for a hot streak
HOT_MIN_UP_GAMES = 4       # how many of them must be "up" games (lobby average above your Elo)
HOT_UP_MARGIN = 50         # how far above your Elo the lobby average must be to count as "up"
HOT_MIN_WINRATE = 0.6      # win rate in up games where the boost starts
HOT_MAX = 0.75             # boost at a 100% win rate in up games
WIN_CAP = 2.0              # most a win can be multiplied by
LOSS_FLOOR = 0.5           # least a loss can be multiplied by


def expected(a: float, b: float) -> float:
    return 1 / (1 + 10 ** ((b - a) / 400))


def gap_bonus(own_elo: float, lobby_avg: float) -> float:
    """0 to UNDER_MAX (below the lobby average) or 0 to OVER_MAX (above it), growing with the gap."""
    gap = own_elo - lobby_avg
    return (UNDER_MAX if gap < 0 else OVER_MAX) * min(1.0, abs(gap) / GAP_SCALE)


def adjust(won: bool, own_elo: float, lobby_avg: float, hot: float = 0.0) -> float:
    """Multiplier for one player's change: gap bonus for both wins and losses, hot streak for wins."""
    g = gap_bonus(own_elo, lobby_avg)
    return min(WIN_CAP, (1 + g) * (1 + hot)) if won else max(LOSS_FLOOR, 1 - g)


def elo_change(own_avg: float, opp_avg: float, won: bool, placement: bool = False, mult: float = 1.0) -> int:
    """Whole-number Elo change for one player, from the two team averages (and their own multiplier)."""
    k = K * (PLACEMENT_MULTIPLIER if placement else 1)
    e = expected(own_avg, opp_avg)
    d = max(1, round(k * ((1 - e) if won else e) * mult))
    return d if won else -d


def hot_streak(db: Session, player: Player, season: Season) -> float:
    """Win-gain boost for someone winning most of their recent games against higher-rated lobbies."""
    recent = db.execute(select(MatchPlayer.match_id, MatchPlayer.elo_before, MatchPlayer.team, Match.winner)
                        .join(Match, MatchPlayer.match_id == Match.id)
                        .where(MatchPlayer.player_id == player.id, Match.season_id == season.id)
                        .order_by(Match.id.desc()).limit(HOT_GAMES)).all()
    if not recent:
        return 0.0
    lobby = dict(db.execute(select(MatchPlayer.match_id, func.avg(MatchPlayer.elo_before))
                            .where(MatchPlayer.match_id.in_([r.match_id for r in recent]))
                            .group_by(MatchPlayer.match_id)).all())
    up = [r.team == r.winner for r in recent if lobby[r.match_id] >= r.elo_before + HOT_UP_MARGIN]
    if len(up) < HOT_MIN_UP_GAMES:
        return 0.0
    rate = sum(up) / len(up)
    return max(0.0, HOT_MAX * (rate - HOT_MIN_WINRATE) / (1 - HOT_MIN_WINRATE))


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
    lobby_avg = sum(r.elo for *_, r in entries) / len(entries)
    hot = {p.id: hot_streak(db, p, season) for p, *_ in entries}  # before this match is saved
    m = Match(season_id=season.id, map_id=map_id, winner=winner, reported_by=reported_by)
    db.add(m)
    db.flush()
    changes = []
    for p, team, pos, r in entries:
        before = r.elo
        won = team == winner
        opp = "blue" if team == "red" else "red"
        mult = adjust(won, before, lobby_avg, hot[p.id])
        r.elo = max(ELO_FLOOR, before + elo_change(avg[team], avg[opp], won, r.games < PLACEMENT_GAMES, mult))
        r.games += 1
        r.wins += int(won)
        db.add(MatchPlayer(match_id=m.id, player_id=p.id, team=team, position=pos,
                           elo_before=before, elo_after=r.elo))
        changes.append({"name": p.tagpro_name, "team": team, "before": before, "after": r.elo,
                        "change": r.elo - before, "multiplier": round(mult, 2)})
    return m, changes
