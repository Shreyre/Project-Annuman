import json

import numpy as np

from anumaan import crg as G, planner as PL, sim


def test_plan_takes_nearest_safe_surplus_and_escalates_state_failures():
    run = sim.simulate(seed=0, days=40, n_states=2, n_wh=1, n_phc=3)   # S0: f0-f2, S1: f3-f5
    obs = G.aggregate(run.ix, run.dx, run.slips, run.na)
    t, d, sub = 39, run.ix["di"]["amoxicillin_500"], run.ix["di"]["erythromycin_250"]
    use = obs["exp_units"][t - 13:t + 1].mean(0)
    # f0 short; f1 4 km off with 5 spare courses; f2 33 km off with plenty; f3 is nearest of all
    # but across the state line in S1 (where f4's alarm also says the state is failing); f5 is 200 km away
    coords = np.array([[22.0, 78.0], [22.0, 78.04], [22.3, 78.0], [22.01, 78.0], [23.0, 79.0], [24.0, 80.0]])
    spare = {1: 5, 2: 1000, 3: 1000, 5: 1000}
    run.book[t, :, d] = [0, *((14 * use[f, d] + 15 * spare.get(f, 0) + 1) / PL.DISCOUNT for f in range(1, 6))]
    run.book[t, 0, sub] = (10 * 20 + 1) / PL.DISCOUNT          # 10 erythromycin courses on f0's shelf
    run.book[t, 0, run.ix["di"]["doxycycline_100"]] = 0        # and none of the other guideline substitute
    calm, out = np.tile([1.0, 0, 0], (run.book.shape[0], 1)), np.tile([0, 0.1, 0.9], (run.book.shape[0], 1))
    post = {(f, d): out if f in (0, 4) else calm for f in range(6)}
    labels = {(0, d): "LOCAL", (4, d): "STATE-PROCUREMENT"}

    p = PL.plan(t, run, post, labels, coords, obs=obs)
    need = int(round(obs["N"][t - 13:t + 1, 0, d].mean() * 14))
    got = {run.facilities.index(x["from_fac"]): x["courses"] for x in p["transfers"]}
    assert p["recipients"][0]["sub_courses"] == 10                # the substitute cuts the need
    assert got == {1: 5, 2: need - 10 - 5}                         # nearest first; S1 and far f5 untouched
    assert all(x["units"] == 15 * x["courses"] and x["minutes"] <= 180 for x in p["transfers"])
    assert [o["qty_units"] for o in p["orders"]] == [x["units"] for x in p["transfers"]]
    assert [(e["fac"], e["level"]) for e in p["escalations"]] == [(run.facilities[4], "STATE-PROCUREMENT")]
    # the state line alone keeps f3 out; a NATIONAL alarm on the drug stops every donor
    assert PL.plan(t, run, post, {(0, d): "LOCAL"}, coords, obs=obs)["transfers"] == p["transfers"]
    assert PL.plan(t, run, post, {(0, d): "LOCAL", (4, d): "NATIONAL"}, coords, obs=obs)["transfers"] == []
    # with a shadow stock, donors are sized on it instead: f1 now holds plenty, so it covers everything
    cover = {(f, d): np.full(run.book.shape[0], 1000.0) for f in (1, 2)}
    q = PL.plan(t, run, post, labels, coords, obs=obs, cover=cover)
    assert {x["from_fac"]: x["courses"] for x in q["transfers"]} == {run.facilities[1]: need - 10}
    assert "shadow stock shows 1000d" in q["transfers"][0]["reason"]


def test_travel_times_and_routes_tiles():
    run = sim.simulate(seed=1, days=5)
    c = PL.synthetic_coords(run, seed=1)
    m = PL.travel_minutes(c)
    assert abs(PL.travel_minutes(np.array([[20.0, 78.0], [21.0, 78.0]]))[0, 1] - 111.2 * 1.4 / 35 * 60) < 1
    same = np.array(run.wh)[:, None] == np.array(run.wh)[None]
    assert m[same].max() <= 60 * 1.4 / 35 * 60 + 1                 # PHCs within 30 km of their warehouse
    reqs = PL.route_matrix_requests(c)
    assert all(len(b["origins"]) * len(b["destinations"]) <= 625 for _, _, b in reqs)
    assert sum(len(b["origins"]) * len(b["destinations"]) for _, _, b in reqs) == len(c) ** 2


def test_cached_road_times_apply_to_their_own_map_only(tmp_path, monkeypatch):
    c = np.array([[22.0, 78.0], [22.0, 78.1], [22.1, 78.0]])
    line = PL.travel_minutes(c)                                     # no cached map for these points
    # Routes streams origin 0 -> [1, 2] out of order; 0 -> 2 has no road
    arcs = PL._arcs(0, [1, 2], [{"destinationIndex": 1, "status": {}, "condition": "ROUTE_NOT_FOUND"},
                                {"destinationIndex": 0, "status": {}, "distanceMeters": 9000, "duration": "600s",
                                 "condition": "ROUTE_EXISTS"}])
    assert arcs == [[0, 1, 600, 9000]]
    (f := tmp_path / "road.json").write_text(json.dumps({"maps": {"0": {"coords": c.tolist(), "arcs": arcs}}}))
    monkeypatch.setattr(PL, "ROAD_MINUTES", f)
    PL._road_maps.cache_clear()
    try:
        m = PL.travel_minutes(c)
        assert m[0, 1] == 10 and m[0, 2] == line[0, 2] and m[1, 0] == line[1, 0]   # no road / not asked: straight line
        assert PL.travel_minutes(c + 1e-9)[0, 1] == 10              # float noise between machines still matches
        assert PL.travel_minutes(c + 0.01)[0, 1] > 20               # any other map: straight line only
    finally:
        PL._road_maps.cache_clear()
