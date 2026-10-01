import base64
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from anumaan import intake, planner as PL, realcheck_india, sim, triage, voice, whatif
from app.main import app, briefs, gemini_used, ledger, replay

client = TestClient(app)
SAMPLES = Path(main.__file__).resolve().parent.parent / "tools" / "samples"


@pytest.fixture(autouse=True)
def _cap(monkeypatch):
    """The fakes below make no real call: give them the default daily cap, whatever ANUMAAN_GEMINI_DAILY_CAP says."""
    monkeypatch.setattr(main, "GEMINI_CAP", 200)


class Fake:
    """Stands in for genai.Client: records each request and answers with `reply`. No network."""

    def __init__(self, reply):
        self.reply, self.calls = reply, []
        self.models = self

    def generate_content(self, **kw):
        self.calls.append(kw)
        return self.reply


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
    # a stale page's click is refused: not in that day's plan for the network the request names
    assert client.post("/api/approve", json=dict(body, drug="nope")).status_code == 409
    assert client.post("/api/approve?net=kerala", json=body).status_code == 409          # a demo transfer sent to Kerala
    assert client.post("/api/approve?net=kerala", json=dict(body, net="demo")).json() == o   # the body names its network
    assert client.post("/api/approve", json=dict(body, t=replay.clock + 1)).status_code == 409
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


def test_the_demo_opens_on_the_hidden_stock_out_that_broke_furthest_upstream():
    s = client.get("/api/meta").json()["start"]
    cells = client.get(f"/api/day/{s['day']}").json()["cells"]
    rank = lambda c: (triage.LEVELS.index(c["level"]) % 4, c["p"][2])      # as the Kerala replay's opener is ranked
    top = next(c for c in cells if (c["f"], c["j"]) == (s["f"], s["j"]))
    assert top["phantom"] and rank(top) == max(rank(c) for c in cells if c["phantom"])
    assert top["level"] not in ("LOCAL", "DEMAND-SURGE")                  # a break upstream, so the chain shows


def test_the_district_screen_adds_up_to_the_views_it_is_made_of():
    t, run = client.get("/api/meta").json()["start"]["day"], replay.run
    care, plan = client.get(f"/api/care?t={t}").json(), client.get(f"/api/plan?t={t}").json()
    cells = client.get(f"/api/day/{t}").json()["cells"]
    wh_of = lambda fid: run.wh[run.facilities.index(fid)]
    for r in care["rows"]:      # footfall: diagnoses recorded, today and over the last 7 days
        assert r["footfall"] == dict(today=int(run.dx[t, r["f"]].sum()), mean_7d=round(float(run.dx[t - 6:t + 1, r["f"]].sum(1).mean()), 1))
    assert care["summary"]["footfall"]["today"] == int(run.dx[t].sum()) and "not every visit" in care["footfall_is"]
    for w in replay.whs:
        d = client.get(f"/api/district?t={t}&wh={w}").json()
        fs = [f for f in range(len(run.facilities)) if run.wh[f] == w]
        rows, hot = [care["rows"][f] for f in fs], [c for c in cells if c["f"] in fs and c["alarm"]]
        assert (d["beds"]["capacity"], d["beds"]["free"]) == (sum(r["capacity"] for r in rows), sum(r["free"] for r in rows))
        assert [x["f"] for x in d["beds"]["full"]] == [r["f"] for r in rows if r["pressure"]]
        assert d["staff"]["verify"] == sum(x["verify"] for r in rows for x in r["staff"])
        assert d["actions"]["transfers"] == sum(wh_of(x["to_fac"]) == w for x in plan["transfers"])
        assert len(d["actions"]["escalations"]) == sum(wh_of(e["fac"]) == w for e in plan["escalations"])
        assert sum(m["alarm"] for m in d["medicines"]) == len(hot) and sum(m["hidden"] for m in d["medicines"]) == sum(c["phantom"] for c in hot)
        # diagnoses recorded today, against the mean of the 28 days before: today is not in "usual"
        assert d["footfall"]["today"] == sum(r["footfall"]["today"] for r in rows)
        assert abs(d["footfall"]["usual"] - run.dx[t - 28:t, fs].sum((1, 2)).mean()) < 0.06
    net = client.get(f"/api/district?t={t}").json()
    assert net["actions"]["transfers"] == len(plan["transfers"]) and net["beds"]["free"] == sum(r["free"] for r in care["rows"])
    assert net["footfall"]["today"] == care["summary"]["footfall"]["today"] and len(net["districts"]) == len(replay.whs)
    assert next(x for x in net["districts"] if x["wh"] == net["busiest"])["todo"] == max(x["todo"] for x in net["districts"])
    assert client.get(f"/api/district?t={t}&wh=nowhere").status_code == 404


