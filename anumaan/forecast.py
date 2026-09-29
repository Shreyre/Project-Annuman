"""Demand forecasting from morbidity: forecast diagnoses, convert through the CRG.

Consumption-based forecasting learns from what was dispensed, and a stock-out
censors exactly that: an empty shelf dispenses nothing, so the forecast learns
"no demand" right when demand is unmet. Diagnoses keep coming during a stock-out,
so we forecast each condition's diagnoses per facility and multiply by the CRG's
expected units per diagnosis (ix["units"]). The fair comparison is consumption with
out-days filled from the in-stock rate (adjust_consumption); against that the CRG's
lead after stock-outs is modest, not the gap plain consumption suggests.

    python -m anumaan.forecast --seeds 5-9
    python -m anumaan.forecast --bigquery anumaan-c4c.anumaan_forecast   # + TimesFM on BigQuery

Scored on SYNTHETIC data against GROUND TRUTH run.true_use. The sim defines demand
as diagnoses x CRG units, so this measures what censoring and prescribing drift
cost consumption forecasts, not whether the CRG itself is right.
"""
import argparse
import json
import time

import numpy as np

from anumaan import crg as G, sim
from anumaan.evaluate import _range

# ponytail: damped-trend exponential smoothing on a small grid, run locally. BigQuery
# AI.FORECAST (TimesFM, bigquery_sql) runs on the same series with --bigquery; on seeds 5-9 it
# trails this ETS (it forecasts the daily median, which undershoots sparse counts).
# (alpha level, beta trend, phi damping); grid and the in-sample-mean level start chosen on
# seeds 0-4, scored on the CRG arm only (the consumption arms reuse it untuned)
GRID = np.array([(a, b, p) for a in (0.01, 0.02, 0.05, 0.1, 0.2, 0.4) for b in (0.0, 0.05, 0.2) for p in (0.8, 0.95)])
Z80 = 1.2816   # two-sided 80% normal quantile
BURN = 14      # one-step errors before this are ignored when picking parameters
MIN_ERR = 7    # fewer one-step errors than this: no interval (NaN), not a fake-narrow one
TIMESFM = "TimesFM 3.0"   # Preview in BigQuery (docs of 2026-09-29); "TimesFM 2.5" is the GA default


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


def bigquery_sql(table, origins, horizon=14, model=TIMESFM):
    """The BigQuery version of forecast_series, as timesfm_forecast runs it: one univariate,
    zero-shot AI.FORECAST call over every seed x facility x condition x origin. `table` has
    one row per seed x facility x condition x day (all INT64): seed, fac, cond, day, dx
    (diagnoses). Each origin gets its own series, cut at day <= origin, and origin is an
    id column, so one call backtests every origin. Rows back: the ids, day, forecast_value
    (the median), the 80% interval and ai_forecast_status ('' when it worked).
    No covariates: the sim has none. Real rainfall (past) and holidays (future) would use
    the multivariate form - target_cols, past_covariate_cols, future_covariate_cols -
    which needs TimesFM 3.0 (Preview). Multiply by the CRG units per diagnosis downstream,
    as forecast_demand does."""
    return f"""WITH s AS (
  SELECT seed, fac, cond, origin, DATE_ADD(DATE '2000-01-01', INTERVAL day DAY) AS date, dx
  FROM `{table}`, UNNEST([{', '.join(str(int(t)) for t in origins)}]) AS origin
  WHERE day <= origin)
SELECT seed, fac, cond, origin, DATE_DIFF(DATE(forecast_timestamp), DATE '2000-01-01', DAY) AS day,
  forecast_value, prediction_interval_lower_bound, prediction_interval_upper_bound, ai_forecast_status
FROM AI.FORECAST(
  TABLE s,
  data_col => 'dx',
  timestamp_col => 'date',
  model => '{model}',
  id_cols => ['seed', 'fac', 'cond', 'origin'],
  horizon => {int(horizon)},
  confidence_level => 0.8)"""


def parse_timesfm(rows, seeds, origins, F, C, horizon=14):
    """bigquery_sql's rows (seed, fac, cond, origin, day, value, lo, hi, status) ->
    {seed: {origin: (mean, lo, hi)}}, each [horizon, F, C] daily diagnoses. Raises unless
    every series came back once, for days origin+1..origin+horizon, with no error status."""
    bad = [r[-1] for r in rows if r[-1]]
    if bad:
        raise RuntimeError(f"AI.FORECAST failed on {len(bad)} rows, e.g. {bad[0]!r}")
    a = np.array([r[:-1] for r in rows], float).reshape(-1, 8)
    s, f, c, o, day = a[:, :5].astype(int).T
    h = day - o - 1
    X = np.full((3, len(seeds), len(origins), horizon, F, C), np.nan)
    if len(a) != X[0].size or ((h < 0) | (h >= horizon)).any():
        raise RuntimeError(f"expected {X[0].size} rows for days origin+1..origin+{horizon}, got {len(a)}")
    si, oi = ({v: i for i, v in enumerate(k)} for k in (seeds, origins))
    X[:, [si[v] for v in s], [oi[v] for v in o], h, f, c] = a[:, 5:].T
    if np.isnan(X).any():
        raise RuntimeError("some series x day came back twice, others never")
    return {seed: {t: X[:, i, j] for j, t in enumerate(origins)} for i, seed in enumerate(seeds)}


