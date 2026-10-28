"""The bot as group leader for a whole match: launches games when both teams are full, kicks chatty spectators.

The browser side (tagpro_group.PageGroupConn) only reports group state and sends socket events; the
rules live here so they can be tested without a browser.

- Players pick their own team in the group. When Red and Blue both have TEAM_SIZE players, the bot
  counts down in group chat and launches the game. It launches by itself once; after that, anyone on
  Red or Blue types !start to relaunch (a crash, an early end).
- If the teams still aren't full after MISSING_DM_SECONDS, both captains get one DM with the counts,
  and anyone on Red or Blue can type !start to launch with whoever is there.
- While a game is live, anyone in the spectator/waiting slots who chats is kicked from the group,
  once. That's the only consequence. Between games, on the group page, nobody is kicked. A game
  counts as live while the group's "game" event carries a game id, or, if the page hasn't said,
  for GAME_MINUTES after launch. The bot sits on the group page, so this covers group chat;
  spectators typing in the game itself aren't seen. Players on Red or Blue, drafted players (by TagPro name, as a safety net when
  one is sitting in spectators) and the bot itself are never kicked.
- The bot leaves when the match is no longer live (reported, cancelled) or after MAX_HOST_MINUTES.

Socket event names and payloads are the live page's where known; the env overrides exist for the
ones still being confirmed against tagpro.koalabeast.com (see docs/tagpro-groups.md).
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

log = logging.getLogger("rankedpugs")

TEAM_NUMBERS = json.loads(os.getenv("TAGPRO_TEAM_NUMBERS", "") or '{"red": 1, "blue": 2}')
# Group teams on the live page: 0 playing (public), 1 red, 2 blue, 3 spectating, 4 waiting. On launch,
# everyone except WAITING is sent to the game, so the bot parks itself in WAITING to keep the group page.
WAITING_TEAM = 4
TEAM_EVENT = os.getenv("TAGPRO_TEAM_EVENT", "team")
KICK_EVENT = os.getenv("TAGPRO_KICK_EVENT", "kick")
LAUNCH_EVENT = os.getenv("TAGPRO_LAUNCH_EVENT", "groupPlay")
TEAM_SIZE = int(os.getenv("QUEUE_SIZE", "8")) // 2
START_COMMAND = os.getenv("TAGPRO_START_COMMAND", "!start")
COUNTDOWN_SECONDS = int(os.getenv("TAGPRO_COUNTDOWN_SECONDS", "10"))
MISSING_DM_SECONDS = int(os.getenv("TAGPRO_MISSING_DM_SECONDS", "180"))
MAX_HOST_MINUTES = int(os.getenv("TAGPRO_MAX_HOST_MINUTES", "90"))
GAME_MINUTES = int(os.getenv("TAGPRO_GAME_MINUTES", "15"))  # 8 minutes plus overtime and loading
POLL_SECONDS = 2.0


@dataclass
class Seat:
    tagpro_name: str
    team: str          # red | blue
    is_captain: bool = False


class GroupConn(Protocol):
    async def state(self) -> dict:
        """{"you": id, "members": {id: {"id", "name", "team", ...}}, "chats": [new chat messages],
        "in_game": True/False if the page knows whether a game is running, else None}"""
    async def emit(self, event: str, payload=None): ...
    async def say(self, text: str): ...


def _name(m: dict | None) -> str:
    return str((m or {}).get("name") or "").strip().lower()


def chat_sender(chat: dict, members: dict) -> dict | None:
    """The member who sent a chat message. TagPro's chat names the sender by id or by name."""
    for key in ("from", "id", "playerId", "sender"):
        v = chat.get(key)
        if v is not None and str(v) in members:
            return members[str(v)]
    who = str(chat.get("from") or chat.get("name") or "").strip().lower()
    return next((m for m in members.values() if who and _name(m) == who), None)


def chat_text(chat: dict) -> str:
    return str(chat.get("message") or chat.get("text") or "").strip()


