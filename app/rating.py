"""Team Elo. Every player on a team moves by the same amount, based on team averages.

Ratings are stored as Elo but shown as small "points" like the original site
(about 10 for a new player, 40+ for the very top). Tune the constants here.
"""
START_ELO = 1500.0
K = 32           # how far one game moves a rating
MIN_GAMES = 10   # games needed before someone appears on the Top 100


def points(elo: float) -> int:
    return max(0, round((elo - 1400) / 10))


def elo_from_points(pts: int) -> float:
    return 1400.0 + pts * 10


def expected(a: float, b: float) -> float:
    return 1 / (1 + 10 ** ((b - a) / 400))


def team_delta(red: list[float], blue: list[float], winner: str, k: float = K) -> tuple[float, float]:
    """Return (change for each red player, change for each blue player)."""
    r, b = sum(red) / len(red), sum(blue) / len(blue)
    d = k * ((1.0 if winner == "red" else 0.0) - expected(r, b))
    return d, -d
