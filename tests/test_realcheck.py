import json

import numpy as np
import pytest

from anumaan import realcheck as RC


def test_wide_rule_needs_two_flagged_icbs_in_each_of_two_regions():
    region = ["N", "N", "N", "S", "S", "E"]                        # one row per ICB, one column per month
    flag = np.array([[1, 1, 1, 0], [1, 1, 0, 0], [0, 1, 0, 0], [1, 0, 1, 0], [1, 0, 0, 0], [0, 1, 1, 0]], bool)
    # month 0: two in N and two in S. month 1: three in N, one in E. month 2: one each in N, S, E. month 3: none
    assert RC.wide(flag, region).tolist() == [True, False, False, False]


def test_scores_leave_one_out_in_the_baseline_and_floor_a_flat_one():
    x = np.full((2, 25), 100.0)
    x[0, 5] = x[0, 20] = 90.0           # a 10% dip inside the baseline, and the same dip after it
    x[1, 20] = 99.5                     # a flat baseline has MAD 0: the floor keeps half a percent far from a flag
    z = RC.scores(-x, rel=True)
    assert z[0, 5] >= RC.Z and z[0, 20] >= RC.Z
    assert (np.delete(z[0], [5, 20]) == 0).all()      # the baseline dip scored itself, not its eleven neighbours
    assert 0 < z[1, 20] < 1 and np.isfinite(z).all()
    share = np.full((1, 25), 0.03)
    share[0, 20] = 0.08                 # the floor on a share is absolute: 5 points up is a flag, 2 points is not
    assert RC.scores(share, rel=False)[0, 20] >= RC.Z > RC.scores(share - 0.03 * (share > 0.05), rel=False)[0, 20]


def test_cached_england_data_gives_the_published_result(monkeypatch):
    monkeypatch.setattr(RC, "_rows", lambda *a: pytest.fail("summary() must not touch the network"))
    d = json.loads(RC.DATA.read_text(encoding="utf-8"))
    assert len(d["regions"]) == 42 and len(set(d["regions"].values())) == 7
    assert all(np.shape(s[k]) == (42, 25) for s in d["studies"] for k in ("index_items", "index_qty", "substitute_items"))
    s = RC.summary()
    assert json.loads(json.dumps(s)) == s and s["source"]["licence"] and s["caveats"]      # the endpoint returns it as is
    creon, oestrogel = s["shortages"]
    months = lambda r, key: dict(zip(r["months"], r[key]["flagged"]))
    # Creon: both fingerprints meet the wide rule in the month of the first notice, but only substitution lasts;
    # both had already met it, leave-one-out, in the stock-out of September 2023
    assert creon["notice_month"] == creon["cut_short"]["first_wide"] == creon["substituting"]["first_wide"] == "2024-02"
    assert months(creon, "cut_short")["2023-09"] == 26 and max(creon["cut_short"]["flagged"][12:]) == 5
    assert months(creon, "substituting")["2024-09"] == 28 == max(creon["substituting"]["flagged"])
    assert creon["cut_short"]["false_flags"]["wide"] == ["2023-09"]
    assert creon["substituting"]["false_flags"] == dict(months=12, icb_rate=0.0575, wide=["2023-09", "2023-11"])
    # Oestrogel: never cut short; substituted in 41 of 42 ICBs the month BEFORE the first notice
    assert oestrogel["cut_short"]["first_wide"] is None and sum(oestrogel["cut_short"]["flagged"]) == 0
    assert oestrogel["substituting"]["first_wide"] == oestrogel["notice_month"] == "2022-04"
    assert months(oestrogel, "substituting")["2022-03"] == 41 and months(oestrogel, "substituting")["2022-05"] == 37
    assert oestrogel["substituting"]["false_flags"]["wide"] == ["2022-02", "2022-03"]
    # placebos: amlodipine never flags; azathioprine's slow drift meets the wide rule once, in its last month
    fired = {(p["name"], key): p[key]["wide_months"] for p in s["placebos"] for key in ("cut_short", "substituting")}
    assert len(fired) == 8 and {k: v for k, v in fired.items() if v} == {
        ("placebo: azathioprine 50 mg, Creon months", "cut_short"): ["2025-02"]}
    assert all(sum(p[key]["flagged"]) == 0 for p in s["placebos"] if "amlodipine" in p["name"]
               for key in ("cut_short", "substituting"))
