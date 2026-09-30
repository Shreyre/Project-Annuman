"""Beds (R3) and staff attendance (R4) at each PHC: SYNTHETIC feeds, and what the app
infers from them without ever seeing the truth.

Beds. A PHC has about 6 beds (IPHS). A delivery stays about 48 h (the JSY norm), other
inpatients 1-4 days. When the ward is full the patient nearest their discharge is sent
home early to make room; if everyone came in today, the new patient is referred out.
The ADT feed (like e-Hospital) records every admission but only 80-95% of discharges,
so "admitted minus discharged" drifts up for ever. The count the app shows closes any
stay still open after 5 days and never shows more patients than beds: plain,
explainable, and close enough.

Staff. Four cadres: medical officer (MO), staff nurse (SN), pharmacist (PH), lab
technician (LT). The attendance feed (AEBAS-like) marks a present cadre present 97% of
the time, but some facilities also mark absent staff present (proxy marking). The care
record is harder to fake: MO-signed prescriptions, SN-conducted deliveries, PH
dispensing and LT tests all but stop when that cadre is out. So presence is inferred
from acts *per patient*: a quiet OPD with no prescriptions is not an absent doctor; a
40-patient day with none is. Cadre level only - no individuals, no GPS.

The feeds are drawn from an existing sim.Run on their own RNG stream (seed + 1000), so
the stock simulation and its headline numbers are unchanged.

    python -m anumaan.care --seeds 5-9
"""
import argparse
from dataclasses import dataclass

import numpy as np

from anumaan import sim
from anumaan.evaluate import _range

CADRES = ("MO", "SN", "PH", "LT")
SANCTIONED = np.array([2, 3, 1, 1])   # IPHS 2022 posts for a 24x7 PHC (approximate)
AUTO_CLOSE = 5        # a stay with no recorded discharge is assumed gone after this many days
VERIFY = 0.1          # marked present but P(present | acts) below this on a busy day: ask to verify
BUSY = 3              # a busy day: at least this many cadre acts expected had the cadre been in
# VERIFY and BUSY were chosen on seeds 0-4; report seeds 5-9
WARMUP = 30
AUDIT_MIN = 20        # a PHC's marking habit is not rated on fewer days than this with a cadre evidently away


@dataclass
class Care:
    facilities: list
    capacity: np.ndarray     # [fac] beds
    admits: list             # (day, fac, stay, kind) ADT admissions, all recorded
    discharges: list         # (day, fac, stay, early) ADT discharges, only those recorded
    in_position: np.ndarray  # [fac, cadre] posts filled (HR records)
    marked: np.ndarray       # [day, fac, cadre] attendance feed: cadre marked present
    exposure: np.ndarray     # [day, fac, cadre] chances for a cadre-only act: OPD footfall, deliveries for SN
    acts: np.ndarray         # [day, fac, cadre] cadre-only acts in the care record
    occupancy: np.ndarray    # GROUND TRUTH [day, fac] beds occupied at the end of the day
    present: np.ndarray      # GROUND TRUTH [day, fac, cadre] anyone of the cadre at work


