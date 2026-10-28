"""Database setup and models."""
import os
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String,
                        UniqueConstraint, create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker



def database_url(raw: str) -> str:
    """Use the psycopg 3 driver for Postgres URLs written the way Railway and Heroku hand them out."""
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw[len(prefix):]
    return raw


DATABASE_URL = database_url(os.getenv("DATABASE_URL", "sqlite:///./rankedpugs.db"))
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,  # Postgres drops idle connections; reconnect instead of failing a request
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Season(Base):
    __tablename__ = "seasons"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True)
    banner: Mapped[str] = mapped_column(String(500), default="")       # main banner text (use \n for a new line)
    banner_note: Mapped[str] = mapped_column(String(500), default="")  # highlighted part shown in yellow
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)


class Player(Base):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(primary_key=True)
    discord_id: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    tagpro_name: Mapped[str] = mapped_column(String(24), unique=True)
    reddit_name: Mapped[str | None] = mapped_column(String(24), nullable=True)
    position: Mapped[str] = mapped_column(String(8), default="offense")  # main position: offense | defense
    server: Mapped[str | None] = mapped_column(String(24), nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["OG", "S3 50", "S3 ★"]
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Rating(Base):
    """One rating per player, per season, per position, so someone can be ranked on O and D."""
    __tablename__ = "ratings"
    __table_args__ = (UniqueConstraint("player_id", "season_id", "position"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    position: Mapped[str] = mapped_column(String(8))
    elo: Mapped[float] = mapped_column(Float, default=1500.0)
    games: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)


class Map(Base):
    __tablename__ = "maps"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(48), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class MapVote(Base):
    """A vote from -5 to 10. Imported archive votes have no player, only a voter_label."""
    __tablename__ = "map_votes"
    __table_args__ = (UniqueConstraint("map_id", "player_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    map_id: Mapped[int] = mapped_column(ForeignKey("maps.id"))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    voter_label: Mapped[str | None] = mapped_column(String(48), nullable=True)
    score: Mapped[int] = mapped_column(Integer)


class Match(Base):
    __tablename__ = "matches"
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    map_id: Mapped[int | None] = mapped_column(ForeignKey("maps.id"), nullable=True)
    winner: Mapped[str] = mapped_column(String(4))  # red | blue
    reported_by: Mapped[str] = mapped_column(String(32))  # Discord id of the admin who entered it
    played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MatchPlayer(Base):
    __tablename__ = "match_players"
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    team: Mapped[str] = mapped_column(String(4))
    position: Mapped[str] = mapped_column(String(8))
    elo_before: Mapped[float] = mapped_column(Float)
    elo_after: Mapped[float] = mapped_column(Float)


class QueueEntry(Base):
    """A player waiting for a match. Times are Unix seconds so they compare the same on SQLite and Postgres."""
    __tablename__ = "queue"
    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), unique=True)
    joined_at: Mapped[float] = mapped_column(Float)


class QueueBan(Base):
    """Short ban from queueing, given for missing a ready check."""
    __tablename__ = "queue_bans"
    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), unique=True)
    until: Mapped[float] = mapped_column(Float)


class Lobby(Base):
    """One popped queue, from ready check through draft, map veto and server vote to the game itself.

    status: ready_check -> drafting -> map_veto -> server_vote -> awaiting_group -> in_progress,
    or cancelled at any point. Captain "red" picks first; "blue" bans first.
    """
    __tablename__ = "lobbies"
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"))
    status: Mapped[str] = mapped_column(String(16), default="ready_check")
    created_at: Mapped[float] = mapped_column(Float)
    deadline: Mapped[float | None] = mapped_column(Float, nullable=True)  # when the current step times out
    map_pool: Mapped[list] = mapped_column(JSON, default=list)  # [{"id", "name", "banned_by": None | "red" | "blue"}]
    map_id: Mapped[int | None] = mapped_column(ForeignKey("maps.id"), nullable=True)
    server: Mapped[str | None] = mapped_column(String(32), nullable=True)
    group_link: Mapped[str | None] = mapped_column(String(200), nullable=True)
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(200), default="")  # why it was cancelled, etc.


class LobbyPlayer(Base):
    __tablename__ = "lobby_players"
    __table_args__ = (UniqueConstraint("lobby_id", "player_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    lobby_id: Mapped[int] = mapped_column(ForeignKey("lobbies.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    joined_at: Mapped[float] = mapped_column(Float)  # original queue time, so accepters keep their place
    elo: Mapped[float] = mapped_column(Float)          # main-position rating when the queue popped
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    team: Mapped[str | None] = mapped_column(String(4), nullable=True)
    is_captain: Mapped[bool] = mapped_column(Boolean, default=False)
    pick_no: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1-6 draft order
    server_vote: Mapped[str | None] = mapped_column(String(32), nullable=True)


class GroupJob(Base):
    """The bot's attempt to create a lobby's TagPro group and DM the captains.

    status: running -> done, or retry (until next_try) -> ... -> failed (captains make the group by hand).
    """
    __tablename__ = "group_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    lobby_id: Mapped[int] = mapped_column(ForeignKey("lobbies.id"), unique=True)
    status: Mapped[str] = mapped_column(String(8), default="running")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str] = mapped_column(String(300), default="")
    dms_sent: Mapped[int] = mapped_column(Integer, default=0)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
