"""Cross-district redistribution: move treatment courses from calm PHCs to short ones.

When the filter says a PHC is going short and triage says the break is at the PHC,
at its warehouse, or a demand surge, the quickest relief is often a van from a
nearby PHC that holds more than it needs. We plan in treatment COURSES, not
tablets: the diagnoses say "this PHC needs 40 pneumonia courses over the next two
weeks"; the register cannot.

  recipients   in alarm (P(SCARCE) + P(OUT) >= 0.7) and labelled LOCAL, WAREHOUSE
               or DEMAND-SURGE. Need = trailing diagnoses x horizon, less any
               guideline substitute the register shows on the shelf.
  donors       calm for a week (P(OK) >= 0.9 every day, so not rationing) with
               register cover above the reserve. Surplus is discounted because
               registers overstate, and nobody donates a drug that carries a
               WAREHOUSE, STATE-PROCUREMENT or NATIONAL alarm on their warehouse,
               state or country: their "surplus" is likely next month's gap (see DISCOUNT).
  escalations  STATE-PROCUREMENT and NATIONAL alarms. Moving stock around inside a
               shortage that big cannot fix it.

The match is a min-cost flow (Google OR-Tools): integer courses, arcs only within
max_minutes of road AND inside one state (DVDMS indents and procurement run per
state; a cross-state loan is the STATE-PROCUREMENT escalation, not a routine issue),
as much need covered as possible at the fewest minutes x courses.
Coordinates are SYNTHETIC and travel times a haversine estimate (see
route_matrix_requests for the Google Maps Routes hook).

    python -m anumaan.planner --seeds 5-9
"""
import argparse
from collections import Counter

import numpy as np
from ortools.graph.python import min_cost_flow

from anumaan import crg as G, filter as FL, sim, triage
from anumaan.evaluate import _range as rng

THR = 0.7
GIVE_TO = ("LOCAL", "WAREHOUSE", "DEMAND-SURGE")
ESCALATE = ("STATE-PROCUREMENT", "NATIONAL")
# Share of the register balance we trust. Registers overstate (issues posted late or
# never, opening balances inflated) - on seeds 0-4 calm PHCs held a median 0.5 of what
# their register said - so the discount applies BEFORE the reserve is taken off: taking
# it off the surplus alone still sent half the donors under their reserve.
# Picked on seeds 0-4 with evaluate_plan (same-state arcs, donor veto on), on a 0.05 grid:
# the largest discount keeping 85% of courses from donors with true surplus (0.50: 0.72,
# 0.40: 0.846, 0.35: 0.88, 0.30: 0.91, at a steep cost in volume).
# The donor veto was chosen on the same seeds by the same rule. It fires on labels that
# are mostly NOT true state/national failures (CLI: escalations), and at a fixed discount
# it costs need met (seeds 0-4 pooled: surplus 0.88 vs 0.84 without, need met 0.20 vs
# 0.26, to-truly-short 0.76 vs 0.78). But buying that surplus back with a tighter
# discount costs more: at 0.30 without veto, surplus 0.88 and need met only 0.14;
# WAREHOUSE-only veto at 0.30: 0.885, 0.12. So the veto is the cheaper lever.
# ponytail: one network-wide number; upgrade to a per-facility trust learned like the
# filter's tau once shelf checks come back.
DISCOUNT = 0.35
ROAD, KMH = 1.4, 35          # road km per straight-line km, average rural van speed
ROUTES_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"


