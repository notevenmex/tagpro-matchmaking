"""Queue rules: who may queue, cooldowns for missed ready checks and leavers, idle queue expiry,
and admin bans. Modeled on FACEIT.

- Missing or declining a ready check gives a cooldown that grows with each strike: 5 min, 15 min,
  1 h, then 24 h. Strikes chain while they're less than STRIKE_RESET_HOURS apart; a clean day resets it.
- A leaver or no-show, confirmed by both captains or by an admin, loses LEAVER_ELO Elo and gets a
  longer cooldown (30 min, 2 h, 24 h). An unfinished match is voided, so nobody else's Elo changes.
- Anyone left in the queue for QUEUE_IDLE_MINUTES without the queue popping is taken out.
- Admins can ban for a set time or permanently, with a reason the player sees, and lift bans.
Bots (archived players with no Discord account, DEV_LOGIN only) are never penalised or expired.
All times are Unix seconds from play.clock(), so tests can move time forward.
"""
import math
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import auth, play
from .db import LeaverFlag, Lobby, LobbyPlayer, Player, QueueBan, QueueEntry, Rating, Strike, get_db
from .rating import START_ELO


def _minutes(name: str, default: str) -> list[int]:
    return [int(x) for x in os.getenv(name, default).split(",") if x.strip()]


READY_CHECK_COOLDOWNS = _minutes("READY_CHECK_COOLDOWNS", "5,15,60,1440")  # minutes, 1st strike, 2nd, ...
LEAVER_COOLDOWNS = _minutes("LEAVER_COOLDOWNS", "30,120,1440")
STRIKE_RESET_HOURS = float(os.getenv("STRIKE_RESET_HOURS", 24))
LEAVER_ELO = int(os.getenv("LEAVER_ELO", 25))
QUEUE_IDLE_MINUTES = int(os.getenv("QUEUE_IDLE_MINUTES", 45))
PERMANENT = 100 * 365 * 86400  # an admin ban "forever" lasts 100 years
LEAVER_STATUSES = ("awaiting_group", "in_progress", "disputed", "finished")  # when a leaver can be flagged
UNFINISHED = ("awaiting_group", "in_progress", "disputed")

router = APIRouter()


# ---------- bans and strikes ----------
def _is_bot(db: Session, player_id: int) -> bool:
    return auth.DEV_LOGIN and db.scalar(select(Player.discord_id).where(Player.id == player_id)) is None


def active_ban(db: Session, player_id: int) -> QueueBan | None:
    return db.scalar(select(QueueBan).where(QueueBan.player_id == player_id, QueueBan.until > play.clock()))


def ban(db: Session, player_id: int, seconds: float, kind: str, reason: str) -> QueueBan:
    """Ban a player from queueing. If they're already banned for longer, that ban stays."""
    until = play.clock() + seconds
    b = db.scalar(select(QueueBan).where(QueueBan.player_id == player_id))
    if not b:
        b = QueueBan(player_id=player_id, until=until, kind=kind, reason=reason)
        db.add(b)
    elif until >= b.until or b.until <= play.clock():
        b.until, b.kind, b.reason = until, kind, reason
    db.execute(delete(QueueEntry).where(QueueEntry.player_id == player_id))
    db.flush()
    return b


def strike_count(db: Session, player_id: int, kind: str) -> int:
    """How many strikes of this kind in the current chain (each less than STRIKE_RESET_HOURS after the last)."""
    times = db.scalars(select(Strike.at).where(Strike.player_id == player_id, Strike.kind == kind)
                       .order_by(Strike.at.desc())).all()
    n, last = 0, play.clock()
    for at in times:
        if last - at >= STRIKE_RESET_HOURS * 3600:
            break
        n, last = n + 1, at
    return n


def _strike(db: Session, player_id: int, kind: str, ladder: list[int], lobby_id: int | None) -> tuple[int, int]:
    db.add(Strike(player_id=player_id, kind=kind, at=play.clock(), lobby_id=lobby_id))
    db.flush()
    n = strike_count(db, player_id, kind)
    return n, ladder[min(n, len(ladder)) - 1]


