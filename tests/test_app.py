from fastapi.testclient import TestClient

from anumaan import voice
from app.main import app, briefs, gemini_used, ledger, replay

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


def test_no_use_yet_is_not_a_number_of_days():
    cells = client.get("/api/day/0").json()["cells"]        # day 0: some medicines not yet called for
    assert any(c["cover"] is None for c in cells)
    assert all(c["cover"] is None or c["cover"] < 1000 for c in cells)
    assert all((c["shadow"] is None) == (c["cover"] is None) for c in cells)


def test_shelf_check_and_voice_fallback():
    meta = client.get("/api/meta").json()
    t, f, j = meta["start"]["day"], meta["start"]["f"], meta["start"]["j"]
    p = client.post("/api/confirm", json=dict(f=f, j=j, t=t, answer="available")).json()["p"]
    assert p[0] > 0.5                                             # "we have it" moves the estimate
    replay.confirm.clear()
    replay.refilter(f, j)
    assert client.post("/api/confirm", json=dict(f=f, j=j, t=t, answer="maybe")).status_code == 422
    spent = sum(gemini_used.values())
    r = client.post(f"/api/voice?f={f}&j={j}&t={t}", content=b"not audio", headers={"Content-Type": "text/plain"})
    assert r.status_code == 422                                   # bad upload is refused before Gemini
    assert sum(gemini_used.values()) == spent                     # ...and does not eat the daily cap


def test_approval_goes_to_the_ledger_and_the_brief_reads_the_same_evidence(monkeypatch):
    meta = client.get("/api/meta").json()
    t, f, j = meta["start"]["day"], meta["start"]["f"], meta["start"]["j"]
    x = client.get(f"/api/plan?t={t}").json()["transfers"][0]
    body = dict(t=t, from_fac=x["from_fac"], to_fac=x["to_fac"], drug=x["drug"])
    o = client.post("/api/approve", json=body).json()
    assert o["qty_units"] == x["units"] and o["indent_id"].startswith(f"RD-{t:03d}-")
    assert client.post("/api/approve", json=body).json() == o      # one order per transfer, however often it is clicked
    assert client.get("/api/ledger").json() == [o]
    assert client.get(f"/api/plan?t={t}").json()["transfers"][0]["approved"]
    assert client.post("/api/approve", json=dict(body, drug="nope")).status_code == 404
    seen = []
    monkeypatch.setattr(voice, "write_brief", lambda facts, language: seen.append((facts, language)) or dict(summary="s", next_step="n"))
    assert client.get(f"/api/brief/{f}/{j}?t={t}&language=or").json() == dict(summary="s", next_step="n")
    facts, language = seen[0]
    assert language == "or" and facts["alarm"] and facts["phc"].startswith("PHC ")      # the names the UI shows
    client.get(f"/api/brief/{f}/{j}?t={t}&language=or")
    assert len(seen) == 1                                          # the same evidence never pays Gemini twice
    assert client.get(f"/api/brief/{f}/{j}?t={t}&language=xx").status_code == 422
    ledger.clear(), briefs.clear()
