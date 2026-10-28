"""Captain score reporting: both captains enter the final score once the game is played.

The score normally comes from tagpro.eu instead (tagpro_eu.py). While that search is in its first
minutes captains can't report; if it finds nothing, they report as below.

- Matching reports finish the match and update everyone's Elo (rating.record_match).
- Different reports put the match in the admin dispute queue; an admin sets the final score.
  Captains can still correct their own report while it's disputed, and if the two then match,
  the match finishes on its own.
- Both captains are kept out of the queue until their match is finished. The other six players
  can queue again as soon as the game starts.
- A verified score opens the Salty Runback vote (runback.py), which holds all eight players out of
  the queue until it's decided.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth, play, runback, tagpro_eu
from .db import Lobby, LobbyPlayer, Map, Match, Player, ScoreReport, Season, get_db
from .rating import record_match

# A lobby in one of these states still needs a verified score.
UNREPORTED = ("in_progress", "disputed")

router = APIRouter()


class ScoreIn(BaseModel):
    red_score: int = Field(ge=0, le=99)
    blue_score: int = Field(ge=0, le=99)

    @model_validator(mode="after")
    def no_ties(self):
        if self.red_score == self.blue_score:
            raise ValueError("Ranked matches can't end in a tie. Enter the score after overtime.")
        return self


# ---------- helpers ----------
def _reports(db: Session, lobby: Lobby) -> dict[str, ScoreReport]:
    return {r.team: r for r in db.scalars(select(ScoreReport).where(ScoreReport.lobby_id == lobby.id))}


def _same(a: ScoreReport, b: ScoreReport) -> bool:
    return (a.red_score, a.blue_score) == (b.red_score, b.blue_score)


def queue_block(db: Session, player_id: int) -> str | None:
    """Why this player can't queue yet, if they captained a match that has no verified score (or are
    waiting on a Salty Runback vote)."""
    held = runback.hold_reason(db, player_id)
    if held:
        return held
    row = db.execute(select(Lobby, LobbyPlayer.team).join(LobbyPlayer, LobbyPlayer.lobby_id == Lobby.id)
                     .where(LobbyPlayer.player_id == player_id, LobbyPlayer.is_captain.is_(True),
                            Lobby.status.in_(UNREPORTED))).first()
    if not row:
        return None
    lobby, team = row
    if tagpro_eu.captains_wait_until(db, lobby):
        return f"Waiting for the result of match {lobby.id} from tagpro.eu. You can queue once it's in."
    if lobby.status == "disputed":
        return f"The two captains reported different scores for match {lobby.id}. You can queue once an admin settles it."
    if team not in _reports(db, lobby):
        return f"Report the final score of match {lobby.id} before you queue again."
    return f"Waiting for the other captain to confirm the score of match {lobby.id}."


def finish(db: Session, lobby: Lobby, red_score: int, blue_score: int, reported_by: str) -> Match:
    """Save the verified score as a Match, update Elo and close the lobby. Doesn't commit."""
    lineup = [(p, lp.team, p.position) for lp, p in play._lobby_players(db, lobby)]
    m, _ = record_match(db, db.get(Season, lobby.season_id), winner="red" if red_score > blue_score else "blue",
                        lineup=lineup, map_id=lobby.map_id, reported_by=reported_by,
                        queue_id=lobby.queue_id)
    m.red_score, m.blue_score = red_score, blue_score
    lobby.match_id, lobby.status, lobby.deadline = m.id, "finished", play.clock()
    runback.open_vote(db, lobby, reported_by)
    return m


def score_view(db: Session, lobby: Lobby, me: Player | None) -> dict | None:
    """Score reporting state for the match page. Captains see what each side reported; once the
    match is finished everyone sees the final score."""
    if lobby.status not in UNREPORTED + ("finished",):
        return None
    reports = _reports(db, lobby)
    lp = me and db.scalar(select(LobbyPlayer).where(LobbyPlayer.lobby_id == lobby.id, LobbyPlayer.player_id == me.id))
    final = db.get(Match, lobby.match_id) if lobby.match_id else None
    show = bool(lp and lp.is_captain)
    lk = tagpro_eu.lookup(db, lobby.id)
    wait = tagpro_eu.captains_wait_until(db, lobby)
    return {
        "reported": sorted(reports),  # which captains have reported: [], ["red"], ["blue"] or both
        "reports": {t: {"red_score": r.red_score, "blue_score": r.blue_score} for t, r in reports.items()}
        if show else None,
        "can_report": bool(show and lobby.status in UNREPORTED and not wait),
        "final": {"red_score": final.red_score, "blue_score": final.blue_score, "winner": final.winner,
                  "verified_by": final.reported_by if final.reported_by in ("captains", "tagpro.eu") else "admin",
                  "tagpro_eu": tagpro_eu.match_url(lk.tagpro_eu_id) if lk and lk.tagpro_eu_id else None}
        if final else None,
        # the tagpro.eu search: searching (captains_wait_until is set while captains can't report yet),
        # found, gave_up, closed, or None when it's off
        "tagpro_eu": {"status": lk.status, "captains_wait_until": wait, "can_hint": show} if lk else None,
    }


