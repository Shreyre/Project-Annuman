import numpy as np

from anumaan import triage


def test_where_it_broke_reads_the_warehouse_ledger():
    wh = ["S0-W0", "S0-W1", "S0-W2", "S1-W0", "S1-W1", "S1-W2"]     # one PHC per warehouse
    st = [w[:2] for w in wh]
    T = 60
    asked = np.zeros((T, 6, 1))
    asked[::7] = 100.0                                             # weekly indents to the state
    posted = np.tile(np.arange(T)[:, None, None], (1, 6, 1))       # receipts posted the day they came

    def label(starved, lift=1.0):
        got = asked.copy()
        got[28:, starved] = 0                                      # the state stops filling from day 28
        fill = triage.fill_rate(asked, got, posted)
        return triage.classify([(0, 0, 55)], wh, st, fill, np.full((T, 6, 1), lift), 56)[0]

    assert label([]) == "LOCAL"
    assert label([], lift=1.5) == "DEMAND-SURGE"
    assert label([0]) == "WAREHOUSE"
    assert label([1]) == "LOCAL"                  # another warehouse starved is not this PHC's break
    assert label([1, 2]) == "STATE-PROCUREMENT"
    assert label([0, 1, 3, 4]) == "NATIONAL"
    assert label([0], lift=1.5) == "WAREHOUSE"    # a supply break outranks a surge


def test_a_state_failure_takes_a_third_of_a_states_warehouses():
    assert triage.wide(2, 3) and not triage.wide(1, 3)            # the 3-warehouse states the rule was tuned on
    assert not triage.wide(4, 14) and triage.wide(5, 14)          # in 14 districts a few lost ledger postings are chance
    wh = [f"S0-W{i}" for i in range(14)]                          # one state, one PHC per district warehouse
    T = 60
    asked = np.zeros((T, 14, 1))
    asked[::7] = 100.0
    posted = np.tile(np.arange(T)[:, None, None], (1, 14, 1))

    def label(starved):
        got = asked.copy()
        got[28:, starved] = 0
        return triage.classify([(0, 0, 55)], wh, ["S0"] * 14, triage.fill_rate(asked, got, posted), np.ones((T, 14, 1)), 56)[0]

    assert label([0, 1]) == "WAREHOUSE" and label([1, 2, 3]) == "LOCAL"
    assert label([1, 2, 3, 4, 5]) == "STATE-PROCUREMENT"