def synthetic_coords(run, seed=0):
    """SYNTHETIC [F, 2] lat/lon. Each state is a column of districts ~70 km apart, the
    states side by side, somewhere in central India; the warehouse sits at the district
    centre and its PHCs are scattered uniformly within 30 km of it."""
    rng = np.random.default_rng(seed)
    state_of = dict(zip(run.wh, run.st))
    states = sorted(set(run.st))
    lat0, lon0 = rng.uniform(19, 23), rng.uniform(75, 81)
    centre = {}
    for w in sorted(state_of):
        row = sum(state_of[x] == state_of[w] for x in centre)
        centre[w] = np.array([lat0 + 0.65 * row, lon0 + 0.7 * states.index(state_of[w])]) + rng.uniform(-0.1, 0.1, 2)
    c = np.array([centre[w] for w in run.wh])
    km, ang = 30 * np.sqrt(rng.random(len(c))), rng.uniform(0, 2 * np.pi, len(c))
    return c + np.column_stack([km * np.sin(ang) / 111, km * np.cos(ang) / (111 * np.cos(np.radians(c[:, 0])))])


def travel_minutes(coords):
    """[F, F] drive minutes: great-circle km x ROAD at KMH.
    ponytail: straight-line estimate; with a Maps key configured, fill this matrix from
    Routes computeRouteMatrix instead (route_matrix_requests builds the calls)."""
    lat, lon = np.radians(coords).T
    h = (np.sin((lat[:, None] - lat) / 2) ** 2
         + np.cos(lat)[:, None] * np.cos(lat) * np.sin((lon[:, None] - lon) / 2) ** 2)
    return 2 * 6371 * np.arcsin(np.sqrt(h)) * ROAD / KMH * 60


def route_matrix_requests(coords, tile=25):
    """STUB - builds, never sends. The Google Maps Routes computeRouteMatrix bodies that
    would replace travel_minutes when an API key is configured. One request may carry at
    most 625 elements (origins x destinations), so the F x F matrix goes in 25 x 25 tiles.
    To go live: POST each body to ROUTES_URL with headers X-Goog-Api-Key: <key> and
    X-Goog-FieldMask: originIndex,destinationIndex,duration,condition; every streamed
    element with condition ROUTE_EXISTS sets minutes[o0 + originIndex, d0 + destinationIndex]
    = duration seconds / 60. Returns [(o0, d0, body)]."""
    wp = [{"waypoint": {"location": {"latLng": {"latitude": float(a), "longitude": float(b)}}}} for a, b in coords]
    return [(o, d, {"origins": wp[o:o + tile], "destinations": wp[d:d + tile],
                    "travelMode": "DRIVE", "routingPreference": "TRAFFIC_UNAWARE"})
            for o in range(0, len(wp), tile) for d in range(0, len(wp), tile)]


def labels_at(t, run, post, obs):
    """Triage label of the alarm live at day t for each (f, d), else None. Causal: only
    onsets confirmed by day t are clustered, as the demo app does it."""
    segs = {k: FL.alarms(p[:t + 1], THR) for k, p in post.items()}
    known = [(f, d, s) for (f, d), ss in segs.items() for s, _ in ss]
    surge = {(f, d, s) for f, d, s in known if triage.demand_led(obs["N"][:, f, d], run.receipts[:, f, d], s + 1)}
    lab = dict(zip(known, triage.classify(known, run.wh, run.st, settle=t + 1, surge=surge)))
    return {(f, d): lab[(f, d, ss[-1][0])] if ss and ss[-1][1] == t + 1 else None for (f, d), ss in segs.items()}


