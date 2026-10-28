"""Salty Runback: after a match, every player is asked whether to play it again.

When a match's score is verified (by tagpro.eu or both captains), a vote opens for RUNBACK_SECONDS.
If every player says yes, a new lobby starts with the same teams, captains, map and server, skipping
the queue, draft, map veto and server vote. It goes straight to awaiting_group, so the group bot
(autogroup.py), team voice (voice.py) and the "match ready" DM (match_dm.py) handle it like any other
match, and its score counts for Elo as a normal ranked match. Any "no", or the timer running out,
ends the vote.

While a vote is open its players (and their alts) are held: they can't join the queue, and anyone
already queued keeps their place but isn't popped into a match until the vote is over. A passed vote
takes them out of the queue.

No vote opens when an admin settles a disputed score (that's usually long after the game), and the
vote fails right away when a player is already in another match or banned from queueing.
RUNBACK=0 turns it off.
"""
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import auth, play, restrictions
from .db import Lobby, LobbyPlayer, Player, QueueEntry, RunbackVote, Season, get_db

ENABLED = os.getenv("RUNBACK", "1") == "1"
RUNBACK_SECONDS = int(os.getenv("RUNBACK_SECONDS", 120))
BUSY = play.PRE_GAME + ("in_progress", "disputed")  # a player in a lobby like this can't play a runback

router = APIRouter()


def _vote(db: Session, lobby_id: int) -> RunbackVote | None:
    return db.scalar(select(RunbackVote).where(RunbackVote.lobby_id == lobby_id))


def _open_votes(db: Session) -> list[RunbackVote]:
    return db.scalars(select(RunbackVote).where(RunbackVote.status == "open")).all()


def _player_ids(db: Session, lobby_id: int) -> list[int]:
    return list(db.scalars(select(LobbyPlayer.player_id).where(LobbyPlayer.lobby_id == lobby_id)))


def _why_not(db: Session, lobby: Lobby) -> str | None:
    """Why this lobby's players can't play a runback right now, if they can't."""
    for _, p in play._lobby_players(db, lobby):
        if restrictions.active_ban(db, p.id):
            return f"{p.tagpro_name} can't queue right now."
        for pid in restrictions.linked_ids(db, p.id):
            if db.scalar(select(Lobby.id).join(LobbyPlayer, LobbyPlayer.lobby_id == Lobby.id)
                         .where(LobbyPlayer.player_id == pid, Lobby.id != lobby.id, Lobby.status.in_(BUSY))):
                return f"{p.tagpro_name} is already in another match."
    return None


def open_vote(db: Session, lobby: Lobby, reported_by: str):
    """Start the vote for a lobby whose score was just verified (reporting.finish). Doesn't commit."""
    if not ENABLED or reported_by not in ("captains", "tagpro.eu") or _vote(db, lobby.id):
        return
    t = play.clock()
    why = _why_not(db, lobby)
    db.add(RunbackVote(lobby_id=lobby.id, status="failed" if why else "open", deadline=t if why else t + RUNBACK_SECONDS,
                       votes={}, note=why or ""))
    db.flush()


def held_ids(db: Session) -> set[int]:
    """Players (and their alts) who are waiting on an open runback vote, so the queue skips them."""
    held = set()
    for v in _open_votes(db):
        for pid in _player_ids(db, v.lobby_id):
            held.update(restrictions.linked_ids(db, pid))
    return held


def hold_reason(db: Session, player_id: int) -> str | None:
    """Why this player can't queue yet, if they (or an alt) are in an open runback vote."""
    ids = set(restrictions.linked_ids(db, player_id))
    for v in _open_votes(db):
        if ids & set(_player_ids(db, v.lobby_id)):
            return f"The Salty Runback vote for match {v.lobby_id} is open. You can queue once it's decided."
    return None


def _fail(db: Session, v: RunbackVote, note: str):
    v.status, v.deadline, v.note = "failed", play.clock(), note
    db.flush()
    play.try_pop(db)  # anyone held in the queue can be matched again