def simulate(run, seed=0):
    """SYNTHETIC bed and staff feeds for run's facilities. Facility size comes from its
    diagnosis footfall in run.dx; nothing else of the run is used."""
    rng = np.random.default_rng(seed + 1000)
    T, F = run.dx.shape[:2]
    K = len(CADRES)
    footfall = run.dx.sum(2)
    size = footfall.mean(0) / sum(sim.BASE_RATE.values())
    cap = rng.integers(4, 7, F)
    p_rec = rng.uniform(0.8, 0.95, F)

    # --- beds ---
    occ, deliveries = np.zeros((T, F), int), np.zeros((T, F), int)
    admits, discharges = [], []
    ward = [[] for _ in range(F)]        # (stay, admitted, due) per bed in use
    for t in range(T):
        for f in range(F):
            def leave(bed, early):
                ward[f].remove(bed)
                if rng.random() < p_rec[f]:
                    discharges.append((t, f, bed[0], early))
            for bed in [b for b in ward[f] if b[2] == t]:
                leave(bed, False)
            n_del, n_ip = rng.poisson(size[f] * np.array([0.7, 0.5]))
            for kind in rng.permutation(["delivery"] * n_del + ["inpatient"] * n_ip):
                if len(ward[f]) >= cap[f]:
                    older = [b for b in ward[f] if b[1] < t]
                    if not older:
                        continue                       # referred out: never admitted here
                    leave(min(older, key=lambda b: b[2]), True)
                admits.append((t, f, len(admits), str(kind)))
                ward[f].append((len(admits) - 1, t, t + (2 if kind == "delivery" else int(rng.integers(1, 5)))))
                deliveries[t, f] += kind == "delivery"
            occ[t, f] = len(ward[f])

    # --- staff ---
    in_pos = np.stack([rng.integers(1, 3, F), rng.integers(1, 4, F),
                       rng.random(F) > 0.2, rng.random(F) > 0.3], 1).astype(int)
    absence = rng.uniform([0.25, 0.1, 0.1, 0.1], [0.4, 0.25, 0.3, 0.3], (F, K))   # MO highest
    present = (in_pos > 0) & (rng.random((T, F, K)) > absence)
    ghost = rng.uniform(0, 0.5, F)       # proxy-marking habit of the facility
    marked = np.where(present, rng.random((T, F, K)) < 0.97,
                      (in_pos > 0) & (rng.random((T, F, K)) < ghost[:, None]))
    exposure = np.repeat(footfall[:, :, None], K, 2)
    exposure[:, :, 1] = deliveries
    q = rng.uniform([0.6, 0.8, 0.7, 0.15], [0.95, 1.0, 0.95, 0.35], (F, K))       # acts per patient
    # a present cadre does 60-100% of its usual share (half days, meetings); others cover 0-10%
    rate = q * np.where(present, rng.uniform(0.6, 1.0, (T, F, K)), rng.uniform(0, 0.1, (T, F, K)))
    acts = rng.binomial(exposure, rate)
    return Care(list(run.facilities), cap, admits, discharges, in_pos, marked, exposure, acts, occ, present)


# ---------- inference: feeds only, never the GROUND TRUTH fields ----------

def census(care, auto_close=AUTO_CLOSE):
    """Plain ADT count [day, fac] at the end of each day: admitted minus recorded
    discharges, any stay still open after auto_close days closed (None: the naive count)."""
    T = care.marked.shape[0]
    n = np.zeros((T + 1, len(care.facilities)))
    out = {(f, s): t for t, f, s, _ in care.discharges}      # a stay id is only unique within its PHC
    for t, f, s, _ in care.admits:
        n[t, f] += 1
        n[min(out.get((f, s), T), t + auto_close if auto_close else T), f] -= 1
    return n.cumsum(0)[:T]


def beds(care):
    """Every day at once: occupied [day, fac] (plain census, capped at capacity: a ward
    cannot hold more than its beds, overflow is referred out), early [day, fac] share of the
    last 7 days' recorded discharges flagged early (nan if none), pressure [day, fac]: the
    count says every bed is taken. Folding the early share into the flag cost precision
    for no recall on seeds 0-4, so it is shown beside the count, not used in the flag."""
    T, F = care.marked.shape[:2]
    n, early = np.zeros((T, F)), np.zeros((T, F))
    for t, f, _, e in care.discharges:
        n[t, f] += 1
        early[t, f] += e
    week = lambda x: x.cumsum(0) - np.vstack([np.zeros((7, F)), x.cumsum(0)[:-7]])
    share = week(early) / np.where(week(n) > 0, week(n), np.nan)
    occupied = np.minimum(census(care), care.capacity)
    return dict(occupied=occupied, early=share, pressure=occupied >= care.capacity)


def beds_view(care, t):
    b = beds(care)
    return [dict(fac=fid, capacity=int(care.capacity[f]), occupied=int(b["occupied"][t, f]),
                 free=int(care.capacity[f] - b["occupied"][t, f]),
                 early_share_7d=None if np.isnan(b["early"][t, f]) else round(float(b["early"][t, f]), 2),
                 pressure=bool(b["pressure"][t, f]))
            for f, fid in enumerate(care.facilities)]


