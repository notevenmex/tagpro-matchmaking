"""One-time setup of the community Discord: roles, categories and channels (see docs/discord-server.md).

Run it once after inviting the bot:

    DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... python -m app.discord_setup

It only creates what's missing (matched by name), so it's safe to run again, and it never deletes
or edits anything you already have. It prints the IDs to put in the site's environment.
"""
import asyncio
import os
import sys

from .discord_bot import DiscordNotifier
from .voice import CONNECT, MANAGE, MOVE, SEND, SPEAK, VIEW

HISTORY, REACT, MUTE, DEAFEN, MANAGE_MESSAGES, TIMEOUT = 1 << 16, 1 << 6, 1 << 22, 1 << 23, 1 << 13, 1 << 40

ROLES = [  # name, permissions, color
    ("Moderator", MANAGE_MESSAGES | MOVE | MUTE | DEAFEN | TIMEOUT, 0x3498DB),
    ("Player", 0, 0x2ECC71),
]

# Each category: (name, who can see it, [(channel name, type, extra)]). type 0 = text, 2 = voice.
# "read_only" text channels let only Moderators post.
LAYOUT = [
    ("📢 Info", "everyone", [
        ("welcome", 0, "read_only"), ("rules", 0, "read_only"), ("announcements", 0, "read_only"),
        ("how-to-play", 0, "read_only")]),
    ("🎮 Ranked PUGs", "everyone", [
        ("general", 0, ""), ("looking-for-pug", 0, ""), ("match-results", 0, "read_only"),
        ("support", 0, "")]),
    ("🔊 Voice", "everyone", [
        ("Waiting Room", 2, ""), ("Post-Game Lounge", 2, ""), ("Casual 1", 2, ""), ("Casual 2", 2, "")]),
    ("🏆 Live Matches", "everyone", []),  # the bot creates and deletes the team channels here
    ("🛠 Staff", "staff", [("staff-chat", 0, ""), ("bot-log", 0, "")]),
]


async def main() -> int:
    guild = os.getenv("DISCORD_GUILD_ID", "")
    api = DiscordNotifier()
    if not api.token or not guild:
        print("Set DISCORD_BOT_TOKEN and DISCORD_GUILD_ID first.", file=sys.stderr)
        return 1

    async def call(method, path, body=None):
        r = await api.request(method, path, body, reason="Ranked PUGs server setup")
        if r.status_code >= 400:
            raise SystemExit(f"{method} {path} failed: {r.status_code} {r.text[:300]}\n"
                             "Check the bot is in the server with the permissions in docs/discord-server.md.")
        return r.json()

    me = await call("GET", "/users/@me")
    roles = {r["name"]: r["id"] for r in await call("GET", f"/guilds/{guild}/roles")}
    for name, perms, color in ROLES:
        if name not in roles:
            roles[name] = (await call("POST", f"/guilds/{guild}/roles",
                                      {"name": name, "permissions": str(perms), "color": color,
                                       "hoist": name == "Moderator", "mentionable": False}))["id"]
            print(f"created role {name}")
    staff = roles["Moderator"]
    bot = {"id": me["id"], "type": 1, "allow": str(VIEW | CONNECT | SPEAK | SEND | MANAGE | MOVE), "deny": "0"}

    channels = await call("GET", f"/guilds/{guild}/channels")
    by_name = {(c.get("parent_id"), c["name"].lower()): c["id"] for c in channels}
    ids = {}
    for pos, (cat_name, who, children) in enumerate(LAYOUT):
        overwrites = []
        if who in ("hidden", "staff"):
            overwrites = [{"id": guild, "type": 0, "deny": str(VIEW), "allow": "0"},
                          {"id": staff, "type": 0, "allow": str(VIEW | CONNECT), "deny": "0"}, bot]
        cat = by_name.get((None, cat_name.lower()))
        if not cat:
            cat = (await call("POST", f"/guilds/{guild}/channels",
                              {"name": cat_name, "type": 4, "position": pos, "permission_overwrites": overwrites}))["id"]
            print(f"created category {cat_name}")
        ids[cat_name] = cat
        for name, kind, extra in children:
            key = name if kind == 2 else name.lower()
            if (cat, key.lower()) in by_name:
                ids[name] = by_name[(cat, key.lower())]
                continue
            ow = list(overwrites)
            if extra == "read_only":
                ow += [{"id": guild, "type": 0, "deny": str(SEND), "allow": "0"},
                       {"id": staff, "type": 0, "allow": str(SEND), "deny": "0"}, bot]
            ids[name] = (await call("POST", f"/guilds/{guild}/channels",
                                    {"name": key, "type": kind, "parent_id": cat, "permission_overwrites": ow}))["id"]
            print(f"created {'voice' if kind == 2 else 'text'} channel {name}")

    print("\nAdd these to the site's environment (Render > your service > Environment):\n")
    print(f"DISCORD_GUILD_ID={guild}")
    print(f"VOICE_CATEGORY_ID={ids['🏆 Live Matches']}")
    print(f"VOICE_LOBBY_CHANNEL_ID={ids['Post-Game Lounge']}")
    print(f"VOICE_STAFF_ROLE_ID={staff}")
    print("VOICE_CHANNELS=1")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
