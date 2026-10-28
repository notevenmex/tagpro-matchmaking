from conftest import client


def test_announcement_is_empty_until_an_admin_posts_one():
    assert client().get("/api/season").json()["banner"] == ""  # fresh seeds have no banner
    assert client().put("/api/admin/announcement", json={"banner": "x"}).status_code == 401
    assert client("announce-player").put("/api/admin/announcement", json={"banner": "x"}).status_code == 403

    a = client("admin-1")
    r = a.put("/api/admin/announcement", json={"banner": " Season 5 starts Friday \n", "banner_note": "Placements reset."})
    assert r.status_code == 200
    s = client().get("/api/season").json()
    assert s["banner"] == "Season 5 starts Friday" and s["banner_note"] == "Placements reset."

    a.put("/api/admin/announcement", json={"banner": "", "banner_note": ""})
    s = client().get("/api/season").json()
    assert s["banner"] == "" and s["banner_note"] == ""
