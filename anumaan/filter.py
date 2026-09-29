"""Per facility x drug input-output HMM over the supply regime {OK, SCARCE, OUT}.

Evidence each day comes from the CRG-decoded care record: of the courses the
diagnoses say were needed, how many were given in full, rationed, swapped for a
guideline substitute, recorded not-available, or silently missing. The stock
register is one more sensor, mixed with an uninformative component because
registers drift (trust tau). Warehouse receipts drive the transitions.
"""
import numpy as np

REGIMES = ("OK", "SCARCE", "OUT")

# P(outcome of an expected course | regime): full, ration, sub, na, missing
EMIT = np.array([[.88, .04, .01, .005, .065],
                 [.40, .40, .05, .03, .12],
                 [.01, .01, .40, .15, .43]])
# P(register cover bin | regime) for an honest register; bins in days of use
BOOK_EDGES = (0.5, 5, 15)
BOOK_HONEST = np.array([[.01, .09, .30, .60],
                        [.10, .60, .25, .05],
                        [.80, .15, .04, .01]])
# Belief right after a pharmacist's shelf check. It replaces that day's belief rather
# than adding to it: one look at the shelf outweighs a day of indirect evidence.
CONFIRM = {"empty": np.array([.02, .18, .80]), "available": np.array([.75, .23, .02])}
T_NONE = np.array([[.97, .025, .005], [.03, .90, .07], [.01, .02, .97]])
T_RECEIPT = np.array([[.99, .01, 0], [.85, .15, 0], [.80, .10, .10]])


def trailing_mean(x, k=14):
    return np.convolve(x, np.ones(k))[:len(x)] / np.minimum(np.arange(1, len(x) + 1), k)


def entry_gaps(given, expected, frac=0.25):
    """Days a facility entered almost no slips across ALL medicines while its diagnoses
    say care happened. That is a data-entry gap, not a stock-out: stock-outs hit single
    medicines. given / expected: [T] totals over the facility's medicines."""
    return given < frac * expected


def filter_series(N, cats, book, exp_units, receipts, skip=None, tau=None, warmup=30, confirm=None, rho=None):
    """Forward-filter one facility x drug.

    N: expected courses/day from diagnoses; cats: [T, 4] full/ration/sub/na counts;
    book: register balance; exp_units: expected units/day; receipts: units received;
    skip: [T] bool, days whose slips are missing (see entry_gaps) - no care evidence;
    tau: trust in the register, learned from the warm-up window when None;
    confirm: {day: "empty" | "available"} shelf checks by the pharmacist;
    rho: local prescribing rate vs the guideline, learned from the warm-up window when
    None (a new facility can take the national prior instead - see federation).
    Returns the regime posterior [T, 3].
    """
    ok = np.ones(len(N), bool) if skip is None else ~skip
    if rho is None:   # learned on the warm-up window; the 1.2 cap bounds the damage when
        # diagnosis capture was patchier during warm-up than after
        rho = np.clip(cats[:warmup][ok[:warmup]].sum() / max(N[:warmup][ok[:warmup]].sum(), 1e-9), 0.3, 1.2)
    if tau is None:   # how well the register's daily drawdown tracked CRG-implied use
        drawn = receipts[1:warmup] - np.diff(book[:warmup])
        use_w = (rho * exp_units)[1:warmup]
        tau = float(np.clip(1 - np.abs(drawn - use_w).sum() / max(use_w.sum(), 1e-9), 0.05, 0.8))
    need = rho * N
    counts = np.column_stack([cats, np.maximum(need - cats.sum(1), 0)]) * ok[:, None]
    ll = counts @ np.log(EMIT).T

    use = np.maximum(trailing_mean(rho * exp_units), 1e-9)
    z = np.digitize(book / use, BOOK_EDGES)
    ll += np.log(tau * BOOK_HONEST[:, z].T + (1 - tau) * 0.25)
    confirm = confirm or {}

    got = receipts > 3 * use
    post = np.empty((len(N), 3))
    a = np.array([.9, .08, .02])
    for t in range(len(N)):
        a = a @ (T_RECEIPT if got[t] else T_NONE)
        a = a * np.exp(ll[t] - ll[t].max())
        a /= a.sum()
        if t in confirm:     # later evidence can still move it, e.g. if the check was wrong
            a = CONFIRM[confirm[t]].copy()
        post[t] = a
    return post


def segments(mask, min_len=2):
    """[start, end) runs of True lasting at least min_len days."""
    runs, t = [], 0
    while t < len(mask):
        if mask[t]:
            s = t
            while t < len(mask) and mask[t]:
                t += 1
            if t - s >= min_len:
                runs.append((s, t))
        else:
            t += 1
    return runs


def alarms(post, thr=0.7):
    """Anticipated-scarcity alarms: P(SCARCE or OUT) >= thr for 2+ days."""
    return segments(post[:, 1] + post[:, 2] >= thr)
