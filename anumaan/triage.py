"""Where did the supply break? Cluster alarm onsets up the supply tree.

An onset at one PHC is LOCAL. Several PHCs under one warehouse going short on the
same drug in the same weeks is a WAREHOUSE failure. Several warehouses in one
state is STATE-PROCUREMENT; hot warehouses in more than one state is NATIONAL.
If the diagnoses show demand jumped while deliveries kept arriving, it is a
DEMAND-SURGE, whatever its spread: the fix is a bigger indent, not an audit.
"""
from collections import Counter

LEVELS = ("LOCAL", "WAREHOUSE", "STATE-PROCUREMENT", "NATIONAL", "DEMAND-SURGE")
ACTION = {"LOCAL": "redistribute from nearby calm PHCs",
          "WAREHOUSE": "redistribute from other warehouses; audit this warehouse",
          "STATE-PROCUREMENT": "escalate to the state medical services corporation; cross-state loan or rate-contract piggyback",
          "NATIONAL": "escalate to MoHFW; redistribution cannot fix a national shortage",
          "DEMAND-SURGE": "raise indents for the surge; demand jumped while deliveries kept coming"}


def demand_led(N, receipts, t, warmup=30, lift=1.25, kept=0.5):
    """Did demand for this drug jump while deliveries kept arriving, as of day t?
    N: expected courses/day from diagnoses; receipts: units received per day."""
    base_n = N[:warmup].mean()
    base_r = receipts[:warmup].sum() / warmup
    recent_n = N[max(t - 14, 0):t + 1].mean()
    recent_r = receipts[max(t - 30, 0):t + 1].sum() / min(t + 1, 30)
    return bool(recent_n > lift * base_n and recent_r >= kept * base_r)


def classify(onsets, wh, st, window=21, settle=7, min_hot=3, surge=frozenset()):
    # window / min_hot / demand_led lift were grid-searched on seeds 0-4 (per-cause accuracy);
    # adding warehouse fill-rate as extra evidence gave no gain, so it is left out
    """Label each onset (fac, drug, day) using only onsets confirmed by day + settle.

    wh / st: warehouse / state id per facility index. A warehouse is "hot" when at
    least min_hot of its PHCs went short on the drug inside the window. surge: the
    onsets that demand_led() flagged.
    """
    state_of = dict(zip(wh, st))
    labels = []
    for f, d, t in onsets:
        if (f, d, t) in surge:
            labels.append("DEMAND-SURGE")
            continue
        # an onset at t2 is only confirmed on day t2 + 1 (alarms need 2 days)
        peers = {g for g, d2, t2 in onsets if d2 == d and t - window <= t2 < t + settle}
        hit = Counter(wh[g] for g in peers)
        hot = {w for w, n in hit.items() if n >= min_hot}
        states = Counter(state_of[w] for w in hot)
        if sum(n >= 2 for n in states.values()) >= 2:
            labels.append("NATIONAL")
        elif states.get(st[f], 0) >= 2:
            labels.append("STATE-PROCUREMENT")
        elif wh[f] in hot:
            labels.append("WAREHOUSE")
        else:
            labels.append("LOCAL")
    return labels
