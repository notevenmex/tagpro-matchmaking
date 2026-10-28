import asyncio

from app import group_host
from app.group_host import GroupHost, Seat, chat_sender

RED, BLUE, SPEC = 1, 2, 3


class FakeConn:
    def __init__(self):
        self.members = {"bot": {"id": "bot", "name": "Some Ball", "team": SPEC}}
        self.chats, self.emits, self.said = [], [], []

    def join(self, mid, name, team):
        self.members[mid] = {"id": mid, "name": name, "team": team}

    async def state(self):
        chats, self.chats = self.chats, []
        return {"you": "bot", "members": dict(self.members), "chats": chats}

    async def emit(self, event, payload=None):
        self.emits.append((event, payload))

    async def say(self, text):
        self.said.append(text)


def make_host(conn, t, missing):
    async def live():
        return True

    async def on_missing(counts):
        missing.append(counts)

    seats = [Seat(f"R{i}", "red", i == 0) for i in range(4)] + [Seat(f"B{i}", "blue", i == 0) for i in range(4)]
    return GroupHost(conn, 1, seats, live, on_missing, clock=lambda: t[0])


def launches(conn):
    return [e for e in conn.emits if e[0] == group_host.LAUNCH_EVENT]


def test_launches_once_both_teams_are_full_and_relaunches_on_start():
    conn, t, missing = FakeConn(), [0.0], []
    host = make_host(conn, t, missing)
    for i in range(4):
        conn.join(f"r{i}", f"R{i}", RED)
    for i in range(3):
        conn.join(f"b{i}", f"Guest{i}", BLUE)  # names don't matter for launching
    asyncio.run(host.step())
    assert not launches(conn) and not conn.said

    conn.join("b3", "B3", BLUE)
    asyncio.run(host.step())
    assert "Launching in" in conn.said[-1] and not launches(conn)
    del conn.members["b3"]  # someone leaves during the countdown
    t[0] += 20
    asyncio.run(host.step())
    assert "cancelled" in conn.said[-1] and not launches(conn)

    conn.join("b3", "B3", BLUE)
    asyncio.run(host.step())
    t[0] += group_host.COUNTDOWN_SECONDS
    asyncio.run(host.step())
    assert len(launches(conn)) == 1
    t[0] += 600
    asyncio.run(host.step())
    assert len(launches(conn)) == 1  # never relaunches by itself

    conn.chats.append({"from": "b2", "message": "!start"})
    asyncio.run(host.step())
    assert len(launches(conn)) == 2
    assert not missing


def test_missing_dm_and_forced_start():
    conn, t, missing = FakeConn(), [0.0], []
    host = make_host(conn, t, missing)
    conn.join("r0", "R0", RED)
    conn.join("b0", "B0", BLUE)
    t[0] = group_host.MISSING_DM_SECONDS
    asyncio.run(host.step())
    asyncio.run(host.step())
    assert missing == ["Red 1/4, Blue 1/4"]
    conn.chats.append({"from": "r0", "message": "!START"})
    asyncio.run(host.step())
    assert len(launches(conn)) == 1


def test_kicks_chatting_spectators_once_but_never_players():
    conn, t, missing = FakeConn(), [0.0], []
    host = make_host(conn, t, missing)
    conn.join("s1", "Lurker", SPEC)
    conn.join("s2", "R1", SPEC)        # a drafted player who hasn't picked a team yet
    conn.join("r0", "R0", RED)
    conn.chats += [{"from": "s1", "message": "gl"}, {"from": "s1", "message": "hi again"},
                   {"from": "Lurker", "message": "by name"}, {"from": "s2", "message": "which team?"},
                   {"from": "r0", "message": "hi"}, {"from": "bot", "message": "me"},
                   {"message": "system notice"}]
    asyncio.run(host.step())
    assert conn.emits == [(group_host.KICK_EVENT, "s1")]


def test_chat_sender_by_id_or_name():
    members = {"7": {"id": 7, "name": "Neon"}}
    assert chat_sender({"from": 7}, members)["name"] == "Neon"
    assert chat_sender({"from": "neon"}, members)["id"] == 7
    assert chat_sender({"from": "nobody"}, members) is None
