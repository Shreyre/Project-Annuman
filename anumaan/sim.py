"""SYNTHETIC PHC network with injected supply failures (the ground truth).

Mimics the shape of real feeds - diagnoses, dispensing slips, not-available
records, the stock register ("book") and warehouse issues - so the pipeline can
be tested end to end. Nothing here is real data.

The world is deliberately messy: routine indents are part-filled, upstream
failures still let a trickle through, warehouses hold 30-90 days, a monsoon
surge lifts fever and diarrhoea, and some days' slips are never entered.

Each district warehouse also keeps a DVDMS-style ledger: its weekly indent to
the state and what arrived against it, posted 0-10 days late and 5% never.
"""
from dataclasses import dataclass

import numpy as np

from anumaan import crg as G
from anumaan.filter import segments

# Diagnoses per day at a mid-size PHC (each facility gets a 0.5-1.5x size factor).
BASE_RATE = {"fever": 12, "pneumonia": 3, "acute_diarrhoea": 4, "hypertension": 6,
             "type2_diabetes": 4, "uti": 2, "anaemia_pregnancy": 2}
SURGE_CONDS = ("fever", "acute_diarrhoea")

# Frontline behaviour, drawn per facility. "alt" differs in form (ramp instead of
# step rationing) and values from the filter's emission table, and records no
# not-available slips at all (DVDMS has no such field), so evaluating on it
# checks the model is not just decoding its own assumptions.
BEHAVIOUR = {
    "default": dict(ramp=False, ration_cover=(4, 12), p_ration=(0.4, 0.8), p_sub=(0.5, 0.9),
                    p_na=(0.2, 0.5), bias=(0.8, 1.05), p_post=(0.5, 0.95)),
    "alt": dict(ramp=True, ration_cover=(2, 20), p_ration=(0.2, 0.6), p_sub=(0.2, 0.5),
                p_na=(0.0, 0.0), bias=(0.6, 1.2), p_post=(0.3, 0.98)),
}
TYPES = ("LOCAL", "WAREHOUSE", "STATE-PROCUREMENT", "NATIONAL", "DEMAND-SURGE")


@dataclass
class Run:
    ix: dict
    facilities: list     # facility ids
    wh: list             # warehouse id per facility
    st: list             # state id per facility
    dx: np.ndarray       # [day, fac, cond] diagnoses as entered
    slips: list          # (day, fac, cond, drug, days_of_therapy, units) as entered
    na: list             # (day, fac, cond, drug) not-available records
    book: np.ndarray     # [day, fac, drug] stock register, end of day
    receipts: np.ndarray  # [day, fac, drug] units received from the warehouse
    wh_asked: np.ndarray  # [day, warehouse, drug] units each warehouse indented from the state (weekly)
    wh_got: np.ndarray    # [day, warehouse, drug] units the state sent against that indent
    wh_posted: np.ndarray  # [day, warehouse, drug] day the ledger posted that receipt (days = never)
    true_stock: np.ndarray  # GROUND TRUTH [day, fac, drug] - never given to the model
    rate: np.ndarray     # GROUND TRUTH [fac, drug] baseline units/day (what indents are sized on)
    true_use: np.ndarray  # GROUND TRUTH [day, fac, drug] expected units/day incl. the surge
    ration_cover: np.ndarray  # GROUND TRUTH [fac] days of cover below which staff start rationing
    episodes: list       # GROUND TRUTH injected failures


