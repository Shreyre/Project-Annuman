import hashlib
import json

import pytest

from anumaan import realcheck_india as RI

FY = ["2019-2020"]


def _district(state, name, empty=(3, 7, 10), zero=()):
    """One district-year: the IFA ledger in use, EMPTY in the given months (each after a month that closed at zero)
    and STOCKED in the others, bar zero: months whose ledger reads all zeros after a month that closed with stock,
    a blank typed as zeros and not EMPTY. 100 women registered a month; 80 given the full IFA course, but 30 in an
    EMPTY or zero month; 70 given calcium throughout, from a calcium ledger that is always stocked."""
    month = [[1000, 500, 0, 400, 1100] for _ in range(12)]          # balance, received, unusable, distributed, total
    for m in empty:
        month[m], month[m - 1] = [0] * 5, [1000, 500, 0, 1500, 0]
    for m in zero:
        month[m] = [0] * 5
    none = [None] * 12
    return {"state": state, "district": name, "fy": FY[0], "1.1": [100] * 12,
            "1.2.4": [30 if m in empty + zero else 80 for m in range(12)], "1.2.5": [70] * 12, "9.9": none, "9.10": none,
            "19.6": [list(f) for f in zip(*month)], "19.16": [[1000] * 12, [500] * 12, [0] * 12, [400] * 12, [1100] * 12],
            "19.9": [none] * 5, "19.15": [none] * 5, "prev_close": {"19.6": None, "19.9": None, "19.15": None}}


def test_a_scripted_stock_out_with_a_care_dip_is_flagged_and_the_placebos_are_not():
    # 10 districts in 5 states, 3 EMPTY months each; one district also has a zero row in September
    series = [_district(f"S{k % 5}", f"D{k}", zero=(5,) if k == 1 else ()) for k in range(10)]
    series[0]["1.2.4"][0] = series[0]["1.2.5"][0] = 30              # one well-stocked month dips too, in both
    r = {k: RI.analyse(series, k, FY) for k in RI.P["analyses"]}
    p = r["primary"]
    assert (p["empty_flagged"], p["empty_months"], p["stocked_flagged"], p["stocked_months"]) == (30, 30, 1, 89)
    assert (p["districts"], p["states"], p["label"]) == (10, 5, "support") and p["ci"][0] > 1
    ca = r["placebo_calcium"]           # calcium, given at the same visits, did not dip when the IFA ran out
    assert (ca["empty_flagged"], ca["empty_months"], ca["reading"]) == (0, 30, "quiet")
    t = r["placebo_timing"]             # six months on, only July's marker still falls inside the year: January
    assert (t["empty_flagged"], t["empty_months"], t["reading"]) == (0, 10, "quiet")
    assert r["ifa_syrup"]["series_in_use"] == 0 and r["ifa_syrup"]["rr"] is None
    series[9]["1.1"][3], series[8]["1.1"][7] = None, 40   # the gap guard: no ANC count, or under half the median
    assert RI.analyse(series, "primary", FY)["empty_months"] == 28


def test_cached_hmis_data_gives_the_published_result(monkeypatch):
    monkeypatch.setattr(RI, "_download", lambda *a: pytest.fail("summary() must not touch the network"))
    d = json.loads(RI.DATA.read_text(encoding="utf-8"))
    assert [(f["fy"], f["sha256"][:8]) for f in d["files"]] == [
        ("2017-2018", "6eac1362"), ("2018-2019", "5fc13094"), ("2019-2020", "993fc850")]
    assert [d["coverage"][fy]["ledger_in_use"]["19.6"] for fy in RI.P["years"]["v1"]] == [81, 84, 110]
    assert RI.results(d["series"]) == d["results"]                 # the stored rows give the stored results
    s = RI.summary()
    assert json.loads(json.dumps(s)) == s and s["source"]["licence"] and s["caveats"] and s["rules"]["unchanged"]
    assert s["decision"] == {"v0": "refuted", "v1": "inconclusive"} and s["headline_version"] == "v1"
    rows = {r["key"]: r for r in s["rows"]}
    v0, v1 = rows["primary"]["v0"], rows["primary"]["v1"]
    # FY 2017-20: 8 of 132 EMPTY months flagged against 93 of 2,526 STOCKED; FY 2019-20 alone meets the refutation rule
    assert (v1["empty_flagged"], v1["empty_months"], v1["stocked_flagged"], v1["stocked_months"]) == (8, 132, 93, 2526)
    assert (v1["rr"], v1["ci"], v1["districts"], v1["states"]) == (1.6461, [0.274, 3.797], 55, 16)
    assert (v0["empty_flagged"], v0["empty_months"], v0["stocked_flagged"], v0["stocked_months"]) == (1, 70, 33, 973)
    assert (v0["rr"], v0["ci"]) == (0.4212, [0.0, 1.4962])
    ca, t = rows["placebo_calcium"]["v1"], rows["placebo_timing"]["v1"]
    assert (ca["rr"], ca["reading"], t["empty_flagged"], t["reading"]) == (1.6464, "quiet", 0, "quiet")
    assert "decision: inconclusive" in s["headline"] and "FY 2019-20 alone: refuted" in s["headline"]


def test_the_results_record_the_rules_written_before_them():
    d = json.loads(RI.DATA.read_text(encoding="utf-8"))
    lf = RI.RULES.read_bytes().replace(b"\r\n", b"\n")                # as git stores it, whatever the checkout
    assert d["rules_sha256"] == hashlib.sha256(lf).hexdigest() == RI.rules_sha256() == (
        "9467c31048736c023bf1557132e9ca31fbbce65fdd7c62d0edacb9b4034afd62")
    assert d["rules_written_at"] == json.loads(lf)["written_at"] < min(a["at"] for a in d["amendments"])
