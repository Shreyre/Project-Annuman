import numpy as np
import pytest

from anumaan import crg as G, federation as FED, filter as FL, sim


def test_state_node_reproduces_the_central_filter():
    # computing inside the state on its own slice, with rho/tau passed explicitly,
    # must give exactly what a central filter_series run would (and keeps learn() in sync)
    run = sim.simulate(seed=5)
    node = FED.StateNode(run, "S1")
    obs = G.aggregate(run.ix, run.dx, run.slips, run.na)
    P = run.ix["primaries"]
    given = sum(obs[k][:, :, P].sum(2) for k in G.CATS)
    for i in (0, 11):
        f = node.fac[i]
        skip = FL.entry_gaps(given[:, f], obs["N"][:, f, P].sum(1))
        for d in P:
            cats = np.stack([obs[k][:, f, d] for k in G.CATS], 1)
            p = FL.filter_series(obs["N"][:, f, d], cats, run.book[:, f, d], obs["exp_units"][:, f, d],
                                 run.receipts[:, f, d], skip)
            assert np.allclose(node.post[(i, d)], p)


def test_only_gated_aggregates_reach_the_national_project():
    run = sim.simulate(seed=5)
    nodes = [FED.StateNode(run, s) for s in ("S0", "S1")]
    nat = FED.National()
    for n in nodes:
        nat.ingest(n.export(120))
    ex = nat.exports["S0"]
    assert len(ex["rows"]) == 3 * len(run.ix["primaries"]) and ex["suppressed"] == 0
    assert all(r["OK"] + r["SCARCE"] + r["OUT"] == r["n"] == 6 for r in ex["rows"])
    pri = nat.priors()
    assert set(pri) == {run.ix["drugs"][d] for d in run.ix["primaries"]}
    assert all(0.3 <= p["rho"] <= 1.2 and 0.05 <= p["tau"] <= 0.8 for p in pri.values())
    assert set(nat.view()) == set(pri)

    strict = FED.StateNode(run, "S0", k=7).export(120)       # 6 PHCs per warehouse < 7
    assert strict["rows"] == [] and strict["suppressed"] == len(ex["rows"])
    slips = [list(map(int, s)) for s in nodes[0].slips[:1000]]
    for bad in (dict(ex, slips=[(1, 2, 3, 4, 5, 6)]),                        # raw rows
                dict(ex, rows=[dict(ex["rows"][0], drug=slips)]),            # raw rows inside a field
                dict(ex, rows=[dict(ex["rows"][0], onsets=["S0-W0-P3"])]),   # facility id
                dict(ex, rows=[dict(ex["rows"][0], n="6")]),                 # not a count
                dict(ex, rows=[dict(ex["rows"][0], OK=1, SCARCE=0, OUT=0)]), # counts that do not add up
                dict(ex, rows=[dict(ex["rows"][0], starved=2)]),             # a warehouse flag is 0 or 1
                dict(ex, rows=[dict(ex["rows"][0], n=3)])):                  # group under k
        with pytest.raises(AssertionError):
            nat.ingest(bad)


def test_national_flag_catches_the_injected_failure_and_nothing_else():
    run = sim.simulate(seed=5)                                     # held out
    nodes = [FED.StateNode(run, s) for s in ("S0", "S1")]
    ep = next(e for e in run.episodes if e["type"] == "NATIONAL" and not e.get("secondary"))
    flagged = set()
    for t in range(0, run.book.shape[0], 5):
        nat = FED.National()
        for n in nodes:
            nat.ingest(n.export(t))
        flagged |= {(d, ep["start"] <= t < ep["end"]) for d, v in nat.view().items() if v["national"]}
    assert (run.ix["drugs"][ep["drug"]], True) in flagged
    assert {d for d, _ in flagged} == {run.ix["drugs"][ep["drug"]]}


def test_cold_start_with_national_priors_still_detects():
    r = FED.cold_start(sim.simulate(seed=6), "S1", ks=(3,))[3]
    assert r["priors"]["events"] > 20
    assert r["priors"]["recall_4d"] >= 0.9 and r["priors"]["false_per_series_year"] <= 0.15
    # three days of local history learn rho too roughly for the shadow stock; the priors fix that
    # (in 7 of the 10 held-out cold states; `python -m anumaan.federation` reports all 10)
    assert r["priors"]["false_per_series_year"] < 0.5 * r["local"]["false_per_series_year"]
