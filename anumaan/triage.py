"""Where did the supply break? Read it off the supply chain's own ledger.

Each district warehouse indents from the state every week (DVDMS records the
indent and what arrived against it). When the state stops filling a warehouse's
indents for a drug, the break is at or above that warehouse, even while its
30-90 days of buffer still hide it from the PHCs. One warehouse starved is a
WAREHOUSE failure; a third of a state's warehouses (and at least 2) is STATE-PROCUREMENT;
that in each of 2+ states is NATIONAL. If supply kept flowing, the alarm is a DEMAND-SURGE when the state's
diagnoses for the drug run well above its warm-up (the fix is a bigger indent, not
an audit), else LOCAL: something at the PHC itself.
"""
from collections import Counter

import numpy as np

LEVELS = ("LOCAL", "WAREHOUSE", "STATE-PROCUREMENT", "NATIONAL", "DEMAND-SURGE")
ACTION = {"LOCAL": "redistribute from nearby calm PHCs",
          "WAREHOUSE": "redistribute from other warehouses; audit this warehouse",
          "STATE-PROCUREMENT": "escalate to the state medical services corporation; cross-state loan or rate-contract piggyback",
          "NATIONAL": "escalate to MoHFW; redistribution cannot fix a national shortage",
          "DEMAND-SURGE": "raise indents for the surge; demand jumped while deliveries kept coming"}
# All grid-searched on seeds 0-4 for per-cause accuracy 7 days after the alarm. The surface is
# flat (the top ten settings within 0.02); shorter windows raise the national flag about a week
# sooner for a little less accuracy.
STARVED = 0.3     # a warehouse got under 30% of what it indented...
LAG, WIN = 10, 28  # ...over the indents placed 10-38 days ago (receipts post up to 10 days late)
LOOK = 7          # a warehouse starved up to a week before an alarm still explains it
SURGE, SURGE_DAYS = 1.2, 7   # the state's expected courses over 7 days, 20%+ over its warm-up


def wide(n_starved, n_warehouses):
    """Has a state's own supply broken? At least 2 of its warehouses starved, and at least a
    third of them. For the 3-warehouse states the constants above were tuned on this is the
    same "2 or more". A lost ledger posting can starve a warehouse by chance, so with 14
    warehouses "2 or more" called 116 ordinary stock-outs state or national on seeds 0-4
    (accuracy 0.82); a third calls 18 (0.88) and misses 2 more real ones. A half misses 9 more."""
    return n_starved >= max(2, -(-n_warehouses // 3))


def fill_rate(asked, got, posted, lag=LAG, win=WIN):
    """[T, W, D]: share of what each warehouse indented on days [t-lag-win, t-lag) that its
    ledger had posted as received by day t; NaN where it indented nothing.
    asked / got / posted: sim.Run.wh_asked / wh_got / wh_posted."""
    fill = np.full(asked.shape, np.nan)
    for t in range(lag + 1, len(asked)):
        lo, hi = max(t - lag - win, 0), t - lag
        a = asked[lo:hi].sum(0)
        fill[t] = np.where(a > 0, (got[lo:hi] * (posted[lo:hi] <= t)).sum(0) / np.maximum(a, 1e-9), np.nan)
    return fill


def starved(fill, lo, hi):
    """[W, D] bool: warehouses whose fill rate fell under STARVED on any day in [lo, hi]."""
    return (np.nan_to_num(fill[max(lo, 0):hi + 1], nan=1.0) < STARVED).any(0)


def surge_lift(N, st, warmup=30, k=SURGE_DAYS):
    """[T, F, D]: expected courses (diagnoses x CRG) over the last k days, pooled over each
    PHC's state, against the state's warm-up mean. N: [T, F, D] from crg.aggregate."""
    st, lift = np.asarray(st), np.empty(N.shape)
    for s in set(st.tolist()):
        c = np.cumsum(N[:, st == s].sum(1), 0)                       # [T, D]
        recent = (c - np.vstack([np.zeros((k, c.shape[1])), c[:-k]])) / k
        lift[:, st == s] = (recent / np.maximum(c[warmup - 1] / warmup, 1e-9))[:, None]
    return lift


def classify(onsets, wh, st, fill, lift, asof):
    """Label each alarm onset (fac, drug, day) from the ledger and diagnoses up to day asof
    (one day for all, or one per onset; at least the day after the onset, when an alarm is
    confirmed). wh / st: warehouse / state id per facility; fill: fill_rate, warehouses in
    sorted order; lift: surge_lift."""
    whs = sorted(set(wh))
    w_of, st_of = [whs.index(w) for w in wh], [dict(zip(wh, st))[w] for w in whs]
    last, n_wh = len(fill) - 1, Counter(st_of)
    labels = []
    for (f, d, t), day in zip(onsets, np.broadcast_to(asof, (len(onsets),))):
        hit = np.nonzero(starved(fill, t - LOOK, min(day, last))[:, d])[0]
        short = {s for s, n in Counter(st_of[w] for w in hit).items() if wide(n, n_wh[s])}
        if len(short) >= 2:
            labels.append("NATIONAL")
        elif st[f] in short:
            labels.append("STATE-PROCUREMENT")
        elif w_of[f] in hit:
            labels.append("WAREHOUSE")
        elif lift[min(t + 1, last), f, d] >= SURGE:
            labels.append("DEMAND-SURGE")
        else:
            labels.append("LOCAL")
    return labels