def _start_rematch(db: Session, v: RunbackVote):
    """Everyone said yes: a new lobby with the same teams, map and server, ready for its group."""
    old = db.get(Lobby, v.lobby_id)
    season = db.scalar(select(Season).where(Season.is_active.is_(True)))
    why = "The season is over." if not season or season.id != old.season_id else _why_not(db, old)
    if why:
        return _fail(db, v, why)
    t = play.clock()
    lobby = Lobby(season_id=season.id, status="awaiting_group", created_at=t, deadline=None,
                  map_pool=list(old.map_pool or []), map_id=old.map_id, server=old.server,
                  note=f"Salty Runback of match #{old.id}")
    db.add(lobby)
    db.flush()
    for i, (lp, p) in enumerate(play._lobby_players(db, old)):
        db.add(LobbyPlayer(lobby_id=lobby.id, player_id=p.id, joined_at=t + i * 1e-3,
                           elo=play._main_elo(db, p, season.id), accepted=True, team=lp.team,
                           is_captain=lp.is_captain, pick_no=lp.pick_no))
        db.execute(delete(QueueEntry).where(QueueEntry.player_id.in_(restrictions.linked_ids(db, p.id))))
    v.status, v.deadline, v.new_lobby_id = "passed", t, lobby.id
    db.flush()


def cast(db: Session, v: RunbackVote, player_id: int, yes: bool):
    v.votes = {**v.votes, str(player_id): bool(yes)}  # new dict so SQLAlchemy sees the JSON change
    db.flush()
    if not yes:
        _fail(db, v, "Not everyone wanted the runback.")
    elif all(v.votes.get(str(pid)) for pid in _player_ids(db, v.lobby_id)):
        _start_rematch(db, v)


def tick(db: Session, t: float) -> bool:
    """Close votes whose time ran out; with DEV_LOGIN, bots vote yes. Returns True if anything changed."""
    changed = False
    for v in _open_votes(db):
        if v.deadline <= t:
            _fail(db, v, "Not everyone answered in time.")
            changed = True
        elif auth.DEV_LOGIN and t >= v.deadline - RUNBACK_SECONDS + play.BOT_DELAY:
            bots = db.scalars(select(Player.id).join(LobbyPlayer, LobbyPlayer.player_id == Player.id)
                              .where(LobbyPlayer.lobby_id == v.lobby_id, Player.discord_id.is_(None))).all()
            for pid in bots:
                if str(pid) not in v.votes and v.status == "open":
                    cast(db, v, pid, True)
                    changed = True
    return changed


def recently_over(db: Session, lobby: Lobby, since: float) -> bool:
    """Keep a finished lobby on its players' screens while its vote is open or just ended."""
    v = _vote(db, lobby.id)
    return bool(v and (v.status == "open" or v.deadline >= since))


def view(db: Session, lobby: Lobby, me: Player | None) -> dict | None:
    v = _vote(db, lobby.id)
    if not v:
        return None
    ids = _player_ids(db, lobby.id)
    mine = me.id if me and me.id in ids else None
    return {"status": v.status, "deadline": v.deadline if v.status == "open" else None, "seconds": RUNBACK_SECONDS,
            "yes": sum(1 for x in v.votes.values() if x), "size": len(ids),
            "my_vote": v.votes.get(str(mine)) if mine else None, "can_vote": bool(mine and v.status == "open"),
            "new_lobby_id": v.new_lobby_id, "note": v.note}


def runback_of(db: Session, lobby_id: int) -> int | None:
    """The match this lobby is a runback of, if it is one."""
    return db.scalar(select(RunbackVote.lobby_id).where(RunbackVote.new_lobby_id == lobby_id))


class RunbackIn(BaseModel):
    yes: bool


@router.post("/api/lobbies/{lobby_id}/runback")
def vote(lobby_id: int, body: RunbackIn, p: Player = Depends(auth.require_player), db: Session = Depends(get_db)):
    with play.LOCK:
        lobby = db.get(Lobby, lobby_id)
        if not lobby:
            raise HTTPException(404, "No such match.")
        if p.id not in _player_ids(db, lobby.id):
            raise HTTPException(403, "You're not in this match.")
        v = _vote(db, lobby.id)
        if not v or v.status != "open":
            raise HTTPException(409, "The Salty Runback vote is over.")
        if str(p.id) in v.votes:
            raise HTTPException(409, "You already voted.")
        cast(db, v, p.id, body.yes)
        play._done(db)
        return {"ok": True, "status": v.status, "new_lobby_id": v.new_lobby_id}
