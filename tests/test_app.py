import base64
import json
import threading

from fastapi.testclient import TestClient

import app.main as main
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


def test_beds_and_staff_board_sends_a_full_phc_to_the_nearest_free_bed():
    t = client.get("/api/meta").json()["start"]["day"]
    b = client.get(f"/api/care?t={t}&truth=true").json()
    full = [r for r in b["rows"] if r["pressure"]]
    assert full and b["summary"]["full"] == len(full) and all(r["free"] == 0 and r["full_7d"] >= 1 for r in full)
    st = replay.run.st
    for r in full:
        to = b["rows"][r["refer"]["f"]]
        assert to["free"] == r["refer"]["free"] > 0 and st[to["f"]] == st[r["f"]] and r["refer"]["minutes"] <= b["refer_minutes"]
    assert all(r["refer"] is None for r in b["rows"] if not r["pressure"])
    marks = [x for r in b["rows"] for x in r["staff"] if x["verify"]]
    assert marks and b["summary"]["verify"] == len(marks) and all(x["marked_present"] and x["verify_7d"] >= 1 for x in marks)
    one = client.get(f"/api/facility/{full[0]['f']}?t={t}&truth=true").json()      # the detail panel reads the same row
    assert one["staff"] == full[0]["staff"] and one["beds"]["occupied"] == full[0]["occupied"] == full[0]["capacity"]


def test_kerala_replay_calls_the_state_failure_and_escalates_it():
    m = client.get("/api/meta?net=kerala").json()
    assert m["names"]["states"] == {"S0": "Kerala"} and len(m["names"]["warehouses"]) == 14 and m["scenario"]["script"]
    t, f, j = m["start"]["day"], m["start"]["f"], m["start"]["j"]
    cells = client.get(f"/api/day/{t}?net=kerala").json()["cells"]
    c = next(c for c in cells if (c["f"], c["j"]) == (f, j))
    assert c["level"] == "STATE-PROCUREMENT" and c["warehouses"] == 14 and c["starved"] >= 5     # a third of the districts
    early = client.get(f"/api/day/{main.scenario.BREAK}?net=kerala").json()["summary"]
    assert early["STATE-PROCUREMENT"] == 0                                # nothing is called before the supply breaks
    p = client.get(f"/api/plan?t={t}&net=kerala").json()
    assert p["escalations"] and {e["level"] for e in p["escalations"]} == {"STATE-PROCUREMENT"}
    view = client.get(f"/api/national?t={t}&net=kerala").json()["view"]
    assert any(v["short_states"] == ["S0"] for v in view.values()) and not any(v["national"] for v in view.values())
    assert main._facts(main._net("kerala"), t, f, j)["state"] == "Kerala"  # the brief names the real district too
    assert client.get("/api/meta?net=nowhere").status_code == 404
    assert client.get("/api/ledger?net=kerala").json() == []              # each network keeps its own orders


def test_live_feed_gives_the_same_view_as_a_replay_of_the_same_records(monkeypatch):
    live = main._net("live")
    s = client.get("/api/live").json()
    t0 = s["day"]
    assert t0 == s["history"] - 1 and s["complete"] and client.get(f"/api/day/{t0 + 1}?net=live").status_code == 404
    for _ in range(3):
        assert client.post("/api/live/step").json()["via"] == "direct"    # no Pub/Sub topic in the tests
    t = t0 + 3
    batch = main.Replay(live.src, live.seed)                              # the same records, all at once
    plain = lambda x: json.loads(json.dumps(x, default=float))
    labels = batch.labels(t)
    assert client.get(f"/api/day/{t}?net=live&truth=true").json()["cells"] == plain(
        [batch.cell(t, f, j, labels, True) for f in range(len(live.upto)) for j in range(len(live.drugs))])
    assert client.get(f"/api/care?t={t}&net=live").json() == plain(batch.board(t))
    assert client.get(f"/api/national?t={t}&net=live").json()["exports"] == plain([n.export(t) for n in batch.nodes])

    # the push endpoint: a token, Pub/Sub's envelope, at-least-once delivery, and a message it cannot read
    msgs = live.feed(t + 1)
    wrapped = {"message": {"data": base64.b64encode(json.dumps(msgs[0]).encode()).decode()}}
    assert client.post("/api/ingest?token=x", json=wrapped).status_code == 403     # no token configured: closed
    monkeypatch.setattr(main, "INGEST_TOKEN", "secret")
    assert client.post("/api/ingest?token=wrong", json=wrapped).status_code == 403
    assert client.post("/api/ingest?token=secret", json=wrapped).json()["ok"]
    n = len(live.run.slips)
    assert client.post("/api/ingest?token=secret", json=msgs[0]).json()["ok"] and len(live.run.slips) == n
    assert not client.post("/api/ingest?token=secret", json=dict(msgs[1], phc="nowhere")).json()["ok"]
    s = client.get("/api/live").json()
    assert (s["day"], s["reported"], s["complete"]) == (t + 1, 1, False)
    cells = client.get(f"/api/day/{t + 1}?net=live").json()["cells"]
    assert [c.get("as_of") for c in cells[:len(live.drugs) + 1]] == [None] * len(live.drugs) + [t]   # the rest show their last report
    assert client.post("/api/live/reset").json()["day"] == t0


def test_a_report_landing_mid_request_waits_for_it(monkeypatch):
    live = main._net("live")
    t, labels, waited = live.clock, live.labels, []
    late = threading.Thread(target=lambda: [live.ingest(m) for m in live.feed(t + 1)])

    def labels_then_a_report_arrives(day):      # the alarm labels are worked out, then a push lands before the cells are read
        out = labels(day)
        late.start()
        late.join(0.5)
        waited.append(late.is_alive())
        return out

    monkeypatch.setattr(live, "labels", labels_then_a_report_arrives)
    assert client.get(f"/api/day/{t}?net=live").status_code == 200 and waited == [True]     # it waited for the request's lock
    late.join()
    assert client.get("/api/live").json()["day"] == t + 1                                   # ...and then landed
    main.nets.pop("live")


def test_reports_pushed_together_in_any_order_give_the_same_view(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(main, "INGEST_TOKEN", "secret")
    live = main._net("live")
    t0, bad = live.clock, []

    def push(m):
        wrapped = {"message": {"data": base64.b64encode(json.dumps(m).encode()).decode()}}
        r = client.post("/api/ingest?token=secret", json=wrapped)
        bad.extend([r.text] * (r.status_code != 200 or not r.json()["ok"]))

    def read(t):
        for url in (f"/api/day/{t}?net=live", f"/api/care?t={t}&net=live", f"/api/national?t={t}&net=live", f"/api/plan?t={t}&net=live"):
            r = client.get(url)
            bad.extend([url] * (r.status_code not in (200, 404)))      # 404: that day has not begun yet

    with ThreadPoolExecutor(12) as pool:          # Pub/Sub pushes a day's reports together, some twice, in any order
        for t in range(t0 + 1, t0 + 5):
            msgs = live.feed(t)
            jobs = [pool.submit(push, m) for m in msgs[::-1] + msgs[:6]] + [pool.submit(read, t) for _ in range(4)]
            [j.result() for j in jobs]
    assert not bad
    batch, t = main.Replay(live.src, live.seed), t0 + 4
    labels = batch.labels(t)
    assert client.get(f"/api/day/{t}?net=live").json()["cells"] == json.loads(json.dumps(
        [batch.cell(t, f, j, labels, False) for f in range(len(live.upto)) for j in range(len(live.drugs))], default=float))
    assert client.post("/api/live/reset").json()["day"] == t0
