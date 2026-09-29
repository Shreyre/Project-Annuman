"""Demand forecasting from morbidity: forecast diagnoses, convert through the CRG.

Consumption-based forecasting learns from what was dispensed, and a stock-out
censors exactly that: an empty shelf dispenses nothing, so the forecast learns
"no demand" right when demand is unmet. Diagnoses keep coming during a stock-out,
so we forecast each condition's diagnoses per facility and multiply by the CRG's
expected units per diagnosis (ix["units"]). The fair comparison is consumption with
out-days filled from the in-stock rate (adjust_consumption); against that the CRG's
lead after stock-outs is modest, not the gap plain consumption suggests.

    python -m anumaan.forecast --seeds 5-9

Scored on SYNTHETIC data against GROUND TRUTH run.true_use. The sim defines demand
as diagnoses x CRG units, so this measures what censoring and prescribing drift
cost consumption forecasts, not whether the CRG itself is right.
"""
import argparse

import numpy as np

from anumaan import crg as G, sim
from anumaan.evaluate import _range

# ponytail: damped-trend exponential smoothing on a small grid. The cloud version is
# BigQuery AI.FORECAST with TimesFM 3.0 (see bigquery_sql); swap it in when the feeds
# live in BigQuery.
# (alpha level, beta trend, phi damping); grid and the in-sample-mean level start chosen on
# seeds 0-4, scored on the CRG arm only (the consumption arms reuse it untuned)
GRID = np.array([(a, b, p) for a in (0.01, 0.02, 0.05, 0.1, 0.2, 0.4) for b in (0.0, 0.05, 0.2) for p in (0.8, 0.95)])
Z80 = 1.2816   # two-sided 80% normal quantile
BURN = 14      # one-step errors before this are ignored when picking parameters
MIN_ERR = 7    # fewer one-step errors than this: no interval (NaN), not a fake-narrow one


