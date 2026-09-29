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
