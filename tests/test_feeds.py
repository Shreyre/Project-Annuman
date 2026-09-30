import csv

import numpy as np
import pytest

from anumaan import feeds, sim


def test_csv_feeds_give_the_same_alarms_as_the_simulator(tmp_path):
    run = sim.simulate(seed=5, days=120)
    feeds.export(run, tmp_path)
    loaded, start, unregistered = feeds.load(tmp_path)
    assert start == feeds.START and not unregistered
    obs0, s0 = feeds.infer(run)
    obs1, s1 = feeds.infer(loaded)
    assert s0.keys() == s1.keys() and all(s0[k][2] == s1[k][2] for k in s0)
    rows = feeds.alarms_on(loaded, obs1, s1, 110, start)
    assert rows and rows == feeds.alarms_on(run, obs0, s0, 110, start)

    # no warehouse ledger: "where it broke" can only say LOCAL or DEMAND-SURGE
    (tmp_path / "indents.csv").unlink()
    bare, _, _ = feeds.load(tmp_path)
    assert {r["level"] for r in feeds.alarms_on(bare, *feeds.infer(bare), 110, start)} <= {"LOCAL", "DEMAND-SURGE"}

    # a register written up weekly carries its balance forward; one never written up is skipped
    with (tmp_path / "stock.csv").open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    kept = [rows[0]] + [r for r in rows[1:] if r[0].endswith(("01", "08", "15", "22")) and
                        not (r[1] == run.facilities[0] and r[2] == run.ix["drugs"][run.ix["primaries"][0]])]
    with (tmp_path / "stock.csv").open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(kept)
    sparse, _, unregistered = feeds.load(tmp_path)
    assert (0, run.ix["primaries"][0]) in unregistered
    assert not np.isnan(sparse.book).any() and sparse.book[1, 1, 0] == sparse.book[0, 1, 0]
    assert (0, run.ix["primaries"][0]) not in feeds.infer(sparse, unregistered)[1]

    with (tmp_path / "receipts.csv").open("a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow(["2026-01-02", run.facilities[0], "not_a_drug", 5])
    with pytest.raises(ValueError, match="receipts.csv line .*not_a_drug"):
        feeds.load(tmp_path)
