"""Discord login (OAuth2, 'identify' scope only) and permission helpers."""
import logging
import os
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Player, get_db

log = logging.getLogger("rankedpugs")
DISCORD_API = "https://discord.com/api"
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8000/auth/callback")
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_DISCORD_IDS", "").split(",") if x.strip()}
DEV_LOGIN = os.getenv("DEV_LOGIN") == "1"

router = APIRouter(prefix="/auth")


@router.get("/login")
def login(request: Request):
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(503, "Discord login isn't configured. Set DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET.")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    q = urlencode({"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "response_type": "code",
                   "scope": "identify", "state": state, "prompt": "none"})
    return RedirectResponse(f"https://discord.com/oauth2/authorize?{q}", 303)


@router.get("/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None):
    expected = request.session.pop("oauth_state", None)
    fail = RedirectResponse("/?login=failed#home", 303)
    if not code or not state or not expected or not secrets.compare_digest(state, expected):
        return fail
    async with httpx.AsyncClient(timeout=10) as c:
        tok = await c.post(f"{DISCORD_API}/oauth2/token", auth=(CLIENT_ID, CLIENT_SECRET),
                           data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI})
        if tok.status_code != 200:
            log.warning("Discord token exchange failed: %s", tok.text[:200])
            return fail
        me = await c.get(f"{DISCORD_API}/users/@me",
                         headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
        if me.status_code != 200:
            return fail
    user = me.json()
    request.session["discord_id"] = user["id"]
    request.session["discord_name"] = user.get("global_name") or user["username"]
    return RedirectResponse("/#home", 303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


if DEV_LOGIN:
    log.warning("DEV_LOGIN is on: anyone can sign in as anyone. Never enable this in production.")

    @router.get("/dev-login")
    def dev_login(request: Request, discord_id: str = "dev-1", name: str = "Dev"):
        request.session["discord_id"] = discord_id
        request.session["discord_name"] = name
        return RedirectResponse("/#home", 303)


def is_admin(discord_id: str | None, player: Player | None) -> bool:
    return bool(discord_id) and (discord_id in ADMIN_IDS or bool(player and player.is_admin))


def current_player(request: Request, db: Session = Depends(get_db)) -> Player | None:
    did = request.session.get("discord_id")
    return db.scalar(select(Player).where(Player.discord_id == did)) if did else None


def require_player(request: Request, db: Session = Depends(get_db)) -> Player:
    p = current_player(request, db)
    if not p:
        raise HTTPException(401, "Log in and register before doing that.")
    return p


def require_admin(request: Request, db: Session = Depends(get_db)) -> str:
    did = request.session.get("discord_id")
    if not did:
        raise HTTPException(401, "Log in first.")
    if not is_admin(did, current_player(request, db)):
        raise HTTPException(403, "Only admins can do that.")
    return did
