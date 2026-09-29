"""Score Anumaan against injected ground truth on SYNTHETIC data.

    python -m anumaan.evaluate --seeds 5-9 --behaviour alt [--ration 0]

Every comparison sees only what a real deployment would see:
  care_only     the filter alone, without the shadow-stock alarm
  register <Nd  trust the register: alarm when it shows under N days of expected use
  register best the register threshold with the most catches at the model's false-alarm rate
  receipt_gap   no real delivery for 33 days
  crg_rule      the same CRG decoding (7-day full-course share) with a tuned threshold, no filter
  drug_only     the filter and shadow stock without diagnoses: each drug against its own warm-up dispensing
Filter, shadow, triage and crg_rule thresholds were tuned on seeds 0-4; report seeds 5-9.
"""
import argparse
from collections import Counter

import numpy as np

from anumaan import crg as G, filter as FL, sim, triage

EARLY = 21   # an alarm live from 3 weeks before the shelf empties until it refills is a detection


def _hits(alarm_map, e):
    return [s for s, t in alarm_map.get((e["fac"], e["drug"]), []) if s < e["end"] and t > e["out"] - EARLY]


def _score(alarm_map, events, scarce, days):
    """scarce[(f, d)]: GROUND TRUTH mask of days the shelf was genuinely short: under a
    week of stock, or low enough that staff were rationing. A false alarm is one raised
    while neither was true."""
    false = sum(not scarce[key][max(s - 3, 0):t + 3].any() for key, segs in alarm_map.items() for s, t in segs)
    caught, leads = Counter(), []
    for e in events:
        hits = _hits(alarm_map, e)
        caught[(e["type"], bool(hits))] += 1
        if hits:
            leads.append(e["out"] - (min(hits) + 1))    # an alarm is confirmed on its second day
    recall = lambda types: (sum(caught[(k, True)] for k in types) /
                            max(sum(caught[(k, b)] for k in types for b in (True, False)), 1))
    long = [e for e in events if e["end"] - e["out"] >= 4]
    return dict(recall=recall(sim.TYPES), early=sum(l > 0 for l in leads) / max(len(events), 1),
                recall_4d=float(np.mean([bool(_hits(alarm_map, e)) for e in long])) if long else float("nan"),
                by_type={k: recall([k]) for k in sim.TYPES if caught[(k, True)] + caught[(k, False)]},
                median_lead=float(np.median(leads)) if leads else float("nan"),
                false_per_series_year=false / len(alarm_map) * 365 / days)


def _episode(e):
    if e["ep"] is not None:
        return e["ep"]
    return ("surge", e["drug"]) if e["type"] == "DEMAND-SURGE" else ("local", e["fac"], e["drug"], e["out"])


def evaluate(run):
    ix, P = run.ix, run.ix["primaries"]
    obs = G.aggregate(ix, run.dx, run.slips, run.na)
    events = sim.stockout_events(run)
    days, F = run.book.shape[:2]
    given = sum(obs[k][:, :, P].sum(2) for k in G.CATS)
    expected = obs["N"][:, :, P].sum(2)
    methods = {k: {} for k in ("model", "care_only", "drug_only", "crg_rule", "receipt_gap")}
    cover, scarce = {}, {}
    for f in range(F):
        skip = FL.entry_gaps(given[:, f], expected[:, f])
        for d in P:
            cats = np.stack([obs[k][:, f, d] for k in G.CATS], 1)
            N, rec, book, exp, units = (obs["N"][:, f, d], run.receipts[:, f, d], run.book[:, f, d],
                                        obs["exp_units"][:, f, d], obs["units"][:, f, d])
            rho, tau = FL.learn(N, cats, book, exp, rec, skip)
            post = FL.filter_series(N, cats, book, exp, rec, skip, tau=tau, rho=rho)
            shadow = FL.shadow_cover(book, rec, units, FL.trailing_mean(rho * exp), post, skip)
            methods["model"][(f, d)] = FL.alarms(post, cover=shadow)
            methods["care_only"][(f, d)] = FL.alarms(post)
            flat_n = np.full(days, cats[:30][~skip[:30]].sum(1).mean())
            flat_u = np.full(days, obs["units"][:30, f, d][~skip[:30]].mean())
            post0 = FL.filter_series(flat_n, cats, book, flat_u, rec, skip)
            methods["drug_only"][(f, d)] = FL.alarms(post0, cover=FL.shadow_cover(book, rec, units, flat_u, post0, skip))
            full = FL.trailing_mean(cats[:, 0] * ~skip, 7) / np.maximum(FL.trailing_mean(N * ~skip, 7), 1e-9)
            methods["crg_rule"][(f, d)] = FL.segments(full < 0.6 * full[:30].mean(), min_len=5)
            got = rec > 3 * rec[:30].sum() / 30
            last = np.maximum.accumulate(np.where(got, np.arange(days), 0))
            methods["receipt_gap"][(f, d)] = FL.segments(np.arange(days) - last > 33)
            cover[(f, d)] = book / np.maximum(FL.trailing_mean(obs["exp_units"][:, f, d]), 1e-9)
            scarce[(f, d)] = run.true_stock[:, f, d] < max(7, run.ration_cover[f]) * run.true_use[:, f, d]

    res = dict(events=len(events), types=Counter(e["type"] for e in events),
               availability=float(1 - (run.true_stock[:, :, P] < 0.5 * run.true_use[:, :, P]).mean()),
               **{k: _score(m, events, scarce, days) for k, m in methods.items()})
    sweep = {k: _score({key: FL.segments(c < k) for key, c in cover.items()}, events, scarce, days) for k in range(1, 31)}
    res["register"] = {k: sweep[k] for k in (3, 7, 14, 21)}
    cap = max(res["model"]["false_per_series_year"], 0.1)
    fair = [(s["recall"], k) for k, s in sweep.items() if s["false_per_series_year"] <= cap]
    res["register_best"] = dict(sweep[max(fair)[1]], threshold=max(fair)[1]) if fair else None

    # where did it break: score on caught events, per event and per episode
    model = methods["model"]
    onsets = [(f, d, s) for (f, d), segs in model.items() for s, _ in segs]
    fill = triage.fill_rate(run.wh_asked, run.wh_got, run.wh_posted)
    lift = triage.surge_lift(obs["N"], run.st)
    caught = [(e, (e["fac"], e["drug"], min(h))) for e in events if (h := _hits(model, e))]
    first = {}
    for e, o in caught:
        k = _episode(e)
        if k not in first or o[2] < first[k][1][2]:
            first[k] = (e, o)
    counts = Counter(e["type"] for e, _ in caught)
    res["triage_majority"] = max(counts.values()) / len(caught) if caught else float("nan")
    for settle in (7, 21):
        labels = dict(zip(onsets, triage.classify(onsets, run.wh, run.st, fill, lift,
                                                  [s + 1 + settle for _, _, s in onsets])))
        pairs = [(e["type"], labels[o]) for e, o in caught]
        res[f"triage_{settle}d"] = dict(
            accuracy=float(np.mean([a == b for a, b in pairs])) if pairs else float("nan"),
            macro=float(np.mean([np.mean([a == b for a, b in pairs if a == k]) for k in counts])) if pairs else float("nan"),
            episodes=float(np.mean([labels[o] == e["type"] for e, o in first.values()])) if first else float("nan"),
            still_out=float(np.mean([o[2] + 1 + settle < e["end"] for e, o in caught])) if caught else float("nan"),
            confusion=Counter(pairs))
    return res


