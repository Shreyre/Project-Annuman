import csv

import numpy as np
import pytest

from anumaan import care as CARE, crg as G, feeds, sim


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


def test_beds_and_staff_come_through_the_csv_feeds_unchanged(tmp_path):
    run = sim.simulate(seed=5, days=60)
    care = CARE.simulate(run, 5)
    feeds.export(run, tmp_path, care=care)
    loaded, start, _ = feeds.load(tmp_path)
    back = feeds.load_care(tmp_path, loaded, start)
    assert back.occupancy is None and back.present is None        # no ground truth in a feed
    assert all(np.array_equal(CARE.beds(care)[k], CARE.beds(back)[k], equal_nan=True) for k in ("occupied", "early", "pressure"))
    assert np.array_equal(CARE.staff(care)["verify"], CARE.staff(back)["verify"])
    full, verify = feeds.care_on(back, 50)
    assert (full, verify) == feeds.care_on(care, 50) and full
    (tmp_path / "posts.csv").unlink()
    assert feeds.load_care(tmp_path, loaded, start) is None       # a state without the bed and staff feeds still runs


def test_the_stream_carries_the_same_rows_one_phc_day_at_a_time():
    run = sim.simulate(seed=5, days=60)
    care = CARE.simulate(run, 5)
    blank = np.zeros_like
    live = sim.Run(run.ix, run.facilities, run.wh, run.st, blank(run.dx), [], [], blank(run.book), blank(run.receipts),
                   blank(run.wh_asked), blank(run.wh_got), np.full(run.wh_posted.shape, 60), None, None, None, None, None)
    seen = CARE.Care(care.facilities, care.capacity, [], [], care.in_position, blank(care.marked), blank(care.exposure),
                     blank(care.acts), None, None)
    obs, day = G.aggregate(live.ix, live.dx, [], []), feeds.stream(run, care)
    for t in range(60):
        for m in day(t):
            feeds.absorb(m, live, obs, seen)
    ref = G.aggregate(run.ix, run.dx, run.slips, run.na)
    assert all(np.allclose(obs[k], ref[k]) for k in ref)
    assert all(np.array_equal(getattr(live, k), getattr(run, k)) for k in ("dx", "book", "receipts", "wh_asked"))
    got = lambda r: r.wh_got * (r.wh_posted < 60)                  # a receipt reaches the feed the day the ledger posts it
    assert np.array_equal(got(live), got(run)) and np.array_equal(live.wh_posted, np.where(got(run) != 0, run.wh_posted, live.wh_posted))
    assert sorted(live.slips) == sorted(run.slips) and sorted(seen.admits) == sorted(care.admits)
    assert np.array_equal(seen.marked, care.marked) and np.array_equal(seen.acts, care.acts)

    m, n = day(30)[0], len(live.slips)
    feeds.absorb(m, live, obs, seen, skip=lambda kind, t, f: True)             # a report already in is left alone
    assert len(live.slips) == n
    for bad in (dict(m, phc="nowhere"), dict(m, date="2030-01-01"), dict(m, slips=[["fever"]]), dict(m, kind="other")):
        with pytest.raises(ValueError):
            feeds.absorb(bad, live, obs, seen)
    assert len(live.slips) == n                                                # and a bad one writes nothing