class GroupHost:
    def __init__(self, conn: GroupConn, lobby_id: int, seats: list[Seat],
                 is_live: Callable[[], Awaitable[bool]],
                 on_missing: Callable[[str], Awaitable[None]],
                 clock: Callable[[], float] = time.monotonic):
        self.conn, self.lobby_id, self.seats = conn, lobby_id, seats
        self.is_live, self.on_missing, self.clock = is_live, on_missing, clock
        self.drafted = {s.tagpro_name.strip().lower() for s in seats}
        self.started = clock()
        self.launches = 0
        self.countdown_at: float | None = None   # when the pending launch fires
        self.warned_missing = False
        self.kicked: set[str] = set()
        self.launched_at: float | None = None
        self.parked = False

    async def run(self):
        while self.clock() - self.started < MAX_HOST_MINUTES * 60 and await self.is_live():
            await self.step()
            await asyncio.sleep(POLL_SECONDS)
        log.info("lobby %s: done hosting the TagPro group (%s launches)", self.lobby_id, self.launches)

    def _team_of(self, m: dict) -> str | None:
        t = str(m.get("team"))
        return next((name for name, num in TEAM_NUMBERS.items() if str(num) == t), None)

    async def step(self):
        st = await self.conn.state()
        you = str(st.get("you"))
        me = (st.get("members") or {}).get(you)
        if me is not None and str(me.get("team")) != str(WAITING_TEAM) and not self.parked:
            self.parked = True
            await self.conn.emit(TEAM_EVENT, {"id": me.get("id"), "team": WAITING_TEAM})
        members = {str(k): m for k, m in (st.get("members") or {}).items() if str(k) != you}
        force = await self._read_chat(st.get("chats") or [], members, you, self._game_live(st))
        counts = {team: 0 for team in TEAM_NUMBERS}
        for m in members.values():
            team = self._team_of(m)
            if team:
                counts[team] += 1
        full = all(n >= TEAM_SIZE for n in counts.values())

        if force:
            await self._launch("a player typed " + START_COMMAND)
            return
        if not full:
            if self.countdown_at is not None:
                self.countdown_at = None
                await self.conn.say("Launch cancelled: waiting for both teams to have " + str(TEAM_SIZE) + ".")
            if self.launches == 0 and not self.warned_missing and self.clock() - self.started >= MISSING_DM_SECONDS:
                self.warned_missing = True
                await self.on_missing(", ".join(f"{t.title()} {n}/{TEAM_SIZE}" for t, n in counts.items()))
            return
        if self.launches == 0 and self.countdown_at is None:
            self.countdown_at = self.clock() + COUNTDOWN_SECONDS
            await self.conn.say(f"Both teams are full. Launching in {COUNTDOWN_SECONDS} seconds. GLHF!")
        elif self.countdown_at is not None and self.clock() >= self.countdown_at:
            await self._launch("both teams are full")

    def _game_live(self, st: dict) -> bool:
        if st.get("in_game") is not None:
            return bool(st["in_game"])
        return self.launched_at is not None and self.clock() - self.launched_at < GAME_MINUTES * 60

    async def _read_chat(self, chats: list, members: dict, you: str, game_live: bool) -> bool:
        force = False
        for chat in chats:
            if not isinstance(chat, dict):
                continue
            sender = chat_sender(chat, members)
            if sender is None or str(sender.get("id")) == you:
                continue  # system messages, the bot itself, or someone who already left
            if self._team_of(sender):
                force = force or chat_text(chat).lower() == START_COMMAND
            elif game_live and _name(sender) not in self.drafted:
                sid = str(sender.get("id"))
                if sid not in self.kicked:
                    self.kicked.add(sid)
                    log.info("lobby %s: kicking %s from the group for chatting as a spectator",
                             self.lobby_id, sender.get("name"))
                    await self.conn.emit(KICK_EVENT, sender.get("id"))
        return force

    async def _launch(self, why: str):
        self.countdown_at = None
        self.launches += 1
        self.launched_at = self.clock()
        log.info("lobby %s: launching game %s (%s)", self.lobby_id, self.launches, why)
        await self.conn.emit(LAUNCH_EVENT)