def _post(a, n, pi, q1, q0):
    l1 = np.log(pi) + a * np.log(q1) + (n - a) * np.log1p(-q1)
    l0 = np.log1p(-pi) + a * np.log(q0) + (n - a) * np.log1p(-q0)
    return 1 / (1 + np.exp(np.clip(l0 - l1, -50, 50)))


def staff(care, warmup=WARMUP, iters=50):
    """Every day at once: p_present [day, fac, cadre], expected [day, fac, cadre] acts had
    the cadre been in, and verify: marked present, but no acts on a busy day.

    Acts ~ Binomial(patients, q1) when the cadre is in, Binomial(patients, q0) when others
    cover; q1, q0 and the share of days in are fitted per facility x cadre by EM on the
    warm-up window. A day without patients carries no evidence and stays at that share;
    a cadre with nobody in post is never present."""
    # ponytail: rates fixed after warm-up; refit on a rolling window if staff turn over
    a, n = care.acts.astype(float), care.exposure.astype(float)
    aw, nw = a[:warmup], n[:warmup]
    q1 = np.clip(aw.sum(0) / np.maximum(nw.sum(0), 1), 0.02, 0.98)
    q0, pi = q1 / 10, np.full(q1.shape, 0.8)
    for _ in range(iters):
        r = _post(aw, nw, pi, q1, q0)
        pi = np.clip(r.mean(0), 0.05, 0.99)
        q1 = np.clip((r * aw).sum(0) / np.maximum((r * nw).sum(0), 1e-9), 0.01, 0.99)
        q0 = np.clip(((1 - r) * aw).sum(0) / np.maximum(((1 - r) * nw).sum(0), 1e-9), 1e-3, q1 / 2)
    p = _post(a, n, pi, q1, q0) * (care.in_position > 0)
    expected = n * q1
    # SN rarely has a busy day (about one delivery a day), so it is rarely flagged: too little evidence
    return dict(p_present=p, expected=expected, verify=care.marked & (p < VERIFY) & (expected >= BUSY))


def proxy_marking(care, lo=WARMUP, hi=None, s=None):
    """Each PHC's habit of marking absent staff present, from the feeds alone. Over days
    [lo, hi): (n, k), each [fac]. n: cadre-days on which the care record shows the cadre away
    (a busy day with P(present | acts) under VERIFY); k: how many of those the attendance feed
    marked present. k / n estimates the PHC's proxy-marking rate. The four cadres are pooled,
    because the habit is the facility's; nothing here is tuned. s: staff(care) if already computed."""
    s = s or staff(care)
    away = (s["p_present"] < VERIFY) & (s["expected"] >= BUSY) & (care.in_position > 0)
    return away[lo:hi].sum((0, 2)), (away & care.marked)[lo:hi].sum((0, 2))


def staff_view(care, t):
    s = staff(care)
    return [dict(fac=fid, cadre=k, sanctioned=int(SANCTIONED[j]), in_position=int(care.in_position[f, j]),
                 marked_present=bool(care.marked[t, f, j]), p_present=round(float(s["p_present"][t, f, j]), 3),
                 acts=int(care.acts[t, f, j]), expected=round(float(s["expected"][t, f, j]), 1),
                 verify=bool(s["verify"][t, f, j]))
            for f, fid in enumerate(care.facilities) for j, k in enumerate(CADRES)]


# ---------- evaluation: the only place GROUND TRUTH is read ----------

def _pr(pred, truth):
    hit = (pred & truth).sum()
    return dict(precision=hit / max(pred.sum(), 1), recall=hit / max(truth.sum(), 1))


