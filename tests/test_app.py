from fastapi.testclient import TestClient

from app.main import app, replay

client = TestClient(app)


def test_every_view_answers_for_the_demo_day():
    meta = client.get("/api/meta").json()
    t, f, j = meta["start"]["day"], meta["start"]["f"], meta["start"]["j"]
    day = client.get(f"/api/day/{t}").json()
    assert day["summary"]["phantom"] > 0                          # the demo opens on hidden stock-outs
    assert all({"level", "by_stock", "lift", "fill"} <= set(c) for c in day["cells"] if c["alarm"])   # the UI explains each label
    assert client.get(f"/api/series/{f}/{j}?t={t}").status_code == 200
    fc = client.get(f"/api/forecast/{f}/{j}?t={t}").json()
    assert len(fc["mean"]) == meta["horizon"] and fc["lo"][0] is not None
    fa = client.get(f"/api/facility/{f}?t={t}").json()
    assert 0 <= fa["beds"]["occupied"] <= fa["beds"]["capacity"] and len(fa["staff"]) == 4
    plan = client.get(f"/api/plan?t={t}").json()
    assert all(p["from_fac"][:2] == p["to_fac"][:2] for p in plan["transfers"])   # never across a state line
    nat = client.get(f"/api/national?t={t}").json()
    assert all("-P" not in str(ex) for ex in nat["exports"])      # no facility ids leave a state
    assert client.get("/api/day/9999").status_code == 404


def test_shelf_check_and_voice_fallback():
    meta = client.get("/api/meta").json()
    t, f, j = meta["start"]["day"], meta["start"]["f"], meta["start"]["j"]
    p = client.post("/api/confirm", json=dict(f=f, j=j, t=t, answer="available")).json()["p"]
    assert p[0] > 0.5                                             # "we have it" moves the estimate
    replay.confirm.clear()
    replay.refilter(f, j)
    assert client.post("/api/confirm", json=dict(f=f, j=j, t=t, answer="maybe")).status_code == 422
    r = client.post(f"/api/voice?f={f}&j={j}&t={t}", content=b"not audio", headers={"Content-Type": "text/plain"})
    assert r.status_code == 422                                   # bad upload is refused before Gemini
