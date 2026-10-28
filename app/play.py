"""Matchmaking: queue, ready check, captains draft, map veto, server vote and group link.

All state lives in the database. Every change goes through LOCK, and a background loop calls
tick() to handle timeouts (auto-pick, random ban, ready-check failures). Run the app as a
single process (one uvicorn worker): the lock and the WebSocket list are in memory.
"""
import asyncio
import contextlib
import logging
import os
import random
import threading
import time
from collections import Counter
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import auth, restrictions
from .db import (GroupJob, Lobby, LobbyPlayer, Map, Player, QueueBan, QueueEntry, Rating, Season,
                 SessionLocal, get_db)
from .rating import START_ELO, level

log = logging.getLogger("rankedpugs")


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


QUEUE_SIZE = _int("QUEUE_SIZE", 8)               # players per match; must be even
READY_SECONDS = _int("READY_SECONDS", 30)
PICK_SECONDS = _int("PICK_SECONDS", 30)          # per pick, then the best-rated player left is auto-picked
BAN_SECONDS = _int("BAN_SECONDS", 30)            # per ban, then a random map is banned
VOTE_SECONDS = _int("VOTE_SECONDS", 20)
MAP_POOL_SIZE = _int("MAP_POOL_SIZE", 7)
CAPTAIN_MODE = os.getenv("CAPTAIN_MODE", "top")  # top | random
SERVERS = [s.strip() for s in os.getenv("SERVERS", "US East,US Central,US West,Europe").split(",") if s.strip()]

# Red captain picks first (A, B, B, A, A, B, ...); blue captain bans first.
PICK_ORDER = [("red", "blue", "blue", "red")[i % 4] for i in range(QUEUE_SIZE - 2)]
PRE_GAME = ("ready_check", "drafting", "map_veto", "server_vote", "awaiting_group")
TIMED = {"ready_check", "drafting", "map_veto", "server_vote"}
STEP_SECONDS = {"ready_check": READY_SECONDS, "drafting": PICK_SECONDS, "map_veto": BAN_SECONDS,
                "server_vote": VOTE_SECONDS}

LOCK = threading.Lock()
clock = time.time  # tests replace this to move time forward
rng = random.Random()

router = APIRouter()
router.include_router(restrictions.router)


# ---------- helpers ----------
def _season(db: Session) -> Season:
    s = db.scalar(select(Season).where(Season.is_active.is_(True)))
    if not s:
        raise HTTPException(503, "No season is active yet.")
    return s


def _main_elo(db: Session, p: Player, season_id: int) -> float:
    elo = db.scalar(select(Rating.elo).where(Rating.player_id == p.id, Rating.season_id == season_id))
    return START_ELO if elo is None else elo


def _lobby_players(db: Session, lobby: Lobby) -> list[tuple[LobbyPlayer, Player]]:
    return db.execute(select(LobbyPlayer, Player).join(Player, LobbyPlayer.player_id == Player.id)
                      .where(LobbyPlayer.lobby_id == lobby.id).order_by(LobbyPlayer.joined_at)).all()


def _pregame_lobby(db: Session, player_id: int) -> Lobby | None:
    return db.scalar(select(Lobby).join(LobbyPlayer, LobbyPlayer.lobby_id == Lobby.id)
                     .where(LobbyPlayer.player_id == player_id, Lobby.status.in_(PRE_GAME)))


def _picks_done(lps) -> int:
    return sum(1 for lp, _ in lps if lp.pick_no is not None)


def _bans_done(lobby: Lobby) -> int:
    return sum(1 for m in lobby.map_pool if m["banned_by"])


def turn(db: Session, lobby: Lobby) -> str | None:
    """Which captain ("red" or "blue") has to act right now, if any."""
    if lobby.status == "drafting":
        return PICK_ORDER[_picks_done(_lobby_players(db, lobby))]
    if lobby.status == "map_veto":
        return "blue" if _bans_done(lobby) % 2 == 0 else "red"
    return None


