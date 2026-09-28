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


def test_beats_the_register_on_a_held_out_network():
    for behaviour in ("default", "alt"):
        r = evaluate(sim.simulate(seed=5, behaviour=behaviour))   # tuning used seeds 0-4
        m = r["model"]
        assert m["recall_4d"] >= 0.9
        assert m["false_per_series_year"] <= 0.15
        assert m["recall_4d"] > r["register_best"]["recall_4d"] + 0.3        # same false-alarm budget
        assert r["drug_only"]["false_per_series_year"] > 5 * m["false_per_series_year"]   # diagnoses earn their place
    # early warning exists only because staff ration before the shelf empties (see --ration 0)
    assert evaluate(sim.simulate(seed=5))["model"]["early"] >= 0.8
