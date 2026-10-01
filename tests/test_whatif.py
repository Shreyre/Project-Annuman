"""What-if worlds (anumaan/whatif.py), the simulator's health emergencies and the district surge rule.
Every record is SYNTHETIC."""
import hashlib
import json
import sys
from functools import cache

import numpy as np
import pytest

from anumaan import crg as G, scenario, sim, triage, whatif

# sha256 of each world's records on commit 1d51118, taken before sim.simulate had surges
BEFORE = dict(seed5="db32a838de74363fce6cc18bc43f8c67e84be834f7b45cab5461b41a3eb26625",
              seed0="14e6e1957482094f2f6ba661146729aed68195fe8aa921a25173ea35446008b7",
              kerala="14fcf8b6e6fc42d90e124d02e7aea6039575941001f1c7d479c75d490aa99f6a",
              kerala_jitter="dcaff9ac5552df421f75a8374c288cceee6fa0b036aff780987bdd975fdee6df")
ARRAYS = ("dx", "book", "receipts", "wh_asked", "wh_got", "wh_posted", "true_stock", "rate", "true_use", "ration_cover")


built = cache(lambda key: whatif.build(whatif.parse(key)))      # (run, coords, names, about)


def fingerprint(run):
    h = hashlib.sha256()
    for k in ARRAYS:
        a = np.ascontiguousarray(getattr(run, k))
        h.update(f"{k}{a.dtype}{a.shape}".encode())
        h.update(a.tobytes())
    h.update(np.array(run.slips, float).tobytes())
    h.update(np.array(run.na, float).tobytes())
    h.update(json.dumps([run.facilities, run.wh, run.st, run.episodes], sort_keys=True, default=float).encode())
    return h.hexdigest()


@pytest.mark.skipif((np.__version__, sys.platform) != ("2.4.3", "win32"),
                    reason="fingerprints recorded with numpy 2.4.3 on Windows; other builds may draw differently")
def test_every_existing_world_is_bit_identical():
    assert fingerprint(sim.simulate(seed=5)) == BEFORE["seed5"]
    assert fingerprint(sim.simulate(seed=0)) == BEFORE["seed0"]
    run, coords, _ = scenario.kerala()
    assert fingerprint(run) == BEFORE["kerala"]
    if not scenario.REAL_SITES:
        assert hashlib.sha256(np.ascontiguousarray(coords).tobytes()).hexdigest() == BEFORE["kerala_jitter"]


def test_an_emergency_changes_its_district_from_its_first_day_only():
    kw = dict(seed=3, days=70, n_states=1, n_wh=3, n_phc=2)
    a = sim.simulate(**kw)
    for same in (sim.simulate(**kw, surges=[]),       # x1 moves nothing, not even a random draw
                 sim.simulate(**kw, surges=[dict(root="S0-W1", conds=whatif.CONDS, lift=1.0, start=20, dur=28)])):
        assert fingerprint(same) == fingerprint(a)
    b = sim.simulate(**kw, surges=[dict(root="S0-W1", conds=whatif.CONDS, lift=3.0, start=40, dur=28)])
    assert all(np.array_equal(getattr(a, k)[:40], getattr(b, k)[:40]) for k in ("dx", "book", "true_stock", "receipts"))
    ci, w1 = [a.ix["ci"][c] for c in whatif.CONDS], [f for f, w in enumerate(a.wh) if w == "S0-W1"]
    assert b.dx[40:68, w1][:, :, ci].sum() > 2 * a.dx[40:68, w1][:, :, ci].sum()


def test_kerala_uses_real_sites_but_never_their_names():
    _, coords, names = scenario.kerala()
    if scenario.REAL_SITES:
        sites = json.loads(scenario.SITES.read_text(encoding="utf-8"))["sites"]
        assert np.array_equal(coords, [[s["lat"], s["lon"]] for s in sites])
        assert "real public PHC/FHC locations from OpenStreetMap; every record is synthetic" in scenario.ABOUT["note"]
        shown = json.dumps([names, scenario.ABOUT, [built(m["key"])[2:] for m in whatif.MENU]])
        assert not any(s["name"] in shown for s in sites)


