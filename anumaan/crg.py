"""Care-to-Resource Grammar (CRG): what each care event should consume.

The grammar is the observation model: diagnoses say how many courses of a drug
*should* have been dispensed; slips are decoded against it into full courses,
rationed courses (fewer days than the guideline), guideline substitutes, and
recorded not-available.
"""
import json
from pathlib import Path

import numpy as np

DEFAULT = Path(__file__).resolve().parent.parent / "grammar" / "crg" / "tracer.json"
CATS = ("full", "ration", "sub", "na")


def load(path=DEFAULT):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def index(crg):
    """Lists and lookup tables used by the simulator and the filter."""
    conds = sorted(crg["conditions"])
    drugs = sorted(crg["drugs"])
    ci = {c: i for i, c in enumerate(conds)}
    di = {d: i for i, d in enumerate(drugs)}
    share = np.zeros((len(conds), len(drugs)))   # expected courses per diagnosis
    units = np.zeros((len(conds), len(drugs)))   # expected units per diagnosis
    days, sub_of = {}, {}
    for c, spec in crg["conditions"].items():
        for co in spec["courses"]:
            a, b = ci[c], di[co["drug"]]
            share[a, b] = co.get("share", 1.0)
            units[a, b] = share[a, b] * co["units_per_day"] * co["days"]
            days[(a, b)] = co["days"]
            for s in co["substitutes"]:
                sub_of[(a, di[s["drug"]])] = b
    primaries = sorted({b for (_, b) in days})
    return dict(conds=conds, drugs=drugs, ci=ci, di=di, share=share, units=units,
                days=days, sub_of=sub_of, primaries=primaries)


def aggregate(ix, dx, slips, na):
    """Decode raw events into per (day, facility, drug) observation counts.

    dx:    int array [day, facility, condition] of diagnoses
    slips: iterable of (day, fac, cond, drug, days_of_therapy, units)
    na:    iterable of (day, fac, cond, drug) not-available records
    Returns dict with N (expected courses), exp_units and the CATS counts.
    """
    n_days, n_fac, _ = dx.shape
    shape = (n_days, n_fac, len(ix["drugs"]))
    out = {k: np.zeros(shape) for k in CATS}
    out["N"] = dx @ ix["share"]
    out["exp_units"] = dx @ ix["units"]
    for t, f, c, d, dot, _units in slips:
        if (c, d) in ix["days"]:
            out["full" if dot >= ix["days"][(c, d)] else "ration"][t, f, d] += 1
        elif (c, d) in ix["sub_of"]:
            out["sub"][t, f, ix["sub_of"][(c, d)]] += 1
    for t, f, c, d in na:
        out["na"][t, f, d] += 1
    return out
