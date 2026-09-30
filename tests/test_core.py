import numpy as np

from anumaan import filter as FL, sim
from anumaan.evaluate import evaluate


def test_filter_sees_empty_shelf_the_register_hides():
    # 40 normal days, then the shelf empties: diagnoses continue, the drug stops,
    # a substitute appears - while the register still shows 40 days of stock.
    T = 60
    N = np.full(T, 10.0)
    cats = np.zeros((T, 4))
    cats[:40, 0] = 9                     # full courses
    cats[40:, 2], cats[40:, 3] = 4, 1    # substitutes, not-available
    post = FL.filter_series(N, cats, np.full(T, 400.0), exp_units=np.full(T, 10.0), receipts=np.zeros(T))
    assert post[:40, 0].min() > 0.7      # calm while care is normal
    assert post[43, 2] > 0.7             # OUT within 3 days despite the register


def test_data_entry_gap_is_not_a_stockout():
    T = 60
    cats = np.zeros((T, 4))
    cats[:, 0] = 9
    cats[40:44] = 0                      # nothing entered for 4 days...
    skip = np.zeros(T, bool)
    skip[40:44] = True                   # ...on every medicine, so entry_gaps flags it
    post = FL.filter_series(np.full(T, 10.0), cats, np.full(T, 400.0), np.full(T, 10.0), np.zeros(T), skip)
    assert post[40:44, 2].max() < 0.3


def test_shadow_stock_warns_before_the_shelf_empties_without_rationing():
    # 300 units, 10 dispensed a day in full, no delivery: empty on day 30. The register never
    # posts an issue and care looks normal until the shelf is bare.
    T = 40
    cats = np.zeros((T, 4))
    cats[:30, 0] = 10                    # full courses...
    cats[30:, 3] = 3                     # ...then not-available slips
    units = np.where(np.arange(T) < 30, 10.0, 0.0)
    book, rec, ten = np.full(T, 300.0), np.zeros(T), np.full(T, 10.0)
    post = FL.filter_series(ten, cats, book, ten, rec)
    cover = FL.shadow_cover(book, rec, units, ten, post)
    assert FL.alarms(post)[0][0] >= 29                   # care alone: only once it is empty
    assert FL.alarms(post, cover=cover)[0][0] == 23      # under 8 days left: a week ahead


def test_beats_the_register_on_a_held_out_network():
    for behaviour in ("default", "alt"):
        r = evaluate(sim.simulate(seed=5, behaviour=behaviour))   # tuning used seeds 0-4
        m = r["model"]
        assert m["recall_4d"] >= 0.9
        assert m["false_per_series_year"] <= 0.15
        assert m["recall_4d"] > r["register_best"]["recall_4d"] + 0.3        # same false-alarm budget
        assert r["drug_only"]["false_per_series_year"] > 5 * m["false_per_series_year"]   # diagnoses earn their place
        assert r["triage_7d"]["accuracy"] > r["triage_majority"] + 0.25      # where it broke: well past the commonest-cause guess
    assert evaluate(sim.simulate(seed=5))["model"]["early"] >= 0.8
    # staff who never ration give care no early sign; the shadow stock still warns for most outages
    r = evaluate(sim.simulate(seed=5, p_ration=0.0))
    assert r["care_only"]["early"] < 0.15 and r["model"]["early"] >= 0.45
    assert r["model"]["false_per_series_year"] <= 0.15


def test_a_scripted_scenario_injects_its_failures_and_no_others():
    script = [dict(type="STATE-PROCUREMENT", root="S0", drug="amoxicillin_500", start=21, dur=28, short=0.0)]
    run = sim.simulate(seed=3, days=70, n_states=1, n_wh=4, n_phc=2, episodes=script)
    assert {(e["type"], e["root"], e["start"], e["end"]) for e in run.episodes} == {("STATE-PROCUREMENT", "S0", 21, 49)}
    d = run.ix["di"]["amoxicillin_500"]
    assert not run.wh_got[21:49, :, d].any() and run.wh_asked[21:49, :, d].any()      # the state fills none of its indents
    assert run.wh_got[:21, :, d].any() and run.wh_got[49:, :, d].any()
    assert len(sim.simulate(seed=3, days=70).episodes) >= 8                         # unscripted: the usual random failures
