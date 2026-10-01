"""Cross-district redistribution: move treatment courses from calm PHCs to short ones.

When the filter says a PHC is going short and triage says the break is at the PHC,
at its warehouse, or a demand surge, the quickest relief is often a van from a
nearby PHC that holds more than it needs. We plan in treatment COURSES, not
tablets: the diagnoses say "this PHC needs 40 pneumonia courses over the next two
weeks"; the register cannot.

  recipients   in alarm (P(SCARCE) + P(OUT) >= 0.7) and labelled LOCAL, WAREHOUSE
               or DEMAND-SURGE. Need = trailing diagnoses x horizon, less any
               guideline substitute the register shows on the shelf.
  donors       calm for a week (P(OK) >= 0.9 every day, so not rationing), in no
               alarm, with shadow stock above the reserve (see SHADOW_TRUST). Nobody
               donates a drug that carries a WAREHOUSE, STATE-PROCUREMENT or NATIONAL
               alarm on their warehouse, state or country: no resupply is coming, so
               their "surplus" is next month's gap.
  escalations  STATE-PROCUREMENT and NATIONAL alarms. Moving stock around inside a
               shortage that big cannot fix it.

The match is a min-cost flow (Google OR-Tools): integer courses, arcs only within
max_minutes of road AND inside one state (DVDMS indents and procurement run per
state; a cross-state loan is the STATE-PROCUREMENT escalation, not a routine issue),
as much need covered as possible at the fewest minutes x courses.
Coordinates are SYNTHETIC, except the Kerala replay's 70 real public PHC/FHC sites from
OpenStreetMap (kerala_phcs.json). Travel times are real Google Maps Routes drive times between
those points where road_minutes.json holds the map (seeds 5-9, so the demo's seed 5 too, and the
Kerala sites), else a straight-line estimate. The app only reads that file; fetch_road_minutes fills it.

    python -m anumaan.planner --seeds 5-9
    python -m anumaan.planner --seeds 5-9 --fetch-routes   # first fetch missing maps (billed)
"""
import argparse
import json
import time
from collections import Counter
from datetime import date
from functools import cache
from pathlib import Path

import numpy as np
from ortools.graph.python import min_cost_flow

from anumaan import crg as G, filter as FL, sim, triage
from anumaan.evaluate import _range as rng

THR = 0.7
GIVE_TO = ("LOCAL", "WAREHOUSE", "DEMAND-SURGE")
ESCALATE = ("STATE-PROCUREMENT", "NATIONAL")
# Donor surplus comes from the shadow stock (filter.shadow_cover), which unposted issues
# cannot inflate; its opening balance still can, so only SHADOW_TRUST of it counts, taken
# BEFORE the reserve comes off. Picked on seeds 0-4 with evaluate_plan on a 0.05 grid: the
# largest share keeping 85% of courses from donors with true surplus (0.50: 0.94, 0.55: 0.89,
# 0.60: 0.81). Trusting 35% of the register instead: 0.84 of courses and a quarter of the need.
# ponytail: one network-wide number; upgrade to a per-facility trust once shelf checks come back.
SHADOW_TRUST = 0.55
# Without a shadow stock (the register-fed baseline, and a substitute on a recipient's own
# shelf) only DISCOUNT of the register counts. Registers overstate - on seeds 0-4 calm PHCs
# held a median 0.5 of what their register said. Chosen by the same rule when the planner ran
# on the register.
DISCOUNT = 0.35
# The donor veto no longer moves the one-day scores now that labels come from the ledger
# (seeds 0-4: surplus 0.891 vs 0.890 without it, need met 0.48 vs 0.50). It stays because a
# donor under an upstream failure has no resupply coming, which a one-day snapshot cannot score.
ROAD, KMH = 1.4, 35          # road km per straight-line km, average rural van speed
ROUTES_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
PROJECT = "anumaan-c4c"
ROAD_MINUTES = Path(__file__).with_name("road_minutes.json")


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
    """[F, F] drive minutes. Real Google Maps road times for the arcs road_minutes.json holds
    for exactly this map; every other pair, and every map it lacks, great-circle km x ROAD at
    KMH. Reads a file, never calls Routes (the Cloud Run identity may not)."""
    lat, lon = np.radians(coords).T
    h = (np.sin((lat[:, None] - lat) / 2) ** 2
         + np.cos(lat)[:, None] * np.cos(lat) * np.sin((lon[:, None] - lon) / 2) ** 2)
    m = 2 * 6371 * np.arcsin(np.sqrt(h)) * ROAD / KMH * 60
    if (a := _road_arcs(coords)) is not None:
        m[a[:, 0], a[:, 1]] = a[:, 2] / 60
    return m


