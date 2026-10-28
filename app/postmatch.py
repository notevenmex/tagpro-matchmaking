"""The Post Match page: one permanent page per match (/match/<id>, shown at /#match/<id>).

It has the final score, both teams with each player's Elo change and tagpro.eu stats, the tagpro.eu
link, the map (so players can rate it) and, right after the game, the Salty Runback vote
(runback.py). Players see it as a pop-up when their match finishes, and every match on a profile's
recent-matches list links to it.

Players choose whether others can see their recent matches, and how many are listed (PlayerPrefs).
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth, runback, tagpro_eu
from .db import Lobby, LobbyPlayer, Map, Match, MatchPlayer, Player, PlayerPrefs, PlayerStat, Rating, get_db
from .rating import PLACEMENT_GAMES

RECENT_CHOICES = (5, 10, 20)
STATS = ("caps", "grabs", "hold", "returns", "tags", "pops", "prevent", "drops", "played")

router = APIRouter()


def prefs(db: Session, player_id: int) -> PlayerPrefs:
    """The player's settings, or the defaults (not saved) if they never changed them."""
    return db.get(PlayerPrefs, player_id) or PlayerPrefs(player_id=player_id, show_recent=True, recent_count=10)


def _placed(db: Session, player_id: int, season_id: int) -> bool:
    games = db.scalar(select(Rating.games).where(Rating.player_id == player_id, Rating.season_id == season_id))
    return (games or 0) >= PLACEMENT_GAMES


def verified_by(m: Match) -> str:
    return m.reported_by if m.reported_by in ("captains", "tagpro.eu") else "admin"


def match_view(db: Session, m: Match, me: Player | None) -> dict:
    lobby = db.scalar(select(Lobby).where(Lobby.match_id == m.id))
    rows = db.execute(select(MatchPlayer, Player).join(Player, MatchPlayer.player_id == Player.id)
                      .where(MatchPlayer.match_id == m.id).order_by(MatchPlayer.id)).all()
    stats = {s.player_id: s for s in db.scalars(select(PlayerStat).where(PlayerStat.match_id == m.id))}
    caps = {lp.player_id: lp.is_captain for lp in
            db.scalars(select(LobbyPlayer).where(LobbyPlayer.lobby_id == lobby.id))} if lobby else {}
    lk = tagpro_eu.lookup(db, lobby.id) if lobby else None
    eu_id = lk.tagpro_eu_id if lk else None
    if eu_id is None:
        eu_id = next((s.tagpro_eu_id for s in stats.values()), None)

    def player(mp: MatchPlayer, p: Player) -> dict:
        st = stats.get(p.id)
        placed = _placed(db, p.id, m.season_id)  # Elo stays hidden during placements, like everywhere else
        return {"name": p.tagpro_name, "position": mp.position, "is_captain": caps.get(p.id, False),
                "elo_after": mp.elo_after if placed else None,
                "change": mp.elo_after - mp.elo_before if placed else None,
                "stats": {k: getattr(st, k) for k in STATS} if st else None}

    mine = next((mp for mp, p in rows if me and p.id == me.id), None)
    return {
        "id": m.id, "lobby_id": lobby.id if lobby else None, "played_at": m.played_at.isoformat(),
        "map": db.get(Map, m.map_id).name if m.map_id else None, "server": lobby.server if lobby else None,
        "red_score": m.red_score, "blue_score": m.blue_score, "winner": m.winner, "verified_by": verified_by(m),
        "tagpro_eu": tagpro_eu.match_url(eu_id) if eu_id else None,
        "teams": {t: sorted((player(mp, p) for mp, p in rows if mp.team == t), key=lambda x: not x["is_captain"])
                  for t in ("red", "blue")},
        "runback": runback.view(db, lobby, me) if lobby else None,
        "runback_of": runback.runback_of(db, lobby.id) if lobby else None,
        "me": {"team": mine.team} if mine else None,
    }


@router.get("/api/matches/{match_id}")
def get_match(match_id: int, request: Request, db: Session = Depends(get_db)):
    m = db.get(Match, match_id)
    if not m:
        raise HTTPException(404, "No such match.")
    return match_view(db, m, auth.current_player(request, db))


@router.get("/match/{match_id}", include_in_schema=False)
def match_link(match_id: int):
    """A short, shareable link to a match's page."""
    return RedirectResponse(f"/#match/{match_id}")


def recent_matches(db: Session, p: Player, viewer_did: str | None) -> dict:
    """The profile's recent-matches list, unless the player hid it from others."""
    pr = prefs(db, p.id)
    own = bool(viewer_did and p.discord_id == viewer_did)
    out = {"recent_hidden": not pr.show_recent, "recent_count": pr.recent_count, "own": own, "recent": []}
    if not pr.show_recent and not own:
        return out
    placed = {}
    rows = db.execute(
        select(MatchPlayer, Match, Map.name).join(Match, MatchPlayer.match_id == Match.id)
        .outerjoin(Map, Match.map_id == Map.id).where(MatchPlayer.player_id == p.id)
        .order_by(Match.played_at.desc(), Match.id.desc()).limit(pr.recent_count)).all()
    for mp, m, mname in rows:
        if m.season_id not in placed:
            placed[m.season_id] = _placed(db, p.id, m.season_id)
        out["recent"].append({
            "match_id": m.id, "map": mname, "team": mp.team, "position": mp.position, "won": mp.team == m.winner,
            "red_score": m.red_score, "blue_score": m.blue_score,
            # Elo changes stay hidden until placements are done, like the Elo itself
            "change": mp.elo_after - mp.elo_before if placed[m.season_id] else None,
            "played_at": m.played_at.isoformat()})
    return out


class HistoryIn(BaseModel):
    show: bool | None = None
    count: Literal[5, 10, 20] | None = None


@router.patch("/api/me/match-history")
def set_history(body: HistoryIn, p: Player = Depends(auth.require_player), db: Session = Depends(get_db)):
    """Show or hide your recent matches on your profile, and pick how many are listed."""
    pr = db.get(PlayerPrefs, p.id)
    if not pr:
        pr = PlayerPrefs(player_id=p.id, show_recent=True, recent_count=10)
        db.add(pr)
    if body.show is not None:
        pr.show_recent = body.show
    if body.count is not None:
        pr.recent_count = body.count
    db.commit()
    return {"show": pr.show_recent, "count": pr.recent_count}
