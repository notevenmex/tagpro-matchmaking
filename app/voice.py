"""Temporary team voice channels on the community Discord, one Red and one Blue per match.

Once the draft is done (the lobby reaches map_veto), the bot creates two voice channels under
VOICE_CATEGORY_ID. Anyone on the server can join them, unless VOICE_PRIVATE=1 limits each one to that
team's players (plus staff and the bot). Players already sitting in a voice channel on the server
are moved straight into their team's; everyone gets a DM with a link to it. When the lobby is over (finished, disputed or cancelled), the channels are
deleted after VOICE_GRACE_SECONDS, and anyone still in them is moved to VOICE_LOBBY_CHANNEL_ID
first if that's set.

Turn it on with VOICE_CHANNELS=1. Needs DISCORD_BOT_TOKEN, DISCORD_GUILD_ID and VOICE_CATEGORY_ID;
docs/discord-server.md has the server setup and the bot's permissions.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy import select

from . import play
from .db import Lobby, LobbyPlayer, Player, SessionLocal, VoiceRoom
from .discord_bot import DiscordNotifier

log = logging.getLogger("rankedpugs")

ENABLED = os.getenv("VOICE_CHANNELS", "0") == "1"
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")
CATEGORY_ID = os.getenv("VOICE_CATEGORY_ID", "")
LOBBY_CHANNEL_ID = os.getenv("VOICE_LOBBY_CHANNEL_ID", "")  # where players go when a match's channels close
STAFF_ROLE_ID = os.getenv("VOICE_STAFF_ROLE_ID", "")        # can see and join every team channel
GRACE_SECONDS = int(os.getenv("VOICE_GRACE_SECONDS", "120"))
MAX_HOURS = float(os.getenv("VOICE_MAX_HOURS", "3"))        # safety net if a lobby never ends
SEND_DMS = os.getenv("VOICE_DMS", "1") == "1"
PRIVATE = os.getenv("VOICE_PRIVATE", "0") == "1"  # 1 = only that team (and staff) can join; 0 = anyone can

# Teams are final once the draft is over; the channels stay up until the game is.
OPEN = ("map_veto", "server_vote", "awaiting_group", "in_progress")

# Discord permission bits
VIEW, STREAM, SEND, HISTORY = 1 << 10, 1 << 9, 1 << 11, 1 << 16
CONNECT, SPEAK, MOVE, VAD = 1 << 20, 1 << 21, 1 << 24, 1 << 25
MANAGE = 1 << 4
PLAYER_ALLOW = VIEW | CONNECT | SPEAK | VAD | STREAM | SEND | HISTORY
BOT_ALLOW = PLAYER_ALLOW | MANAGE | MOVE


class Api(Protocol):
    token: str

    async def request(self, method: str, path: str, body: dict | None = None, reason: str = "") -> httpx.Response: ...

    async def send_dm(self, user_id: str, content: str) -> bool: ...


@dataclass
class Member:
    name: str
    discord_id: str | None


@dataclass
class Room:
    lobby_id: int
    team: str
    players: list[Member]
    channel_id: str | None = None


def channel_url(channel_id: str) -> str:
    return f"https://discord.com/channels/{GUILD_ID}/{channel_id}"


def channel_name(lobby_id: int, team: str) -> str:
    return f"{'🔴' if team == 'red' else '🔵'} Match {lobby_id} · {team.title()}"


# ---------- database steps (run in a thread, under play.LOCK) ----------
_retry_at: dict[tuple[int, str], float] = {}  # (lobby, team) -> when to try creating a failed channel again
RETRY_SECONDS = 30


def claim_new_rooms() -> list[Room]:
    """Reserve a row for each team channel that should exist but doesn't yet, and return them."""
    t = play.clock()
    with play.LOCK, SessionLocal() as db:
        rooms = []
        for lobby in db.scalars(select(Lobby).where(Lobby.status.in_(OPEN))).all():
            have = set(db.scalars(select(VoiceRoom.team).where(VoiceRoom.lobby_id == lobby.id)).all())
            todo = [team for team in ("red", "blue") if team not in have and _retry_at.get((lobby.id, team), 0) <= t]
            if not todo:
                continue
            rows = db.execute(select(LobbyPlayer, Player).join(Player, LobbyPlayer.player_id == Player.id)
                              .where(LobbyPlayer.lobby_id == lobby.id)
                              .order_by(LobbyPlayer.is_captain.desc(), LobbyPlayer.pick_no)).all()
            for team in todo:
                db.add(VoiceRoom(lobby_id=lobby.id, team=team, created_at=t))
                rooms.append(Room(lobby.id, team, [Member(p.tagpro_name, p.discord_id)
                                                   for lp, p in rows if lp.team == team]))
        db.commit()
        return rooms


