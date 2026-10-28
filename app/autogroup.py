"""Bot that turns a drafted lobby into a TagPro group and DMs both captains the link.

When a lobby reaches "awaiting_group" (draft, map veto and server vote are done), the worker creates
a private competitive group on TagPro with the chosen map and server, stores the link on the lobby
(which moves it to "in_progress", same as a captain pasting it), and DMs both captains. If the group
can't be created after a few tries, the captains get a DM asking the red captain to make it by hand,
and the site's paste-the-link flow still works.

Turn it on with AUTO_GROUP=1. Needs `pip install -r requirements-bot.txt && playwright install chromium`
for group creation, and DISCORD_BOT_TOKEN for DMs (either one works without the other).
"""
import asyncio
import logging
import os
from typing import Protocol

from sqlalchemy import select

from . import play
from .db import GroupJob, Lobby, LobbyPlayer, Map, Player, SessionLocal
from .discord_bot import DiscordNotifier
from .tagpro_group import Captain, GroupError, GroupSpec, PlaywrightGroupCreator

log = logging.getLogger("rankedpugs")

ENABLED = os.getenv("AUTO_GROUP", "0") == "1"
MAX_ATTEMPTS = int(os.getenv("AUTO_GROUP_ATTEMPTS", "3"))
RETRY_SECONDS = int(os.getenv("AUTO_GROUP_RETRY_SECONDS", "15"))
SITE_URL = os.getenv("SITE_URL", "").rstrip("/")


class GroupCreator(Protocol):
    async def create(self, spec: GroupSpec) -> str: ...


class Notifier(Protocol):
    async def send_dm(self, user_id: str, content: str) -> bool: ...


# ---------- database steps (run in a thread, under play.LOCK) ----------
def _spec(db, lobby: Lobby) -> GroupSpec:
    rows = db.execute(select(LobbyPlayer, Player).join(Player, LobbyPlayer.player_id == Player.id)
                      .where(LobbyPlayer.lobby_id == lobby.id, LobbyPlayer.is_captain.is_(True))
                      .order_by(LobbyPlayer.team.desc())).all()  # red first
    map_name = db.get(Map, lobby.map_id).name if lobby.map_id else None
    return GroupSpec(lobby_id=lobby.id, map_name=map_name, server=lobby.server,
                     captains=[Captain(lp.team, p.tagpro_name, p.discord_id) for lp, p in rows])


def claim_jobs() -> list[GroupSpec]:
    """Mark every lobby that needs a group as being worked on, and return what to create."""
    t = play.clock()
    with play.LOCK, SessionLocal() as db:
        lobbies = db.scalars(select(Lobby).where(Lobby.status == "awaiting_group",
                                                 Lobby.group_link.is_(None))).all()
        specs = []
        for lobby in lobbies:
            job = db.scalar(select(GroupJob).where(GroupJob.lobby_id == lobby.id))
            if job is None:
                job = GroupJob(lobby_id=lobby.id, status="running", attempts=0, next_try=0, error="", dms_sent=0)
                db.add(job)
            elif job.status != "retry" or job.next_try > t:
                continue
            job.status, job.attempts = "running", job.attempts + 1
            specs.append(_spec(db, lobby))
        db.commit()
        return specs


def reset_running():
    """After a restart, jobs left "running" were interrupted; try them again."""
    with play.LOCK, SessionLocal() as db:
        for job in db.scalars(select(GroupJob).where(GroupJob.status == "running")).all():
            job.status, job.next_try = "retry", 0
        db.commit()


def save_link(lobby_id: int, link: str) -> bool:
    """Store the link and start the game, unless the lobby moved on (cancelled, or a captain pasted one)."""
    with play.LOCK, SessionLocal() as db:
        lobby = db.get(Lobby, lobby_id)
        job = db.scalar(select(GroupJob).where(GroupJob.lobby_id == lobby_id))
        job.status, job.error = "done", ""
        ok = lobby.status == "awaiting_group" and lobby.group_link is None
        if ok:
            lobby.group_link, lobby.status = link, "in_progress"
        db.commit()
    if ok:
        play.bump()
    return ok


