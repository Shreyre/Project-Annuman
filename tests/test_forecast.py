import numpy as np
import pytest

from anumaan import forecast as FC, sim


def test_forecast_series_level_trend_and_interval():
    rng = np.random.default_rng(0)
    flat = rng.poisson(10, (150, 4, 3))                   # batch of 12 series, shape kept
    mean, lo, hi = FC.forecast_series(flat[:120], 14)
    assert mean.shape == lo.shape == hi.shape == (14, 4, 3)
    assert np.abs(mean - 10).max() < 1.5
    assert (np.diff(hi - lo, axis=0) >= -1e-9).all()      # widens with the horizon
    inside = ((flat[120:134] >= lo) & (flat[120:134] <= hi)).mean()
    assert 0.65 < inside < 0.95                           # roughly the promised 80%
    ramp = 5 + 0.3 * np.arange(100) + rng.normal(0, 1, 100)
    assert FC.forecast_series(ramp, 14)[0][-1] > ramp[-7:].mean()   # follows the trend
    mean, lo, hi = FC.forecast_series(flat[:8], 14)       # too short for a spread
    assert np.isfinite(mean).all() and np.isnan(lo).all() and np.isnan(hi).all()


def test_diagnoses_beat_censored_consumption_on_held_out_network():
    run = sim.simulate(seed=5)
    ets = {t: FC.forecast_series(run.dx[:t + 1], 14) for t in range(45, 186, 14)}   # stand-in for TimesFM
    r = FC.evaluate_forecast(run, timesfm=ets)            # grid chosen on seeds 0-4
    for k in ("all", "after_out"):
        assert r[k]["crg"] < 0.75 * r[k]["consumption"]
        assert r[k]["crg"] < r[k]["consumption_adj"]      # also beats the fair out-day-adjusted baseline
    assert r["after_out"]["crg"] < 0.75 * r["after_out"]["rescaled"]   # not just prescribing bias
    assert 0.7 < r["coverage80"] < 0.9
    # the TimesFM arm is scored exactly like crg: same cells, origins, horizon and CRG step
    assert all(r[k]["timesfm"] == r[k]["crg"] for k in ("all", "after_out", "surge", "calm"))
    assert r["coverage80_timesfm"] == r["coverage80"] and r["bias"]["timesfm"] == r["bias"]["crg"]


def test_bigquery_series_per_origin_and_rows_parse_back():
    sql = FC.bigquery_sql("p.ds.dx", range(45, 60, 14), 14)
    assert "UNNEST([45, 59]) AS origin" in sql and "WHERE day <= origin" in sql   # history cut per origin
    assert "id_cols => ['seed', 'fac', 'cond', 'origin']" in sql and "model => 'TimesFM 3.0'" in sql
    seeds, origins, F, C, H = [7, 5], [45, 59], 2, 3, 4
    # REST hands every value back as a string; value encodes where it belongs
    rows = [[str(v) for v in (s, f, c, t, t + 1 + h, 100000 * s + 1000 * t + 100 * f + 10 * c + h, -1, 99)] + [""]
            for s in seeds for f in range(F) for c in range(C) for t in origins for h in range(H)]
    out = FC.parse_timesfm(rows[::-1], seeds, origins, F, C, H)          # any row order
    mean, lo, hi = out[7][59]
    assert mean.shape == lo.shape == (H, F, C) and mean[3, 1, 2] == 759123 and out[5][45][0][0, 0, 0] == 545000
    assert (lo == -1).all() and (hi == 99).all()
    shifted = [r[:4] + [str(int(r[4]) + H)] + r[5:] for r in rows]       # days past the horizon
    for bad in (rows[1:], rows[:-1] + rows[:1], shifted, rows[:-1] + [rows[-1][:-1] + ["too short"]]):
        with pytest.raises(RuntimeError):
            FC.parse_timesfm(bad, seeds, origins, F, C, H)


def _weekly_rows(seed, origins, F, C, total, lo=0, hi=0):
    """AI.FORECAST-style weekly rows: week w ahead ends on day origin + 7 (w + 1)."""
    return [[str(v) for v in (seed, f, c, t, t + 7 * (w + 1), total(t, w, f, c), lo, hi)] + [""]
            for t in origins for w in range(2) for f in range(F) for c in range(C)]