def forecast_series(y, horizon):
    """Forecast daily y[T] (or [T, ...] for many series at once) `horizon` days ahead.

    Damped-trend Holt, ETS(A,Ad,N), fitted for every grid point at once; each series
    keeps the one with the lowest in-sample one-step squared error. Fitted on the raw
    daily series: pre-smoothing to a 7-day mean and picking on its one-step error
    doubled the 14-day error on seeds 0-4 (it rewards chasing the smoother's own lag).
    The level starts at the in-sample mean, so a small alpha shrinks to the history
    mean; a 7-day start lost to that plain mean (CRG WAPE 0.083-0.089 vs 0.065-0.071
    on seeds 0-4), this start gets 0.070-0.075 and keeps the ETS's edge at surges.
    Returns (mean, lo, hi), each [horizon, ...]: forecast daily values and an 80%
    interval from the residual spread, widening with the horizon (the analytic ETS
    variance: sigma^2 * (1 + sum of c_j^2), c_j = alpha * (1 + beta * (phi + .. + phi^j))).
    lo and hi are NaN for series too short to estimate a spread (under ~13 days).
    """
    y = np.asarray(y, float)
    s = y.reshape(len(y), -1)                                                # [T, K]
    a, b, p = (GRID[:, i, None] for i in range(3))                           # [G, 1]
    lev = np.repeat(s.mean(0, keepdims=True), len(GRID), 0)                  # [G, K]
    tr, sse = np.zeros_like(lev), np.zeros_like(lev)
    burn = min(BURN, len(s) // 2)
    for t in range(1, len(s)):            # error-correction form
        pred = lev + p * tr
        e = s[t] - pred
        if t >= burn:
            sse += e ** 2
        lev, tr = pred + a * e, p * tr + a * b * e
    k, cols = sse.argmin(0), np.arange(s.shape[1])
    ak, bk, phi = GRID[k].T
    damp = np.cumsum(phi[None, :] ** np.arange(1, horizon + 1)[:, None], 0)  # [H, K]
    mean = np.maximum(lev[k, cols] + tr[k, cols] * damp, 0)
    c2 = np.cumsum((ak * (1 + bk * damp[:-1])) ** 2, 0)
    n_err = len(s) - max(burn, 1)
    var = sse[k, cols] / n_err if n_err >= MIN_ERR else np.nan
    sd = np.sqrt(var * (1 + np.vstack([np.zeros_like(ak), c2])))
    shape = (horizon, *y.shape[1:])
    return (mean.reshape(shape), np.maximum(mean - Z80 * sd, 0).reshape(shape),
            (mean + Z80 * sd).reshape(shape))


def _total(y, t, horizon):
    """Forecast units summed over days t+1..t+horizon using data up to day t only."""
    return forecast_series(y[:t + 1], horizon)[0].sum(0)


def forecast_demand(ix, dx, t, horizon=14):
    """[F, D] expected units over days t+1..t+horizon: each facility x condition's
    diagnoses forecast from dx[:t+1], times the CRG's units per diagnosis. Substitute
    drugs come out 0: the CRG only prices the guideline course."""
    return _total(dx, t, horizon) @ ix["units"]


def adjust_consumption(units, out, t, window=56):
    """WHO/MSH "adjusted consumption": dispensed units [T, F, D] with each out-day
    replaced by the in-stock rate, units per in-stock day over the last `window` days
    up to t. out is an OBSERVABLE mask [T, F, D], e.g. from crg.aggregate:
    (na > 0) | (sub > 0) | (units == 0) - a not-available record, a substitute given,
    or nothing dispensed."""
    # ponytail: one rate as of t fills every past out-day, and units == 0 also drops honest
    # zero days on low-volume drugs (nudges the rate up); a censored-Poisson fit is the upgrade
    w = slice(max(t + 1 - window, 0), t + 1)
    ok = ~out[w]
    return np.where(out, (units[w] * ok).sum(0) / np.maximum(ok.sum(0), 1), units)


def forecast_consumption(units, t, horizon=14, out=None):
    """Baseline: the same method on dispensed units [T, F, D]; with an observable
    out-day mask, on adjust_consumption(units, out, t) instead (the fair baseline)."""
    return _total(units if out is None else adjust_consumption(units, out, t), t, horizon)


def bigquery_sql(table, horizon=14):
    """The cloud version of forecast_series: BigQuery AI.FORECAST with TimesFM 3.0.
    NOT executed anywhere here - no BigQuery in this prototype; the string is for the
    deployment notes. Expects a long table, one row per facility x condition x day:
    day DATE, facility_id, condition, diagnoses (target), rainfall_mm (past covariate:
    observed only), is_holiday (future covariate). Future rows carry the day and
    is_holiday with NULL diagnoses / rainfall_mm. Multiply the forecast by the CRG
    units per diagnosis downstream, as forecast_demand does."""
    return f"""SELECT *
FROM AI.FORECAST(
  TABLE `{table}`,
  model => 'TimesFM 3.0',
  timestamp_col => 'day',
  target_cols => ['diagnoses'],
  past_covariate_cols => ['rainfall_mm'],
  future_covariate_cols => ['is_holiday'],
  id_cols => ['facility_id', 'condition'],
  horizon => {int(horizon)},
  confidence_level => 0.8
)"""


def evaluate_forecast(run, horizon=14, first=45, every=14):
    """Score 14-day demand per facility x primary drug against GROUND TRUTH true_use.

    Arms: crg (diagnoses ETS x CRG), consumption (ETS on dispensed units),
    consumption_adj (ETS on adjust_consumption, the fair baseline), crg_mean and
    consumption_adj_mean (plain history means of the same inputs), rescaled (see below).
    Slices (all GROUND TRUTH, used only for scoring): after_out = origin during a
    stock-out at that facility x drug or within 30 days after it ended (consumption
    history censored); surge = true_use at the origin above 1.15x the baseline rate
    (the monsoon lift); calm = neither. crg_wins = share of cells where crg beat
    consumption_adj.
    """
    ix, P = run.ix, list(run.ix["primaries"])
    T, F = run.dx.shape[:2]
    obs = G.aggregate(ix, run.dx, run.slips, run.na)
    units, events = obs["units"], sim.stockout_events(run)
    out = (obs["na"] > 0) | (obs["sub"] > 0) | (units == 0)      # observable out-days
    # decomposition, not a baseline: consumption moved to the CRG level seen in the first
    # 30 days removes prescribing bias (definitional here), leaving censoring and noise
    level = obs["exp_units"][:30].sum(0) / np.maximum(units[:30].sum(0), 1e-9)
    truth, pred, after, surge, cover = [], {}, [], [], []
    for t in range(first, T - horizon, every):
        truth.append(run.true_use[t + 1:t + 1 + horizon].sum(0)[:, P])
        adj = adjust_consumption(units, out, t)
        for k, v in (("crg", forecast_demand(ix, run.dx, t, horizon)),
                     ("consumption", forecast_consumption(units, t, horizon)),
                     ("consumption_adj", _total(adj, t, horizon)),
                     # naive reference: the history mean of each arm's own input
                     ("crg_mean", run.dx[:t + 1].mean(0) @ ix["units"] * horizon),
                     ("consumption_adj_mean", adj[:t + 1].mean(0) * horizon)):
            pred.setdefault(k, []).append(v[:, P])
        pred.setdefault("rescaled", []).append(pred["consumption"][-1] * level[:, P])
        m = np.zeros((F, len(P)), bool)
        for e in events:
            if e["out"] <= t < e["end"] + 30:
                m[e["fac"], P.index(e["drug"])] = True
        after.append(m)
        surge.append(run.true_use[t][:, P] > 1.15 * run.rate[:, P])
        _, lo, hi = forecast_series(run.dx[:t + 1].reshape(t + 1, -1), horizon)
        fut = run.dx[t + 1:t + 1 + horizon].reshape(horizon, -1)
        cover.append(((fut >= lo) & (fut <= hi)).mean())
    y, after, surge = np.array(truth), np.array(after), np.array(surge)
    err = {k: np.abs(np.array(v) - y) for k, v in pred.items()}
    res = dict(coverage80=float(np.mean(cover)))
    for name, m in (("all", np.ones_like(after)), ("after_out", after), ("surge", surge),
                    ("calm", ~after & ~surge)):
        res[name] = dict(cells=int(m.sum()), crg_wins=float((err["crg"] < err["consumption_adj"])[m].mean()),
                         **{k: float(e[m].sum() / y[m].sum()) for k, e in err.items()})
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="5-9", help="held-out seeds, e.g. 5-9 or 0,3")
    ap.add_argument("--behaviour", default="default", choices=sorted(sim.BEHAVIOUR))
    a = ap.parse_args()
    seeds = (list(range(int(a.seeds.split("-")[0]), int(a.seeds.split("-")[1]) + 1)) if "-" in a.seeds
             else [int(s) for s in a.seeds.split(",")])
    rs = [evaluate_forecast(sim.simulate(seed=s, behaviour=a.behaviour)) for s in seeds]
    print(f"SYNTHETIC demand-forecast evaluation - seeds {a.seeds}, behaviour '{a.behaviour}'")
    print("14-day demand per facility x primary drug, origins every 14 days from day 45;")
    print("WAPE vs GROUND TRUTH true_use (lower is better), range across seeds\n")
    slices = ("all", "after_out", "surge", "calm")
    rows = (("cells/run", "cells", "{:.0f}"), ("diagnoses+CRG", "crg", "{:.3f}"),
            ("consumption", "consumption", "{:.3f}"), ("cons. adj.", "consumption_adj", "{:.3f}"),
            ("CRG closer", "crg_wins", "{:.0%}"), ("mean: CRG", "crg_mean", "{:.3f}"),
            ("mean: cons. adj.", "consumption_adj_mean", "{:.3f}"), ("rescaled*", "rescaled", "{:.3f}"))
    print(f"{'':18}" + "".join(f"{s:>12}" for s in slices))
    for label, key, fmt in rows:
        print(f"{label:18}" + "".join(f"{_range([x[s][key] for x in rs], fmt):>12}" for s in slices))
    ratio = lambda s: _range([x[s]["consumption_adj"] / x[s]["crg"] for x in rs], "{:.1f}")
    print(f"\n80% interval coverage of next-14-day daily diagnoses per facility x condition: "
          f"{_range([x['coverage80'] for x in rs], '{:.0%}')}")
    print("after_out = origin during a stock-out or <30 days after; surge = true use >1.15x baseline; calm = neither.")
    print("The top three rows share one forecaster (damped-trend ETS). cons. adj. = the WHO/MSH fix from")
    print("observable feeds: out-days (not-available record, substitute given, or nothing dispensed) are")
    print("filled with units per in-stock day over the last 56 days. It is the fair baseline.")
    print(f"cons. adj. / CRG error: all {ratio('all')}x, after_out {ratio('after_out')}x, surge {ratio('surge')}x.")
    print("CRG closer = share of cells where the CRG beat cons. adj.; cons. adj. was closer in the rest.")
    print("  After stock-outs it wins fewer cells (about half on some seeds); its WAPE lead there rests")
    print("  partly on fewer large misses, not on winning most cells.")
    print("mean: = plain history mean of the same input x horizon, a naive reference. It matches or beats")
    print("the ETS overall, in calm periods and after stock-outs; the ETS pays off only at surge origins.")
    print("The ETS grid was tuned on seeds 0-4 for the CRG arm only; the consumption arms reuse it.")
    print("* rescaled = consumption moved to the CRG level of days 0-29: a decomposition, not a baseline.")
    print("  Its gap to the CRG is censoring, rationing and lost slips - not prescribing bias.")
    print(f"Weakest spot: at surge origins the CRG error is {_range([x['surge']['crg'] / x['calm']['crg'] for x in rs], '{:.1f}')}x"
          " its calm error - neither method sees the monsoon coming; both lag it.")
    print("Not claimed: the sim's true demand IS diagnoses x CRG units, so part of the CRG's edge is")
    print("definitional (consumption also carries each PHC's prescribing bias); real CRG error is untested.")


if __name__ == "__main__":
    main()
