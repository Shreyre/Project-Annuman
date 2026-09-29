import numpy as np

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
    r = FC.evaluate_forecast(sim.simulate(seed=5))        # grid chosen on seeds 0-4
    for k in ("all", "after_out"):
        assert r[k]["crg"] < 0.75 * r[k]["consumption"]
        assert r[k]["crg"] < r[k]["consumption_adj"]      # also beats the fair out-day-adjusted baseline
    assert r["after_out"]["crg"] < 0.75 * r["after_out"]["rescaled"]   # not just prescribing bias
    assert 0.7 < r["coverage80"] < 0.9
    assert "model => 'TimesFM 3.0'" in FC.bigquery_sql("proj.ds.dx", 14)
