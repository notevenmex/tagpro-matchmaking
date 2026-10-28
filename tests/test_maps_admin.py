from conftest import client

from app.maps_admin import MLTP_MAJORS


def names(c):
    return [m["name"] for m in c.get("/api/maps").json()]


def test_only_admins_edit_maps():
    assert client().get("/api/admin/maps").status_code == 401
    assert client("maps-player").post("/api/admin/maps", json={"name": "Iron"}).status_code == 403


def test_add_rename_rotation_votes_delete():
    a = client("admin-1")
    assert a.post("/api/admin/maps", json={"name": "Test Map"}).status_code == 201
    assert a.post("/api/admin/maps", json={"name": "test map"}).status_code == 409
    mid = next(m["id"] for m in a.get("/api/admin/maps").json() if m["name"] == "Test Map")

    assert a.patch(f"/api/admin/maps/{mid}", json={"name": "Basenji"}).status_code == 409
    assert a.patch(f"/api/admin/maps/{mid}", json={"name": "Test Map 2"}).status_code == 200
    assert "Test Map 2" in names(a)

    assert a.patch(f"/api/admin/maps/{mid}", json={"active": False}).status_code == 200
    assert "Test Map 2" not in names(a)
    assert a.post("/api/admin/maps", json={"name": "Test Map 2"}).status_code == 201  # back in rotation
    assert "Test Map 2" in names(a)

    u = client("maps-voter", "Voter")
    u.post("/api/register", json={"tagpro_name": "MapsVoter", "position": "offense"})
    assert u.put(f"/api/maps/{mid}/vote", json={"score": 7}).status_code == 200
    assert a.delete(f"/api/admin/maps/{mid}/votes").status_code == 200
    assert next(m for m in a.get("/api/admin/maps").json() if m["id"] == mid)["votes"] == 0

    assert a.delete(f"/api/admin/maps/{mid}").status_code == 200
    assert "Test Map 2" not in [m["name"] for m in a.get("/api/admin/maps").json()]


def test_use_mltp_majors_keeps_old_maps_out_of_rotation():
    a = client("admin-1")
    a.post("/api/admin/maps", json={"name": "Old Map"})
    assert a.post("/api/admin/maps/use-mltp-majors").json()["maps"] == MLTP_MAJORS
    assert sorted(names(a)) == sorted(MLTP_MAJORS)
    old = next(m for m in a.get("/api/admin/maps").json() if m["name"] == "Old Map")
    assert not old["active"]
    a.delete(f"/api/admin/maps/{old['id']}")
