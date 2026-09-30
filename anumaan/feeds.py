"""A state's own exports in, the day's alarms out: the pipeline the demo runs on the
simulator, fed from CSV files instead.

    python -m anumaan.feeds export sample/          # the SYNTHETIC network in this format
    python -m anumaan.feeds run sample/ [--day 2026-07-19] [--json]

One folder, one file per feed, dates as YYYY-MM-DD. Condition and drug ids are the
grammar's (grammar/crg/compiled.json), so a state first maps its own drug codes to them.

  facilities.csv     phc, warehouse, state
  diagnoses.csv      date, phc, condition, count                 OPD register, HMIS, e-Hospital
  slips.csv          date, phc, condition, drug, days, units     one row per prescription line dispensed
  stock.csv          date, phc, drug, balance                    the stock register's closing balance
  receipts.csv       date, phc, drug, units                      issues received from the district warehouse
  not_available.csv  date, phc, condition, drug                  optional: "not available" slips
  indents.csv        date, warehouse, drug, asked, got, posted   optional: the DVDMS warehouse ledger

The care files set the window. A stock register carries its last balance forward over
days with no row. A PHC x medicine with no stock row at all is skipped and listed: a
shelf count at go-live gives it an opening balance. Without indents.csv, "where it
broke" can only say LOCAL or DEMAND-SURGE; posted is the day the receipt was entered,
blank if never, and ledger rows outside the window are ignored.
"""
import argparse
import csv
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from anumaan import crg as G, filter as FL, sim, triage

START = date(2026, 1, 1)     # calendar day 0 of the SYNTHETIC export
COLUMNS = {"facilities": ("phc", "warehouse", "state"),
           "diagnoses": ("date", "phc", "condition", "count"),
           "slips": ("date", "phc", "condition", "drug", "days", "units"),
           "stock": ("date", "phc", "drug", "balance"),
           "receipts": ("date", "phc", "drug", "units"),
           "not_available": ("date", "phc", "condition", "drug"),
           "indents": ("date", "warehouse", "drug", "asked", "got", "posted")}
OPTIONAL = ("not_available", "indents")


def _read(folder, name):
    """[(where, row)] from folder/name.csv, header checked; where is 'file line n' for errors."""
    path = Path(folder) / f"{name}.csv"
    if not path.exists():
        if name in OPTIONAL:
            return []
        raise FileNotFoundError(f"{path} is missing (see python -m anumaan.feeds --help)")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        r = csv.DictReader(fh)
        missing = set(COLUMNS[name]) - set(r.fieldnames or ())
        if missing:
            raise ValueError(f"{path.name} lacks column(s) {', '.join(sorted(missing))}")
        return [(f"{path.name} line {i}", row) for i, row in enumerate(r, 2)]


def _parse(where, fn, value, what):
    try:
        return fn(value)
    except (KeyError, ValueError):
        raise ValueError(f"{where}: bad {what} {value!r}") from None


