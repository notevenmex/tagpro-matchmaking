"""Private queues: invite-only queues such as "S Queue".

- Admins create queues and add or remove members whenever they like.
- Members can wait in any of their private queues and in the general queue at the same time.
  The first queue to fill pops, and everyone in that match leaves every other queue.
- Matches from a private queue are drafted, vetoed and reported exactly like general ones, but
  each private queue has its own Elo (rating.QueueRating) and its own leaderboard, under the same
  rules as the general Elo.
- Every queue rule applies in every queue: bans, ready-check cooldowns, captains waiting on a
  score, one account per person at a time, and the idle timeout.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import auth, play, restrictions
from .db import Lobby, Player, PrivateQueue, PrivateQueueEntry, PrivateQueueMember, Season, get_db
from .rating import find_rating, rating_out

router = APIRouter()


def _queue(db: Session, queue_id: int) -> PrivateQueue:
    q = db.get(PrivateQueue, queue_id)
    if not q or q.archived:
        raise HTTPException(404, "No such queue.")
    return q


def is_member(db: Session, queue_id: int, player_id: int) -> bool:
    return db.scalar(select(PrivateQueueMember.id).where(PrivateQueueMember.queue_id == queue_id,
                                                         PrivateQueueMember.player_id == player_id)) is not None


def views(db: Session, me: Player | None, admin: bool) -> list[dict]:
    """The private queues this player can see: the ones they belong to (admins see them all)."""
    if not me and not admin:
        return []
    stmt = select(PrivateQueue).where(PrivateQueue.archived.is_(False)).order_by(PrivateQueue.name)
    if not admin:
        stmt = stmt.join(PrivateQueueMember, PrivateQueueMember.queue_id == PrivateQueue.id) \
                   .where(PrivateQueueMember.player_id == me.id)
    out = []
    season_id = db.scalar(select(Season.id).where(Season.is_active.is_(True)))
    for q in db.scalars(stmt).all():
        waiting = db.execute(select(PrivateQueueEntry, Player.tagpro_name)
                             .join(Player, PrivateQueueEntry.player_id == Player.id)
                             .where(PrivateQueueEntry.queue_id == q.id).order_by(PrivateQueueEntry.joined_at)).all()
        mine = next((e for e, _ in waiting if me and e.player_id == me.id), None)
        r = find_rating(db, me.id, season_id, q.id) if me and season_id else None
        out.append({"id": q.id, "name": q.name, "size": play.QUEUE_SIZE, "rating": rating_out(r),
                    "member": bool(me) and is_member(db, q.id, me.id),
                    "queue": [name for _, name in waiting],
                    "in_queue": mine is not None, "queued_at": mine.joined_at if mine else None})
    return out


# ---------- players ----------
@router.post("/api/queues/{queue_id}")
def join(queue_id: int, p: Player = Depends(auth.require_player), db: Session = Depends(get_db)):
    with play.LOCK:
        q = _queue(db, queue_id)
        if not is_member(db, q.id, p.id):
            raise HTTPException(403, f"Only members can join {q.name}. Ask an admin to add you.")
        play.check_queue_rules(db, p)
        if not db.scalar(select(PrivateQueueEntry).where(PrivateQueueEntry.queue_id == q.id,
                                                         PrivateQueueEntry.player_id == p.id)):
            db.add(PrivateQueueEntry(queue_id=q.id, player_id=p.id, joined_at=play.clock()))
            db.flush()
            play.try_pop(db)
        return play._done(db)


@router.delete("/api/queues/{queue_id}")
def leave(queue_id: int, p: Player = Depends(auth.require_player), db: Session = Depends(get_db)):
    with play.LOCK:
        db.execute(delete(PrivateQueueEntry).where(PrivateQueueEntry.queue_id == queue_id,
                                                   PrivateQueueEntry.player_id == p.id))
        return play._done(db)


@router.delete("/api/queues")
def leave_all(p: Player = Depends(auth.require_player), db: Session = Depends(get_db)):
    """Leave every queue, general and private."""
    with play.LOCK:
        play.clear_queues(db, [p.id])
        return play._done(db)


# ---------- admins ----------
class QueueIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class MemberIn(BaseModel):
    name: str = Field(min_length=1, max_length=24)


def _name_taken(db: Session, name: str, except_id: int | None = None) -> bool:
    other = db.scalar(select(PrivateQueue.id).where(func.lower(PrivateQueue.name) == name.lower(),
                                                    PrivateQueue.archived.is_(False)))
    return other is not None and other != except_id


def _admin_out(db: Session, q: PrivateQueue) -> dict:
    members = db.scalars(select(Player.tagpro_name).join(PrivateQueueMember, PrivateQueueMember.player_id == Player.id)
                         .where(PrivateQueueMember.queue_id == q.id).order_by(func.lower(Player.tagpro_name))).all()
    return {"id": q.id, "name": q.name, "members": members}


@router.get("/api/admin/queues")
def admin_list(_: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    return [_admin_out(db, q) for q in db.scalars(select(PrivateQueue).where(PrivateQueue.archived.is_(False))
                                                   .order_by(PrivateQueue.name))]


@router.post("/api/admin/queues", status_code=201)
def create(body: QueueIn, _: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Give the queue a name.")
    with play.LOCK:
        if _name_taken(db, name):
            raise HTTPException(409, f"There's already a queue called {name}.")
        q = PrivateQueue(name=name, created_at=play.clock())
        db.add(q)
        db.flush()
        out = _admin_out(db, q)
        play._done(db)
        return out


@router.patch("/api/admin/queues/{queue_id}")
def rename(queue_id: int, body: QueueIn, _: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    with play.LOCK:
        q = _queue(db, queue_id)
        name = body.name.strip()
        if not name or _name_taken(db, name, q.id):
            raise HTTPException(409, f"There's already a queue called {name}." if name else "Give the queue a name.")
        q.name = name
        out = _admin_out(db, q)
        play._done(db)
        return out


@router.delete("/api/admin/queues/{queue_id}")
def remove(queue_id: int, _: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    with play.LOCK:
        q = _queue(db, queue_id)
        if db.scalar(select(Lobby.id).where(Lobby.queue_id == q.id, Lobby.status.notin_(("cancelled", "finished")))):
            raise HTTPException(409, f"A match from {q.name} is still going. Delete the queue once it's over.")
        # archived rather than deleted, so its past matches and Elo history stay intact
        db.execute(delete(PrivateQueueEntry).where(PrivateQueueEntry.queue_id == q.id))
        db.execute(delete(PrivateQueueMember).where(PrivateQueueMember.queue_id == q.id))
        q.archived = True
        return play._done(db)


@router.post("/api/admin/queues/{queue_id}/members", status_code=201)
def add_member(queue_id: int, body: MemberIn, _: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    with play.LOCK:
        q = _queue(db, queue_id)
        p = restrictions._named(db, body.name)
        if not is_member(db, q.id, p.id):
            db.add(PrivateQueueMember(queue_id=q.id, player_id=p.id))
            db.flush()
        out = _admin_out(db, q)
        play._done(db)
        return out


@router.delete("/api/admin/queues/{queue_id}/members/{name}")
def remove_member(queue_id: int, name: str, _: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    with play.LOCK:
        q = _queue(db, queue_id)
        p = restrictions._named(db, name)
        db.execute(delete(PrivateQueueMember).where(PrivateQueueMember.queue_id == q.id,
                                                    PrivateQueueMember.player_id == p.id))
        db.execute(delete(PrivateQueueEntry).where(PrivateQueueEntry.queue_id == q.id,
                                                   PrivateQueueEntry.player_id == p.id))
        out = _admin_out(db, q)
        play._done(db)
        return out