def _ok(r):
    if not r.ok:            # keep BigQuery's own message: it names the failing SQL or quota
        raise RuntimeError(f"BigQuery HTTP {r.status_code}: {r.text}")
    return r.json()


def timesfm_forecast(dataset, seeds, dxs, origins, horizon=14, model=TIMESFM):
    """Run bigquery_sql for real. Loads the SYNTHETIC diagnoses (dxs: one [T, F, C] per seed)
    into `project.dataset`.dx_synthetic, runs one AI.FORECAST query and parse_timesfm's it.
    Application-default credentials, BigQuery REST. A missing dataset is created in Mumbai
    (asia-south1): TimesFM runs in every BigQuery region, and Indian facility data should
    stay in India. Returns ({seed: {origin: (mean, lo, hi)}}, the finished query job)."""
    import google.auth      # only this path needs the cloud; the app and tests never import it
    from google.auth.transport.requests import AuthorizedSession
    project, ds = dataset.split(".")
    bq = AuthorizedSession(google.auth.default(scopes=["https://www.googleapis.com/auth/bigquery"])[0])
    api = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}"

    def done(job):
        ref = job["jobReference"]
        while job["status"]["state"] != "DONE":
            time.sleep(2)
            job = _ok(bq.get(f"{api}/jobs/{ref['jobId']}", params={"location": ref["location"]}))
        if "errorResult" in job["status"]:
            raise RuntimeError(f"BigQuery job failed: {job['status']}")
        return job

    r = bq.post(f"{api}/datasets", json={"datasetReference": {"projectId": project, "datasetId": ds},
                                         "location": "asia-south1"})
    if r.status_code != 409:                                # 409: the dataset exists
        _ok(r)
    load = dict(destinationTable=dict(projectId=project, datasetId=ds, tableId="dx_synthetic"),
                destinationTableProperties=dict(description="SYNTHETIC daily diagnoses from anumaan.sim"),
                sourceFormat="CSV", writeDisposition="WRITE_TRUNCATE",
                schema=dict(fields=[dict(name=n, type="INT64") for n in ("seed", "fac", "cond", "day", "dx")]))
    csv = "\n".join(f"{s},{f},{c},{t},{v}" for s, dx in zip(seeds, dxs) for (t, f, c), v in np.ndenumerate(dx))
    body = (f"--anumaan\r\nContent-Type: application/json\r\n\r\n{json.dumps(dict(configuration=dict(load=load)))}"
            f"\r\n--anumaan\r\nContent-Type: text/csv\r\n\r\n{csv}\r\n--anumaan--\r\n")
    done(_ok(bq.post(f"https://bigquery.googleapis.com/upload/bigquery/v2/projects/{project}/jobs",
                     params={"uploadType": "multipart"}, data=body.encode(),
                     headers={"Content-Type": "multipart/related; boundary=anumaan"})))
    sql = bigquery_sql(f"{project}.{ds}.dx_synthetic", origins, horizon, model)
    job = done(_ok(bq.post(f"{api}/jobs", json=dict(configuration=dict(query=dict(query=sql, useLegacySql=False))))))
    ref, rows, page = job["jobReference"], [], {}
    while True:                                             # ~200k rows: a few 10 MB pages
        r = _ok(bq.get(f"{api}/queries/{ref['jobId']}", params={"location": ref["location"], **page}))
        rows += [[v["v"] for v in row["f"]] for row in r.get("rows", [])]
        if "pageToken" not in r:
            break
        page = {"pageToken": r["pageToken"]}
    return parse_timesfm(rows, seeds, origins, *dxs[0].shape[1:], horizon), job


