"""A state's own exports in, the day's alarms out: the pipeline the demo runs on the
simulator, fed from CSV files or from a live stream instead.

    python -m anumaan.feeds export sample/          # the SYNTHETIC network in this format
    python -m anumaan.feeds run sample/ [--day 2026-07-19] [--json]
    python -m anumaan.feeds publish sample/ --topic projects/P/topics/T [--day 2026-07-19]

One folder, one file per feed, dates as YYYY-MM-DD. Condition and drug ids are the
grammar's (grammar/crg/compiled.json), so a state first maps its own drug codes to them.

  facilities.csv     phc, warehouse, state
  diagnoses.csv      date, phc, condition, count                 OPD register, HMIS, e-Hospital
  slips.csv          date, phc, condition, drug, days, units     one row per prescription line dispensed
  stock.csv          date, phc, drug, balance                    the stock register's closing balance
  receipts.csv       date, phc, drug, units                      issues received from the district warehouse
  not_available.csv  date, phc, condition, drug                  optional: "not available" slips
  indents.csv        date, warehouse, drug, asked, got, posted   optional: the DVDMS warehouse ledger

Beds and staff, optional, all four together:

  posts.csv          phc, beds, MO, SN, PH, LT                   beds, and posts filled per cadre
  admissions.csv     date, phc, stay, kind                       the admission-discharge feed (e-Hospital ADT)
  discharges.csv     date, phc, stay, early                      early: 1 if sent home before the usual stay
  attendance.csv     date, phc, cadre, marked, patients, acts    AEBAS mark; that cadre's acts in the care record

The care files set the window. A stock register carries its last balance forward over
days with no row. A PHC x medicine with no stock row at all is skipped and listed: a
shelf count at go-live gives it an opening balance. Without indents.csv, "where it
broke" can only say LOCAL or DEMAND-SURGE; posted is the day the receipt was entered,
blank if never, and ledger rows outside the window are ignored.

A live feed carries the same rows as messages (stream): one per PHC per day and one per
warehouse whose ledger changed. absorb writes a message into the same arrays, so the
app's filter runs on them as they arrive; publish sends them to a Pub/Sub topic.
"""
import argparse
import base64
import csv
import json
from datetime import date, timedelta
from functools import cache
from pathlib import Path

import numpy as np

from anumaan import care as CARE, crg as G, filter as FL, sim, triage

START = date(2026, 1, 1)     # calendar day 0 of the SYNTHETIC export
COLUMNS = {"facilities": ("phc", "warehouse", "state"),
           "diagnoses": ("date", "phc", "condition", "count"),
           "slips": ("date", "phc", "condition", "drug", "days", "units"),
           "stock": ("date", "phc", "drug", "balance"),
           "receipts": ("date", "phc", "drug", "units"),
           "not_available": ("date", "phc", "condition", "drug"),
           "indents": ("date", "warehouse", "drug", "asked", "got", "posted"),
           "posts": ("phc", "beds", *CARE.CADRES),
           "admissions": ("date", "phc", "stay", "kind"),
           "discharges": ("date", "phc", "stay", "early"),
           "attendance": ("date", "phc", "cadre", "marked", "patients", "acts")}
CARE_FEEDS = ("posts", "admissions", "discharges", "attendance")
OPTIONAL = ("not_available", "indents", *CARE_FEEDS)


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
    except (KeyError, ValueError, TypeError):
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
    feeds = {n: _read(folder, n) for n in COLUMNS if n != "facilities" and n not in CARE_FEEDS}
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


