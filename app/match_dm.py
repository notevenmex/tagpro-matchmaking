"""The "match ready" DM: one message to every player once a lobby's TagPro group link is known.

It has the map and server, the TagPro group link, the player's team voice channel (voice.py), an
invite to the community Discord, and the match rules. Admins edit the invite link and the rules
on the Rules page (stored as SiteText "discord_invite" and "match_rules"); DISCORD_INVITE_URL is
the invite until an admin sets one. If a captain later corrects the group link, everyone gets the
new one.

It's the only DM players get when a match starts: the group bot (autogroup.py) still DMs captains
when it can't make the group, and voice.py only DMs when VOICE_DMS=1.

On by default whenever DISCORD_BOT_TOKEN is set; MATCH_DMS=0 turns it off.
"""
import asyncio
import logging
import os
from dataclasses import dataclass, field

from sqlalchemy import select

from . import play
from .db import GroupJob, Lobby, LobbyPlayer, Map, MatchNotice, Player, SessionLocal, SiteText, VoiceRoom
from .discord_bot import DiscordNotifier

log = logging.getLogger("rankedpugs")

ENABLED = os.getenv("MATCH_DMS", "1") == "1"
DEFAULT_INVITE = os.getenv("DISCORD_INVITE_URL", "")
DEFAULT_RULES = "VC Required\nDon't be toxic"
STALE_SECONDS = 3600  # on startup, skip lobbies older than this so a deploy doesn't re-announce old games


@dataclass
class Recipient:
    discord_id: str
    name: str
    team: str
    is_captain: bool


@dataclass
class Notice:
    lobby_id: int
    link: str
    map_name: str | None
    server: str | None
    resend: bool  # the group link changed after the first DM
    bot_group: bool = False  # the bot made the group, so the first captain in gets leader
    teams: dict[str, list[str]] = field(default_factory=dict)  # team -> names, captain first
    voice: dict[str, str] = field(default_factory=dict)         # team -> channel URL
    recipients: list[Recipient] = field(default_factory=list)
    invite: str = ""
    rules: list[str] = field(default_factory=list)


# ---------- settings (admins edit these on the Rules page; see pages.py) ----------
def settings(db) -> dict:
    invite, rules = db.get(SiteText, "discord_invite"), db.get(SiteText, "match_rules")
    return {"discord_invite": invite.body if invite else DEFAULT_INVITE,
            "rules": rules.body if rules else DEFAULT_RULES}


def rule_lines(text: str) -> list[str]:
    """One rule per line; a leading "1." or "-" is dropped since the DM numbers them."""
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*").strip()
        head, dot, rest = line.partition(".")
        if dot and head.isdigit():
            line = rest.strip()
        if line:
            out.append(line)
    return out


# ---------- database steps (run in a thread, under play.LOCK) ----------
def skip_old_lobbies():
    """Mark games that were already running before this start as announced."""
    t = play.clock()
    with play.LOCK, SessionLocal() as db:
        done = select(MatchNotice.lobby_id)
        for lobby in db.scalars(select(Lobby).where(Lobby.status == "in_progress", Lobby.group_link.is_not(None),
                                                    Lobby.created_at < t - STALE_SECONDS,
                                                    Lobby.id.not_in(done))).all():
            db.add(MatchNotice(lobby_id=lobby.id, link=lobby.group_link, sent=0, sent_at=t))
        db.commit()


def claim_notices() -> list[Notice]:
    """Lobbies with a group link whose players haven't been sent it yet (or were sent an old one)."""
    t = play.clock()
    with play.LOCK, SessionLocal() as db:
        out = []
        for lobby in db.scalars(select(Lobby).where(Lobby.status == "in_progress",
                                                    Lobby.group_link.is_not(None))).all():
            row = db.scalar(select(MatchNotice).where(MatchNotice.lobby_id == lobby.id))
            if row and row.link == lobby.group_link:
                continue
            resend = row is not None
            if row is None:
                row = MatchNotice(lobby_id=lobby.id)
                db.add(row)
            row.link, row.sent_at = lobby.group_link, t
            out.append(_notice(db, lobby, resend))
        db.commit()
        return out