@cache
def _road_maps():
    """[(coords [F, 2], arcs [n, 4]: origin, destination, seconds, metres)] from ROAD_MINUTES."""
    maps = json.loads(ROAD_MINUTES.read_text())["maps"].values() if ROAD_MINUTES.exists() else ()
    return [(np.array(m["coords"]), np.array(m["arcs"], int).reshape(-1, 4)) for m in maps]


def _road_arcs(coords):
    """Cached arcs for this map, matched on its coordinates to within float noise between
    machines (numpy's trig differs in the last bits across CPUs), else None."""
    return next((a for c, a in _road_maps()
                 if c.shape == np.shape(coords) and np.allclose(c, coords, rtol=0, atol=1e-6)), None)


def route_matrix_requests(coords, want=None, limit=625):
    """Google Maps Routes computeRouteMatrix bodies for the origin -> destination arcs in
    want ([F, F] bool, default all): one origin per request, so no unwanted pair is billed,
    and at most `limit` elements (origins x destinations, the per-request cap) in each.
    Returns [(origin, [destination], body)]."""
    want = np.ones((len(coords),) * 2, bool) if want is None else want
    wp = [{"waypoint": {"location": {"latLng": {"latitude": float(a), "longitude": float(b)}}}} for a, b in coords]
    return [(o, ds, {"origins": [wp[o]], "destinations": [wp[d] for d in ds],
                     "travelMode": "DRIVE", "routingPreference": "TRAFFIC_UNAWARE"})
            for o in range(len(wp)) for row in [np.flatnonzero(want[o]).tolist()]
            for ds in (row[k:k + limit] for k in range(0, len(row), limit))]


def _arcs(o, ds, elements):
    """[origin, destination, seconds, metres] for each streamed element that found a road.
    ROUTE_NOT_FOUND and per-element errors are left out: travel_minutes keeps the
    straight-line estimate for those pairs."""
    return [[o, ds[e.get("destinationIndex", 0)], round(float(e.get("duration", "0s")[:-1])), e.get("distanceMeters", 0)]
            for e in elements if e.get("condition") == "ROUTE_EXISTS" and not e.get("status", {}).get("code")]


def fetch_road_minutes(seeds=(), maps=None):
    """Add real Google Maps road times to ROAD_MINUTES for each seed's SYNTHETIC map, and for each
    named map in maps, {name: (coords [F, 2], same_state [F, F] bool)}: the Kerala replay's real
    sites are {"kerala": (their lat/lon in kerala_phcs.json order, all True)}, 70 x 69 = 4,830
    elements. Maps it already holds are skipped, so a re-run costs nothing. Asks only for the arcs
    plan can use (one state, never a PHC to itself) as Compute Route Matrix Essentials (DRIVE,
    TRAFFIC_UNAWARE, no traffic or tolls): 2 states x 18 x 17 = 612 elements a default map. Needs
    application-default credentials and routes.googleapis.com on PROJECT. Offline tool: the app
    never calls it."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    s = AuthorizedSession(creds)
    hdr = {"X-Goog-User-Project": PROJECT,
           "X-Goog-FieldMask": "originIndex,destinationIndex,duration,distanceMeters,status,condition"}
    data = {"about": "REAL road times between each map's points: Google Maps Routes computeRouteMatrix, DRIVE, "
                     "TRAFFIC_UNAWARE. A map named by a seed holds SYNTHETIC points (planner.synthetic_coords); "
                     "'kerala' holds 70 real public PHC/FHC sites from OpenStreetMap (kerala_phcs.json, ODbL 1.0, "
                     "(c) OpenStreetMap contributors), in that file's order. arcs: [origin, destination, seconds, "
                     "metres], same-state pairs only; a pair missing here gets the straight-line estimate. "
                     "Add maps: python -m anumaan.planner --seeds 5-9 --fetch-routes",
            "maps": json.loads(ROAD_MINUTES.read_text())["maps"] if ROAD_MINUTES.exists() else {}}
    for name, (coords, same) in ({str(seed): _seed_map(seed) for seed in seeds} | (maps or {})).items():
        coords = np.asarray(coords, float)
        if _road_arcs(coords) is not None:
            continue
        want = np.asarray(same, bool) & ~np.eye(len(coords), dtype=bool)
        arcs = []
        for o, ds, body in route_matrix_requests(coords, want):
            while (r := s.post(ROUTES_URL, json=body, headers=hdr, timeout=60)).status_code == 429:
                time.sleep(60)                          # Routes allows 3,000 matrix elements a minute
            r.raise_for_status()
            arcs += _arcs(o, ds, r.json())
            time.sleep(len(ds) / 45)                    # so pace the asks to about 2,700 a minute
        n = int(want.sum())
        data["maps"][name] = dict(fetched=date.today().isoformat(), elements=n, no_route=n - len(arcs),
                                  coords=coords.tolist(), arcs=arcs)
        ROAD_MINUTES.write_text(json.dumps(data))       # after every map: nothing paid for is lost
        _road_maps.cache_clear()
        print(f"map {name}: {n} elements asked, {n - len(arcs)} without a road route (straight line kept)")


def _seed_map(seed):
    """A seed's SYNTHETIC coordinates and same-state mask."""
    run = sim.simulate(seed=seed)
    st = np.array(run.st)
    return synthetic_coords(run, seed), st[:, None] == st