def test_district_rule_calls_a_one_district_surge():
    wh, T = ["S0-W0", "S0-W1", "S0-W2"], 60                      # one PHC a warehouse, every indent filled
    asked = np.zeros((T, 3, 1))
    asked[::7] = 100.0
    fill = triage.fill_rate(asked, asked.copy(), np.tile(np.arange(T)[:, None, None], (1, 3, 1)))
    flat = np.ones((T, 3, 1))

    def label(district, fill=fill):
        dlift = flat.copy()
        dlift[:, 0] = district
        return triage.classify([(0, 0, 55)], wh, ["S0"] * 3, fill, flat, 56, dlift)[0]

    assert label(triage.DISTRICT) == "DEMAND-SURGE" and label(1.9) == "LOCAL"
    assert triage.classify([(0, 0, 55)], wh, ["S0"] * 3, fill, flat, 56)[0] == "LOCAL"      # callers that pass no dlift
    got = asked.copy()
    got[28:, 0] = 0
    starved = triage.fill_rate(asked, got, np.tile(np.arange(T)[:, None, None], (1, 3, 1)))
    assert label(3.0, starved) == "WAREHOUSE"                    # a supply break still outranks a surge


def test_a_warehouse_cut_is_called_in_its_district_only():
    pick = dict(world="kerala", event="warehouse", district="S0-W6", medicine="amoxicillin_500", day=40)
    run, _, _, about = built(whatif.validate(pick))
    w = about["whatif"]
    assert w["level"] == "WAREHOUSE" and w["called"] is not None and w["stockouts"] and about["script"]
    assert run.wh[w["start"]["f"]] == "S0-W6" and w["start"]["day"] == w["called"]
    d = run.ix["di"]["amoxicillin_500"]
    obs = G.aggregate(run.ix, run.dx, run.slips, run.na)        # every PHC, not just the district's
    segs = scenario.alarm_spells(run, obs, range(len(run.facilities)), [d])
    fill, N = triage.fill_rate(run.wh_asked, run.wh_got, run.wh_posted), obs["N"]
    for t in (w["called"] - 1, w["called"]):
        live = [(f, d, s) for (f, _), ss in segs.items() for s, e in ss if s + 1 <= t < e]
        lab = triage.classify(live, run.wh, run.st, fill, triage.surge_lift(N, run.st), t, triage.surge_lift(N, run.wh))
        hit = {run.wh[f] for (f, _, _), x in zip(live, lab) if x == "WAREHOUSE"}
        assert hit == ({"S0-W6"} if t == w["called"] else set())
    assert whatif.LABEL in about["note"] and about["sources"] == []


def test_a_state_cut_is_a_state_procurement_failure():
    _, _, names, about = built(whatif.validate(dict(world="demo", event="state", medicine="amoxicillin_500", day="40")))
    w = about["whatif"]
    assert w["level"] == "STATE-PROCUREMENT" and w["called"] is not None and w["seed"] == whatif.DEMO_SEED
    assert "calls a state procurement failure" in about["script"] and names == {}


def test_an_emergency_is_called_a_demand_surge_by_the_district_rule(monkeypatch):
    pick = dict(world="kerala", event="emergency", district="S0-W6", lift=3, day=60)
    w = built(whatif.validate(pick))[3]["whatif"]
    assert w["level"] == "DEMAND-SURGE" and w["day"] <= w["called"] < w["day"] + whatif.SPELL
    monkeypatch.setattr(triage, "DISTRICT", float("inf"))        # the state-wide rule alone
    late = whatif.build(pick)[3]["whatif"]["called"]
    assert late is None or late > w["called"]


def test_bad_picks_raise_value_error():
    good = dict(world="kerala", event="warehouse", district="S0-W6", medicine="amoxicillin_500", day=40)
    assert whatif.validate(good) == whatif.validate(dict(good, day="40")) == whatif.MENU[0]["key"]
    assert whatif.MENU[0]["key"] == "whatif:kerala:warehouse:S0-W6:amoxicillin_500:40"
    surge = dict(world="kerala", event="emergency", district="S0-W6", lift="2.5", day="60")
    assert whatif.parse(whatif.validate(surge)) == dict(surge)
    bad = [None, "kerala", dict(good, world="goa"), dict(good, world=["kerala"]), dict(good, event="flood"),
           dict(good, district="S0-W14"), dict(good, district="S1-W0"), dict(good, medicine="aspirin"),
           dict(good, day=34), dict(good, day=111), dict(good, day="4O"), dict(good, day=40.5), dict(good, day=True),
           dict(good, lift=3), {k: v for k, v in good.items() if k != "day"}, dict(surge, lift=5), dict(surge, lift="nan"),
           dict(surge, lift=[3]), dict(good, world="demo")]
    for p in bad:
        with pytest.raises(ValueError):
            whatif.build(p)                                      # refused before anything is simulated
    for key in (None, "kerala", "whatif:kerala:emergency:S0-W6:3.0:60", "whatif:kerala:state:metformin_500:040",
                "whatif:kerala:warehouse:S0-W6:amoxicillin_500", "whatif:kerala:flood:S0-W6"):
        with pytest.raises(ValueError):
            whatif.parse(key)
    assert all(whatif.validate(whatif.parse(m["key"])) == m["key"] for m in whatif.MENU)