# ---------- captains ----------
@router.post("/api/lobbies/{lobby_id}/report")
def report_score(lobby_id: int, body: ScoreIn, p: Player = Depends(auth.require_player),
                 db: Session = Depends(get_db)):
    with play.LOCK:
        lobby = db.get(Lobby, lobby_id)
        if not lobby:
            raise HTTPException(404, "No such match.")
        lp = db.scalar(select(LobbyPlayer).where(LobbyPlayer.lobby_id == lobby.id, LobbyPlayer.player_id == p.id))
        if not lp:
            raise HTTPException(403, "You're not in this match.")
        if not lp.is_captain:
            raise HTTPException(403, "Only the two captains report the score.")
        if lobby.status not in UNREPORTED:
            raise HTTPException(409, "This match is already finished." if lobby.status == "finished"
                                else "You can report once the game has started.")
        if tagpro_eu.captains_wait_until(db, lobby):
            raise HTTPException(409, "We're still looking for this game on tagpro.eu. If it doesn't turn up, "
                                     "you'll be asked to report the score.")

        reports = _reports(db, lobby)
        mine = reports.get(lp.team)
        if mine:
            mine.red_score, mine.blue_score, mine.reported_at = body.red_score, body.blue_score, play.clock()
        else:
            mine = reports[lp.team] = ScoreReport(lobby_id=lobby.id, team=lp.team, player_id=p.id,
                                                  red_score=body.red_score, blue_score=body.blue_score,
                                                  reported_at=play.clock())
            db.add(mine)
        db.flush()

        other = reports.get("blue" if lp.team == "red" else "red")
        if not other:
            result = "waiting"
        elif _same(mine, other):
            finish(db, lobby, body.red_score, body.blue_score, "captains")
            result = "verified"
        else:
            lobby.status = "disputed"
            result = "disputed"
        play._done(db)
        return {"ok": True, "result": result}


class TagproEuIn(BaseModel):
    link: str = Field(max_length=200)


@router.post("/api/lobbies/{lobby_id}/tagpro-eu")
def tagpro_eu_link(lobby_id: int, body: TagproEuIn, request: Request, db: Session = Depends(get_db)):
    """A captain or admin points the tagpro.eu search at the game's match page. The worker still checks
    that the players and time fit before using it."""
    with play.LOCK:
        lobby = db.get(Lobby, lobby_id)
        if not lobby:
            raise HTTPException(404, "No such match.")
        did = request.session.get("discord_id")
        p = auth.current_player(request, db)
        lp = p and db.scalar(select(LobbyPlayer).where(LobbyPlayer.lobby_id == lobby.id,
                                                       LobbyPlayer.player_id == p.id))
        if not did or not ((lp and lp.is_captain) or auth.is_admin(did, p)):
            raise HTTPException(403, "Only the captains or an admin can do that.")
        if not tagpro_eu.ENABLED:
            raise HTTPException(409, "tagpro.eu lookups are turned off.")
        if lobby.status not in UNREPORTED:
            raise HTTPException(409, "This match is already finished.")
        eu_id = tagpro_eu.parse_match_id(body.link)
        if eu_id is None:
            raise HTTPException(400, "Paste the tagpro.eu match link (https://tagpro.eu/?match=…).")
        tagpro_eu.add_hint(db, lobby, eu_id)
        play._done(db)
        return {"ok": True}


# ---------- admins ----------
@router.get("/api/disputes")
def disputes(admin_id: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    """Matches whose captains reported different scores, oldest first."""
    out = []
    for lobby in db.scalars(select(Lobby).where(Lobby.status == "disputed").order_by(Lobby.id)).all():
        lps = play._lobby_players(db, lobby)
        reports = _reports(db, lobby)
        names = {p.id: p.tagpro_name for _, p in lps}
        out.append({
            "id": lobby.id, "map": db.get(Map, lobby.map_id).name if lobby.map_id else None, "server": lobby.server,
            "group_link": lobby.group_link,
            "teams": {t: [{"name": p.tagpro_name, "is_captain": lp.is_captain} for lp, p in lps if lp.team == t]
                      for t in ("red", "blue")},
            "reports": {t: {"by": names.get(r.player_id), "red_score": r.red_score, "blue_score": r.blue_score}
                        for t, r in reports.items()},
        })
    return out


@router.post("/api/lobbies/{lobby_id}/resolve")
def resolve(lobby_id: int, body: ScoreIn, admin_id: str = Depends(auth.require_admin),
            db: Session = Depends(get_db)):
    """Set the final score of a disputed match. Also works for a match where a captain never
    reported, so an admin can free up the other captain."""
    with play.LOCK:
        lobby = db.get(Lobby, lobby_id)
        if not lobby:
            raise HTTPException(404, "No such match.")
        if lobby.status not in UNREPORTED:
            raise HTTPException(409, "Only a match that's been played and isn't finished can be settled.")
        m = finish(db, lobby, body.red_score, body.blue_score, admin_id)
        play._done(db)
        return {"ok": True, "match_id": m.id}


@router.get("/api/lobbies/{lobby_id}/score")
def get_score(lobby_id: int, request: Request, db: Session = Depends(get_db)):
    lobby = db.get(Lobby, lobby_id)
    if not lobby:
        raise HTTPException(404, "No such match.")
    return score_view(db, lobby, auth.current_player(request, db))
