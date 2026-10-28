"""Season titles and rewards. Admins set up titles for any season (e.g. "Champion", reward
"Gold Discord role") and award each to a player. Awarded titles show next to the player's name
as "S4 Champion"; the 2017 archive tags (players.tags) are no longer shown."""
import os
from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import auth
from .db import Player, PlayerStat, Season, SeasonTitle, StatTitle, get_db

router = APIRouter()


# ---------- automatic stat-leader titles ----------
# Each season, the best per-game average among players with STAT_MIN_GAMES stat games (games read
# from tagpro.eu) holds the title. K/D is tags per pop over the whole season. Holders change as
# games come in, and a past season's leaders stay as they were when it ended.
STAT_TITLES = {  # stat: default title
    "caps": "Cap Leader", "returns": "Return Leader", "hold": "Hold Leader", "kd": "K/D Leader"}
STAT_MIN_GAMES = int(os.getenv("STAT_TITLE_MIN_GAMES", "10"))


def _stat_value(stat: str, n: int, caps: int, returns: int, hold: int, tags: int, pops: int) -> float:
    if stat == "kd":
        return tags / max(pops, 1)
    return {"caps": caps, "returns": returns, "hold": hold}[stat] / n


def _stat_text(stat: str, v: float) -> str:
    if stat == "kd":
        return f"{v:.2f} K/D"
    if stat == "hold":
        s = round(v)
        return f"{s // 60}:{s % 60:02d} hold per game"
    return f"{v:.2f} {stat} per game"


def stat_settings(db: Session) -> dict[str, StatTitle]:
    rows = {r.stat: r for r in db.scalars(select(StatTitle)).all()}
    return {s: rows.get(s) or StatTitle(stat=s, title=t, reward="", enabled=True) for s, t in STAT_TITLES.items()}


def stat_leaders(db: Session, season: Season) -> list[dict]:
    """This season's leader for each enabled stat title (player None while nobody qualifies)."""
    rows = db.execute(
        select(PlayerStat.player_id, Player.tagpro_name, func.count(), func.sum(PlayerStat.caps),
               func.sum(PlayerStat.returns), func.sum(PlayerStat.hold), func.sum(PlayerStat.tags),
               func.sum(PlayerStat.pops))
        .join(Player, PlayerStat.player_id == Player.id)
        .where(PlayerStat.season_id == season.id)
        .group_by(PlayerStat.player_id, Player.tagpro_name)
        .having(func.count() >= STAT_MIN_GAMES)).all()
    out = []
    for stat, cfg in stat_settings(db).items():
        if not cfg.enabled:
            continue
        best = max(((_stat_value(stat, n, *(int(x or 0) for x in sums)), n, name, pid)
                    for pid, name, n, *sums in rows), default=None)
        out.append({"id": None, "stat": stat, "season": season.number, "title": cfg.title, "reward": cfg.reward,
                    "player_id": best[3] if best else None, "player": best[2] if best else None,
                    "value": _stat_text(stat, best[0]) if best else f"Needs {STAT_MIN_GAMES} games with stats"})
    return out


def all_stat_leaders(db: Session) -> list[dict]:
    """Stat leaders for the active season and every season with stats, newest first."""
    with_stats = set(db.scalars(select(PlayerStat.season_id).distinct()).all())
    seasons = db.scalars(select(Season).where(or_(Season.is_active.is_(True), Season.id.in_(with_stats)))
                         .order_by(Season.number.desc())).all()
    return [t for s in seasons for t in stat_leaders(db, s)]


def titles_by_player(db: Session, player_ids) -> dict[int, list[dict]]:
    """Titles per player (awarded by an admin, or held as a stat leader), newest season first."""
    out = defaultdict(list)
    ids = list(player_ids)
    if not ids:
        return out
    rows = db.scalars(select(SeasonTitle).where(SeasonTitle.player_id.in_(ids))
                      .order_by(SeasonTitle.season.desc(), SeasonTitle.id)).all()
    for t in rows:
        out[t.player_id].append({"label": f"S{t.season} {t.title}", "reward": t.reward, "season": t.season})
    wanted = set(ids)
    for t in all_stat_leaders(db):
        if t["player_id"] in wanted:
            out[t["player_id"]].append({"label": f"S{t['season']} {t['title']}", "reward": t["reward"],
                                        "season": t["season"], "value": t["value"]})
    for held in out.values():
        held.sort(key=lambda x: -x["season"])
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
    manual = [title_out(t, name) for t, name in db.execute(stmt).all()]
    stats = [{k: v for k, v in t.items() if k != "player_id"} for t in all_stat_leaders(db)
             if season is None or t["season"] == season]
    return manual + stats


class StatTitleEdit(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=40)
    reward: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None


@router.get("/api/admin/stat-titles", dependencies=[Depends(auth.require_admin)])
def list_stat_titles(db: Session = Depends(get_db)):
    return {"min_games": STAT_MIN_GAMES,
            "titles": [{"stat": s, "title": c.title, "reward": c.reward, "enabled": c.enabled}
                       for s, c in stat_settings(db).items()]}


@router.patch("/api/admin/stat-titles/{stat}", dependencies=[Depends(auth.require_admin)])
def edit_stat_title(stat: Literal["caps", "returns", "hold", "kd"], body: StatTitleEdit, db: Session = Depends(get_db)):
    row = db.get(StatTitle, stat) or StatTitle(stat=stat, title=STAT_TITLES[stat], reward="", enabled=True)
    if body.title is not None:
        row.title = body.title.strip()
    if body.reward is not None:
        row.reward = body.reward.strip()
    if body.enabled is not None:
        row.enabled = body.enabled
    db.add(row)
    db.commit()
    return {"ok": True}


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