def load(folder, crg=None):
    """CSV feeds -> (sim.Run with the ground-truth fields None, first date, PHC x drug
    pairs with no stock register row)."""
    ix = G.index(crg or G.load())
    fac_rows = _read(folder, "facilities")
    fac, wh, st = ([r[k] for _, r in fac_rows] for k in COLUMNS["facilities"])
    if len(set(fac)) < len(fac):
        raise ValueError("facilities.csv lists a PHC twice")
    fi, whs = {p: i for i, p in enumerate(fac)}, sorted(set(wh))
    wi = {w: i for i, w in enumerate(whs)}
    feeds = {n: _read(folder, n) for n in COLUMNS if n != "facilities"}
    for rows in feeds.values():
        for where, r in rows:
            r["_date"] = _parse(where, date.fromisoformat, r["date"], "date")
    care = [r["_date"] for n in ("diagnoses", "slips", "stock", "receipts") for _, r in feeds[n]]
    if not care:
        raise ValueError("no rows in diagnoses, slips, stock or receipts")
    start = min(care)
    T, F, W, C, D = (max(care) - start).days + 1, len(fac), len(whs), len(ix["conds"]), len(ix["drugs"])

    def ids(where, r, *keys):
        table = dict(phc=fi, condition=ix["ci"], drug=ix["di"], warehouse=wi)
        return [(r["_date"] - start).days] + [_parse(where, table[k].__getitem__, r[k], k) for k in keys]

    dx = np.zeros((T, F, C), int)
    for where, r in feeds["diagnoses"]:
        t, f, c = ids(where, r, "phc", "condition")
        dx[t, f, c] += _parse(where, int, r["count"], "count")
    slips = [(*ids(where, r, "phc", "condition", "drug"), _parse(where, int, r["days"], "days"),
              _parse(where, float, r["units"], "units")) for where, r in feeds["slips"]]
    na = [tuple(ids(where, r, "phc", "condition", "drug")) for where, r in feeds["not_available"]]
    book, rec = np.full((T, F, D), np.nan), np.zeros((T, F, D))
    for where, r in feeds["stock"]:
        t, f, d = ids(where, r, "phc", "drug")
        book[t, f, d] = _parse(where, float, r["balance"], "balance")
    for where, r in feeds["receipts"]:
        t, f, d = ids(where, r, "phc", "drug")
        rec[t, f, d] += _parse(where, float, r["units"], "units")
    asked, got, posted = np.zeros((T, W, D)), np.zeros((T, W, D)), np.full((T, W, D), T)
    for where, r in feeds["indents"]:
        t, w, d = ids(where, r, "warehouse", "drug")
        if 0 <= t < T:
            asked[t, w, d] += _parse(where, float, r["asked"], "asked")
            got[t, w, d] += _parse(where, float, r["got"], "got")
            if r["posted"].strip():
                posted[t, w, d] = (_parse(where, date.fromisoformat, r["posted"], "posted date") - start).days
    seen = ~np.isnan(book)
    unregistered = {(f, d) for f in range(F) for d in range(D) if not seen[:, f, d].any()}
    last = np.maximum.accumulate(np.where(seen, np.arange(T)[:, None, None], 0), axis=0)
    book = np.take_along_axis(book, np.where(np.logical_or.accumulate(seen, 0), last, seen.argmax(0)), 0)
    run = sim.Run(ix, fac, wh, st, dx, slips, na, np.nan_to_num(book), rec, asked, got, posted,
                  None, None, None, None, None)
    return run, start, unregistered


def infer(run, skip_series=()):
    """The demo's per-series pipeline (app.main.Replay.refilter) over every PHC x primary
    drug: (aggregated care record, {(f, d): (posterior, shadow cover, alarm spells)})."""
    obs = G.aggregate(run.ix, run.dx, run.slips, run.na)
    P = run.ix["primaries"]
    given = sum(obs[k][:, :, P].sum(2) for k in G.CATS)
    series = {}
    for f in range(len(run.facilities)):
        skip = FL.entry_gaps(given[:, f], obs["N"][:, f, P].sum(1))
        for d in P:
            if (f, d) in skip_series:
                continue
            cats = np.stack([obs[k][:, f, d] for k in G.CATS], 1)
            N, rec, book, exp = obs["N"][:, f, d], run.receipts[:, f, d], run.book[:, f, d], obs["exp_units"][:, f, d]
            rho, tau = FL.learn(N, cats, book, exp, rec, skip)
            post = FL.filter_series(N, cats, book, exp, rec, skip, tau=tau, rho=rho)
            cover = FL.shadow_cover(book, rec, obs["units"][:, f, d], FL.trailing_mean(rho * exp), post, skip)
            series[(f, d)] = post, cover, FL.alarms(post, cover=cover)
    return obs, series


def alarms_on(run, obs, series, t, start):
    """Alarms live on day t (an alarm counts from its second day), worst first, each with
    where it broke from the ledger up to day t."""
    live = [(f, d, s) for (f, d), (_, _, segs) in series.items() for s, e in segs if s + 1 <= t < e]
    fill = triage.fill_rate(run.wh_asked, run.wh_got, run.wh_posted)
    labels = triage.classify(live, run.wh, run.st, fill, triage.surge_lift(obs["N"], run.st), t)
    rows = []
    for (f, d, s), level in zip(live, labels):
        post, cover, _ = series[(f, d)]
        use = FL.trailing_mean(obs["exp_units"][:, f, d])[t]
        rows.append(dict(phc=run.facilities[f], drug=run.ix["drugs"][d], since=(start + timedelta(s)).isoformat(),
                         p_short=round(float(post[t, 1] + post[t, 2]), 2), shadow_days=round(float(cover[t]), 1),
                         register_days=round(float(run.book[t, f, d] / max(use, 1e-9)), 1),
                         level=level, action=triage.ACTION[level]))
    return sorted(rows, key=lambda r: (-r["p_short"], r["shadow_days"]))