def simulate(seed=0, days=200, n_states=2, n_wh=3, n_phc=6, behaviour="default", crg=None,
             fill=(0.5, 1.0), wh_days=(30, 90), short=(0.0, 0.25), surge=0.5, gap_rate=0.01,
             p_ration=None, episodes=None, surges=None):
    """fill: share of a routine indent the warehouse actually sends; wh_days: warehouse
    buffer in days of network use; short: share of supply that still flows during an
    upstream failure; surge: peak monsoon lift of fever/diarrhoea; gap_rate: chance a
    facility starts a 1-5 day data-entry gap on any day; p_ration: override rationing;
    episodes: script the failures instead of drawing them (a scenario replay): dicts of
    type, root ("IN", a state, warehouse or facility id), drug id, start, dur and
    optionally short. The default draws are untouched, so every seed's world is unchanged.
    surges: scripted health emergencies: dicts of root (a state, warehouse or facility id),
    conds (condition ids), lift, start and dur; those diagnoses, and the use they call for,
    are multiplied by lift. None changes nothing, not even the random draws."""
    rng = np.random.default_rng(seed)
    ix = G.index(crg or G.load())
    B = dict(BEHAVIOUR[behaviour])
    if p_ration is not None:
        B["p_ration"] = (p_ration, p_ration)
    C, D = len(ix["conds"]), len(ix["drugs"])
    fac, wh, st = [], [], []
    for s in range(n_states):
        for w in range(n_wh):
            for p in range(n_phc):
                fac.append(f"S{s}-W{w}-P{p}"), wh.append(f"S{s}-W{w}"), st.append(f"S{s}")
    F = len(fac)
    whs = sorted(set(wh))
    wi = np.array([whs.index(w) for w in wh])
    wst = [w.split("-")[0] for w in whs]
    draw = lambda lo_hi, shape=F: rng.uniform(*lo_hi, shape)
    ration_cover, p_ration_f, p_sub, p_na, p_post = (draw(B[k]) for k in
                                                     ("ration_cover", "p_ration", "p_sub", "p_na", "p_post"))
    bias = draw(B["bias"], (F, D))

    size = rng.uniform(0.5, 1.5, F)
    lam = np.array([BASE_RATE[c] for c in ix["conds"]], float)
    rate = size[:, None] * (lam @ ix["units"])[None, :]
    for (_, s), p in ix["sub_of"].items():          # substitutes stocked near their background use
        rate[:, s] += 0.15 * rate[:, p]

    # --- injected failures: distinct drugs for the upstream ones, varied lengths ---
    if episodes is None:
        prim = [int(d) for d in rng.permutation(ix["primaries"])]
        spec = [("NATIONAL", "IN", (50, 90), (40, 80)),
                ("STATE-PROCUREMENT", f"S{rng.integers(n_states)}", (40, 100), (40, 80)),
                ("WAREHOUSE", str(rng.choice(whs)), (40, 120), (25, 60)),
                ("WAREHOUSE", str(rng.choice(whs)), (40, 120), (25, 60))]
        eps = [dict(type=t, root=r, drug=prim[i], start=int(rng.integers(*s)), dur=int(rng.integers(*l)))
               for i, (t, r, s, l) in enumerate(spec)]
        for _ in range(4):
            eps.append(dict(type="LOCAL", root=fac[rng.integers(F)], drug=prim[4 + rng.integers(len(prim) - 4)],
                            start=int(rng.integers(40, 150)), dur=int(rng.integers(20, 50))))
    else:
        eps = [dict(e, drug=ix["di"][e["drug"]]) for e in episodes]
    for e in eps:
        e["end"] = e["start"] + e["dur"]
        e.setdefault("short", float(rng.uniform(*short)))
    sub_for = {p: s for (_, s), p in ix["sub_of"].items()}
    for e in list(eps):                              # the substitute often fails with it
        if e["type"] != "LOCAL" and e["drug"] in sub_for and rng.random() < 0.5:
            eps.append(dict(e, drug=sub_for[e["drug"]], secondary=True))

    def upstream_share(w, d, t):
        return min([e["short"] for e in eps if e["drug"] == d and e["start"] <= t < e["end"] and
                    (e["type"] == "NATIONAL" or (e["type"] == "STATE-PROCUREMENT" and e["root"] == wst[w])
                     or (e["type"] == "WAREHOUSE" and e["root"] == whs[w]))], default=1.0)

    def local_blocked(f, d, t):
        return any(e["type"] == "LOCAL" and e["root"] == fac[f] and e["drug"] == d
                   and e["start"] <= t < e["end"] for e in eps)

    # --- state ---
    S = rate * rng.uniform(25, 45, (F, D))             # true PHC stock (units)
    target = rate * 45                                 # monthly indent tops up to 45 days
    wtarget = rng.uniform(*wh_days, (len(whs), D)) * np.array([rate[wi == w].sum(0) for w in range(len(whs))])
    W = wtarget.copy()
    off = rng.integers(0, 30, F)
    book = S * rng.uniform(1.0, 1.3, (F, D))           # opening balances already overstated
    back_r, back_i = np.zeros((F, D)), np.zeros((F, D))
    gap_left = np.zeros(F, int)
    surge_idx = [ix["ci"][c] for c in SURGE_CONDS if c in ix["ci"]]
    peak = int(rng.integers(90, 130))

    dx = np.zeros((days, F, C), int)
    BOOK, TRUE, REC, USE = (np.zeros((days, F, D)) for _ in range(4))
    ASKED, GOT = np.zeros((days, len(whs), D)), np.zeros((days, len(whs), D))
    slips, na = [], []

    for t in range(days):
        for e in eps:                                   # a failing warehouse's shelf drops at once
            if e["type"] == "WAREHOUSE" and t == e["start"]:
                W[whs.index(e["root"]), e["drug"]] *= e["short"]
        if t % 7 == 0:                                  # weekly indent to the state, up to target
            for w in range(len(whs)):
                for d in range(D):
                    new = max(W[w, d], wtarget[w, d] * upstream_share(w, d, t))
                    ASKED[t, w, d], GOT[t, w, d] = wtarget[w, d] - W[w, d], new - W[w, d]
                    W[w, d] = new
        rec = np.zeros((F, D))
        for f in range(F):
            cycle = (t - off[f]) % 30 == 0
            for d in range(D):   # an emergency indent only gets answered half the time
                emergency = S[f, d] < rate[f, d] and (t - off[f]) % 7 == 0 and rng.random() < 0.5
                if (cycle or emergency) and not local_blocked(f, d, t):
                    give = min(max(target[f, d] - S[f, d], 0) * rng.uniform(*fill), W[wi[f], d])
                    W[wi[f], d] -= give
                    S[f, d] += give
                    rec[f, d] = give

        lift = np.ones(C)
        lift[surge_idx] += surge * np.exp(-((t - peak) / 25) ** 2)
        m = np.ones((F, C))                             # a health emergency: x lift where and while it runs
        for s in surges or ():
            if s["start"] <= t < s["start"] + s["dur"]:
                where = [s["root"] in (wh[f], st[f], fac[f]) for f in range(F)]
                m[np.ix_(where, [ix["ci"][c] for c in s["conds"]])] *= s["lift"]
        dx[t] = rng.poisson((lam * lift)[None, :] * size[:, None] * m)
        USE[t] = size[:, None] * ((lam * lift) @ ix["units"])[None, :]
        on = (m != 1).any(1)
        USE[t, on] = size[on, None] * ((lam * lift * m[on]) @ ix["units"])
        for (_, s), p in ix["sub_of"].items():
            USE[t, :, s] = rate[:, s]
        issued = np.zeros((F, D))
        gap_left = np.where(gap_left > 0, gap_left - 1, (rng.random(F) < gap_rate) * rng.integers(1, 6, F))
        lost = gap_left > 0                            # slips on these days never reach the system

        def give(f, c, d, dot, units):
            if not lost[f]:
                slips.append((t, f, c, d, dot, units))
            S[f, d] -= units
            issued[f, d] += units

        for f in range(F):
            for c in range(C):
                n = dx[t, f, c]
                for d in (np.nonzero(ix["share"][c])[0] if n else ()):
                    upd, n_days, subs = ix["course"][(c, d)]
                    for _ in range(rng.binomial(n, min(1.0, ix["share"][c, d] * bias[f, d]))):
                        if subs and rng.random() < 0.01:          # off-guideline swap, background noise
                            sd, supd, sdays = subs[0]
                            if S[f, sd] >= supd * sdays:
                                give(f, c, sd, sdays, supd * sdays)
                                continue
                        if S[f, d] < upd:                         # nothing usable on the shelf
                            for sd, supd, sdays in subs:
                                if S[f, sd] >= supd * sdays and rng.random() < p_sub[f]:
                                    give(f, c, sd, sdays, supd * sdays)
                                    break
                            else:
                                if rng.random() < p_na[f] and not lost[f]:
                                    na.append((t, f, c, d))
                            continue
                        cover = S[f, d] / rate[f, d]
                        if B["ramp"]:
                            ration = rng.random() < p_ration_f[f] * np.clip(1 - cover / ration_cover[f], 0, 1)
                        else:
                            ration = cover < ration_cover[f] and rng.random() < p_ration_f[f]
                        ration = ration or rng.random() < 0.02
                        dot = max(1, round(n_days * rng.uniform(0.3, 0.6))) if ration else n_days
                        dot = min(dot, int(S[f, d] // upd))       # part-issue when short
                        give(f, c, d, dot, upd * dot)
        dx[t, lost & (rng.random(F) < 0.5)] = 0                   # half the gaps also lose the diagnoses

        # the register: receipts mostly posted, issues posted unreliably, month-end catch-up
        post_r = rng.random((F, D)) < 0.95
        book += np.where(post_r, rec, 0)
        back_r += np.where(post_r, 0, rec)
        post_i = rng.random((F, D)) < p_post[:, None]
        book -= np.where(post_i, issued, 0)
        back_i += np.where(post_i, 0, issued)
        flush = ((t - off - 15) % 30 == 0)[:, None] & (rng.random((F, D)) < 0.5)
        book += np.where(flush, back_r - back_i, 0)
        back_r[flush] = back_i[flush] = 0
        book = np.maximum(book, 0)
        BOOK[t], TRUE[t], REC[t] = book, S, rec

    # drawn after the loop, so the world above is the same with or without the ledger
    posted = np.arange(days)[:, None, None] + rng.integers(0, 11, GOT.shape)
    posted[rng.random(GOT.shape) < 0.05] = days
    return Run(ix, fac, wh, st, dx, slips, na, BOOK, REC, ASKED, GOT, posted, TRUE, rate, USE, ration_cover, eps)


def stockout_events(run, min_len=2, merge_gap=2):
    """GROUND TRUTH: runs where a primary drug's true stock is under half a day of use
    (gaps of <= merge_gap days merged), labelled with the injected failure that caused
    them, else DEMAND-SURGE if demand was 15%+ above normal when it ran out, else LOCAL."""
    rank = {t: i for i, t in enumerate(TYPES)}
    events = []
    for f in range(len(run.facilities)):
        for d in run.ix["primaries"]:
            runs = []
            for s, t in segments(run.true_stock[:, f, d] < 0.5 * run.true_use[:, f, d], min_len):
                if runs and s - runs[-1][1] <= merge_gap:
                    runs[-1] = (runs[-1][0], t)
                else:
                    runs.append((s, t))
            for s, t in runs:
                causes = [e for e in run.episodes if e["drug"] == d and e["start"] <= s <= e["end"] + 21
                          and e["root"] in ("IN", run.st[f], run.wh[f], run.facilities[f])]
                cause = max(causes, key=lambda e: rank[e["type"]], default=None)
                if cause:
                    kind = cause["type"]
                elif run.true_use[s, f, d] >= 1.15 * run.rate[f, d]:
                    kind = "DEMAND-SURGE"
                else:
                    kind = "LOCAL"
                events.append(dict(fac=f, drug=d, out=s, end=t, type=kind, injected=cause is not None,
                                   ep=run.episodes.index(cause) if cause else None))
    return events