def test_weekly_sql_and_rows_keep_the_weekly_totals():
    sql = FC.bigquery_sql("p.ds.dx", range(45, 60, 14), 14, "TimesFM 2.5", 0.1, "week")
    assert "UNNEST([45, 59]) AS origin" in sql and "WHERE day <= origin" in sql         # history cut per origin
    assert "DIV(origin - day, 7) AS wk" in sql and "HAVING COUNT(*) = 7" in sql           # whole weeks back from it
    assert "INTERVAL origin - 7 * wk DAY" in sql and "horizon => 2," in sql               # week 0 ends on the origin
    assert "confidence_level => 0.1)" in sql and "model => 'TimesFM 2.5'" in sql
    total = lambda t, w, f, c: 1000 * w + 100 * f + 10 * c + t % 10                        # encodes where it belongs
    rows = _weekly_rows(3, [45, 59], 2, 3, total, lo=-7, hi=14)
    mean, lo, hi = FC.parse_timesfm(rows[::-1], [3], [45, 59], 2, 3, 14, "week")[3][59]
    assert mean.shape == lo.shape == (14, 2, 3)
    assert np.allclose(mean[:7, 1, 2], total(59, 0, 1, 2) / 7) and np.allclose(mean[7:, 1, 2], total(59, 1, 1, 2) / 7)
    assert np.allclose(mean.sum(0), [[total(59, 0, f, c) + total(59, 1, f, c) for c in range(3)] for f in range(2)])
    qmean = FC.parse_timesfm(rows, [3], [45, 59], 2, 3, 14, "week", "qmean")[3][59][0]
    assert np.allclose(qmean.sum(0), 0.4 * mean.sum(0) + 2 * (0.3 * -7 + 0.3 * 14))      # 0.4 m + 0.3 lo + 0.3 hi
    off = [r[:4] + [str(int(r[4]) - 1)] + r[5:] for r in rows]       # weeks not ending on origin + 7k
    with pytest.raises(RuntimeError):
        FC.parse_timesfm(off, [3], [45, 59], 2, 3, 14, "week")


def test_blend_with_ets_weight_1_scores_exactly_like_the_ets():
    run = sim.simulate(seed=5)
    F, C, origins = *run.dx.shape[1:], range(45, 186, 14)
    ets = {t: FC.forecast_series(run.dx[:t + 1], 14) for t in origins}
    other = {t: (np.full((14, F, C), 3.0),) * 3 for t in origins}                          # any other forecast
    r1, r0 = FC.evaluate_forecast(run, timesfm=other, blend=1.0), FC.evaluate_forecast(run, timesfm=other)
    assert all(r1[k]["timesfm"] == r1[k]["crg"] for k in ("all", "after_out", "surge", "calm"))
    assert r1["bias"]["timesfm"] == r1["bias"]["crg"] and r0["all"]["timesfm"] != r0["all"]["crg"]
    # the ETS as weekly totals through the weekly parse scores like the ETS itself
    rows = _weekly_rows(5, origins, F, C, lambda t, w, f, c: ets[t][0][7 * w:7 * w + 7, f, c].sum())
    r = FC.evaluate_forecast(run, timesfm=FC.parse_timesfm(rows, [5], list(origins), F, C, 14, "week")[5])
    assert abs(r["all"]["timesfm"] - r["all"]["crg"]) < 1e-9


def test_saved_rows_are_rescored_without_bigquery(tmp_path, monkeypatch):
    run, calls = sim.simulate(seed=5, days=60), []
    F, C = run.dx.shape[1:]

    def fake(project, ds, table, csv, sql):
        calls.append(sql)
        return _weekly_rows(5, [45], F, C, lambda *_: 7.0, lo=0, hi=21), {"jobReference": {}}
    monkeypatch.setattr(FC, "_bigquery_rows", fake)
    save = str(tmp_path / "rows.json")
    q50 = FC.timesfm_forecast("p.ds", [5], [run.dx], [45], grain="week", save=save)[0][5][45][0]
    qmean = FC.timesfm_forecast("p.ds", [5], [run.dx], [45], grain="week", point="qmean", save=save)[0][5][45][0]
    assert len(calls) == 1 and np.allclose(q50, 1.0) and np.allclose(qmean, (0.4 * 7 + 0.3 * 21) / 7)
    for kw, dx in ((dict(point="q55"), run.dx), ({}, run.dx + 1)):    # another query, or other data: not reused
        with pytest.raises(RuntimeError):
            FC.timesfm_forecast("p.ds", [5], [dx], [45], grain="week", save=save, **kw)
    assert len(calls) == 1