def test_the_google_badge_says_live_only_with_a_moment_this_process_saw(monkeypatch):
    monkeypatch.setattr(voice, "answered", {})
    monkeypatch.setattr(main, "seen", {})
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert not [s for s in client.get("/api/google").json()["services"] if s["status"] == "live"]
    # a brief through a fake Gemini client, a Pub/Sub push, and Cloud Run's own variables
    briefs.clear()
    monkeypatch.setattr(voice, "_client", lambda: Fake(SimpleNamespace(parsed=voice.Brief(summary="s", next_step="n"))))
    s = client.get("/api/meta").json()["start"]
    assert client.get(f"/api/brief/{s['f']}/{s['j']}?t={s['day']}").status_code == 200
    monkeypatch.setattr(main, "INGEST_TOKEN", "secret-token")
    live = main._net("live")
    wrapped = {"message": {"data": base64.b64encode(json.dumps(live.feed(live.clock)[0]).encode()).decode()}}
    assert client.post("/api/ingest?token=secret-token", json=wrapped).json()["ok"]
    monkeypatch.setenv("K_SERVICE", "anumaan-demo")
    monkeypatch.setenv("K_REVISION", "anumaan-demo-00001-abc")
    g = client.get("/api/google")
    by = {x["service"]: x for x in g.json()["services"]}
    assert {k for k, x in by.items() if x["status"] == "live"} == {"Gemini on Vertex AI", "Pub/Sub", "Cloud Run"}
    started = datetime.fromisoformat(g.json()["started"])
    assert all(datetime.fromisoformat(x["at"]) >= started for x in by.values() if x["status"] == "live")
    assert all(x["at"] is None for x in by.values() if x["status"] != "live")
    assert "anumaan-demo-00001-abc" in by["Cloud Run"]["detail"] and f"of {main.GEMINI_CAP} calls today" in by["Gemini on Vertex AI"]["detail"]
    assert (by["BigQuery clean room"]["status"], by["Google Maps Routes API"]["status"]) == ("offline", "cached")
    assert "8,400 rows" in by["BigQuery clean room"]["detail"] and "6 guideline PDFs" in by["Gemini grammar compiler"]["detail"]
    tfm = by["TimesFM on BigQuery (AI.FORECAST)"]
    assert tfm["as_of"] is None and "a plain mean of past demand" in tfm["detail"]
    # never a token, a project number or id, or an email
    assert "secret-token" not in g.text and "@" not in g.text and not re.search(r"\d{12}", g.text)
    assert not os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ["GOOGLE_CLOUD_PROJECT"] not in g.text
    briefs.clear()