def labels_at(t, run, post, obs, cover=None):
    """Triage label of the alarm live at day t for each (f, d), else None. Causal: only
    alarms confirmed by day t are labelled, from the ledger up to day t, as the demo app
    does it. cover: {(f, d): shadow cover [T]}, to count shadow-stock alarms too."""
    cover = cover or {}
    segs = {k: FL.alarms(p[:t + 1], THR, c[:t + 1] if (c := cover.get(k)) is not None else None)
            for k, p in post.items()}
    known = [(f, d, s) for (f, d), ss in segs.items() for s, _ in ss]
    fill = triage.fill_rate(run.wh_asked, run.wh_got, run.wh_posted)
    lab = dict(zip(known, triage.classify(known, run.wh, run.st, fill, triage.surge_lift(obs["N"], run.st), t,
                                          triage.surge_lift(obs["N"], run.wh))))
    return {(f, d): lab[(f, d, ss[-1][0])] if ss and ss[-1][1] == t + 1 else None for (f, d), ss in segs.items()}


def plan(t, run, post, labels, coords, horizon=14, reserve_days=14, max_minutes=180, obs=None, cover=None):
    """Redistribution plan for day t.

    post: {(f, d): regime posterior [T, 3]} per facility x drug index; labels: {(f, d):
    triage label or None} at day t (labels_at); coords: [F, 2] lat/lon; obs: the
    G.aggregate output if already computed; cover: {(f, d): shadow cover [T]} (without
    it, donors are sized on the discounted register). Reads only what a deployment sees.
    Returns dict(transfers, escalations, orders, recipients).
    """
    ix, fac, names = run.ix, run.facilities, run.ix["drugs"]
    obs = obs or G.aggregate(ix, run.dx, run.slips, run.na)
    cover = cover or {}
    st = np.array(run.st)
    mins = np.where(st[:, None] == st, travel_minutes(coords), np.inf)   # no routine issue across a state line
    lo = max(t - 13, 0)
    use = obs["exp_units"][lo:t + 1].mean(0)          # [F, D] expected units/day, trailing 14 days
    courses_per_day = obs["N"][lo:t + 1].mean(0)

    def held(f, d):        # units on the shelf as the plan sees them, and the share trusted
        if (f, d) in cover:
            return cover[(f, d)][t] * use[f, d], SHADOW_TRUST
        return run.book[t, f, d], DISCOUNT

    def spare(f, d, cu):   # trusted share above the reserve, in whole courses
        units, trust = held(f, d)
        return int(max(trust * units - reserve_days * use[f, d], 0) // cu)

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
            elif lv is None and (p[max(t - 6, 0):t + 1, 0] >= 0.9).all() and not {run.wh[f], run.st[f], "IN"} & hot:
                if (s := spare(f, d, cu)) > 0:
                    donors[f] = s
        out["recipients"] += rows.values()
        need = {f: r["need"] - r["sub_courses"] for f, r in rows.items() if r["need"] > r["sub_courses"]}
        for (a, b), k in _match(donors, need, mins, max_minutes).items():
            r, units = rows[b], int(round(k * cu))
            r["planned"] += k
            sub = f" after {r['sub_courses']} courses of {r['substitute']} on its shelf" if r["substitute"] else ""
            units_held, trust = held(a, d)
            out["transfers"].append(dict(
                from_fac=fac[a], to_fac=fac[b], drug=names[d], courses=k, units=units, minutes=round(float(mins[a, b])),
                reason=f"{fac[b]}: {r['level']} alarm, P(short) {r['p_short']:.2f}, needs {need[b]} courses over "
                       f"{horizon}d{sub}. {fac[a]}: calm 7 days, {'shadow stock' if (a, d) in cover else 'register'} "
                       f"shows {units_held / max(use[a, d], 1e-9):.0f}d of use; counting {trust:.0%} of it still "
                       f"leaves a {reserve_days}d reserve"))
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
    """Model posteriors and shadow cover per (f, d), built exactly as evaluate.py does."""
    P = run.ix["primaries"]
    given = sum(obs[k][:, :, P].sum(2) for k in G.CATS)
    expected = obs["N"][:, :, P].sum(2)
    post, cover = {}, {}
    for f in range(len(run.facilities)):
        skip = FL.entry_gaps(given[:, f], expected[:, f])
        for d in P:
            cats = np.stack([obs[k][:, f, d] for k in G.CATS], 1)
            N, rec, book, exp = obs["N"][:, f, d], run.receipts[:, f, d], run.book[:, f, d], obs["exp_units"][:, f, d]
            rho, tau = FL.learn(N, cats, book, exp, rec, skip)
            post[(f, d)] = p = FL.filter_series(N, cats, book, exp, rec, skip, tau=tau, rho=rho)
            cover[(f, d)] = FL.shadow_cover(book, rec, obs["units"][:, f, d], FL.trailing_mean(rho * exp), p, skip)
    return post, cover


def _register_view(run, obs, days=7):
    """Baseline: the same planner fed the register instead of the model. A PHC is short
    when its register shows under `days` of expected use; every shortage is treated as local.
    Returns posteriors and labels_at's stand-in: t -> {(f, d): "LOCAL" while short, else None}."""
    post = {}
    for f in range(len(run.facilities)):
        for d in run.ix["primaries"]:
            short = run.book[:, f, d] < days * FL.trailing_mean(obs["exp_units"][:, f, d])
            post[(f, d)] = np.column_stack([~short, 0 * short, short]).astype(float)
    return post, lambda t: {k: "LOCAL" if p[t, 2] else None for k, p in post.items()}


def evaluate_plan(run, seed=0, days=range(35, 200, 7), horizon=14, reserve_days=14, max_minutes=180):
    """Plan every 7th day and score the plans against GROUND TRUTH. The simulated world
    does not react to the plans: each day is scored as if only that day's plan ran."""
    obs = G.aggregate(run.ix, run.dx, run.slips, run.na)
    coords = synthetic_coords(run, seed)
    fi, di = {x: i for i, x in enumerate(run.facilities)}, run.ix["di"]
    model, cover = posteriors(run, obs)
    reg_post, reg_labels = _register_view(run, obs)
    res = {}
    for name in ("anumaan", "register <7d"):
        n = short = surplus = minutes = need = got = 0
        esc = []
        for t in days:
            if t >= run.book.shape[0]:
                break
            post, labels = (model, labels_at(t, run, model, obs, cover)) if name == "anumaan" else (reg_post, reg_labels(t))
            p = plan(t, run, post, labels, coords, horizon, reserve_days, max_minutes, obs,
                     cover if name == "anumaan" else None)
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
    res["real_roads"] = _road_arcs(coords) is not None
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
    ap.add_argument("--fetch-routes", action="store_true",
                    help="first add real road times for these seeds' maps to road_minutes.json (billed Routes calls)")
    a = ap.parse_args()
    seeds = (list(range(int(a.seeds.split("-")[0]), int(a.seeds.split("-")[1]) + 1)) if "-" in a.seeds
             else [int(s) for s in a.seeds.split(",")])
    if a.fetch_routes:
        fetch_road_minutes(seeds)
    rs = [evaluate_plan(sim.simulate(seed=s, behaviour=a.behaviour), seed=s) for s in seeds]
    real = ",".join(str(s) for s, r in zip(seeds, rs) if r["real_roads"]) or "none"
    print(f"SYNTHETIC redistribution plans - seeds {a.seeds}, behaviour '{a.behaviour}', a plan every 7 days "
          f"from day 35; horizon 14d, reserve 14d, same-state arcs <= 180 min; donors counted at "
          f"{SHADOW_TRUST} of their shadow stock (register baseline: {DISCOUNT} of the register)")
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
          "  state or country. It barely moves these one-day scores; it stays because such a donor has no resupply\n"
          "  coming (see SHADOW_TRUST).\n"
          "  Not claimed: plans are never applied to the simulated world, so courses/run sums independent weekly\n"
          "  plans that re-plan the same shortages. Maps are SYNTHETIC: minutes are real Google Maps road times\n"
          f"  between their points for seeds {real} (road_minutes.json), straight-line estimates otherwise.")


if __name__ == "__main__":
    main()
