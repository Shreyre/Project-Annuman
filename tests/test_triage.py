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