# ---------- state transitions ----------
def try_pop(db: Session):
    """Start a lobby for every full group of QUEUE_SIZE players, oldest first."""
    while True:
        entries = db.scalars(select(QueueEntry).order_by(QueueEntry.joined_at, QueueEntry.id).limit(QUEUE_SIZE)).all()
        if len(entries) < QUEUE_SIZE:
            return
        s = _season(db)
        t = clock()
        lobby = Lobby(season_id=s.id, status="ready_check", created_at=t, deadline=t + READY_SECONDS, map_pool=[])
        db.add(lobby)
        db.flush()
        for e in entries:
            p = db.get(Player, e.player_id)
            db.add(LobbyPlayer(lobby_id=lobby.id, player_id=p.id, joined_at=e.joined_at, elo=_main_elo(db, p, s.id)))
            db.delete(e)
        db.flush()


def fail_ready_check(db: Session, lobby: Lobby, offenders: set[int], why: str):
    """Cancel the lobby, give the offenders a cooldown, and put everyone else back in the queue."""
    t = clock()
    names = []
    for lp, p in _lobby_players(db, lobby):
        if lp.player_id in offenders:
            restrictions.penalize_ready_check(db, lp.player_id, lobby.id)
            names.append(p.tagpro_name)
        else:
            db.add(QueueEntry(player_id=lp.player_id, joined_at=lp.joined_at))
    lobby.status, lobby.deadline = "cancelled", t
    lobby.note = f"{', '.join(names)} {why}. Everyone else is back in the queue."[:200]
    db.flush()
    try_pop(db)


def start_draft(db: Session, lobby: Lobby):
    lps = [lp for lp, _ in _lobby_players(db, lobby)]
    if CAPTAIN_MODE == "random":
        caps = rng.sample(lps, 2)
    else:
        caps = sorted(lps, key=lambda lp: (-lp.elo, lp.joined_at))[:2]
    # The lower-rated captain gets first pick to even things out.
    low, high = sorted(caps, key=lambda lp: (lp.elo, -lp.joined_at))
    for lp, team in ((low, "red"), (high, "blue")):
        lp.team, lp.is_captain = team, True
    lobby.status, lobby.deadline = "drafting", clock() + PICK_SECONDS


def do_pick(db: Session, lobby: Lobby, lp: LobbyPlayer):
    lps = _lobby_players(db, lobby)
    n = _picks_done(lps)
    lp.team, lp.pick_no = PICK_ORDER[n], n + 1
    left = [x for x, _ in lps if x.team is None and x is not lp]
    if len(left) == 1:  # last player is a forced pick
        left[0].team, left[0].pick_no = PICK_ORDER[n + 1], n + 2
        left = []
    if left:
        lobby.deadline = clock() + PICK_SECONDS
    else:
        start_veto(db, lobby)


def veto_chance(n_maps: int) -> float:
    """Percent chance that a given map in rotation is drawn into a veto."""
    return min(100.0, MAP_POOL_SIZE / n_maps * 100) if n_maps else 0.0


def _draw_pool(db: Session) -> list[Map]:
    """MAP_POOL_SIZE maps drawn at random from the rotation, every map equally likely. Votes don't
    affect the draw."""
    maps = db.scalars(select(Map).where(Map.active.is_(True)).order_by(Map.id)).all()
    return rng.sample(maps, min(MAP_POOL_SIZE, len(maps)))


def start_veto(db: Session, lobby: Lobby):
    pool = _draw_pool(db)
    lobby.map_pool = [{"id": m.id, "name": m.name, "banned_by": None} for m in pool]
    if len(pool) <= 1:
        lobby.map_id = pool[0].id if pool else None
        start_server_vote(db, lobby)
    else:
        lobby.status, lobby.deadline = "map_veto", clock() + BAN_SECONDS


def do_ban(db: Session, lobby: Lobby, map_id: int, team: str):
    pool = [dict(m) for m in lobby.map_pool]  # new list so SQLAlchemy sees the JSON change
    next(m for m in pool if m["id"] == map_id)["banned_by"] = team
    lobby.map_pool = pool
    left = [m for m in pool if not m["banned_by"]]
    if len(left) == 1:
        lobby.map_id = left[0]["id"]
        start_server_vote(db, lobby)
    else:
        lobby.deadline = clock() + BAN_SECONDS