def test_briefs_come_back_checked_in_every_offered_language_and_are_read_aloud_once(monkeypatch):
    pcm = SimpleNamespace(data=b"\0\0" * 2400, mime_type="audio/L16;codec=pcm;rate=24000")
    fake = Fake(SimpleNamespace(parsed=voice.Brief(summary="s", next_step="n"),     # an English brief, whatever was asked
                                candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=pcm)]))]))
    monkeypatch.setattr(voice, "_client", lambda: fake)
    meta = client.get("/api/meta").json()
    assert list(meta["languages"]) == list(voice.LANGUAGES) and meta["languages"]["as"]["voice"] is None
    t, f, j = meta["start"]["day"], meta["start"]["f"], meta["start"]["j"]
    brief, audio = f"/api/brief/{f}/{j}?t={t}&language=", f"/api/brief/{f}/{j}/audio?t={t}&language="
    assert client.get(audio + "en").status_code == 409                               # no brief to read yet
    for code in voice.LANGUAGES:     # the check's result comes back: English passes, English sent for any other fails
        b = client.get(brief + code).json()
        assert ("problems" in b) == (code != "en")
    n = len(fake.calls)
    assert "Tamil script" in client.get(brief + "ta").json()["problems"][-1] and client.get(audio + "ta").status_code == 409
    assert "problems" not in client.get(brief + "en").json() and len(fake.calls) == n + 1    # a failed brief is written afresh, a passed one is not
    n = len(fake.calls)
    a = client.get(audio + "en")
    assert a.status_code == 200 and a.headers["content-type"] == "audio/wav" and a.content[:4] == b"RIFF"
    assert client.get(audio + "en").content == a.content and len(fake.calls) == n + 1      # paid once a brief
    assert fake.calls[-1]["model"] == os.environ.get("ANUMAAN_TTS_MODEL", voice.TTS_MODEL)
    spent = sum(gemini_used.values())
    for code in ("as", "mni-Mtei", "xx"):     # no voice, or not offered: refused before any call
        assert client.get(audio + code).status_code == 422
    assert sum(gemini_used.values()) == spent and len(fake.calls) == n + 1

    def unavailable():
        raise voice.VoiceUnavailable(voice._HOWTO.format(why="test"))
    monkeypatch.setattr(voice, "_client", unavailable)
    main.clips.clear()
    r = client.get(audio + "en")
    assert r.status_code == 503 and "GOOGLE_GENAI_USE_VERTEXAI" in r.json()["detail"]     # says what to set
    briefs.clear(), main.clips.clear()


def test_a_what_if_world_opens_on_its_story_and_three_at_most_are_kept(monkeypatch):
    main.whatifs.clear()
    pick = whatif.parse(whatif.MENU[0]["key"])          # Ernakulam warehouse out of amoxicillin from day 40
    w = client.post("/api/whatif", json=pick).json()
    key, story = w["net"], w["about"]["whatif"]
    m = client.get(f"/api/meta?net={key}").json()
    assert key == whatif.MENU[0]["key"] and m["start"] == story["start"] and m["scenario"]["script"] == w["about"]["script"]
    fs = [f for f, x in enumerate(m["facilities"]) if x["wh"] == story["district"]]
    levels = lambda t: {c["level"] for c in client.get(f"/api/day/{t}?net={key}").json()["cells"]
                        if c["alarm"] and c["f"] in fs and c["j"] == story["start"]["j"]}   # the medicine it cut
    assert story["level"] in levels(story["called"])                                      # the app calls it the day the story says
    assert not any(story["level"] in levels(t) for t in range(story["day"], story["called"]))
    assert client.get(f"/api/plan?t={story['called']}&net={key}").status_code == 200
    assert client.get("/api/whatif").json()["menu"] == list(whatif.MENU)
    assert client.post("/api/whatif", json=dict(pick, day=1)).status_code == 422
    assert client.get(f"/api/meta?net={key[:-2]}1").status_code == 404                     # day 1: not a what-if
    # three kept, the oldest out first; an evicted world is rebuilt the same on its next request
    build, tiny = whatif.build, sim.simulate(seed=0, days=40, n_states=1, n_wh=1, n_phc=2)
    monkeypatch.setattr(whatif, "build", lambda p: (tiny, PL.synthetic_coords(tiny), {},
                                                    dict(whatif=dict(seed=0, world="demo", start=dict(day=39, f=0, j=0)))))
    keys = [whatif.validate(dict(world="demo", event="state", medicine="ors_sachet", day=d)) for d in (41, 42, 43)]
    for k in keys:
        assert client.get(f"/api/meta?net={k}").json()["start"] == dict(day=39, f=0, j=0)
    assert list(main.whatifs) == keys
    monkeypatch.setattr(whatif, "build", build)
    assert client.get(f"/api/meta?net={key}").json()["start"] == story["start"] and list(main.whatifs) == keys[1:] + [key]
    main.whatifs.clear()


