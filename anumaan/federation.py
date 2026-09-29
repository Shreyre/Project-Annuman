"""Federation: one project per state; only aggregates cross the state line.

Mirrors the cloud design. Each state runs its own GCP project, and its raw
diagnoses, dispensing slips and warehouse ledger never leave it. The state decodes
them against the CRG and runs the filter locally. It exports counts and nothing
else: per warehouse (district) x medicine, how many PHCs look OK / SCARCE / OUT, how
many are in alarm, how many began a demand-led one recently (triage's surge test),
on which days alarms began, and whether the state has stopped filling that
warehouse's indents (starved, triage's ledger test); plus a summary of what its model
learned (median and IQR of the prescribing rate rho and register trust tau per
medicine). A group of fewer than k PHCs is suppressed, like the aggregation threshold
of a BigQuery data clean room; anumaan/cleanroom.py builds the same rows on BigQuery.
The national project reads only these exports: a cross-state shortage view, and shared
priors that it hands back to the states.

A national failure starves warehouses in every state within a week or two, while
their buffers still hide it from the PHCs; no single state can tell it from its own
procurement trouble. Shared priors let a state that joined a few days ago skip the
30-day warm-up for rho and tau.

    python -m anumaan.federation --seeds 5-9 [--behaviour alt]

Everything measured here is on SYNTHETIC data (anumaan/sim.py).
"""
import argparse
import json
import re

import numpy as np

from anumaan import crg as G, filter as FL, sim, triage
from anumaan.evaluate import _range, _score

K = 5                      # minimum PHCs behind any exported number
WINDOW, MIN_HOT = 21, 3    # surge-hot warehouse: 3+ of its PHCs began a demand-led alarm within 3 weeks
TOP = {"state", "day", "k", "rows", "models", "suppressed"}
COUNTS = ("OK", "SCARCE", "OUT", "alarm", "surge")
ROW = {"warehouse", "drug", "n", "onsets", "starved", *COUNTS}
MODEL = {"drug", "n", "rho", "rho_iqr", "tau", "tau_iqr"}
FAC_ID = re.compile(r"-P\d+")


