"""Per facility x drug input-output HMM over the supply regime {OK, SCARCE, OUT}.

Evidence each day comes from the CRG-decoded care record: of the courses the
diagnoses say were needed, how many were given in full, rationed, swapped for a
guideline substitute, recorded not-available, or silently missing. The stock
register is one more sensor, mixed with an uninformative component because
registers drift (trust tau). Warehouse receipts drive the transitions.

Care only changes once staff start rationing or the shelf is empty, so the
shadow stock adds a warning that needs neither: what was received minus what
the slips say was dispensed, re-zeroed whenever the care shows the shelf empty.
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
# Shadow-cover alarm under LOW days of use left, re-zeroed after EMPTY_RUN days the care
# shows empty. Chosen on seeds 0-4: the most early warnings, averaged over the default, alt
# and never-ration behaviours, keeping every run at <= 0.1 false alarms per series-year.
LOW, EMPTY_RUN = 8, 2


def trailing_mean(x, k=14):
    return np.convolve(x, np.ones(k))[:len(x)] / np.minimum(np.arange(1, len(x) + 1), k)


def entry_gaps(given, expected, frac=0.25):
    """Days a facility entered almost no slips across ALL medicines while its diagnoses
    say care happened. That is a data-entry gap, not a stock-out: stock-outs hit single
    medicines. given / expected: [T] totals over the facility's medicines."""
    return given < frac * expected


def learn(N, cats, book, exp_units, receipts, skip=None, warmup=30, rho=None):
    """rho (local prescribing rate vs the guideline) and tau (trust in the register), from the
    warm-up window. A given rho (e.g. a national prior) is kept and tau learned against it."""
    ok = np.ones(len(N), bool) if skip is None else ~skip
    if rho is None:   # the 1.2 cap bounds the damage when diagnosis capture was patchier during warm-up than after
        rho = float(np.clip(cats[:warmup][ok[:warmup]].sum() / max(N[:warmup][ok[:warmup]].sum(), 1e-9), 0.3, 1.2))
    drawn = receipts[1:warmup] - np.diff(book[:warmup])   # how well the register's drawdown tracked CRG-implied use
    use_w = (rho * exp_units)[1:warmup]
    return rho, float(np.clip(1 - np.abs(drawn - use_w).sum() / max(use_w.sum(), 1e-9), 0.05, 0.8))


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
    if rho is None or tau is None:
        rho, learned = learn(N, cats, book, exp_units, receipts, skip, warmup, rho)
        tau = learned if tau is None else tau
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


def shadow_cover(book, receipts, units, use, post, skip=None, confirm=None, thr=0.9, run=EMPTY_RUN):
    """Days of use left on the shelf, rebuilt without the register's posting: its first
    balance, plus receipts, minus the units on the dispensing slips (entry-gap days imputed
    at `use`), never below zero. Registers post issues late or never; the slips are the
    issues. Re-zeroed at the start of every spell the care shows empty (P(OUT) >= thr for
    `run` days) and at a pharmacist's "empty", which also clears drift.
    ponytail: the first balance is the register's, so an overstated opening delays the first
    warning; a shelf count at go-live is the fix.
    units: dispensed units/day from the slips; use: expected units/day (e.g. trailing
    rho x exp_units); post: filter_series output. Returns [T] days of cover."""
    out = units if skip is None else np.where(skip, use, units)
    confirm = confirm or {}
    cover, x, n = np.empty(len(book)), float(book[0]), 0
    for t in range(len(book)):
        if t:
            x = max(x + receipts[t] - out[t], 0.0)
        n = n + 1 if post[t, 2] >= thr else 0
        if n == run:                       # empty since day t - run + 1: replay from zero
            x = 0.0
            for s in range(t - run + 2, t + 1):
                x = max(x + receipts[s] - out[s], 0.0)
        if confirm.get(t) == "empty":
            x = 0.0
        cover[t] = x / max(use[t], 1e-9)
    return cover


def alarms(post, thr=0.7, cover=None, low=LOW):
    """Alarm spells of 2+ days: P(SCARCE or OUT) >= thr, or (given shadow_cover) under
    `low` days of use left."""
    short = post[:, 1] + post[:, 2] >= thr
    if cover is not None:
        short |= cover < low
    return segments(short)
