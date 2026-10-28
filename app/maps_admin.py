"""Admin map pool editing: add, rename, take in or out of rotation, clear votes, delete, and
replace the pool with the current MLTP majors list.

The veto draws from active maps, weighted by vote totals (play._weighted_pool), so every change
here applies to the next queue pop. Names should match TagPro's map names exactly: the group bot
picks the map by name, and the site shows TagPro's own preview image for it.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import auth
from .db import Lobby, Map, MapVote, Match, get_db

# https://www.mltp.gg/maps?tier=majors, MLTP season 40 (checked 2026-10-28)
MLTP_MAJORS = ["Basenji", "Combine", "Milano 2", "Oncilla", "Sardonica", "Shake", "Willow 2",
               "Asida", "Galapagos 2", "House Party", "Korok", "Moon Base 2024"]

router = APIRouter(prefix="/api/admin/maps", dependencies=[Depends(auth.require_admin)])


def _by_name(db: Session, name: str) -> Map | None:
    return db.scalar(select(Map).where(func.lower(Map.name) == name.strip().lower()))


def _in_use(db: Session, map_id: int) -> bool:
    """Played in a match, picked for a lobby, or in a veto that's still running."""
    if db.scalar(select(Match.id).where(Match.map_id == map_id).limit(1)):
        return True
    if db.scalar(select(Lobby.id).where(Lobby.map_id == map_id).limit(1)):
        return True
    vetoes = db.scalars(select(Lobby.map_pool).where(Lobby.status == "map_veto")).all()
    return any(m["id"] == map_id for pool in vetoes for m in pool)


@router.get("")
def list_maps(db: Session = Depends(get_db)):
    votes = dict(db.execute(select(MapVote.map_id, func.count()).group_by(MapVote.map_id)).all())
    totals = dict(db.execute(select(MapVote.map_id, func.sum(MapVote.score)).group_by(MapVote.map_id)).all())
    return [{"id": m.id, "name": m.name, "active": m.active, "votes": votes.get(m.id, 0),
             "total": totals.get(m.id) or 0, "in_use": _in_use(db, m.id)}
            for m in db.scalars(select(Map).order_by(Map.active.desc(), Map.name)).all()]


class MapIn(BaseModel):
    name: str = Field(min_length=1, max_length=48)


class MapEdit(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=48)
    active: bool | None = None


@router.post("", status_code=201)
def add_map(body: MapIn, db: Session = Depends(get_db)):
    name = body.name.strip()
    existing = _by_name(db, name)
    if existing and existing.active:
        raise HTTPException(409, f"“{existing.name}” is already in the pool.")
    if existing:  # bring a retired map back, votes and all
        existing.active = True
    else:
        db.add(Map(name=name, active=True))
    db.commit()
    return {"ok": True}


@router.patch("/{map_id}")
def edit_map(map_id: int, body: MapEdit, db: Session = Depends(get_db)):
    m = db.get(Map, map_id)
    if not m:
        raise HTTPException(404, "That map doesn’t exist.")
    if body.name is not None:
        name = body.name.strip()
        other = _by_name(db, name)
        if other and other.id != m.id:
            raise HTTPException(409, f"There’s already a map called “{other.name}”.")
        m.name = name
    if body.active is not None:
        m.active = body.active
    db.commit()
    return {"ok": True}


@router.delete("/{map_id}/votes")
def clear_votes(map_id: int, db: Session = Depends(get_db)):
    if not db.get(Map, map_id):
        raise HTTPException(404, "That map doesn’t exist.")
    db.execute(delete(MapVote).where(MapVote.map_id == map_id))
    db.commit()
    return {"ok": True}


@router.delete("/{map_id}")
def delete_map(map_id: int, db: Session = Depends(get_db)):
    m = db.get(Map, map_id)
    if not m:
        raise HTTPException(404, "That map doesn’t exist.")
    if _in_use(db, map_id):
        raise HTTPException(409, f"“{m.name}” has been played or is in a match, so it can’t be deleted. "
                                 "Take it out of rotation instead.")
    db.execute(delete(MapVote).where(MapVote.map_id == map_id))
    db.delete(m)
    db.commit()
    return {"ok": True}


@router.post("/use-mltp-majors")
def use_mltp_majors(db: Session = Depends(get_db)):
    """Make the rotation exactly the MLTP majors list. Other maps are taken out of rotation, not
    deleted, so match history and votes stay."""
    wanted = {n.lower() for n in MLTP_MAJORS}
    for m in db.scalars(select(Map)).all():
        m.active = m.name.lower() in wanted
    have = {n.lower() for n in db.scalars(select(Map.name)).all()}
    db.add_all(Map(name=n, active=True) for n in MLTP_MAJORS if n.lower() not in have)
    db.commit()
    return {"ok": True, "maps": MLTP_MAJORS}