def evaluate_care(care):
    b, flag = beds(care), staff(care)["verify"]
    occ, full = care.occupancy, care.occupancy >= care.capacity          # GROUND TRUTH
    naive = np.minimum(census(care, None), care.capacity)                  # capped too, so the comparison is fair
    absent = ~care.present & (care.in_position > 0)[None]                 # GROUND TRUTH
    ghost = care.marked & absent
    zero = care.marked & (care.acts == 0)            # strawman: no acts, whatever the footfall
    # fair baseline, no model: usual acts per patient from the warm-up x today's patients (0.3 not tuned)
    e = care.exposure * care.acts[:WARMUP].sum(0) / np.maximum(care.exposure[:WARMUP].sum(0), 1)
    ratio = care.marked & (e >= BUSY) & (care.acts <= 0.3 * e)
    # each PHC's proxy-marking rate against the truth: does the estimate put the PHCs in the right order?
    rate = lambda n, k: k / np.maximum(n, 1)
    habit = rate(absent[WARMUP:].sum((0, 2)), ghost[WARMUP:].sum((0, 2)))
    order = lambda x: float(np.corrcoef(np.argsort(np.argsort(x)), np.argsort(np.argsort(habit)))[0, 1])
    away_r = (e >= BUSY) & (care.acts <= 0.3 * e) & (care.in_position > 0)
    est = rate(*proxy_marking(care))
    audit = dict(rank=order(est), mae=float(np.abs(est - habit).mean()), rank_feed=order(care.marked[WARMUP:].mean((0, 2))),
                 rank_ratio=order(rate(away_r[WARMUP:].sum((0, 2)), (away_r & care.marked)[WARMUP:].sum((0, 2)))))
    return dict(audit=audit, mae_naive=float(np.abs(naive - occ).mean()), mae_auto=float(np.abs(b["occupied"] - occ).mean()),
                full_rate=float(full.mean()), full_naive=_pr(naive >= care.capacity, full),
                full_auto=_pr(b["pressure"], full),
                absences=int(absent.sum()), feed_missed=float(ghost.sum() / max(absent.sum(), 1)),
                ghost=_pr(flag, ghost), ghost_zero=_pr(zero, ghost), ghost_ratio=_pr(ratio, ghost),
                ghost_by_cadre={k: _pr(flag[..., j], ghost[..., j]) for j, k in enumerate(CADRES)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="5-9", help="held-out seeds, e.g. 5-9 or 0,3")
    a = ap.parse_args()
    lo, _, hi = a.seeds.partition("-")
    seeds = range(int(lo), int(hi) + 1) if hi else [int(s) for s in a.seeds.split(",")]
    rs = [evaluate_care(simulate(sim.simulate(seed=s), s)) for s in seeds]
    pr = lambda key: (f"{_range([r[key]['precision'] for r in rs])} / {_range([r[key]['recall'] for r in rs])}")

    print(f"SYNTHETIC care evaluation (beds R3, attendance R4) - seeds {a.seeds}")
    print(f"beds  census MAE vs truth (both capped at capacity): naive {_range([r['mae_naive'] for r in rs])} beds, "
          f"auto-close {AUTO_CLOSE}d {_range([r['mae_auto'] for r in rs])} beds")
    print(f"      bed-full days ({_range([r['full_rate'] for r in rs], '{:.0%}')} of facility-days), precision / recall:")
    print(f"        naive count >= capacity          {pr('full_naive')}")
    print(f"        auto-close count >= capacity     {pr('full_auto')}")
    print(f"staff attendance feed marks present on {_range([r['feed_missed'] for r in rs], '{:.0%}')} of true absences "
          f"({_range([r['absences'] for r in rs], '{:.0f}')} absent cadre-days per run) - the feed alone misses these")
    print("      ghost-marking flag, precision / recall:")
    print(f"        marked, zero acts (strawman)     {pr('ghost_zero')}")
    print(f"        marked, busy, acts < 0.3x usual  {pr('ghost_ratio')}   <- fair baseline, no model")
    print(f"        marked, busy, EM p_present < {VERIFY} {pr('ghost')}")
    print("        by cadre: " + ", ".join(
        f"{k} {_range([r['ghost_by_cadre'][k]['precision'] for r in rs])} / {_range([r['ghost_by_cadre'][k]['recall'] for r in rs])}"
        for k in CADRES))
    au = lambda key: _range([r["audit"][key] for r in rs])
    print("      which PHCs mark absent staff present: each PHC's share of evident absences marked present, against its\n"
          f"      true proxy-marking rate. Rank correlation over the PHCs {au('rank')} (off by {_range([r['audit']['mae'] for r in rs], '{:.3f}')} on average);\n"
          f"      the no-model ratio rule {au('rank_ratio')}; the attendance feed alone (share of days marked present) {au('rank_feed')}")


if __name__ == "__main__":
    main()
