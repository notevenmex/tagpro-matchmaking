"""Ranked PUGs: FastAPI backend that also serves the front-end from /static."""
import os
from collections import defaultdict
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .db import Base, Map, MapVote, Match, MatchPlayer, Player, Rating, Season, engine, get_db
from .rating import MIN_GAMES, START_ELO, points, team_delta

SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("Set SESSION_SECRET to a long random string (see .env.example).")

app = FastAPI(title="Ranked PUGs")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax",
                   https_only=os.getenv("HTTPS_ONLY", "0") == "1", max_age=60 * 60 * 24 * 30)
app.include_router(auth.router)
Base.metadata.create_all(engine)

Position = Literal["offense", "defense"]
Team = Literal["red", "blue"]


# ---------- helpers ----------
def active_season(db: Session) -> Season:
    s = db.scalar(select(Season).where(Season.is_active.is_(True)))
    if not s:
        raise HTTPException(503, "No season is active yet. Run `python -m app.seed` or create one.")
    return s


def find_player(db: Session, name: str) -> Player | None:
    return db.scalar(select(Player).where(func.lower(Player.tagpro_name) == name.strip().lower()))


def get_rating(db: Session, player: Player, season: Season, position: str) -> Rating:
    r = db.scalar(select(Rating).where(Rating.player_id == player.id, Rating.season_id == season.id,
                                       Rating.position == position))
    if not r:
        r = Rating(player_id=player.id, season_id=season.id, position=position, elo=START_ELO, games=0, wins=0)
        db.add(r)
        db.flush()
    return r


def player_out(p: Player) -> dict:
    return {"name": p.tagpro_name, "position": p.position, "server": p.server, "tags": p.tags or []}


# ---------- season & account ----------
@app.get("/api/season")
def season(db: Session = Depends(get_db)):
    s = active_season(db)
    return {"number": s.number, "banner": s.banner, "banner_note": s.banner_note}


@app.get("/api/me")
def me(request: Request, db: Session = Depends(get_db)):
    did = request.session.get("discord_id")
    if not did:
        return {"logged_in": False, "discord_configured": bool(auth.CLIENT_ID)}
    p = auth.current_player(request, db)
    return {"logged_in": True, "discord_name": request.session.get("discord_name"),
            "player": player_out(p) if p else None, "is_admin": auth.is_admin(did, p)}


class RegisterIn(BaseModel):
    tagpro_name: str = Field(min_length=1, max_length=24)
    reddit_name: str | None = Field(default=None, max_length=24)
    position: Position
    server: str | None = Field(default=None, max_length=24)


@app.post("/api/register", status_code=201)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    did = request.session.get("discord_id")
    if not did:
        raise HTTPException(401, "Log in with Discord first.")
    if auth.current_player(request, db):
        raise HTTPException(409, "This Discord account is already registered.")
    name = body.tagpro_name.strip()
    if find_player(db, name):
        raise HTTPException(409, f"“{name}” is already taken. If it’s yours, ask an admin to link it to your Discord.")
    p = Player(discord_id=did, tagpro_name=name, reddit_name=(body.reddit_name or "").strip() or None,
               position=body.position, server=body.server or None, tags=[])
    db.add(p)
    db.commit()
    return player_out(p)


# ---------- leaderboard & players ----------
@app.get("/api/leaderboard")
def leaderboard(limit: int = 100, db: Session = Depends(get_db)):
    s = active_season(db)
    rows = db.execute(
        select(Rating, Player).join(Player, Rating.player_id == Player.id)
        .where(Rating.season_id == s.id, Rating.games >= MIN_GAMES)
        .order_by(Rating.elo.desc()).limit(min(max(limit, 1), 500))
    ).all()
    return [{**player_out(p), "rank": i + 1, "position": r.position, "rating": points(r.elo),
             "games": r.games, "wins": r.wins} for i, (r, p) in enumerate(rows)]


@app.get("/api/players")
def players(q: str = "", db: Session = Depends(get_db)):
    stmt = select(Player.tagpro_name).order_by(Player.tagpro_name).limit(500)
    if q:
        stmt = stmt.where(func.lower(Player.tagpro_name).contains(q.strip().lower()))
    return db.scalars(stmt).all()