def _ordinal(n: int) -> str:
    return {1: "first", 2: "second", 3: "third"}.get(n, f"{n}th")


def _duration(minutes: float) -> str:
    if minutes >= 1440 and minutes % 1440 == 0:
        d = int(minutes // 1440)
        return f"{d} day" + ("s" if d > 1 else "")
    if minutes >= 60 and minutes % 60 == 0:
        h = int(minutes // 60)
        return f"{h} hour" + ("s" if h > 1 else "")
    return f"{int(minutes)} min"


def penalize_ready_check(db: Session, player_id: int, lobby_id: int | None = None):
    """A player missed or declined a ready check."""
    if _is_bot(db, player_id):
        return
    n, minutes = _strike(db, player_id, "ready_check", READY_CHECK_COOLDOWNS, lobby_id)
    ban(db, player_id, minutes * 60, "ready_check",
        f"You missed a ready check ({_ordinal(n)} time in a row), so you have a {_duration(minutes)} cooldown.")


# ---------- who may queue ----------
def ban_message(b: QueueBan) -> str:
    left = b.until - play.clock()
    if left > PERMANENT / 2:
        when = "This ban is permanent."
    else:
        h, m = divmod(math.ceil(left / 60), 60)
        when = f"You can queue again in {f'{h} h ' if h else ''}{f'{m} min' if m or not h else ''}".rstrip() + "."
    reason = b.reason if b.kind != "admin" else f"An admin banned you from queueing: {b.reason or 'no reason given'}."
    return f"{reason} {when}"


def check_can_queue(db: Session, p: Player):
    """Raise an HTTPException (its detail is shown to the player as-is) if p can't join the queue."""
    b = active_ban(db, p.id)
    if b:
        raise HTTPException(403, ban_message(b))
    if play._pregame_lobby(db, p.id):
        raise HTTPException(409, "You're already in a match.")


def ban_info(db: Session, player_id: int) -> dict:
    b = active_ban(db, player_id)
    return {"banned_until": b.until if b else None, "ban_kind": b.kind if b else None,
            "ban_reason": ban_message(b) if b else None}


# ---------- idle queue ----------
def expire_queue(db: Session) -> bool:
    """Take out anyone who's been waiting QUEUE_IDLE_MINUTES. Accepting a ready check counts as activity,
    so players put back after someone else missed one keep their place without being expired."""
    cutoff = play.clock() - QUEUE_IDLE_MINUTES * 60
    gone = []
    for e in db.scalars(select(QueueEntry).where(QueueEntry.joined_at < cutoff)).all():
        if _is_bot(db, e.player_id):
            continue
        last_accept = db.scalar(select(func.max(Lobby.created_at)).join(LobbyPlayer, LobbyPlayer.lobby_id == Lobby.id)
                                .where(LobbyPlayer.player_id == e.player_id, LobbyPlayer.accepted.is_(True)))
        if not last_accept or last_accept < cutoff:
            gone.append(e)
    for e in gone:
        db.delete(e)
    db.flush()
    return bool(gone)


# ---------- leavers ----------
def _apply_leaver(db: Session, lobby: Lobby, player: Player):
    ratings = db.scalars(select(Rating).where(Rating.player_id == player.id, Rating.season_id == lobby.season_id)).all()
    if not ratings:  # hasn't finished a match yet this season
        extra = {"position": player.position} if hasattr(Rating, "position") else {}  # per-position schema
        ratings = [Rating(player_id=player.id, season_id=lobby.season_id, elo=START_ELO, games=0, wins=0, **extra)]
        db.add(ratings[0])
    for r in ratings:
        r.elo = max(100, r.elo - LEAVER_ELO)
    n, minutes = _strike(db, player.id, "leaver", LEAVER_COOLDOWNS, lobby.id)
    ban(db, player.id, minutes * 60, "leaver",
        f"You were reported for leaving match {lobby.id} and lost {LEAVER_ELO} Elo, "
        f"so you have a {_duration(minutes)} cooldown.")
    if lobby.status in UNFINISHED:
        lobby.status, lobby.deadline = "cancelled", play.clock()
        lobby.note = f"Voided: {player.tagpro_name} left the match. Nobody else's Elo changes."[:200]


class LeaverIn(BaseModel):
    name: str = Field(min_length=1, max_length=24)


@router.post("/api/lobbies/{lobby_id}/leaver")
def flag_leaver(lobby_id: int, body: LeaverIn, p: Player = Depends(auth.require_player),
                db: Session = Depends(get_db)):
    """A captain reports a leaver or no-show. It counts once both captains name the same player,
    or straight away if the reporter is an admin."""
    with play.LOCK:
        lobby = db.get(Lobby, lobby_id)
        if not lobby:
            raise HTTPException(404, "No such match.")
        if lobby.status not in LEAVER_STATUSES:
            raise HTTPException(409, "You can report a leaver once teams are set.")
        lps = play._lobby_players(db, lobby)
        me = next((lp for lp, q in lps if q.id == p.id), None)
        is_admin = auth.is_admin(p.discord_id, p)
        if not is_admin and not (me and me.is_captain):
            raise HTTPException(403, "Only the two captains or an admin can report a leaver.")
        target = next((q for lp, q in lps if q.tagpro_name.lower() == body.name.strip().lower()), None)
        if not target:
            raise HTTPException(400, "That player isn't in this match.")
        if target.id == p.id:
            raise HTTPException(400, "You can't report yourself.")
        by = "admin" if is_admin else me.team
        if not db.scalar(select(LeaverFlag).where(LeaverFlag.lobby_id == lobby.id, LeaverFlag.player_id == target.id,
                                                  LeaverFlag.flagged_by == by)):
            db.add(LeaverFlag(lobby_id=lobby.id, player_id=target.id, flagged_by=by))
            db.flush()
        flags = set(db.scalars(select(LeaverFlag.flagged_by).where(LeaverFlag.lobby_id == lobby.id,
                                                                   LeaverFlag.player_id == target.id)))
        confirmed = "admin" in flags or {"red", "blue"} <= flags
        already = db.scalar(select(Strike).where(Strike.lobby_id == lobby.id, Strike.player_id == target.id,
                                                 Strike.kind == "leaver"))
        if confirmed and not already:
            _apply_leaver(db, lobby, target)
        play._done(db)
        return {"ok": True, "confirmed": confirmed}


# ---------- admin bans ----------
class BanIn(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    minutes: int | None = Field(default=None, ge=1)  # null = permanent
    reason: str = Field(default="", max_length=150)


@router.get("/api/admin/bans")
def list_bans(admin_id: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    rows = db.execute(select(QueueBan, Player.tagpro_name).join(Player, QueueBan.player_id == Player.id)
                      .where(QueueBan.until > play.clock()).order_by(QueueBan.until)).all()
    return [{"name": name, "kind": b.kind, "reason": b.reason, "until": b.until,
             "permanent": b.until - play.clock() > PERMANENT / 2,
             "strikes": {k: strike_count(db, b.player_id, k) for k in ("ready_check", "leaver")}}
            for b, name in rows]


def _named(db: Session, name: str) -> Player:
    p = db.scalar(select(Player).where(func.lower(Player.tagpro_name) == name.strip().lower()))
    if not p:
        raise HTTPException(404, f"No player named “{name}”.")
    return p


@router.post("/api/admin/bans", status_code=201)
def add_ban(body: BanIn, admin_id: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    with play.LOCK:
        p = _named(db, body.name)
        b = db.scalar(select(QueueBan).where(QueueBan.player_id == p.id))
        if b:  # an admin ban replaces whatever was there, even if it's shorter
            db.delete(b)
            db.flush()
        ban(db, p.id, body.minutes * 60 if body.minutes else PERMANENT, "admin", body.reason.strip())
        return play._done(db)


@router.delete("/api/admin/bans/{name}")
def lift_ban(name: str, clear_strikes: bool = False, admin_id: str = Depends(auth.require_admin),
             db: Session = Depends(get_db)):
    with play.LOCK:
        p = _named(db, name)
        db.execute(delete(QueueBan).where(QueueBan.player_id == p.id))
        if clear_strikes:
            db.execute(delete(Strike).where(Strike.player_id == p.id))
        return play._done(db)