def start_server_vote(db: Session, lobby: Lobby):
    if len(SERVERS) <= 1:
        lobby.server = SERVERS[0] if SERVERS else None
        lobby.status, lobby.deadline = "awaiting_group", None
    else:
        lobby.status, lobby.deadline = "server_vote", clock() + VOTE_SECONDS


def resolve_server(db: Session, lobby: Lobby):
    votes = Counter(lp.server_vote for lp, _ in _lobby_players(db, lobby) if lp.server_vote)
    if votes:
        top = max(votes.values())
        lobby.server = rng.choice([s for s in SERVERS if votes[s] == top])
    else:
        lobby.server = rng.choice(SERVERS)
    lobby.status, lobby.deadline = "awaiting_group", None


def tick(db: Session) -> bool:
    """Handle every step whose timer ran out. Returns True if anything changed."""
    t = clock()
    changed = False
    for lobby in db.scalars(select(Lobby).where(Lobby.status.in_(TIMED), Lobby.deadline <= t)).all():
        lps = _lobby_players(db, lobby)
        if lobby.status == "ready_check":
            fail_ready_check(db, lobby, {lp.player_id for lp, _ in lps if not lp.accepted}, "didn't accept in time")
        elif lobby.status == "drafting":
            best = max((lp for lp, _ in lps if lp.team is None), key=lambda lp: (lp.elo, -lp.joined_at))
            do_pick(db, lobby, best)
        elif lobby.status == "map_veto":
            team = turn(db, lobby)
            do_ban(db, lobby, rng.choice([m["id"] for m in lobby.map_pool if not m["banned_by"]]), team)
        elif lobby.status == "server_vote":
            resolve_server(db, lobby)
        changed = True
    if auth.DEV_LOGIN:
        changed = _bot_moves(db, t) or changed
    if db.execute(delete(QueueBan).where(QueueBan.until <= t)).rowcount:
        changed = True
    if restrictions.expire_queue(db):
        changed = True
    return changed


# ---------- test bots (DEV_LOGIN only) ----------
BOT_DELAY = 1.5  # seconds a bot "thinks" before acting


def _bot_moves(db: Session, t: float) -> bool:
    """Archived players with no Discord account act as bots so one person can test a full match."""
    changed = False
    for lobby in db.scalars(select(Lobby).where(Lobby.status.in_(TIMED))).all():
        lps = _lobby_players(db, lobby)
        bots = [lp for lp, p in lps if p.discord_id is None]
        if lobby.status == "ready_check":
            for lp in bots:
                if not lp.accepted and t >= lobby.created_at + BOT_DELAY:
                    lp.accepted = changed = True
            if all(lp.accepted for lp, _ in lps):
                start_draft(db, lobby)
            continue
        step = STEP_SECONDS[lobby.status]
        if t < lobby.deadline - step + BOT_DELAY:
            continue
        team = turn(db, lobby)
        bot_captain = any(lp.is_captain and lp.team == team for lp in bots)
        if lobby.status == "drafting" and bot_captain:
            do_pick(db, lobby, max((lp for lp, _ in lps if lp.team is None), key=lambda lp: lp.elo))
            changed = True
        elif lobby.status == "map_veto" and bot_captain:
            do_ban(db, lobby, rng.choice([m["id"] for m in lobby.map_pool if not m["banned_by"]]), team)
            changed = True
        elif lobby.status == "server_vote":
            for lp in bots:
                if not lp.server_vote:
                    lp.server_vote, changed = rng.choice(SERVERS), True
            db.flush()
            if all(lp.server_vote for lp, _ in lps):
                resolve_server(db, lobby)
    return changed


# ---------- live updates ----------
_version = 0
_sockets: set[WebSocket] = set()


def bump():
    global _version
    _version += 1


def _tick_once() -> bool:
    with LOCK, SessionLocal() as db:
        if tick(db):
            db.commit()
            return True
    return False


