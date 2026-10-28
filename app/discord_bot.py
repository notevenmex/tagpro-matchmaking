"""Send Discord DMs through a bot account, using the plain REST API (no gateway connection needed).

A bot can only DM someone who shares a server with it and allows DMs from that server's members,
so invite the bot to the community Discord. Set DISCORD_BOT_TOKEN; never commit it.
"""
import asyncio
import logging
import os

import httpx

log = logging.getLogger("rankedpugs")
DISCORD_API = "https://discord.com/api/v10"
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")


class DiscordNotifier:
    def __init__(self, token: str = BOT_TOKEN, transport: httpx.AsyncBaseTransport | None = None):
        self.token = token
        self.transport = transport  # tests pass a MockTransport

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def _post(self, client: httpx.AsyncClient, path: str, body: dict) -> httpx.Response:
        for _ in range(3):
            r = await client.post(DISCORD_API + path, json=body)
            if r.status_code != 429:
                return r
            await asyncio.sleep(min(float(r.json().get("retry_after", 1)), 10))
        return r

    async def send_dm(self, user_id: str, content: str) -> bool:
        """DM one user. Returns False (and logs why) instead of raising."""
        if not self.token:
            log.info("DISCORD_BOT_TOKEN isn't set; not sending a DM to %s", user_id)
            return False
        headers = {"Authorization": f"Bot {self.token}", "User-Agent": "RankedPUGs (bot, 1.0)"}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=10, transport=self.transport) as client:
                r = await self._post(client, "/users/@me/channels", {"recipient_id": user_id})
                if r.status_code >= 400:
                    log.warning("Couldn't open a DM with %s: %s %s", user_id, r.status_code, r.text[:200])
                    return False
                r = await self._post(client, f"/channels/{r.json()['id']}/messages",
                                     {"content": content[:2000], "allowed_mentions": {"parse": []}})
                if r.status_code >= 400:  # 50007: the user has DMs closed
                    log.warning("Couldn't DM %s: %s %s", user_id, r.status_code, r.text[:200])
                    return False
                return True
        except httpx.HTTPError as e:
            log.warning("Couldn't DM %s: %s", user_id, e)
            return False
