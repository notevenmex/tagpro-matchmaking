"""Lobby chat: an "All" chat for the 8 players of a match and a "Team" chat for each team.

All chat opens when the captains draft starts. A player gets their team's chat as soon as they're on
a team (captains right away, everyone else when they're picked), so it covers the draft, the map veto
and the game itself. Chat stays open for CHAT_AFTER_SECONDS after the score is in, then the log is
kept for good: the players and admins can read it back, and after the match anyone can read the All
chat (team chat stays with the team).

Limits: CHAT_MAX_LEN characters a line, CHAT_BURST lines per CHAT_WINDOW seconds per login, and at
most CHAT_MAX_LINES lines a match. Admins can read any match's chat, post in its All chat, hide
lines and mute a player's Discord login (every alt with it) from chat.
"""
import os
import re
import threading
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import auth, play
from .db import ChatMessage, ChatMute, Lobby, LobbyPlayer, Player, get_db

CHAT_MAX_LEN = int(os.getenv("CHAT_MAX_LEN", 200))
CHAT_BURST = int(os.getenv("CHAT_BURST", 5))
CHAT_WINDOW = float(os.getenv("CHAT_WINDOW", 10))
CHAT_AFTER_SECONDS = int(os.getenv("CHAT_AFTER_SECONDS", 600))
CHAT_MAX_LINES = int(os.getenv("CHAT_MAX_LINES", 1000))
OPEN = ("drafting", "map_veto", "server_vote", "awaiting_group", "in_progress", "disputed")
CONTROL = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")  # incl. invisible and bidi tricks

router = APIRouter()
_sent: dict[str, deque] = defaultdict(deque)  # login -> times of its recent lines, for the rate limit
_rate_lock = threading.Lock()


def is_open(lobby: Lobby) -> bool:
    if lobby.status in OPEN:
        return True
    return lobby.status == "finished" and play.clock() <= (lobby.deadline or 0) + CHAT_AFTER_SECONDS


def _mute(db: Session, discord_id: str | None) -> ChatMute | None:
    if not discord_id:
        return None
    m = db.scalar(select(ChatMute).where(ChatMute.discord_id == discord_id))
    return m if m and m.until > play.clock() else None


def _rate_ok(key: str) -> bool:
    t = play.clock()
    with _rate_lock:
        q = _sent[key]
        while q and q[0] <= t - CHAT_WINDOW:
            q.popleft()
        if len(q) >= CHAT_BURST:
            return False
        q.append(t)
        return True


def _viewer(db: Session, request: Request, lobby: Lobby) -> tuple[Player | None, LobbyPlayer | None, bool]:
    me = auth.current_player(request, db)
    lp = me and db.scalar(select(LobbyPlayer).where(LobbyPlayer.lobby_id == lobby.id,
                                                     LobbyPlayer.player_id == me.id))
    return me, lp, auth.is_admin(request.session.get("discord_id"), me)


def chat_view(db: Session, lobby: Lobby, me: Player | None, lp: LobbyPlayer | None, admin: bool) -> dict:
    """What this viewer may read of a lobby's chat, and whether they can write."""
    if admin:
        channels = ["all", "red", "blue"]
    elif lp:
        channels = ["all"] + ([lp.team] if lp.team else [])
    elif lobby.status == "finished":
        channels = ["all"]  # the Post Match page shows the All chat to everyone
    else:
        channels = []
    q = (select(ChatMessage, Player.tagpro_name).join(Player, ChatMessage.player_id == Player.id)
         .where(ChatMessage.lobby_id == lobby.id, ChatMessage.channel.in_(channels)).order_by(ChatMessage.id))
    if not admin:
        q = q.where(ChatMessage.hidden.is_(False))
    teams = dict(db.execute(select(LobbyPlayer.player_id, LobbyPlayer.team).where(LobbyPlayer.lobby_id == lobby.id)).all())
    mute = _mute(db, me.discord_id) if me else None
    writable = is_open(lobby) and (bool(lp) or admin)
    return {
        "lobby_id": lobby.id, "open": is_open(lobby), "is_admin": admin,
        "channels": channels,
        "can_send": [c for c in channels if writable and (c == "all" or (lp and lp.team == c))] if not mute else [],
        "muted_until": mute.until if mute else None,
        "max_len": CHAT_MAX_LEN,
        "messages": [{"id": m.id, "channel": m.channel, "name": name, "team": None if m.as_admin else teams.get(m.player_id),
                      "admin": m.as_admin, "body": m.body, "at": m.sent_at, "mine": bool(me and m.player_id == me.id),
                      **({"hidden": m.hidden} if admin else {})}
                     for m, name in db.execute(q)],
    }