async def _loop():
    sent = _version
    while True:
        try:
            if await asyncio.to_thread(_tick_once):
                bump()
        except Exception:
            log.exception("matchmaking tick failed")
        if _version != sent:
            sent = _version
            for ws in list(_sockets):
                try:
                    await ws.send_json({"type": "update", "v": sent})
                except Exception:
                    _sockets.discard(ws)
        await asyncio.sleep(0.5)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    from . import autogroup, tagpro_eu, voice  # they import this module, so load them late
    task = asyncio.create_task(_loop())
    bots = [b for b in (autogroup.start(), voice.start(), tagpro_eu.start()) if b]
    yield
    task.cancel()
    for b in bots:
        b.cancel()


@router.websocket("/ws")
async def ws(websocket: WebSocket):
    """Pushes {"type": "update"} whenever matchmaking state changes; clients then re-fetch /api/play."""
    await websocket.accept()
    _sockets.add(websocket)
    try:
        await websocket.send_json({"type": "update", "v": _version})
        while True:
            await websocket.receive_text()  # we don't expect messages; this just waits for disconnect
    except WebSocketDisconnect:
        pass
    finally:
        _sockets.discard(websocket)


# ---------- views ----------
def lobby_view(db: Session, lobby: Lobby, me: Player | None) -> dict:
    from .reporting import score_view
    from . import tagpro_eu, voice  # they import this module
    lps = _lobby_players(db, lobby)
    stats = tagpro_eu.season_stats(db, lobby.season_id, [p.id for _, p in lps])
    mine = next((lp for lp, p in lps if me and p.id == me.id), None)
    t = turn(db, lobby)
    votes = Counter(lp.server_vote for lp, _ in lps if lp.server_vote)
    map_name = db.get(Map, lobby.map_id).name if lobby.map_id else None
    return {
        "id": lobby.id, "status": lobby.status, "deadline": lobby.deadline, "note": lobby.note,
        "turn": t, "pick_order": PICK_ORDER, "step_seconds": STEP_SECONDS.get(lobby.status),
        "players": [{"name": p.tagpro_name, "position": p.position, "rating": level(lp.elo), "elo": round(lp.elo), "tags": p.tags or [],
                     "accepted": lp.accepted, "team": lp.team, "is_captain": lp.is_captain, "pick_no": lp.pick_no,
                     "stats": stats.get(p.id)}  # season totals from tagpro.eu (tagpro_eu.py)
                    for lp, p in lps],
        "map_pool": lobby.map_pool, "map": map_name,
        "servers": [{"name": s, "votes": votes[s]} for s in SERVERS], "server": lobby.server,
        "group_link": lobby.group_link if mine else None,
        "group_bot": db.scalar(select(GroupJob.status).where(GroupJob.lobby_id == lobby.id)),  # see autogroup.py
        "team_name": tagpro_eu.team_name(lobby.id),  # what to call both teams so tagpro.eu can find the game
        "score": score_view(db, lobby, me),
        "voice_link": voice.voice_links(db, lobby.id).get(mine.team) if mine and mine.team else None,  # voice.py
        "me": None if not mine else {
            "team": mine.team, "is_captain": mine.is_captain, "accepted": mine.accepted,
            "server_vote": mine.server_vote, "my_turn": bool(mine.is_captain and t == mine.team)},
    }


def _my_lobby(db: Session, p: Player) -> Lobby | None:
    """The lobby to show this player: a live one, or one that just ended in the last minute."""
    t = clock()
    lobby = db.scalar(select(Lobby).join(LobbyPlayer, LobbyPlayer.lobby_id == Lobby.id)
                      .where(LobbyPlayer.player_id == p.id).order_by(Lobby.id.desc()).limit(1))
    if not lobby or (lobby.status in ("cancelled", "finished") and (lobby.deadline or 0) < t - 60):
        return None
    return lobby


