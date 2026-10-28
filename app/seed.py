"""Load a starting season, the MLTP majors map pool (no votes yet), and the players visible in the June 2017 archive.

Run once:  python -m app.seed
Archived players get a FACEIT-style Elo converted from their old point total (see
elo_from_points) and 10 games played, so their placements are done and they show on the Top 100. They have no Discord account linked; an admin can link one
later by setting players.discord_id.
"""
from sqlalchemy import select

from .db import Base, Map, Player, Rating, SessionLocal, Season, engine
from .maps_admin import MLTP_MAJORS
from .rating import PLACEMENT_GAMES

ARCHIVE_PLAYERS = [  # name, points, position, tags
    ("Warriors", 43, "o", []), ("Neon", 42, "o", ["S3 50"]), ("swerve", 41, "d", ["OG", "S3 ★", "S3 50", "S3 Top 10"]),
    ("Mr Hat", 41, "o", ["S3 50"]), ("NotEvenMex", 36, "d", []), ("Cosine180_", 35, "o", []),
    ("BallGod", 35, "o", ["S1 50", "S3 50"]), ("Crippy", 33, "o", ["OG", "S1 50", "S1 Top 10", "S3 50", "S3 Top 10"]),
    ("ProTagonist_", 27, "o", ["OG", "S1 50", "S3 50", "S3 Top 10"]), ("Nebuchanezar", 26, "o", ["OG"]),
    ("dopesayo", 26, "o", []), ("ThadCastleee", 25, "o", ["S3 50"]), ("bdll", 24, "d", []), ("Sleepy", 24, "o", []),
    ("Dalek23", 23, "o", ["OG", "S3 50"]), ("Hulzy", 23, "o", ["OG"]), ("swerved", 22, "o", ["S3 50", "S3 Top 10"]),
    ("GOOBR", 22, "o", []), ("slam", 19, "o", []), ("okthen-mute", 19, "o", []), ("Stann", 18, "o", ["OG"]),
    ("Pinkman", 18, "o", []), ("BallDon’tLie", 18, "o", ["OG"]), ("noMistakes", 17, "o", []),
    ("Juke of Yore", 17, "o", []), ("Pulpo", 16, "o", []), ("Eoshy", 14, "o", []), ("dodsfall", 14, "o", ["OG"]),
    ("quibble", 14, "o", []), ("2P", 14, "o", []), ("dowen", 13, "o", []), ("mahatma", 13, "o", []),
    ("Cooostanza", 13, "o", []), ("TOXIC", 12, "o", []), ("eee", 12, "o", ["OG", "S3 50", "S3 Top 10"]),
    ("PiAcc", 12, "d", []), ("DogStrangler", 11, "o", []), ("Megan", 11, "d", []), ("anti-re", 11, "o", []),
    ("Spud", 10, "o", []), ("robert", 10, "o", []), ("smurf l0rd", 10, "o", []), ("DudeMcGuy", 10, "o", ["OG"]),
    ("Balwas", 10, "o", []), ("ayy_lmao2", 10, "o", []), ("VinsantyNZ", 9, "o", []),
]
# Hulzy was also ranked 17 on defense; ratings are no longer per position, so the higher one is kept.


def elo_from_points(pts: int) -> int:
    """Old site points to FACEIT-style Elo. 10 points (a new player there) becomes 1000 (a new
    player here), and each point is worth 31 Elo, so the 2017 #3 (43) lands at 2023."""
    return 1000 + (pts - 10) * 31

POS = {"o": "offense", "d": "defense"}


def run():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if db.scalar(select(Season).limit(1)):
            print("Database already has a season; nothing to do.")
            return
        # no announcement banner until an admin posts one (Report tab)
        s = Season(number=4, is_active=True, banner="", banner_note="")
        db.add(s)
        db.flush()

        for name, pts, pos, tags in ARCHIVE_PLAYERS:
            p = Player(tagpro_name=name, position=POS[pos], tags=tags)
            db.add(p)
            db.flush()
            db.add(Rating(player_id=p.id, season_id=s.id, elo=elo_from_points(pts),
                          games=PLACEMENT_GAMES, wins=PLACEMENT_GAMES // 2))

        db.add_all(Map(name=n) for n in MLTP_MAJORS)
        db.commit()
        print(f"Seeded season 4, {len(ARCHIVE_PLAYERS)} players and {len(MLTP_MAJORS)} maps.")


if __name__ == "__main__":
    run()