def load_care(folder, run, start):
    """The bed and staff feeds for run's PHCs and window -> care.Care with the ground-truth
    fields None, or None when the folder has no posts.csv. Rows outside the window are ignored."""
    rows = {n: _read(folder, n) for n in CARE_FEEDS}
    if not rows["posts"]:
        return None
    T, F, K = len(run.book), len(run.facilities), len(CARE.CADRES)
    fi, ki = {p: i for i, p in enumerate(run.facilities)}, {k: i for i, k in enumerate(CARE.CADRES)}
    num = lambda where, r, key: _parse(where, int, r[key], key)
    cap, in_pos = np.zeros(F, int), np.zeros((F, K), int)
    for where, r in rows["posts"]:
        f = _parse(where, fi.__getitem__, r["phc"], "phc")
        cap[f], in_pos[f] = num(where, r, "beds"), [num(where, r, k) for k in CARE.CADRES]

    def at(where, r):
        t = (_parse(where, date.fromisoformat, r["date"], "date") - start).days
        return t, _parse(where, fi.__getitem__, r["phc"], "phc")

    admits = [(t, f, r["stay"], r["kind"]) for where, r in rows["admissions"] for t, f in [at(where, r)] if 0 <= t < T]
    outs = [(t, f, r["stay"], bool(num(where, r, "early")))
            for where, r in rows["discharges"] for t, f in [at(where, r)] if 0 <= t < T]
    marked, exposure, acts = np.zeros((T, F, K), bool), np.zeros((T, F, K), int), np.zeros((T, F, K), int)
    for where, r in rows["attendance"]:
        (t, f), k = at(where, r), _parse(where, ki.__getitem__, r["cadre"], "cadre")
        if 0 <= t < T:
            marked[t, f, k], exposure[t, f, k], acts[t, f, k] = (num(where, r, c) for c in ("marked", "patients", "acts"))
    return CARE.Care(list(run.facilities), cap, admits, outs, in_pos, marked, exposure, acts, None, None)


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


def care_on(care, t):
    """Beds and staff on day t: PHCs whose count says every bed is taken, and the attendance
    marks the care record does not back, as (phc, cadre)."""
    full = [care.facilities[f] for f in np.flatnonzero(CARE.beds(care)["pressure"][t])]
    verify = [(care.facilities[f], CARE.CADRES[k]) for f, k in np.argwhere(CARE.staff(care)["verify"][t])]
    return full, verify