@router.get("/api/play")
def play_state(request: Request, db: Session = Depends(get_db)):
    me = auth.current_player(request, db)
    t = clock()
    queue = db.execute(select(QueueEntry, Player.tagpro_name).join(Player, QueueEntry.player_id == Player.id)
                       .order_by(QueueEntry.joined_at)).all()
    lobby = _my_lobby(db, me) if me else None
    live = db.scalars(select(Lobby).where(Lobby.status.in_(PRE_GAME + ("in_progress", "disputed")))
                      .order_by(Lobby.id.desc()).limit(10)).all()
    mine = next((e for e, _ in queue if me and e.player_id == me.id), None)
    from .reporting import queue_block
    return {"now": t, "size": QUEUE_SIZE, "dev": auth.DEV_LOGIN,
            "queue": [name for _, name in queue],
            "in_queue": mine is not None, "queued_at": mine.joined_at if mine else None,
            **(restrictions.ban_info(db, me.id) if me else {"banned_until": None, "ban_kind": None, "ban_reason": None}),
            "queue_block": queue_block(db, me.id) if me else None,  # captains waiting on a verified score
            "lobby": lobby_view(db, lobby, me) if lobby else None,
            "live": [{"id": x.id, "status": x.status} for x in live]}


@router.get("/api/lobbies/{lobby_id}")
def get_lobby(lobby_id: int, request: Request, db: Session = Depends(get_db)):
    lobby = db.get(Lobby, lobby_id)
    if not lobby:
        raise HTTPException(404, "No such match.")
    return {"now": clock(), **lobby_view(db, lobby, auth.current_player(request, db))}


# ---------- actions ----------
def _act(db: Session, lobby_id: int, p: Player, status: str) -> tuple[Lobby, LobbyPlayer]:
    lobby = db.get(Lobby, lobby_id)
    if not lobby:
        raise HTTPException(404, "No such match.")
    lp = db.scalar(select(LobbyPlayer).where(LobbyPlayer.lobby_id == lobby.id, LobbyPlayer.player_id == p.id))
    if not lp:
        raise HTTPException(403, "You're not in this match.")
    if lobby.status != status:
        raise HTTPException(409, "That step is already over.")
    return lobby, lp


def _captain_turn(db: Session, lobby: Lobby, lp: LobbyPlayer):
    if not lp.is_captain or turn(db, lobby) != lp.team:
        raise HTTPException(403, "It's not your turn.")


def _done(db: Session):
    db.commit()
    bump()
    return {"ok": True}


@router.post("/api/queue")
def join_queue(p: Player = Depends(auth.require_player), db: Session = Depends(get_db)):
    with LOCK:
        t = clock()
        restrictions.check_can_queue(db, p)
        from .reporting import queue_block
        blocked = queue_block(db, p.id)  # captains wait for a verified score; everyone else can go
        if blocked:
            raise HTTPException(409, blocked)
        if not db.scalar(select(QueueEntry).where(QueueEntry.player_id == p.id)):
            db.add(QueueEntry(player_id=p.id, joined_at=t))
            db.flush()
            try_pop(db)
        return _done(db)


@router.delete("/api/queue")
def leave_queue(p: Player = Depends(auth.require_player), db: Session = Depends(get_db)):
    with LOCK:
        db.execute(delete(QueueEntry).where(QueueEntry.player_id == p.id))
        return _done(db)


@router.post("/api/lobbies/{lobby_id}/accept")
def accept(lobby_id: int, p: Player = Depends(auth.require_player), db: Session = Depends(get_db)):
    with LOCK:
        lobby, lp = _act(db, lobby_id, p, "ready_check")
        lp.accepted = True
        db.flush()
        if all(x.accepted for x, _ in _lobby_players(db, lobby)):
            start_draft(db, lobby)
        return _done(db)


@router.post("/api/lobbies/{lobby_id}/decline")
def decline(lobby_id: int, p: Player = Depends(auth.require_player), db: Session = Depends(get_db)):
    with LOCK:
        lobby, _ = _act(db, lobby_id, p, "ready_check")
        fail_ready_check(db, lobby, {p.id}, "declined")
        return _done(db)


class PickIn(BaseModel):
    name: str