@app.get("/api/players/{name}")
def player_detail(name: str, db: Session = Depends(get_db)):
    p = find_player(db, name)
    if not p:
        raise HTTPException(404, f"No player named “{name}”.")
    s = active_season(db)
    ratings = db.scalars(select(Rating).where(Rating.player_id == p.id, Rating.season_id == s.id)).all()
    recent = db.execute(
        select(MatchPlayer, Match, Map.name).join(Match, MatchPlayer.match_id == Match.id)
        .outerjoin(Map, Match.map_id == Map.id).where(MatchPlayer.player_id == p.id)
        .order_by(Match.played_at.desc()).limit(10)
    ).all()
    return {**player_out(p),
            "ratings": [{"position": r.position, "rating": points(r.elo), "games": r.games, "wins": r.wins}
                        for r in ratings],
            "recent": [{"map": mname, "team": mp.team, "won": mp.team == m.winner,
                        "change": points(mp.elo_after) - points(mp.elo_before), "played_at": m.played_at.isoformat()}
                       for mp, m, mname in recent]}


# ---------- maps ----------
@app.get("/api/maps")
def maps(request: Request, db: Session = Depends(get_db)):
    me_p = auth.current_player(request, db)
    all_maps = db.scalars(select(Map).where(Map.active.is_(True)).order_by(Map.name)).all()
    rows = db.execute(select(MapVote, Player.tagpro_name).outerjoin(Player, MapVote.player_id == Player.id)).all()
    votes, mine = defaultdict(list), {}
    for v, pname in rows:
        votes[v.map_id].append({"voter": pname or v.voter_label or "?", "score": v.score})
        if me_p and v.player_id == me_p.id:
            mine[v.map_id] = v.score
    totals = {m.id: max(0, sum(x["score"] for x in votes[m.id])) for m in all_maps}
    grand = sum(totals.values()) or 1
    return [{"id": m.id, "name": m.name, "chance": round(totals[m.id] / grand * 100, 2),
             "votes": votes[m.id], "my_vote": mine.get(m.id)} for m in all_maps]


class VoteIn(BaseModel):
    score: int | None = Field(default=None, ge=-5, le=10)  # null removes your vote


@app.put("/api/maps/{map_id}/vote")
def vote(map_id: int, body: VoteIn, p: Player = Depends(auth.require_player), db: Session = Depends(get_db)):
    if not db.get(Map, map_id):
        raise HTTPException(404, "That map doesn’t exist.")
    v = db.scalar(select(MapVote).where(MapVote.map_id == map_id, MapVote.player_id == p.id))
    if body.score is None:
        if v:
            db.delete(v)
    elif v:
        v.score = body.score
    else:
        db.add(MapVote(map_id=map_id, player_id=p.id, score=body.score))
    db.commit()
    return {"ok": True}


# ---------- matches (admin) ----------
class MatchPlayerIn(BaseModel):
    tagpro_name: str
    team: Team
    position: Position


class MatchIn(BaseModel):
    map_id: int | None = None
    winner: Team
    players: list[MatchPlayerIn] = Field(min_length=2, max_length=12)


@app.post("/api/matches", status_code=201)
def report_match(body: MatchIn, admin_id: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    s = active_season(db)
    names = [x.tagpro_name.strip().lower() for x in body.players]
    if len(set(names)) != len(names):
        raise HTTPException(400, "A player is listed more than once.")
    if not any(x.team == "red" for x in body.players) or not any(x.team == "blue" for x in body.players):
        raise HTTPException(400, "Each team needs at least one player.")
    if body.map_id is not None and not db.get(Map, body.map_id):
        raise HTTPException(400, "That map doesn’t exist.")
    found = {x.tagpro_name: find_player(db, x.tagpro_name) for x in body.players}
    missing = [n for n, p in found.items() if p is None]
    if missing:
        raise HTTPException(400, "These players aren’t registered: " + ", ".join(missing))

    entries = [(found[x.tagpro_name], x, get_rating(db, found[x.tagpro_name], s, x.position)) for x in body.players]
    d_red, d_blue = team_delta([r.elo for _, x, r in entries if x.team == "red"],
                               [r.elo for _, x, r in entries if x.team == "blue"], body.winner)
    m = Match(season_id=s.id, map_id=body.map_id, winner=body.winner, reported_by=admin_id)
    db.add(m)
    db.flush()
    changes = []
    for p, x, r in entries:
        before = r.elo
        r.elo += d_red if x.team == "red" else d_blue
        r.games += 1
        r.wins += int(x.team == body.winner)
        db.add(MatchPlayer(match_id=m.id, player_id=p.id, team=x.team, position=x.position,
                           elo_before=before, elo_after=r.elo))
        changes.append({"name": p.tagpro_name, "team": x.team, "before": points(before), "after": points(r.elo)})
    db.commit()
    return {"id": m.id, "changes": changes}


# ---------- health check (Railway pings this before switching traffic to a new deploy) ----------
@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"ok": True}


# ---------- front-end ----------
app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "static", html=True), name="static")