def export(run, folder, start=START, care=None):
    """Write a simulated Run (and its bed and staff feeds) in the feeds format, ground truth left out."""
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
    if care:
        tables.update({
            "posts": ((fac[f], int(care.capacity[f]), *map(int, care.in_position[f])) for f in range(len(fac))),
            "admissions": ((day(t), fac[f], s, kind) for t, f, s, kind in care.admits),
            "discharges": ((day(t), fac[f], s, int(early)) for t, f, s, early in care.discharges),
            "attendance": ((day(t), fac[f], CARE.CADRES[k], int(m), int(care.exposure[t, f, k]), int(care.acts[t, f, k]))
                           for (t, f, k), m in np.ndenumerate(care.marked))})
    for name, rows in tables.items():
        with (folder / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            out = csv.writer(fh)
            out.writerow(COLUMNS[name])
            out.writerows(rows)


# ---------- the same rows as a live feed ----------

def stream(run, care=None, start=START):
    """The feeds as messages. Returns day(t) -> [message]: one per PHC (that day's diagnoses,
    slips, not-available slips, register balances and receipts, and with `care` its admissions,
    discharges and attendance) and one per warehouse whose ledger changed that day (indents
    placed, and receipts posted that day against earlier indents)."""
    fac, whs, conds, drugs, T = run.facilities, sorted(set(run.wh)), run.ix["conds"], run.ix["drugs"], len(run.book)
    iso = lambda t: (start + timedelta(int(t))).isoformat()
    slips, na, admits, outs, posted = ({} for _ in range(5))
    for t, f, c, d, dot, u in run.slips:
        slips.setdefault((t, f), []).append([conds[c], drugs[d], int(dot), float(u)])
    for t, f, c, d in run.na:
        na.setdefault((t, f), []).append([conds[c], drugs[d]])
    for t, f, s, kind in (care.admits if care else ()):
        admits.setdefault((t, f), []).append([s, kind])
    for t, f, s, early in (care.discharges if care else ()):
        outs.setdefault((t, f), []).append([s, bool(early)])
    for t0, w, d in np.argwhere((run.wh_posted < T) & ((run.wh_asked != 0) | (run.wh_got != 0))):
        posted.setdefault((int(run.wh_posted[t0, w, d]), int(w)), []).append([iso(t0), drugs[d], float(run.wh_got[t0, w, d])])

    def day(t):
        out = []
        for f, phc in enumerate(fac):
            m = dict(kind="phc", date=iso(t), phc=phc,
                     diagnoses={conds[c]: int(n) for c, n in enumerate(run.dx[t, f]) if n},
                     slips=slips.get((t, f), []), not_available=na.get((t, f), []),
                     stock={drugs[d]: float(b) for d, b in enumerate(run.book[t, f])},
                     receipts={drugs[d]: float(u) for d, u in enumerate(run.receipts[t, f]) if u})
            if care:
                m.update(admissions=admits.get((t, f), []), discharges=outs.get((t, f), []),
                         attendance={k: [bool(care.marked[t, f, j]), int(care.exposure[t, f, j]), int(care.acts[t, f, j])]
                                     for j, k in enumerate(CARE.CADRES)})
            out.append(m)
        for w, wh in enumerate(whs):
            asked = [[drugs[d], float(a)] for d, a in enumerate(run.wh_asked[t, w]) if a]
            if asked or (t, w) in posted:
                out.append(dict(kind="warehouse", date=iso(t), warehouse=wh, indents=asked, posted=posted.get((t, w), [])))
        return out
    return day


def absorb(m, run, obs, care=None, start=START, skip=None):
    """Write one stream message into run, the aggregated care record obs (crg.aggregate's
    output) and care's bed and staff feeds. Everything is checked before anything is written,
    and nothing is if skip(kind, day, index) says so (a report the caller already has).
    Returns (kind, day, PHC or warehouse index); ValueError on anything it cannot place."""
    ix, T, where = run.ix, len(run.book), "message"
    try:
        day = lambda v: (_parse(where, date.fromisoformat, v, "date") - start).days
        idx = lambda names, v, what: _parse(where, list(names).index, v, what)
        drug = lambda v: _parse(where, ix["di"].__getitem__, v, "drug")
        cond = lambda v: _parse(where, ix["ci"].__getitem__, v, "condition")
        t = day(m.get("date"))
        if not 0 <= t < T:
            raise ValueError(f"{where}: date {m.get('date')!r} is outside the window")
        if m.get("kind") == "warehouse":
            w = idx(sorted(set(run.wh)), m.get("warehouse"), "warehouse")
            asked = [(drug(d), float(a)) for d, a in m.get("indents", ())]
            posted = [(day(d0), drug(d), float(g)) for d0, d, g in m.get("posted", ())]
            if not all(0 <= t0 <= t for t0, _, _ in posted):
                raise ValueError(f"{where}: a receipt is posted against an indent outside the window")
            for d, a in asked:
                run.wh_asked[t, w, d] = a
            for t0, d, g in posted:
                run.wh_got[t0, w, d], run.wh_posted[t0, w, d] = g, t
            return "warehouse", t, w
        if m.get("kind") != "phc":
            raise ValueError(f"{where}: bad kind {m.get('kind')!r}")
        f = idx(run.facilities, m.get("phc"), "phc")
        dx = np.zeros(len(ix["conds"]), int)
        for c, n in m.get("diagnoses", {}).items():
            dx[cond(c)] = int(n)
        slips = [(0, 0, cond(c), drug(d), int(dot), float(u)) for c, d, dot, u in m.get("slips", ())]
        na = [(0, 0, cond(c), drug(d)) for c, d in m.get("not_available", ())]
        stock = [(drug(d), float(b)) for d, b in m.get("stock", {}).items()]
        rec = [(drug(d), float(u)) for d, u in m.get("receipts", {}).items()]
        if care is not None and "attendance" in m:
            att = [(idx(CARE.CADRES, k, "cadre"), bool(v[0]), int(v[1]), int(v[2])) for k, v in m["attendance"].items()]
            admits = [(t, f, s, str(kind)) for s, kind in m.get("admissions", ())]
            outs = [(t, f, s, bool(early)) for s, early in m.get("discharges", ())]
    except (TypeError, ValueError, AttributeError) as e:     # a malformed payload is the sender's error, not ours
        raise ValueError(str(e)) from None
    if skip and skip("phc", t, f):
        return "phc", t, f
    run.dx[t, f] = dx
    for k, v in G.aggregate(ix, dx[None, None], slips, na).items():
        obs[k][t, f] = v[0, 0]
    run.slips.extend((t, f, c, d, dot, u) for _, _, c, d, dot, u in slips)      # raw rows stay in the state
    run.na.extend((t, f, c, d) for _, _, c, d in na)
    for d, b in stock:
        run.book[t, f, d] = b
    for d, u in rec:
        run.receipts[t, f, d] = u
    if care is not None and "attendance" in m:
        for k, marked, patients, acts in att:
            care.marked[t, f, k], care.exposure[t, f, k], care.acts[t, f, k] = marked, patients, acts
        care.admits.extend(admits)
        care.discharges.extend(outs)
    return "phc", t, f


@cache
def _session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    return AuthorizedSession(google.auth.default(scopes=["https://www.googleapis.com/auth/pubsub"])[0])


def publish(topic, messages):
    """Send messages to a Pub/Sub topic ("projects/P/topics/T") over REST, 500 a call. Needs
    application-default credentials that may publish to it. Returns how many were sent."""
    for k in range(0, len(messages), 500):
        body = {"messages": [{"data": base64.b64encode(json.dumps(m).encode()).decode()} for m in messages[k:k + 500]]}
        _session().post(f"https://pubsub.googleapis.com/v1/{topic}:publish", json=body, timeout=30).raise_for_status()
    return len(messages)


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
    pb = sub.add_parser("publish", help="send one day of a folder's feeds to a Pub/Sub topic, a message per PHC")
    pb.add_argument("folder")
    pb.add_argument("--topic", required=True, help="projects/PROJECT/topics/TOPIC")
    pb.add_argument("--day", help="YYYY-MM-DD (default: the last day of the feeds)")
    a = ap.parse_args()
    if a.cmd == "export":
        run = sim.simulate(seed=a.seed)
        export(run, a.folder, care=CARE.simulate(run, a.seed))
        print(f"SYNTHETIC network (seed {a.seed}) written to {a.folder}/ as CSV feeds")
        return
    run, start, unregistered = load(a.folder)
    care = load_care(a.folder, run, start)
    T = len(run.book)
    t = T - 1 if a.day is None else (date.fromisoformat(a.day) - start).days
    if not 0 <= t < T:
        ap.error(f"--day must fall in {start} .. {start + timedelta(T - 1)}")
    if a.cmd == "publish":
        n = publish(a.topic, stream(run, care, start)(t))
        print(f"{n} messages for {start + timedelta(t)} sent to {a.topic}")
        return
    obs, series = infer(run, unregistered)
    rows = alarms_on(run, obs, series, t, start)
    full, verify = care_on(care, t) if care else ([], [])
    skipped = sorted(f"{run.facilities[f]} x {run.ix['drugs'][d]}" for f, d in unregistered if d in run.ix["primaries"])
    if a.json:
        print(json.dumps(dict(day=(start + timedelta(t)).isoformat(), series=len(series), skipped=skipped, alarms=rows,
                              beds_full=full, attendance_to_verify=[dict(phc=p, cadre=k) for p, k in verify]), indent=2))
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
    if care:
        print(f"\nbeds: every bed taken at {len(full)} PHC(s)" + (": " + ", ".join(full) if full else ""))
        print(f"attendance to verify (marked present, no work on a busy day): {len(verify)}"
              + (": " + ", ".join(f"{p} {k}" for p, k in verify) if verify else ""))


if __name__ == "__main__":
    main()