def _lobby(db: Session, lobby_id: int) -> Lobby:
    lobby = db.get(Lobby, lobby_id)
    if not lobby:
        raise HTTPException(404, "No such match.")
    return lobby


@router.get("/api/lobbies/{lobby_id}/chat")
def read_chat(lobby_id: int, request: Request, db: Session = Depends(get_db)):
    lobby = _lobby(db, lobby_id)
    v = chat_view(db, lobby, *_viewer(db, request, lobby))
    if not v["channels"]:
        raise HTTPException(403, "Only the players can read this chat until the match is over.")
    return v


class ChatIn(BaseModel):
    channel: str = Field(pattern="^(all|team)$")
    body: str = Field(max_length=2000)


@router.post("/api/lobbies/{lobby_id}/chat", status_code=201)
def send_chat(lobby_id: int, body: ChatIn, request: Request, db: Session = Depends(get_db)):
    me = auth.require_player(request, db)
    lobby = _lobby(db, lobby_id)
    _, lp, admin = _viewer(db, request, lobby)
    if not lp and not admin:
        raise HTTPException(403, "You're not in this match.")
    if not is_open(lobby):
        raise HTTPException(409, "This chat is closed.")
    text = " ".join(CONTROL.sub("", body.body).split())
    if not text:
        raise HTTPException(400, "Type a message first.")
    if len(text) > CHAT_MAX_LEN:
        raise HTTPException(400, f"Keep it under {CHAT_MAX_LEN} characters.")
    if body.channel == "team":
        if not lp or not lp.team:
            raise HTTPException(409, "You get team chat once you're on a team.")
        channel = lp.team
    else:
        channel = "all"
    mute = _mute(db, me.discord_id)
    if mute:
        raise HTTPException(403, "You're muted from chat" + (f": {mute.reason}" if mute.reason else "") + ".")
    if db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.lobby_id == lobby.id)) >= CHAT_MAX_LINES:
        raise HTTPException(409, "This match's chat is full.")
    if not _rate_ok(me.discord_id or str(me.id)):
        raise HTTPException(429, "Slow down a little.")
    db.add(ChatMessage(lobby_id=lobby.id, player_id=me.id, channel=channel, body=text, sent_at=play.clock(),
                       as_admin=admin and not lp))
    db.commit()
    play.chat_ping(lobby.id)
    return {"ok": True}


# ---------- moderation (admins) ----------
class HideIn(BaseModel):
    hidden: bool = True


@router.post("/api/chat/{message_id}/hide")
def hide_line(message_id: int, body: HideIn, admin_id: str = Depends(auth.require_admin),
              db: Session = Depends(get_db)):
    m = db.get(ChatMessage, message_id)
    if not m:
        raise HTTPException(404, "No such message.")
    m.hidden, m.hidden_by = body.hidden, admin_id if body.hidden else None
    db.commit()
    play.chat_ping(m.lobby_id)
    return {"ok": True}


class MuteIn(BaseModel):
    name: str = Field(max_length=40)
    minutes: int = Field(ge=0, le=60 * 24 * 365)  # 0 lifts the mute
    reason: str = Field(default="", max_length=200)


@router.post("/api/chat/mute")
def mute(body: MuteIn, admin_id: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    p = db.scalar(select(Player).where(func.lower(Player.tagpro_name) == body.name.strip().lower()))
    if not p or not p.discord_id:
        raise HTTPException(404, "No registered player by that name.")
    db.execute(delete(ChatMute).where(ChatMute.discord_id == p.discord_id))
    if body.minutes:
        db.add(ChatMute(discord_id=p.discord_id, until=play.clock() + body.minutes * 60,
                        reason=body.reason.strip(), muted_by=admin_id))
    db.commit()
    return {"ok": True, "name": p.tagpro_name, "until": play.clock() + body.minutes * 60 if body.minutes else None}


@router.get("/api/chat/mutes")
def mutes(admin_id: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(ChatMute).where(ChatMute.until > play.clock()).order_by(ChatMute.until)).all()
    out = []
    for m in rows:
        names = [p.tagpro_name for p in auth.accounts(db, m.discord_id)]
        out.append({"names": names, "until": m.until, "reason": m.reason})
    return out


@router.get("/api/chat/recent")
def recent(admin_id: str = Depends(auth.require_admin), db: Session = Depends(get_db)):
    """The latest matches with chat, for the admin chat log picker."""
    rows = db.execute(select(ChatMessage.lobby_id, func.count(ChatMessage.id), func.max(ChatMessage.sent_at))
                      .group_by(ChatMessage.lobby_id).order_by(func.max(ChatMessage.sent_at).desc()).limit(20)).all()
    return [{"lobby_id": lid, "lines": n, "last_at": at,
             "status": db.get(Lobby, lid).status} for lid, n, at in rows]