def export(run, folder, start=START):
    """Write a simulated Run in the feeds format, ground truth left out."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    fac, whs, T = run.facilities, sorted(set(run.wh)), len(run.book)
    conds, drugs = run.ix["conds"], run.ix["drugs"]
    day = lambda t: (start + timedelta(int(t))).isoformat()
    tables = {
        "facilities": zip(fac, run.wh, run.st),
        "diagnoses": ((day(t), fac[f], conds[c], int(n)) for (t, f, c), n in np.ndenumerate(run.dx) if n),
        "slips": ((day(t), fac[f], conds[c], drugs[d], int(dot), float(u)) for t, f, c, d, dot, u in run.slips),
        "stock": ((day(t), fac[f], drugs[d], float(b)) for (t, f, d), b in np.ndenumerate(run.book)),
        "receipts": ((day(t), fac[f], drugs[d], float(u)) for (t, f, d), u in np.ndenumerate(run.receipts) if u),
        "not_available": ((day(t), fac[f], conds[c], drugs[d]) for t, f, c, d in run.na),
        "indents": ((day(t), whs[w], drugs[d], float(a), float(run.wh_got[t, w, d]),
                     day(run.wh_posted[t, w, d]) if run.wh_posted[t, w, d] < T else "")
                    for (t, w, d), a in np.ndenumerate(run.wh_asked) if a or run.wh_got[t, w, d]),
    }
    for name, rows in tables.items():
        with (folder / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            out = csv.writer(fh)
            out.writerow(COLUMNS[name])
            out.writerows(rows)


def main():
    ap = argparse.ArgumentParser(epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="write the SYNTHETIC network as CSV feeds")
    ex.add_argument("folder")
    ex.add_argument("--seed", type=int, default=5)
    rn = sub.add_parser("run", help="infer the shelves from a folder of CSV feeds")
    rn.add_argument("folder")
    rn.add_argument("--day", help="YYYY-MM-DD (default: the last day of the feeds)")
    rn.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.cmd == "export":
        export(sim.simulate(seed=a.seed), a.folder)
        print(f"SYNTHETIC network (seed {a.seed}) written to {a.folder}/ as CSV feeds")
        return
    run, start, unregistered = load(a.folder)
    T = len(run.book)
    t = T - 1 if a.day is None else (date.fromisoformat(a.day) - start).days
    if not 0 <= t < T:
        ap.error(f"--day must fall in {start} .. {start + timedelta(T - 1)}")
    obs, series = infer(run, unregistered)
    rows = alarms_on(run, obs, series, t, start)
    skipped = sorted(f"{run.facilities[f]} x {run.ix['drugs'][d]}" for f, d in unregistered if d in run.ix["primaries"])
    if a.json:
        print(json.dumps(dict(day=(start + timedelta(t)).isoformat(), series=len(series), skipped=skipped,
                              alarms=rows), indent=2))
        return
    print(f"{len(run.facilities)} PHCs, {len(set(run.wh))} warehouses, {len(set(run.st))} states; "
          f"{start} to {start + timedelta(T - 1)}; {len(series)} PHC x medicine series")
    if skipped:
        print(f"skipped, no stock register row (needs a go-live shelf count): {len(skipped)}: "
              + ", ".join(skipped[:5]) + (" ..." if len(skipped) > 5 else ""))
    print(f"\n{len(rows)} alarm(s) live on {start + timedelta(t)}")
    for r in rows:
        print(f"  {r['phc']:<14} {r['drug']:<22} since {r['since']}  P(short) {r['p_short']:.2f}  "
              f"shelf left {r['shadow_days']:>5.1f} d  register says {r['register_days']:>5.1f} d  "
              f"{r['level']}: {r['action']}")


if __name__ == "__main__":
    main()