class StateNode:
    """One state's project. Holds only its own PHCs' raw events and its own warehouses'
    ledger; export() is all that leaves.

    warmup: days of history rho and tau are learned from; priors: {drug: {rho, tau}}
    from National.priors(), used instead of learning (cold start).
    """

    def __init__(self, run, state_id, k=K, warmup=30, priors=None):
        self.state, self.k = state_id, k
        self.fac = [f for f, s in enumerate(run.st) if s == state_id]   # global indexes; never exported
        local = {f: i for i, f in enumerate(self.fac)}
        self.wh = [run.wh[f] for f in self.fac]
        self.drugs, self.P = run.ix["drugs"], run.ix["primaries"]
        # raw events, this state's PHCs and warehouses only, re-indexed 0..n-1
        self.dx = run.dx[:, self.fac]
        self.slips = [(t, local[f], c, d, dot, u) for t, f, c, d, dot, u in run.slips if f in local]
        self.na = [(t, local[f], c, d) for t, f, c, d in run.na if f in local]
        book, receipts = run.book[:, self.fac], run.receipts[:, self.fac]
        mine = [w in self.wh for w in sorted(set(run.wh))]
        self.whs = sorted(set(self.wh))
        self.fill = triage.fill_rate(run.wh_asked[:, mine], run.wh_got[:, mine], run.wh_posted[:, mine])

        obs = G.aggregate(run.ix, self.dx, self.slips, self.na)
        given = sum(obs[c][:, :, self.P].sum(2) for c in G.CATS)
        expected = obs["N"][:, :, self.P].sum(2)
        lift = triage.surge_lift(obs["N"], [state_id] * len(self.fac))
        T = len(book)
        self.post, self.cover, self.segs, self.surge, self.rho, self.tau = {}, {}, {}, {}, {}, {}
        for i in range(len(self.fac)):
            skip = FL.entry_gaps(given[:, i], expected[:, i])
            for d in self.P:
                cats = np.stack([obs[c][:, i, d] for c in G.CATS], 1)
                series = (obs["N"][:, i, d], cats, book[:, i, d], obs["exp_units"][:, i, d], receipts[:, i, d])
                prior = (priors or {}).get(self.drugs[d])      # none if every state's summary was suppressed
                rho, tau = (prior["rho"], prior["tau"]) if prior else FL.learn(*series, skip, warmup)
                self.rho[(i, d)], self.tau[(i, d)] = rho, tau
                self.post[(i, d)] = p = FL.filter_series(*series, skip, tau=tau, rho=rho)
                self.cover[(i, d)] = c = FL.shadow_cover(book[:, i, d], receipts[:, i, d], obs["units"][:, i, d],
                                                         FL.trailing_mean(rho * obs["exp_units"][:, i, d]), p, skip)
                self.segs[(i, d)] = FL.alarms(p, cover=c)
                # onsets triage would call DEMAND-SURGE if supply held (the state's demand is up)
                self.surge[(i, d)] = {s for s, _ in self.segs[(i, d)] if lift[min(s + 1, T - 1), i, d] >= triage.SURGE}

    def export(self, t):
        """Aggregates as of day t. Alarms count once confirmed (day 2), as in the demo.
        ponytail: rho/tau come from the first `warmup` days, so an export before then
        summarises days after t; gate the models on t >= warmup if that ever matters."""
        rows, models, suppressed = [], [], 0
        starved = triage.starved(self.fill, t - triage.LOOK, t)
        for k, w in enumerate(self.whs):
            members = [i for i, x in enumerate(self.wh) if x == w]
            for d in self.P:
                if len(members) < self.k:
                    suppressed += 1
                    continue
                segs = [[(s, e) for s, e in self.segs[(i, d)] if s + 1 <= t] for i in members]
                reg = [FL.REGIMES[int(self.post[(i, d)][t].argmax())] for i in members]
                rows.append(dict(warehouse=w, drug=self.drugs[d], n=len(members),
                                 **{r: reg.count(r) for r in FL.REGIMES},
                                 alarm=sum(any(t < e for _, e in sg) for sg in segs),
                                 # PHCs that began a demand-led alarm in the window
                                 surge=sum(any(s >= t - WINDOW and s in self.surge[(i, d)] for s, _ in sg)
                                           for i, sg in zip(members, segs)),
                                 starved=int(starved[k, d]),
                                 onsets=sorted(int(s) for sg in segs for s, _ in sg)))
        n = len(self.fac)
        for d in self.P:
            if n < self.k:
                suppressed += 1
                continue
            q = {p: np.percentile([getattr(self, p)[(i, d)] for i in range(n)], [25, 50, 75]).round(3)
                 for p in ("rho", "tau")}
            models.append(dict(drug=self.drugs[d], n=n, **{p: float(v[1]) for p, v in q.items()},
                               **{f"{p}_iqr": [float(v[0]), float(v[2])] for p, v in q.items()}))
        return dict(state=self.state, day=int(t), k=self.k, rows=rows, models=models, suppressed=suppressed)