def save_failure(lobby_id: int, error: str) -> bool:
    """Record a failed attempt. Returns True when we've given up on this lobby."""
    with play.LOCK, SessionLocal() as db:
        job = db.scalar(select(GroupJob).where(GroupJob.lobby_id == lobby_id))
        job.error = error[:300]
        gave_up = job.attempts >= MAX_ATTEMPTS
        job.status = "failed" if gave_up else "retry"
        job.next_try = play.clock() + RETRY_SECONDS
        db.commit()
        return gave_up


def save_dms(lobby_id: int, sent: int):
    with SessionLocal() as db:
        job = db.scalar(select(GroupJob).where(GroupJob.lobby_id == lobby_id))
        job.dms_sent += sent
        db.commit()


# ---------- messages ----------
def _details(spec: GroupSpec) -> str:
    return f"Map: **{spec.map_name or 'random'}** · Server: **{spec.server or 'any'}**"


def ready_message(spec: GroupSpec, c: Captain, link: str) -> str:
    other = next((x.tagpro_name for x in spec.captains if x is not c), "the other captain")
    return (f"**Ranked PUGs match #{spec.lobby_id} is ready.** You're the **{c.team.title()}** captain "
            f"(vs {other}).\n{_details(spec)}\nTagPro group: {link}\n"
            f"Join first and share the link with your team. Competitive settings are already set; "
            f"the first captain in becomes group leader.")


def fallback_message(spec: GroupSpec, c: Captain) -> str:
    where = f" at {SITE_URL}/#play" if SITE_URL else " on the match page"
    ask = ("please create a private TagPro group with competitive settings (no kiss) and paste its link"
           + where) if c.team == "red" else "the red captain will make the group and post the link" + where
    return (f"**Ranked PUGs match #{spec.lobby_id} is ready**, but the bot couldn't create the TagPro group. "
            f"You're the **{c.team.title()}** captain.\n{_details(spec)}\nThe red captain: {ask}.")


# ---------- the worker ----------
async def _dm_captains(notifier: Notifier, spec: GroupSpec, text_for) -> int:
    sent = 0
    for c in spec.captains:
        if c.discord_id:
            sent += bool(await notifier.send_dm(c.discord_id, text_for(c)))
        else:
            log.info("lobby %s: captain %s has no Discord linked; no DM", spec.lobby_id, c.tagpro_name)
    return sent


async def handle(spec: GroupSpec, creator: GroupCreator, notifier: Notifier):
    try:
        link = await creator.create(spec)
    except Exception as e:
        error = str(e) if isinstance(e, GroupError) else f"{type(e).__name__}: {e}"
        log.warning("lobby %s: couldn't create the TagPro group: %s", spec.lobby_id, error)
        if await asyncio.to_thread(save_failure, spec.lobby_id, error):
            sent = await _dm_captains(notifier, spec, lambda c: fallback_message(spec, c))
            await asyncio.to_thread(save_dms, spec.lobby_id, sent)
        return
    log.info("lobby %s: created %s", spec.lobby_id, link)
    if await asyncio.to_thread(save_link, spec.lobby_id, link):
        sent = await _dm_captains(notifier, spec, lambda c: ready_message(spec, c, link))
        await asyncio.to_thread(save_dms, spec.lobby_id, sent)


async def run_once(creator: GroupCreator, notifier: Notifier) -> int:
    specs = await asyncio.to_thread(claim_jobs)
    for spec in specs:  # one at a time: each one opens a browser
        await handle(spec, creator, notifier)
    return len(specs)


async def worker(creator: GroupCreator | None = None, notifier: Notifier | None = None, interval: float = 2.0):
    creator = creator or PlaywrightGroupCreator()
    notifier = notifier or DiscordNotifier()
    await asyncio.to_thread(reset_running)
    while True:
        try:
            await run_once(creator, notifier)
        except Exception:
            log.exception("auto group worker failed")
        await asyncio.sleep(interval)


def start() -> asyncio.Task | None:
    """Start the worker if AUTO_GROUP=1. Called from the app's lifespan."""
    if not ENABLED:
        return None
    try:
        import playwright  # noqa: F401
    except ImportError:
        log.error("AUTO_GROUP=1 but Playwright isn't installed; run "
                  "`pip install -r requirements-bot.txt && playwright install chromium`.")
        return None
    return asyncio.create_task(worker())
