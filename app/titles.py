"""Season titles and rewards. Admins set up titles for any season (e.g. "Champion", reward
"Gold Discord role") and award each to a player. Awarded titles show next to the player's name
as "S4 Champion"; the 2017 archive tags (players.tags) are no longer shown."""
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth
from .db import Player, SeasonTitle, get_db

router = APIRouter()


def titles_by_player(db: Session, player_ids) -> dict[int, list[dict]]:
    """Awarded titles per player, newest season first, in one query."""
    out = defaultdict(list)
    ids = list(player_ids)
    if not ids:
        return out
    rows = db.scalars(select(SeasonTitle).where(SeasonTitle.player_id.in_(ids))
                      .order_by(SeasonTitle.season.desc(), SeasonTitle.id)).all()
    for t in rows:
        out[t.player_id].append({"label": f"S{t.season} {t.title}", "reward": t.reward})
    return out


def title_out(t: SeasonTitle, name: str | None) -> dict:
    return {"id": t.id, "season": t.season, "title": t.title, "reward": t.reward, "player": name}


def _player_id(db: Session, name: str | None) -> int | None:
    if not name or not name.strip():
        return None
    p = db.scalar(select(Player).where(func.lower(Player.tagpro_name) == name.strip().lower()))
    if not p:
        raise HTTPException(400, f"No player named “{name.strip()}”.")
    return p.id


@router.get("/api/titles")
def list_titles(season: int | None = None, db: Session = Depends(get_db)):
    stmt = (select(SeasonTitle, Player.tagpro_name).outerjoin(Player, SeasonTitle.player_id == Player.id)
            .order_by(SeasonTitle.season.desc(), SeasonTitle.id))
    if season is not None:
        stmt = stmt.where(SeasonTitle.season == season)
    return [title_out(t, name) for t, name in db.execute(stmt).all()]


class TitleIn(BaseModel):
    season: int = Field(ge=1, le=999)
    title: str = Field(min_length=1, max_length=40)
    reward: str = Field(default="", max_length=200)
    player: str | None = Field(default=None, max_length=24)  # leave empty to award it later


class TitleEdit(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=40)
    reward: str | None = Field(default=None, max_length=200)
    player: str | None = Field(default=None, max_length=24)  # "" takes it away from its holder


@router.post("/api/admin/titles", status_code=201, dependencies=[Depends(auth.require_admin)])
def add_title(body: TitleIn, db: Session = Depends(get_db)):
    t = SeasonTitle(season=body.season, title=body.title.strip(), reward=body.reward.strip(),
                    player_id=_player_id(db, body.player))
    db.add(t)
    db.commit()
    return {"id": t.id}


@router.patch("/api/admin/titles/{title_id}", dependencies=[Depends(auth.require_admin)])
def edit_title(title_id: int, body: TitleEdit, db: Session = Depends(get_db)):
    t = db.get(SeasonTitle, title_id)
    if not t:
        raise HTTPException(404, "That title doesn’t exist.")
    if body.title is not None:
        t.title = body.title.strip()
    if body.reward is not None:
        t.reward = body.reward.strip()
    if body.player is not None:
        t.player_id = _player_id(db, body.player)
    db.commit()
    return {"ok": True}


@router.delete("/api/admin/titles/{title_id}", dependencies=[Depends(auth.require_admin)])
def delete_title(title_id: int, db: Session = Depends(get_db)):
    t = db.get(SeasonTitle, title_id)
    if not t:
        raise HTTPException(404, "That title doesn’t exist.")
    db.delete(t)
    db.commit()
    return {"ok": True}