def test_the_plan_labels_a_district_emergency_as_the_day_view_does():
    main.whatifs.clear()
    key = whatif.MENU[1]["key"]           # Ernakulam: fever and acute diarrhoea x3 from day 60
    meta = client.get(f"/api/meta?net={key}").json()
    fid, names, surge = [x["id"] for x in meta["facilities"]], [d["name"] for d in meta["drugs"]], 0
    for t in (62, 70):
        day = {(fid[c["f"]], names[c["j"]]): c["level"] for c in client.get(f"/api/day/{t}?net={key}").json()["cells"] if c["alarm"]}
        p = client.get(f"/api/plan?t={t}&net={key}").json()
        assert all(x["level"] == day[(x["fac"], x["drug"])] for x in p["recipients"] + p["escalations"])
        surge += sum(x["level"] == "DEMAND-SURGE" for x in p["recipients"])
    assert surge                          # the district rule's label reaches the plan, not LOCAL
    main.whatifs.clear()


def test_a_paper_phc_is_read_checked_and_confirmed_once_ahead_of_the_feed(monkeypatch):
    sample = json.loads((SAMPLES / "sample.json").read_text(encoding="utf-8"))
    pages = [dict(mime="image/jpeg", data=base64.b64encode((SAMPLES / p).read_bytes()).decode()) for p in sample["pages"]]
    fake = Fake(SimpleNamespace(parsed=sample["recorded_read"]["read"]))      # Gemini's recorded read of these pages
    monkeypatch.setattr(intake, "_client", lambda: fake)
    assert client.post("/api/live/reset").json()["day"] == 59
    live, key = main._net("live"), sample["message"]
    body, spent = dict(phc=key["phc"], date=key["date"], images=pages), sum(gemini_used.values())
    for bad, code in ((dict(body, images=[dict(mime="image/jpeg", data="not base64!")]), 422),
                      (dict(body, images=[dict(pages[0], mime="image/png")]), 422),     # the bytes are a JPEG
                      (dict(body, images=pages * 2), 422),                                # more than 4 photos
                      (dict(body, phc="nowhere"), 404),
                      (dict(body, date="2026-03-03"), 409)):                              # not that PHC's next day
        assert client.post("/api/intake/read", json=bad).status_code == code
    too_big = dict(body, images=[dict(pages[0], data="A" * (main.B64_MAX + 4))])            # refused before it is decoded
    assert client.post("/api/intake/read", json=too_big).status_code == 422
    monkeypatch.setattr(main, "GEMINI_CAP", 0)                                             # a bad photo is a 422 even
    assert client.post("/api/intake/read", json=dict(body, images=[dict(pages[0], mime="image/png")])).status_code == 422
    monkeypatch.setattr(main, "GEMINI_CAP", 200)                                           # ...with no call left
    assert sum(gemini_used.values()) == spent and not fake.calls                           # refused before any call
    r = client.post("/api/intake/read", json=body).json()
    m = r["message"]
    assert len(fake.calls) == 1 and sum(gemini_used.values()) == spent + 1
    assert dict(m, slips=sorted(m["slips"])) == dict(key, slips=sorted(key["slips"]))     # the day the pages hold
    assert r["review"] and all(x["flags"] == ["other"] for x in r["review"])               # untracked lines, left out
    fixed = json.loads(json.dumps(r["read"]))                                              # a person corrects a line
    fixed["opd"][r["review"][0]["line"]]["condition"] = "fever"
    c = client.post("/api/intake/check", json=dict(phc=key["phc"], date=key["date"], read=fixed)).json()
    assert c["message"]["diagnoses"]["fever"] == m["diagnoses"]["fever"] + 1 and len(fake.calls) == 1
    # confirmed once, ahead of the feed; a register balance, or a negative count, is not something paper carries
    f, n = live.run.facilities.index(key["phc"]), len(live.run.slips)
    assert client.post("/api/intake/confirm", json=dict(m, stock={"ors_sachet": 5})).status_code == 422
    assert client.post("/api/intake/confirm", json=dict(m, diagnoses=dict(m["diagnoses"], fever=-5))).status_code == 422
    s = client.post("/api/intake/confirm", json=dict(m, received={"ors_sachet": 40})).json()["status"]
    assert (s["day"], s["through"], s["reported"]) == (60, 59, 1) and len(live.run.slips) == n + len(m["slips"])
    assert client.post("/api/intake/confirm", json=m).status_code == 409 and len(live.run.slips) == n + len(m["slips"])
    assert client.post("/api/intake/check", json=dict(phc=key["phc"], date=key["date"], read=fixed)).status_code == 409   # says so before confirm
    ors = live.run.ix["di"]["ors_sachet"]
    assert (live.run.book[60, f] == live.run.book[59, f]).all() and live.run.receipts[60, f, ors] == 40
    d = client.get("/api/district?t=60&net=live").json()                                  # the rest show their last report
    assert d["behind"] == 35 and d["footfall"]["today"] == sum(m["diagnoses"].values()) + int(live.run.dx[59].sum() - live.run.dx[59, f].sum())
    # the feed goes on from the day every PHC has reported, and ignores its own copy of the paper day
    assert client.post("/api/live/step?days=2").json()["day"] == 61
    s = client.get("/api/live").json()
    assert (s["day"], s["through"], s["complete"]) == (61, 61, True)
    assert sum(x[:2] == (60, f) for x in live.run.slips) == len(m["slips"]) and (live.run.book[60, f] == live.run.book[59, f]).all()
    assert client.post("/api/live/reset").json()["day"] == 59