def save_channel(room: Room):
    with play.LOCK, SessionLocal() as db:
        row = db.scalar(select(VoiceRoom).where(VoiceRoom.lobby_id == room.lobby_id, VoiceRoom.team == room.team))
        row.channel_id = room.channel_id
        db.commit()
    play.bump()  # so the match page can show the "join voice" link


def drop_room(lobby_id: int, team: str):
    """Creating the channel failed: forget the row, and try again in a little while."""
    _retry_at[(lobby_id, team)] = play.clock() + RETRY_SECONDS
    with play.LOCK, SessionLocal() as db:
        row = db.scalar(select(VoiceRoom).where(VoiceRoom.lobby_id == lobby_id, VoiceRoom.team == team))
        if row:
            db.delete(row)
            db.commit()


def mark_deleted(lobby_id: int, team: str):
    with play.LOCK, SessionLocal() as db:
        row = db.scalar(select(VoiceRoom).where(VoiceRoom.lobby_id == lobby_id, VoiceRoom.team == team))
        row.deleted = True
        db.commit()
    play.bump()


def rooms_to_close() -> list[Room]:
    """Mark rooms whose lobby is over, and return the ones whose grace period has run out."""
    t = play.clock()
    with play.LOCK, SessionLocal() as db:
        out = []
        for row in db.scalars(select(VoiceRoom).where(VoiceRoom.deleted.is_(False))).all():
            lobby = db.get(Lobby, row.lobby_id)
            if row.ended_at is None and (lobby is None or lobby.status not in OPEN
                                         or t - row.created_at > MAX_HOURS * 3600):
                row.ended_at = t
            if row.ended_at is not None and t - row.ended_at >= GRACE_SECONDS and row.channel_id:
                ids = db.scalars(select(Player.discord_id).join(LobbyPlayer, LobbyPlayer.player_id == Player.id)
                                 .where(LobbyPlayer.lobby_id == row.lobby_id, LobbyPlayer.team == row.team,
                                        Player.discord_id.is_not(None))).all()
                out.append(Room(row.lobby_id, row.team, [Member("", i) for i in ids], row.channel_id))
        db.commit()
        return out


def known_channels() -> set[str]:
    with SessionLocal() as db:
        return set(db.scalars(select(VoiceRoom.channel_id).where(VoiceRoom.channel_id.is_not(None),
                                                         VoiceRoom.deleted.is_(False))).all())


def forget_unfinished():
    """Rows with no channel were interrupted mid-create (a restart); drop them so they're made again."""
    with play.LOCK, SessionLocal() as db:
        for row in db.scalars(select(VoiceRoom).where(VoiceRoom.channel_id.is_(None))).all():
            db.delete(row)
        db.commit()


def voice_links(db, lobby_id: int) -> dict[str, str]:
    """{"red": url, "blue": url} for a lobby's open channels, for the match page."""
    rows = db.scalars(select(VoiceRoom).where(VoiceRoom.lobby_id == lobby_id, VoiceRoom.channel_id.is_not(None),
                                              VoiceRoom.ended_at.is_(None), VoiceRoom.deleted.is_(False))).all()
    return {r.team: channel_url(r.channel_id) for r in rows}


# ---------- Discord steps ----------
_bot_id: str | None = None


async def bot_user_id(api: Api) -> str:
    global _bot_id
    if _bot_id is None:
        r = await api.request("GET", "/users/@me")
        if r.status_code != 200:
            raise RuntimeError(f"couldn't look up the bot's own user (bad DISCORD_BOT_TOKEN?): {r.status_code}")
        _bot_id = r.json()["id"]
    return _bot_id


async def create_channel(api: Api, room: Room) -> str:
    everyone = ({"id": GUILD_ID, "type": 0, "deny": str(VIEW | CONNECT), "allow": "0"} if PRIVATE
                else {"id": GUILD_ID, "type": 0, "allow": str(VIEW | CONNECT | SPEAK | VAD), "deny": "0"})
    overwrites = [everyone,
                  {"id": await bot_user_id(api), "type": 1, "allow": str(BOT_ALLOW), "deny": "0"}]
    if STAFF_ROLE_ID:
        overwrites.append({"id": STAFF_ROLE_ID, "type": 0, "allow": str(PLAYER_ALLOW | MOVE), "deny": "0"})
    for m in room.players:
        if m.discord_id:
            overwrites.append({"id": m.discord_id, "type": 1, "allow": str(PLAYER_ALLOW), "deny": "0"})
    body = {"name": channel_name(room.lobby_id, room.team), "type": 2, "permission_overwrites": overwrites}
    if CATEGORY_ID:
        body["parent_id"] = CATEGORY_ID
    r = await api.request("POST", f"/guilds/{GUILD_ID}/channels", body, reason=f"Ranked PUGs match {room.lobby_id}")
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
    return r.json()["id"]