def evaluate_forecast(run, horizon=14, first=45, every=14, timesfm=None):
    """Score 14-day demand per facility x primary drug against GROUND TRUTH true_use.

    Arms: crg (diagnoses ETS x CRG), consumption (ETS on dispensed units),
    consumption_adj (ETS on adjust_consumption, the fair baseline), crg_mean and
    consumption_adj_mean (plain history means of the same inputs), rescaled (see below);
    with timesfm ({origin: (mean, lo, hi)} from timesfm_forecast) also timesfm: TimesFM's
    daily diagnoses x the same CRG units, and its interval coverage (coverage80_timesfm).
    Slices (all GROUND TRUTH, used only for scoring): after_out = origin during a
    stock-out at that facility x drug or within 30 days after it ended (consumption
    history censored); surge = true_use at the origin above 1.15x the baseline rate
    (the monsoon lift); calm = neither. crg_wins = share of cells where crg beat
    consumption_adj. bias = each arm's forecast total / the true total, all cells.
    """
    ix, P = run.ix, list(run.ix["primaries"])
    T, F = run.dx.shape[:2]
    obs = G.aggregate(ix, run.dx, run.slips, run.na)
    units, events = obs["units"], sim.stockout_events(run)
    out = (obs["na"] > 0) | (obs["sub"] > 0) | (units == 0)      # observable out-days
    # decomposition, not a baseline: consumption moved to the CRG level seen in the first
    # 30 days removes prescribing bias (definitional here), leaving censoring and noise
    level = obs["exp_units"][:30].sum(0) / np.maximum(units[:30].sum(0), 1e-9)
    truth, pred, after, surge, cover = [], {}, [], [], {}
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
        cover.setdefault("coverage80", []).append(((fut >= lo) & (fut <= hi)).mean())
        if timesfm is not None:          # same series, same CRG step; clipped at 0 like the ETS
            mean, lo, hi = timesfm[t]
            pred.setdefault("timesfm", []).append((np.maximum(mean, 0).sum(0) @ ix["units"])[:, P])
            cover.setdefault("coverage80_timesfm", []).append(
                ((fut >= lo.reshape(fut.shape)) & (fut <= hi.reshape(fut.shape))).mean())
    y, after, surge = np.array(truth), np.array(after), np.array(surge)
    err = {k: np.abs(np.array(v) - y) for k, v in pred.items()}
    res = {k: float(np.mean(v)) for k, v in cover.items()}
    res["bias"] = {k: float(np.sum(v) / y.sum()) for k, v in pred.items()}
    for name, m in (("all", np.ones_like(after)), ("after_out", after), ("surge", surge),
                    ("calm", ~after & ~surge)):
        res[name] = dict(cells=int(m.sum()), crg_wins=float((err["crg"] < err["consumption_adj"])[m].mean()),
                         **{k: float(e[m].sum() / y[m].sum()) for k, e in err.items()})
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="5-9", help="held-out seeds, e.g. 5-9 or 0,3")
    ap.add_argument("--behaviour", default="default", choices=sorted(sim.BEHAVIOUR))
    ap.add_argument("--bigquery", metavar="PROJECT.DATASET",
                    help="also score TimesFM: load the diagnoses there, run one AI.FORECAST query (ADC)")
    ap.add_argument("--model", default=TIMESFM, help="AI.FORECAST model for --bigquery, e.g. 'TimesFM 2.5'")
    a = ap.parse_args()
    seeds = (list(range(int(a.seeds.split("-")[0]), int(a.seeds.split("-")[1]) + 1)) if "-" in a.seeds
             else [int(s) for s in a.seeds.split(",")])
    runs = [sim.simulate(seed=s, behaviour=a.behaviour) for s in seeds]
    tfm, job = {}, None
    if a.bigquery:      # evaluate_forecast's origins: every 14 days from day 45, 14-day horizon
        tfm, job = timesfm_forecast(a.bigquery, seeds, [r.dx for r in runs],
                                    range(45, len(runs[0].dx) - 14, 14), model=a.model)
    rs = [evaluate_forecast(r, timesfm=tfm.get(s)) for s, r in zip(seeds, runs)]
    print(f"SYNTHETIC demand-forecast evaluation - seeds {a.seeds}, behaviour '{a.behaviour}'")
    print("14-day demand per facility x primary drug, origins every 14 days from day 45;")
    print("WAPE vs GROUND TRUTH true_use (lower is better), range across seeds\n")
    slices = ("all", "after_out", "surge", "calm")
    rows = (("cells/run", "cells", "{:.0f}"), ("diagnoses+CRG", "crg", "{:.3f}"),
            ("consumption", "consumption", "{:.3f}"), ("cons. adj.", "consumption_adj", "{:.3f}"),
            ("CRG closer", "crg_wins", "{:.0%}"), ("mean: CRG", "crg_mean", "{:.3f}"),
            ("mean: cons. adj.", "consumption_adj_mean", "{:.3f}"), ("rescaled*", "rescaled", "{:.3f}"))
    rows += (("TimesFM+CRG**", "timesfm", "{:.3f}"),) if a.bigquery else ()
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
    if a.bigquery:
        st, q = job["statistics"], job["statistics"]["query"]
        share = lambda k: _range([x["bias"][k] for x in rs], "{:.0%}")
        print(f"** TimesFM+CRG = the same daily diagnoses forecast by BigQuery AI.FORECAST ('{a.model}', zero-shot,")
        print("   univariate, no covariates), times the same CRG units. AI.FORECAST returns the daily median, and")
        print(f"   low counts are right-skewed, so the median sits under the mean: its 14-day totals come to {share('timesfm')}")
        print(f"   of true use (ETS {share('crg')}, mean {share('crg_mean')}). 80% interval coverage: "
              f"{_range([x['coverage80_timesfm'] for x in rs], '{:.0%}')} (ETS {_range([x['coverage80'] for x in rs], '{:.0%}')}).")
        print(f"   One query, {sum(len(v) for v in tfm.values()) * runs[0].dx[0].size} series, {job['jobReference']['location']}: "
              f"{int(q['totalBytesProcessed']) / 1e6:.1f} MB processed, {int(q['totalBytesBilled']) / 1e6:.1f} MB billed, "
              f"{(int(st['endTime']) - int(st['startTime'])) / 1000:.0f} s.")


if __name__ == "__main__":
    main()
