import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["DEV_LOGIN"] = "1"
os.environ["ADMIN_DISCORD_IDS"] = "admin-1"

from fastapi.testclient import TestClient  # noqa: E402

from app import seed  # noqa: E402
from app.main import app  # noqa: E402

seed.run()


def client(discord_id=None, name="Tester"):
    c = TestClient(app)
    if discord_id:
        c.get("/auth/dev-login", params={"discord_id": discord_id, "name": name}, follow_redirects=False)
    return c