def plan(t, run, post, labels, coords, horizon=14, reserve_days=14, max_minutes=180, obs=None):
    """Redistribution plan for day t.

    post: {(f, d): regime posterior [T, 3]} per facility x drug index; labels: {(f, d):
    triage label or None} at day t (labels_at); coords: [F, 2] lat/lon; obs: the
    G.aggregate output if already computed. Reads only what a deployment sees.
    Returns dict(transfers, escalations, orders, recipients).
    """
    ix, fac, names = run.ix, run.facilities, run.ix["drugs"]
    obs = obs or G.aggregate(ix, run.dx, run.slips, run.na)
    st = np.array(run.st)
    mins = np.where(st[:, None] == st, travel_minutes(coords), np.inf)   # no routine issue across a state line
    lo = max(t - 13, 0)
    use = obs["exp_units"][lo:t + 1].mean(0)          # [F, D] expected units/day, trailing 14 days
    courses_per_day = obs["N"][lo:t + 1].mean(0)

    def spare(f, d, cu):   # discounted register above the reserve, in whole courses
        return int(max(DISCOUNT * run.book[t, f, d] - reserve_days * use[f, d], 0) // cu)

    subs = {}
    for (_, d), (_, _, ss) in ix["course"].items():
        subs.setdefault(d, set()).update((s, upd * n) for s, upd, n in ss)
    sub_left = {}          # (f, sub) register courses not yet counted against a need
    out = dict(transfers=[], escalations=[], orders=[], recipients=[])
    for d in sorted({d for _, d in post}):
        cu = ix["units"][:, d].sum() / ix["share"][:, d].sum()     # units in one course
        hot = {run.wh[f] if lv == "WAREHOUSE" else run.st[f] if lv == "STATE-PROCUREMENT" else "IN"
               for (f, d2), lv in labels.items() if d2 == d and lv in ("WAREHOUSE", *ESCALATE)}
        rows, donors = {}, {}
        for f in range(len(fac)):
            if (f, d) not in post:
                continue
            p, lv = post[(f, d)], labels.get((f, d))
            short = round(float(p[t, 1] + p[t, 2]), 3)
            n = int(round(courses_per_day[f, d] * horizon))
            if short >= THR and lv in ESCALATE:
                out["escalations"].append(dict(fac=fac[f], drug=names[d], level=lv, p_short=short,
                                               courses=n, action=triage.ACTION[lv]))
            elif short >= THR and lv in GIVE_TO:
                rows[f] = row = dict(fac=fac[f], drug=names[d], level=lv, p_short=short, need=n,
                                     substitute=None, sub_courses=0, planned=0)
                for s, scu in sorted(subs.get(d, ())):   # a guideline substitute on the shelf covers part
                    got = min(sub_left.setdefault((f, s), spare(f, s, scu)), n - row["sub_courses"])
                    if got > 0:
                        sub_left[(f, s)] -= got
                        row.update(substitute=names[s], sub_courses=row["sub_courses"] + got)
            elif (p[max(t - 6, 0):t + 1, 0] >= 0.9).all() and not {run.wh[f], run.st[f], "IN"} & hot:
                if (s := spare(f, d, cu)) > 0:
                    donors[f] = s
        out["recipients"] += rows.values()
        need = {f: r["need"] - r["sub_courses"] for f, r in rows.items() if r["need"] > r["sub_courses"]}
        for (a, b), k in _match(donors, need, mins, max_minutes).items():
            r, units = rows[b], int(round(k * cu))
            r["planned"] += k
            sub = f" after {r['sub_courses']} courses of {r['substitute']} on its shelf" if r["substitute"] else ""
            out["transfers"].append(dict(
                from_fac=fac[a], to_fac=fac[b], drug=names[d], courses=k, units=units, minutes=round(float(mins[a, b])),
                reason=f"{fac[b]}: {r['level']} alarm, P(short) {r['p_short']:.2f}, needs {need[b]} courses over "
                       f"{horizon}d{sub}. {fac[a]}: calm 7 days, register shows "
                       f"{run.book[t, a, d] / max(use[a, d], 1e-9):.0f}d of use; trusting {DISCOUNT:.0%} of it "
                       f"still leaves a {reserve_days}d reserve"))
            out["orders"].append({"indent_id": f"RD-{t:03d}-{len(out['orders']) + 1:03d}", "from": fac[a],
                                  "to": fac[b], "item": names[d], "qty_units": units})
    return out


def _match(donors, need, mins, max_minutes):
    """Min-cost max-flow over donor -> recipient arcs within max_minutes. {(a, b): courses}."""
    arcs = [(i, j) for i, a in enumerate(donors) for j, b in enumerate(need) if mins[a, b] <= max_minutes]
    if not arcs:
        return {}
    D, R = list(donors), list(need)
    mcf = min_cost_flow.SimpleMinCostFlow()
    tails, heads = np.array(arcs).T
    ids = mcf.add_arcs_with_capacity_and_unit_cost(
        tails, heads + len(D), np.array([min(donors[D[i]], need[R[j]]) for i, j in arcs]),
        np.array([int(round(mins[D[i], R[j]])) for i, j in arcs]))
    mcf.set_nodes_supplies(np.arange(len(D) + len(R)), np.array([donors[a] for a in D] + [-need[b] for b in R]))
    if mcf.solve_max_flow_with_min_cost() != mcf.OPTIMAL:
        raise RuntimeError("min-cost flow failed")
    return {(D[i], R[j]): int(k) for (i, j), k in zip(arcs, mcf.flows(ids)) if k > 0}


# ---------------- evaluation on SYNTHETIC ground truth ----------------

def posteriors(run, obs):
    """Model posteriors per (f, d), built exactly as evaluate.py does (entry-gap skip)."""
    P = run.ix["primaries"]
    given = sum(obs[k][:, :, P].sum(2) for k in G.CATS)
    expected = obs["N"][:, :, P].sum(2)
    post = {}
    for f in range(len(run.facilities)):
        skip = FL.entry_gaps(given[:, f], expected[:, f])
        for d in P:
            cats = np.stack([obs[k][:, f, d] for k in G.CATS], 1)
            post[(f, d)] = FL.filter_series(obs["N"][:, f, d], cats, run.book[:, f, d], obs["exp_units"][:, f, d],
                                            run.receipts[:, f, d], skip)
    return post


def _register_view(run, obs, days=7):
    """Baseline: the same planner fed the register instead of the model. A PHC is short
    when its register shows under `days` of expected use; every shortage is treated as local."""
    post = {}
    for f in range(len(run.facilities)):
        for d in run.ix["primaries"]:
            short = run.book[:, f, d] < days * FL.trailing_mean(obs["exp_units"][:, f, d])
            post[(f, d)] = np.column_stack([~short, 0 * short, short]).astype(float)
    return post, {k: "LOCAL" for k in post}


def evaluate_plan(run, seed=0, days=range(35, 200, 7), horizon=14, reserve_days=14, max_minutes=180):
    """Plan every 7th day and score the plans against GROUND TRUTH. The simulated world
    does not react to the plans: each day is scored as if only that day's plan ran."""
    obs = G.aggregate(run.ix, run.dx, run.slips, run.na)
    coords = synthetic_coords(run, seed)
    fi, di = {x: i for i, x in enumerate(run.facilities)}, run.ix["di"]
    model = posteriors(run, obs)
    reg_post, reg_labels = _register_view(run, obs)
    res = {}
    for name in ("anumaan", "register <7d"):
        n = short = surplus = minutes = need = got = 0
        esc = []
        for t in days:
            if t >= run.book.shape[0]:
                break
            post, labels = (model, labels_at(t, run, model, obs)) if name == "anumaan" else (reg_post, reg_labels)
            p = plan(t, run, post, labels, coords, horizon, reserve_days, max_minutes, obs)
            rows = [(fi[x["from_fac"]], fi[x["to_fac"]], di[x["drug"]], x) for x in p["transfers"]]
            sent = Counter()
            for a, _, d, x in rows:
                sent[(a, d)] += x["units"]
            for a, b, d, x in rows:
                k = x["courses"]
                n += k
                minutes += k * x["minutes"]
                short += k * (run.true_stock[t, b, d] < 7 * run.true_use[t, b, d])
                surplus += k * (run.true_stock[t, a, d] - sent[(a, d)] >= reserve_days * run.true_use[t, a, d])
            need += sum(r["need"] - r["sub_courses"] for r in p["recipients"])
            got += sum(r["planned"] for r in p["recipients"])
            esc += [_upstream(run, t, fi[e["fac"]], di[e["drug"]]) for e in p["escalations"]]
        res[name] = dict(courses=n, to_short=short / max(n, 1), from_surplus=surplus / max(n, 1),
                         minutes=minutes / max(n, 1), need_met=got / max(need, 1), escalations=len(esc),
                         esc_state_national=float(np.mean([s for s, _ in esc])) if esc else float("nan"),
                         esc_any_upstream=float(np.mean([u for _, u in esc])) if esc else float("nan"))
    return res


def _upstream(run, t, f, d):
    """GROUND TRUTH: was an injected state/national (or any upstream) failure on this drug
    live for this facility at day t? Same 21-day tail as sim.stockout_events."""
    live = [e["type"] for e in run.episodes if e["drug"] == d and e["start"] <= t <= e["end"] + 21
            and e["root"] in ("IN", run.st[f], run.wh[f])]
    return any(k in ESCALATE for k in live), bool(live)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="5-9", help="held-out seeds, e.g. 5-9 or 0,3")
    ap.add_argument("--behaviour", default="default", choices=sorted(sim.BEHAVIOUR))
    a = ap.parse_args()
    seeds = (list(range(int(a.seeds.split("-")[0]), int(a.seeds.split("-")[1]) + 1)) if "-" in a.seeds
             else [int(s) for s in a.seeds.split(",")])
    rs = [evaluate_plan(sim.simulate(seed=s, behaviour=a.behaviour), seed=s) for s in seeds]
    print(f"SYNTHETIC redistribution plans - seeds {a.seeds}, behaviour '{a.behaviour}', a plan every 7 days "
          f"from day 35; horizon 14d, reserve 14d, same-state arcs <= 180 min, register discount {DISCOUNT}")
    print(f"\n{'':14}{'courses/run':>12}{'to truly short':>16}{'from true surplus':>19}{'min/course':>12}{'need met':>10}")
    for name in ("anumaan", "register <7d"):
        m = [r[name] for r in rs]
        print(f"{name:14}{rng([x['courses'] for x in m], '{:.0f}'):>12}{rng([x['to_short'] for x in m]):>16}"
              f"{rng([x['from_surplus'] for x in m]):>19}{rng([x['minutes'] for x in m], '{:.0f}'):>12}"
              f"{rng([x['need_met'] for x in m]):>10}")
    m = [r["anumaan"] for r in rs]
    print(f"\nescalations (anumaan): {rng([x['escalations'] for x in m], '{:.0f}')} facility-drug-days per run; "
          f"{rng([x['esc_state_national'] for x in m])} truly state/national failures, "
          f"{rng([x['esc_any_upstream'] for x in m])} any upstream failure")
    print("  truly short = true stock under 7 days of true use on the plan day; true surplus = true stock still\n"
          "  above the 14-day reserve after everything that donor sent that day. 'register <7d' = the same\n"
          "  planner fed the register instead of the model (short under 7 days of cover, every shortage local).\n"
          "  Donor veto (anumaan only): no donor gives a drug with a WAREHOUSE/STATE/NATIONAL alarm on its warehouse,\n"
          "  state or country, though those labels are mostly not true upstream failures (escalations line). At this\n"
          "  discount it trades need met for true surplus (seeds 5-9 pooled, measured once: surplus 0.86 vs 0.81,\n"
          "  need met 0.20 vs 0.27, to truly short 0.73 vs 0.76 without it); a tighter discount buying the same surplus\n"
          "  costs more need met, so the veto stays (chosen on seeds 0-4, see DISCOUNT).\n"
          "  Not claimed: plans are never applied to the simulated world, so courses/run sums independent weekly\n"
          "  plans that re-plan the same shortages; minutes are straight-line estimates on SYNTHETIC maps.")


if __name__ == "__main__":
    main()