def test_the_live_feed_jumps_ahead_and_publishes_each_day(monkeypatch):
    t0 = client.post("/api/live/reset").json()["day"]
    assert [client.post(f"/api/live/step?days={n}").status_code for n in (0, 15)] == [422, 422]
    s = client.post("/api/live/step?days=5").json()
    assert (s["day"], s["days"], s["via"]) == (t0 + 5, 5, "direct")
    s = client.get("/api/live").json()
    assert (s["day"], s["through"], s["complete"]) == (t0 + 5, t0 + 5, True)
    sent = []      # through Pub/Sub: each day published, and applied only when pushed back
    monkeypatch.setattr(main, "TOPIC", "projects/p/topics/t")
    monkeypatch.setattr(main.feeds, "publish", lambda topic, msgs: sent.append(msgs))
    monkeypatch.setattr(main, "INGEST_TOKEN", "secret")
    monkeypatch.setattr(main, "seen", {})
    assert client.post("/api/live/step?days=2").json()["via"] == "Pub/Sub" and len(sent) == 2
    assert client.get("/api/live").json()["through"] == t0 + 5
    for m in sent[1] + sent[0]:     # pushed back in any order
        client.post("/api/ingest?token=secret", json={"message": {"data": base64.b64encode(json.dumps(m).encode()).decode()}})
    assert client.get("/api/live").json()["through"] == t0 + 7
    pubsub = next(x for x in client.get("/api/google").json()["services"] if x["service"] == "Pub/Sub")
    assert pubsub["status"] == "live" and "last published" in pubsub["detail"]
    assert client.post("/api/live/reset").json()["day"] == t0


def test_meta_gives_the_map_its_points_and_says_which_are_real():
    demo, kerala = client.get("/api/meta").json(), client.get("/api/meta?net=kerala").json()
    assert not demo["real_sites"] and demo["sites_note"].startswith("SYNTHETIC")
    assert [(x["lat"], x["lon"]) for x in demo["facilities"]] == [(round(a, 5), round(b, 5)) for a, b in replay.coords.tolist()]
    assert kerala["real_sites"] == main.scenario.REAL_SITES
    if kerala["real_sites"]:        # and Google Maps road times between them
        assert kerala["real_roads"]
        sites = json.loads(main.scenario.SITES.read_text(encoding="utf-8"))["sites"]
        assert [(x["lat"], x["lon"]) for x in kerala["facilities"]] == [(round(s["lat"], 5), round(s["lon"], 5)) for s in sites]
        assert not any(s["name"] in json.dumps(kerala) for s in sites) and "OpenStreetMap" in kerala["sites_note"]


def test_the_india_check_is_served_as_it_came_out():
    d = client.get("/api/real/india").json()
    assert d == json.loads(json.dumps(realcheck_india.summary())) and d["decision"] == {"v0": "refuted", "v1": "inconclusive"}