def _range(vals, fmt="{:.2f}"):
    vals = [v for v in vals if v == v]
    if not vals:
        return "n/a"
    lo, hi = min(vals), max(vals)
    return fmt.format(lo) if lo == hi else f"{fmt.format(lo)}-{fmt.format(hi)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="5-9", help="held-out seeds, e.g. 5-9 or 0,3")
    ap.add_argument("--behaviour", default="default", choices=sorted(sim.BEHAVIOUR))
    ap.add_argument("--ration", type=float, default=None, help="force the rationing probability (0 = staff never ration)")
    a = ap.parse_args()
    seeds = (list(range(int(a.seeds.split("-")[0]), int(a.seeds.split("-")[1]) + 1)) if "-" in a.seeds
             else [int(s) for s in a.seeds.split(",")])
    rs = [evaluate(sim.simulate(seed=s, behaviour=a.behaviour, p_ration=a.ration)) for s in seeds]

    print(f"SYNTHETIC evaluation - seeds {a.seeds}, behaviour '{a.behaviour}'"
          + (f", rationing forced to {a.ration}" if a.ration is not None else ""))
    print(f"  {_range([r['events'] for r in rs], '{:.0f}')} true stock-outs per run; shelf availability "
          f"{_range([r['availability'] for r in rs])}; mix {dict(sum((r['types'] for r in rs), Counter()))}")
    print(f"\n{'':16}{'detected':>10}{'4+ day outs':>13}{'warned early':>14}{'median lead (d)':>17}{'false alarms/series-yr':>24}")
    rows = [("anumaan", [r["model"] for r in rs]), ("care_only", [r["care_only"] for r in rs]),
            ("drug_only", [r["drug_only"] for r in rs]),
            ("crg_rule", [r["crg_rule"] for r in rs]), ("receipt_gap", [r["receipt_gap"] for r in rs])]
    rows += [(f"register <{k}d", [r["register"][k] for r in rs]) for k in (3, 7, 14, 21)]
    rows += [("register best", [r["register_best"] for r in rs if r["register_best"]])]
    for name, ms in rows:
        print(f"{name:16}{_range([m['recall'] for m in ms]):>10}{_range([m['recall_4d'] for m in ms]):>13}{_range([m['early'] for m in ms]):>14}"
              f"{_range([m['median_lead'] for m in ms], '{:+.1f}'):>17}{_range([m['false_per_series_year'] for m in ms]):>24}")
    print(f"  ('register best' = the register threshold ({_range([r['register_best']['threshold'] for r in rs if r['register_best']], '{:.0f}')} days)"
          f" with the most catches at the model's false-alarm rate)")
    print("\nrecall by cause (anumaan): " + ", ".join(
        f"{k} {_range([r['model']['by_type'][k] for r in rs if k in r['model']['by_type']])}" for k in sim.TYPES))
    for settle in (7, 21):
        t = [r[f"triage_{settle}d"] for r in rs]
        print(f"where it broke, {settle} days after the alarm: per event {_range([x['accuracy'] for x in t])}, "
              f"per episode {_range([x['episodes'] for x in t])}, per cause {_range([x['macro'] for x in t])}; "
              f"{_range([x['still_out'] for x in t], '{:.0%}')} of outages still ongoing then")
    print(f"  (always guessing the commonest cause scores {_range([r['triage_majority'] for r in rs])})")


if __name__ == "__main__":
    main()
