from conftest import client

from app.pages import DEFAULT_RULES


def test_rules_default_then_admin_edit():
    r = client().get("/api/rules").json()
    assert r["is_default"] and r["markdown"] == DEFAULT_RULES

    assert client().put("/api/rules", json={"markdown": "x"}).status_code == 401
    assert client("rules-player").put("/api/rules", json={"markdown": "x"}).status_code == 403

    admin = client("admin-1")
    saved = admin.put("/api/rules", json={"markdown": "## New\n- one"}).json()
    assert not saved["is_default"] and saved["updated_at"]
    assert client().get("/api/rules").json()["markdown"] == "## New\n- one"
    assert admin.put("/api/rules", json={"markdown": "x" * 20001}).status_code == 422