@router.post("/api/lobbies/{lobby_id}/pick")
def pick(lobby_id: int, body: PickIn, p: Player = Depends(auth.require_player), db: Session = Depends(get_db)):
    with LOCK:
        lobby, lp = _act(db, lobby_id, p, "drafting")
        _captain_turn(db, lobby, lp)
        target = next((x for x, q in _lobby_players(db, lobby)
                       if q.tagpro_name.lower() == body.name.strip().lower()), None)
        if not target or target.team is not None:
            raise HTTPException(400, "That player isn't available.")
        do_pick(db, lobby, target)
        return _done(db)


class BanIn(BaseModel):
    map_id: int


@router.post("/api/lobbies/{lobby_id}/ban")
def ban(lobby_id: int, body: BanIn, p: Player = Depends(auth.require_player), db: Session = Depends(get_db)):
    with LOCK:
        lobby, lp = _act(db, lobby_id, p, "map_veto")
        _captain_turn(db, lobby, lp)
        if not any(m["id"] == body.map_id and not m["banned_by"] for m in lobby.map_pool):
            raise HTTPException(400, "That map can't be banned.")
        do_ban(db, lobby, body.map_id, lp.team)
        return _done(db)


class ServerIn(BaseModel):
    server: str


@router.post("/api/lobbies/{lobby_id}/server")
def vote_server(lobby_id: int, body: ServerIn, p: Player = Depends(auth.require_player),
                db: Session = Depends(get_db)):
    with LOCK:
        lobby, lp = _act(db, lobby_id, p, "server_vote")
        if body.server not in SERVERS:
            raise HTTPException(400, "Unknown server.")
        lp.server_vote = body.server
        db.flush()
        if all(x.server_vote for x, _ in _lobby_players(db, lobby)):
            resolve_server(db, lobby)
        return _done(db)


class GroupIn(BaseModel):
    link: str = Field(max_length=200)


@router.post("/api/lobbies/{lobby_id}/group")
def set_group(lobby_id: int, body: GroupIn, p: Player = Depends(auth.require_player),
              db: Session = Depends(get_db)):
    with LOCK:
        lobby = db.get(Lobby, lobby_id)
        if lobby and lobby.status == "in_progress":  # let the captain fix a wrong link
            lobby, lp = _act(db, lobby_id, p, "in_progress")
        else:
            lobby, lp = _act(db, lobby_id, p, "awaiting_group")
        if not (lp.is_captain and lp.team == "red"):
            raise HTTPException(403, "The red captain sets up the group.")
        u = urlparse(body.link.strip())
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not (host == "koalabeast.com" or host.endswith(".koalabeast.com")):
            raise HTTPException(400, "Paste the TagPro group link (https://tagpro.koalabeast.com/groups/…).")
        lobby.group_link = body.link.strip()
        lobby.status = "in_progress"
        from . import tagpro_eu  # imports this module
        tagpro_eu.game_started(db, lobby)
        return _done(db)


if auth.DEV_LOGIN:
    @router.post("/api/dev/fill-queue")
    def fill_queue(db: Session = Depends(get_db)):
        """Top the queue up with archived players (bots) so it pops."""
        with LOCK:
            busy = set(db.scalars(select(QueueEntry.player_id))) | set(db.scalars(
                select(LobbyPlayer.player_id).join(Lobby).where(Lobby.status.in_(PRE_GAME))))
            need = QUEUE_SIZE - len(db.scalars(select(QueueEntry.id)).all())
            bots = [p for p in db.scalars(select(Player).where(Player.discord_id.is_(None))) if p.id not in busy]
            t = clock()
            for i, p in enumerate(rng.sample(bots, min(need, len(bots)))):
                db.add(QueueEntry(player_id=p.id, joined_at=t + i * 1e-3))
            db.flush()
            try_pop(db)
            return _done(db)


@router.post("/api/lobbies/{lobby_id}/cancel")
def cancel(lobby_id: int, admin_id: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    with LOCK:
        lobby = db.get(Lobby, lobby_id)
        if not lobby:
            raise HTTPException(404, "No such match.")
        if lobby.status in ("cancelled", "finished"):
            raise HTTPException(409, f"Already {lobby.status}.")
        lobby.status, lobby.deadline, lobby.note = "cancelled", clock(), "Cancelled by an admin."
        return _done(db)
