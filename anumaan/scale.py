"""Load test: one large SYNTHETIC state end to end, then the arithmetic for all of India.

    python -m anumaan.scale [--phcs 1500 --districts 50 --days 200]

Times the nightly work for every PHC x medicine series (learn the register's trust, run
the filter, rebuild the shadow stock), the day's "where it broke" labels and one day's
OR-Tools plan, on one core. The filter is per series, so it scales linearly and shards by
state; the plan runs per state. India had 31,882 PHCs on 31 March 2023 (MoHFW, Health
Dynamics of India 2022-23).
"""
import argparse
import time

import numpy as np

from anumaan import crg as G, filter as FL, planner as PL, sim

INDIA_PHCS = 31_882
USD_PER_VCPU_S = 0.000018      # Cloud Run instance-based CPU, Tier 1 list price; Tier 2 costs more


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phcs", type=int, default=1500)
    ap.add_argument("--districts", type=int, default=50)
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--medicines", type=int, default=25, help="tracer medicines assumed for the national estimate")
    a = ap.parse_args()
    t0 = time.perf_counter()
    run = sim.simulate(seed=5, days=a.days, n_states=1, n_wh=a.districts, n_phc=a.phcs // a.districts)
    obs = G.aggregate(run.ix, run.dx, run.slips, run.na)
    P, F = run.ix["primaries"], len(run.facilities)
    given = sum(obs[k][:, :, P].sum(2) for k in G.CATS)
    post, cover = {}, {}
    t1 = time.perf_counter()
    for f in range(F):
        skip = FL.entry_gaps(given[:, f], obs["N"][:, f, P].sum(1))
        for d in P:
            cats = np.stack([obs[k][:, f, d] for k in G.CATS], 1)
            N, rec, book, exp = obs["N"][:, f, d], run.receipts[:, f, d], run.book[:, f, d], obs["exp_units"][:, f, d]
            rho, tau = FL.learn(N, cats, book, exp, rec, skip)
            post[f, d] = p = FL.filter_series(N, cats, book, exp, rec, skip, tau=tau, rho=rho)
            cover[f, d] = FL.shadow_cover(book, rec, obs["units"][:, f, d], FL.trailing_mean(rho * exp), p, skip, None)
    t2 = time.perf_counter()
    t = a.days - 1
    labels = PL.labels_at(t, run, post, obs, cover)
    t3 = time.perf_counter()
    plan = PL.plan(t, run, post, labels, PL.synthetic_coords(run, 5), obs=obs, cover=cover)
    t4 = time.perf_counter()

    S = F * len(P)
    per_series = (t2 - t1) / S
    national = INDIA_PHCS * a.medicines * per_series
    print(f"SYNTHETIC load test - one state: {F} PHCs in {a.districts} districts x {len(P)} medicines = {S:,} series, "
          f"{a.days} days, one core")
    print(f"  simulate the feeds (not part of production) {t1 - t0:.0f} s")
    print(f"  learn + filter + shadow stock, every series  {t2 - t1:.1f} s = {1000 * per_series:.2f} ms per series")
    print(f"  where it broke, the day's labels            {t3 - t2:.1f} s")
    print(f"  OR-Tools plan for the whole state, one day   {t4 - t3:.1f} s: {len(plan['transfers'])} transfers, "
          f"{len(plan['escalations'])} escalations")
    print(f"India: {INDIA_PHCS:,} PHCs x {a.medicines} tracer medicines = {INDIA_PHCS * a.medicines:,} series -> "
          f"{national / 60:.0f} vCPU-minutes a night for the full {a.days}-day refilter, about "
          f"${national * USD_PER_VCPU_S * 30:.2f} a month of Cloud Run CPU at the Tier 1 list price")


if __name__ == "__main__":
    main()