def assert_no_raw(export, k=K):
    """The clean room's gate: fixed schema, typed and bounded values, no facility ids, no
    group under k. k is the gate's own threshold, not the exporter's.
    ponytail: it caps what an export can carry but cannot tell a day number from any other
    small int; a real clean room computes the aggregates itself in SQL, as anumaan/cleanroom.py does."""
    def need(ok, why):
        if not ok:          # not a bare assert: the gate must survive python -O
            raise AssertionError(why)
    count = lambda x, hi=float("inf"): type(x) is int and 0 <= x <= hi       # bools are not counts
    real = lambda x: type(x) is float
    name = lambda x: type(x) is str and len(x) <= 40
    need(type(export) is dict and set(export) == TOP, f"fields not in the schema: {sorted(set(export) ^ TOP)}")
    state, day = export["state"], export["day"]
    need(type(state) is str and re.fullmatch(r"S\d+", state) and count(day) and count(export["k"])
         and count(export["suppressed"]) and type(export["rows"]) is list and type(export["models"]) is list,
         "top-level values off-schema")
    for r in export["rows"]:
        need(type(r) is dict and set(r) == ROW, f"row fields not in the schema: {sorted(set(r) ^ ROW)}")
        n = r["n"]
        need(count(n) and n >= k, f"group size n={n!r} is not a count of at least k={k} PHCs")
        need(type(r["warehouse"]) is str and re.fullmatch(rf"{state}-W\d+", r["warehouse"]) and name(r["drug"]),
             "row ids off-schema")
        need(all(count(r[c], n) for c in COUNTS) and r["OK"] + r["SCARCE"] + r["OUT"] == n
             and count(r["starved"], 1), "row counts off-schema")
        on = r["onsets"]    # alarms last 2+ days with a gap between: at most one onset per PHC per 3 days
        need(type(on) is list and all(count(s, day) for s in on) and len(on) <= n * (day // 3 + 1),
             "onsets must be day numbers up to the export day")
    for m in export["models"]:
        need(type(m) is dict and set(m) == MODEL and count(m["n"]) and m["n"] >= k, "model summary off-schema or under k")
        need(name(m["drug"]) and real(m["rho"]) and real(m["tau"])
             and all(type(m[q]) is list and len(m[q]) == 2 and all(map(real, m[q])) for q in ("rho_iqr", "tau_iqr")),
             "model values off-schema")
    need(not FAC_ID.search(json.dumps(export)), "facility id in export")


class National:
    """The national project: sees state exports only, and only through the gate."""

    def __init__(self, k=K):
        self.k, self.exports = k, {}

    def ingest(self, export):
        assert_no_raw(export, self.k)
        self.exports[export["state"]] = export

    def view(self):
        """Per medicine, per state: starved warehouses (the state stopped filling their
        indents). 2+ starved in a state is a state shortage; 2+ such states is a national
        shortage (triage's rule, on aggregates alone). Demand-led alarms spread the same way
        (MIN_HOT+ PHCs per warehouse) are a national surge: the fix is bigger indents, not
        escalation to MoHFW."""
        spread = {}
        for s, ex in self.exports.items():
            for r in ex["rows"]:
                for key, hot in (("starved", r["starved"]), ("surge", r["surge"] >= MIN_HOT)):
                    h = spread.setdefault((r["drug"], key), {})
                    h[s] = h.get(s, 0) + hot
        wide = lambda h: sum(n >= 2 for n in h.values()) >= 2
        return {d: dict(starved=h, short_states=sorted(s for s, n in h.items() if n >= 2), national=wide(h),
                        surge=wide(spread[(d, "surge")])) for (d, key), h in spread.items() if key == "starved"}

    def priors(self):
        """What states receive back: per medicine, the median of the states' median rho and tau.
        ponytail: ignores state size and spread; weight by n / shrink hierarchically when
        states differ a lot."""
        by = {}
        for ex in self.exports.values():
            for m in ex["models"]:
                by.setdefault(m["drug"], []).append(m)
        return {d: {p: float(np.median([m[p] for m in ms])) for p in ("rho", "tau")} for d, ms in by.items()}


def cold_start(run, cold, ks=(3, 7), k=K):
    """A state that joined with only `days` of history, scored on stock-outs from then on:
    local  - rho and tau learned from those days (filter_series warm-up = days)
    priors - the national priors, built from the OTHER states' exports only
    warm30 - reference: a 30-day warm-up, which a new state does not have yet.
    Returns {days: {variant: evaluate._score dict}}."""
    T = run.book.shape[0]
    nat = National(k)
    for s in sorted(set(run.st) - {cold}):
        nat.ingest(StateNode(run, s, k).export(T - 1))
    ref, pri = StateNode(run, cold, k), nat.priors()

    # evaluation only below: GROUND TRUTH
    mine = set(ref.fac)
    all_events = sim.stockout_events(run)
    scarce = {(f, d): run.true_stock[:, f, d] < max(7, run.ration_cover[f]) * run.true_use[:, f, d]
              for f in mine for d in ref.P}
    out = {}
    for days in ks:
        events = [e for e in all_events if e["fac"] in mine and e["out"] >= days]
        # a medicine whose prior was suppressed falls back to what the cold state can learn itself
        nodes = dict(local=StateNode(run, cold, k, warmup=days), priors=StateNode(run, cold, k, days, pri), warm30=ref)
        out[days] = {}
        for name, node in nodes.items():   # live from day `days`: alarms from then on
            amap = {(node.fac[i], d): [(s + days, e + days) for s, e in FL.alarms(p[days:], cover=node.cover[(i, d)][days:])]
                    for (i, d), p in node.post.items()}
            out[days][name] = dict(_score(amap, events, scarce, T - days), events=len(events))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="5-9", help="held-out seeds, e.g. 5-9 or 0,3")
    ap.add_argument("--behaviour", default="default", choices=sorted(sim.BEHAVIOUR))
    ap.add_argument("--day", type=int, default=100, help="day of the example export")
    ap.add_argument("--k", type=int, default=K)
    a = ap.parse_args()
    seeds = (list(range(int(a.seeds.split("-")[0]), int(a.seeds.split("-")[1]) + 1)) if "-" in a.seeds
             else [int(s) for s in a.seeds.split(",")])
    ks = (3, 7)
    print(f"SYNTHETIC federation - seeds {a.seeds}, behaviour '{a.behaviour}', k={a.k}")
    res, caught, stray, lead, after = {}, 0, [], [], [0, 0]
    for n_seed, seed in enumerate(seeds):
        run = sim.simulate(seed=seed, behaviour=a.behaviour)
        states, T = sorted(set(run.st)), run.book.shape[0]
        nodes = [StateNode(run, s, a.k) for s in states]
        if n_seed == 0:
            node, ex = nodes[0], nodes[0].export(a.day)
            assert_no_raw(ex, a.k)
            raw = len(node.slips) + len(node.na) + int((node.dx > 0).sum())
            print(f"\nexport of state {node.state}, seed {seed}, day {a.day}: {len(ex['rows'])} rows + "
                  f"{len(ex['models'])} model summaries = {len(json.dumps(ex)):,} bytes of JSON; "
                  f"the {raw:,} raw slip / not-available / diagnosis rows stay in the state")
            print(f"  top level: {sorted(ex)}")
            if ex["rows"]:
                print(f"  row:   {json.dumps(max(ex['rows'], key=lambda r: (r['alarm'], len(r['onsets']))))}")
            if ex["models"]:
                print(f"  model: {json.dumps(ex['models'][0])}")
            strict = StateNode(run, node.state, 7).export(a.day)
            print(f"  suppressed groups (under k={a.k} PHCs): {ex['suppressed']}. At k=7, above the "
                  f"{len(node.fac) // len(set(node.wh))} PHCs per warehouse: {strict['suppressed']} suppressed, "
                  f"{len(strict['rows'])} warehouse rows and {len(strict['models'])} state-level model summaries "
                  f"({len(node.fac)} PHCs) left")
            print("\nnational view, re-run on every day's exports (2+ states with 2+ starved warehouses);\n"
                  "  scored against the injected NATIONAL failures (GROUND TRUTH), counting its start to end + 21 days:")
        flags = {}      # (drug, "national" | "surge") -> flag per day
        for t in range(T):
            nat = National(a.k)
            for n in nodes:
                nat.ingest(n.export(t))
            for d, v in nat.view().items():
                for f in ("national", "surge"):
                    flags.setdefault((d, f), np.zeros(T, bool))[t] = v[f]
        # evaluation only: GROUND TRUTH
        events = sim.stockout_events(run)
        inj = [(run.ix["drugs"][e["drug"]], e["start"], e["end"], k) for k, e in enumerate(run.episodes)
               if e["type"] == "NATIONAL" and e["drug"] in run.ix["primaries"]]
        on = sum(int(flags.get((d, "national"), np.zeros(T))[s:e + 21].sum()) for d, s, e, _ in inj)
        tail = sum(int(flags.get((d, "national"), np.zeros(T))[e + 21:].sum()) for d, s, e, _ in inj)
        off = sum(int(m.sum()) for (d, f), m in flags.items() if f == "national") - on - tail
        caught, stray = caught + (on > 0), stray + [off]
        surged = {d: int(m.sum()) for (d, f), m in flags.items() if f == "surge" and m.any()}
        when = []
        for d, s, e, k in inj:
            hit = np.nonzero(flags.get((d, "national"), np.zeros(T))[s:e + 21])[0]
            outs = [x["out"] for x in events if x["ep"] == k]
            if len(hit):
                first = s + int(hit[0])
                lead.append(first - s)
                after[0], after[1] = after[0] + sum(o >= first for o in outs), after[1] + len(outs)
                when.append(f"first raised day {first}, {first - s} days in; "
                            f"{sum(o >= first for o in outs)} of the {len(outs)} PHC stock-outs it caused began after")
        print(f"  seed {seed}: injected {', '.join(f'{d} days {s}-{e - 1}' for d, s, e, _ in inj) or 'none'}: "
              f"shortage flag on it {on} days{' (' + '; '.join(when) + ')' if when else ''}, {tail} more after, "
              f"on other medicines {off} days; surge flag days {surged or 'none'}")
        if n_seed == 0:
            print("  national priors (median of state medians, from the last day's exports): " + ", ".join(
                f"{d} rho {p['rho']:.2f} tau {p['tau']:.2f}" for d, p in nat.priors().items()) or "none, all suppressed")
        for s in states:
            for days, by in cold_start(run, s, ks, a.k).items():
                for name, m in by.items():
                    res.setdefault((days, name), []).append(m)

    print(f"  national shortage flag raised on the injected failure in {caught}/{len(seeds)} seeds"
          + (f", {_range(lead, '{:.0f}')} days after it began, ahead of {after[0]} of the {after[1]} PHC "
             f"stock-outs those failures caused" if lead else "")
          + f"; {np.mean(stray):.1f} flag-days per seed on other medicines")
    print(f"\ncold start: each state in turn joins with only d days of history; priors come from the "
          f"other state's export. Scored on stock-outs after day d, {len(seeds)} seeds x 2 states:")
    print(f"  mean (min-max) over the {2 * len(seeds)} cold states")
    print(f"{'':34}{'events':>8}{'detected':>18}{'warned early':>18}{'false alarms/series-yr':>26}")
    label = dict(local="learned from {} days", priors="national priors", warm30="reference: 30-day warm-up")
    for days in ks:
        for name in ("local", "priors", "warm30"):
            ms = res[(days, name)]
            col = lambda key: f"{np.nanmean([m[key] for m in ms]):.2f} ({_range([m[key] for m in ms])})"
            print(f"  d={days} {label[name].format(days):28}{_range([m['events'] for m in ms], '{:.0f}'):>8}"
                  f"{col('recall'):>18}{col('early'):>18}{col('false_per_series_year'):>26}")
        pair = lambda key, better: sum(better(p[key], q[key]) for p, q in zip(res[(days, "priors")], res[(days, "local")]))
        print(f"  d={days} priors vs learned, per cold state: fewer false alarms in "
              f"{pair('false_per_series_year', lambda p, q: p < q)}, more in {pair('false_per_series_year', lambda p, q: p > q)}; "
              f"more warned early in {pair('early', lambda p, q: p > q)}, fewer in {pair('early', lambda p, q: p < q)} "
              f"(of {len(res[(days, 'priors')])})")


if __name__ == "__main__":
    main()