async def current_voice(api: Api, user_id: str) -> str | None:
    """The voice channel this member is in on our server, or None."""
    r = await api.request("GET", f"/guilds/{GUILD_ID}/voice-states/{user_id}")
    return r.json().get("channel_id") if r.status_code == 200 else None


async def move(api: Api, user_id: str, channel_id: str) -> bool:
    """Move a member who's already in voice. Discord can't pull someone into voice who isn't connected."""
    r = await api.request("PATCH", f"/guilds/{GUILD_ID}/members/{user_id}", {"channel_id": channel_id},
                          reason="Ranked PUGs team voice")
    return r.status_code < 300


def invite_message(room: Room, moved: bool) -> str:
    mates = ", ".join(m.name for m in room.players)
    head = (f"**Ranked PUGs match #{room.lobby_id}**: you're on **{room.team.title()}** "
            f"with {mates}.")
    if moved:
        return f"{head}\nI moved you into your team's voice channel: {channel_url(room.channel_id)}"
    return f"{head}\nJoin your team's voice channel (required): {channel_url(room.channel_id)}"


async def open_room(api: Api, room: Room):
    try:
        room.channel_id = await create_channel(api, room)
    except Exception as e:
        log.warning("lobby %s: couldn't create the %s voice channel: %s", room.lobby_id, room.team, e)
        await asyncio.to_thread(drop_room, room.lobby_id, room.team)  # try again next round
        return
    await asyncio.to_thread(save_channel, room)
    log.info("lobby %s: %s voice channel %s", room.lobby_id, room.team, room.channel_id)
    for m in room.players:
        if not m.discord_id:
            continue
        moved = False
        try:
            if await current_voice(api, m.discord_id):
                moved = await move(api, m.discord_id, room.channel_id)
        except httpx.HTTPError as e:
            log.warning("lobby %s: couldn't move %s: %s", room.lobby_id, m.name, e)
        if SEND_DMS:
            await api.send_dm(m.discord_id, invite_message(room, moved))


async def close_room(api: Api, room: Room):
    if LOBBY_CHANNEL_ID:
        for m in room.players:
            try:
                if await current_voice(api, m.discord_id) == room.channel_id:
                    await move(api, m.discord_id, LOBBY_CHANNEL_ID)
            except httpx.HTTPError:
                pass  # deleting the channel disconnects them anyway
    r = await api.request("DELETE", f"/channels/{room.channel_id}", reason=f"Ranked PUGs match {room.lobby_id} over")
    if r.status_code < 300 or r.status_code == 404:  # 404: someone already deleted it by hand
        await asyncio.to_thread(mark_deleted, room.lobby_id, room.team)
        log.info("lobby %s: deleted the %s voice channel", room.lobby_id, room.team)
    else:
        log.warning("lobby %s: couldn't delete voice channel %s: %s %s", room.lobby_id, room.channel_id,
                    r.status_code, r.text[:200])


async def sweep_orphans(api: Api):
    """Delete match channels in our category that we've lost track of (e.g. a crash mid-create)."""
    if not CATEGORY_ID:
        return
    r = await api.request("GET", f"/guilds/{GUILD_ID}/channels")
    if r.status_code != 200:
        return
    known = await asyncio.to_thread(known_channels)
    for ch in r.json():
        if (ch.get("parent_id") == CATEGORY_ID and ch.get("type") == 2 and " Match " in ch.get("name", "")
                and ch["id"] not in known):
            await api.request("DELETE", f"/channels/{ch['id']}", reason="Ranked PUGs: leftover match channel")
            log.info("deleted leftover voice channel %s", ch.get("name"))


# ---------- the worker ----------
async def run_once(api: Api) -> tuple[int, int]:
    opened = await asyncio.to_thread(claim_new_rooms)
    for room in opened:
        await open_room(api, room)
    closing = await asyncio.to_thread(rooms_to_close)
    for room in closing:
        await close_room(api, room)
    return len(opened), len(closing)


async def worker(api: Api | None = None, interval: float = 2.0):
    api = api or DiscordNotifier()
    await asyncio.to_thread(forget_unfinished)
    try:
        await sweep_orphans(api)
    except Exception:
        log.exception("voice channel sweep failed")
    while True:
        try:
            await run_once(api)
        except Exception:
            log.exception("voice channel worker failed")
        await asyncio.sleep(interval)


def start() -> asyncio.Task | None:
    """Start the worker if VOICE_CHANNELS=1. Called from the app's lifespan."""
    if not ENABLED:
        return None
    missing = [n for n, v in (("DISCORD_BOT_TOKEN", DiscordNotifier().token), ("DISCORD_GUILD_ID", GUILD_ID),
                              ("VOICE_CATEGORY_ID", CATEGORY_ID)) if not v]
    if missing:
        log.error("VOICE_CHANNELS=1 but %s isn't set; team voice channels are off.", ", ".join(missing))
        return None
    return asyncio.create_task(worker())