def _notice(db, lobby: Lobby, resend: bool) -> Notice:
    rows = db.execute(select(LobbyPlayer, Player).join(Player, LobbyPlayer.player_id == Player.id)
                      .where(LobbyPlayer.lobby_id == lobby.id)
                      .order_by(LobbyPlayer.is_captain.desc(), LobbyPlayer.pick_no)).all()
    s = settings(db)
    n = Notice(lobby.id, lobby.group_link, db.get(Map, lobby.map_id).name if lobby.map_id else None,
               lobby.server, resend, invite=s["discord_invite"].strip(), rules=rule_lines(s["rules"]),
               bot_group=db.scalar(select(GroupJob.status).where(GroupJob.lobby_id == lobby.id)) == "done")
    for lp, p in rows:
        if lp.team:
            n.teams.setdefault(lp.team, []).append(p.tagpro_name)
            if p.discord_id:
                n.recipients.append(Recipient(p.discord_id, p.tagpro_name, lp.team, lp.is_captain))
    from .voice import channel_url  # imports play too
    for room in db.scalars(select(VoiceRoom).where(VoiceRoom.lobby_id == lobby.id, VoiceRoom.channel_id.is_not(None),
                                                   VoiceRoom.deleted.is_(False))).all():
        n.voice[room.team] = channel_url(room.channel_id)
    return n


def save_sent(lobby_id: int, sent: int):
    with SessionLocal() as db:
        row = db.scalar(select(MatchNotice).where(MatchNotice.lobby_id == lobby_id))
        row.sent += sent
        db.commit()


# ---------- the message ----------
def message(n: Notice, r: Recipient) -> str:
    other = "blue" if r.team == "red" else "red"
    dot = {"red": "🔴", "blue": "🔵"}
    mates = [x for x in n.teams.get(r.team, []) if x != r.name]
    lines = [f"**Match ready: Ranked PUGs #{n.lobby_id}**" + (" (updated group link)" if n.resend else ""),
             f"You're on {dot[r.team]} **{r.team.title()}**" + (" as captain" if r.is_captain else "")
             + (f" with {', '.join(mates)}" if mates else "") + ".",
             f"Against {dot[other]} {', '.join(n.teams.get(other, [])) or 'TBD'}.",
             f"Map: **{n.map_name or 'random'}** · Server: **{n.server or 'any'}**",
             "",
             f"🎮 TagPro group: {n.link}"]
    if n.voice.get(r.team):
        lines.append(f"🔊 Team voice: {n.voice[r.team]}")
    if n.invite:
        lines.append(f"💬 Discord: {n.invite}")
    if r.is_captain and n.bot_group and not n.resend:
        lines.append("You're a captain: join the group first and you'll be made group leader.")
    if n.rules:
        lines += ["", "**Rules**"] + [f"{i}. {rule}" for i, rule in enumerate(n.rules, 1)]
    return "\n".join(lines)


# ---------- the worker ----------
async def run_once(notifier) -> int:
    notices = await asyncio.to_thread(claim_notices)
    for n in notices:
        sent = 0
        for r in n.recipients:
            sent += bool(await notifier.send_dm(r.discord_id, message(n, r)))
        await asyncio.to_thread(save_sent, n.lobby_id, sent)
        log.info("lobby %s: match ready DM sent to %s of %s players", n.lobby_id, sent, len(n.recipients))
    return len(notices)


async def worker(notifier=None, interval: float = 2.0):
    notifier = notifier or DiscordNotifier()
    await asyncio.to_thread(skip_old_lobbies)
    while True:
        try:
            await run_once(notifier)
        except Exception:
            log.exception("match ready DM worker failed")
        await asyncio.sleep(interval)


def start() -> asyncio.Task | None:
    """Start the worker when there's a bot token. Called from the app's lifespan."""
    if not ENABLED or not DiscordNotifier().configured:
        return None
    return asyncio.create_task(worker())
