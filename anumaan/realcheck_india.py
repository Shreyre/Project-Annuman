"""Does a stock-out that a district's own register records show in the same district's care record? India's HMIS.

realcheck.py asks this of England's open dispensing records. India publishes no dispensing data at the grain Anumaan
reads, but its Health Management Information System (HMIS, MoHFW) publishes district-month totals that put a
full-course care count next to a monthly stock ledger for the same medicine: item 1.2.4, pregnant women given the full
180 IFA tablets, beside ledger 19.6, adult IFA tablets. The rules, fixed in realcheck_india_rules.json before any
outcome was computed, ask: in months the district's IFA ledger has nothing to issue (EMPTY), is 1.2.4 per woman
registered for antenatal care (1.1) flagged low more often than in months it has plenty (STOCKED)? Each month is a
robust z against the district-year's own STOCKED months, each of those left out in turn; the rate ratio of flags gets a
bootstrap CI over districts. Placebos: calcium, given at the same visits, and the marker moved six months on.
Secondary: a one-month lag, and children's IFA syrup and albendazole against their own ledgers.

    python -m anumaan.realcheck_india               # the table, from realcheck_india.json; no network
    python -m anumaan.realcheck_india --fetch DIR   # rebuild it from the official zips in DIR, downloading any missing

What it does NOT show: it tests the premise at district-month grain on self-reported aggregates, not the filter; not
hidden stock-outs, since the marker is the register itself; and only districts whose ledger is in use. The JSON's
caveats say the rest; summary() returns them.
"""
import argparse
import hashlib
import html
import io
import json
import re
import shutil
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path

import numpy as np

DATA = Path(__file__).with_name("realcheck_india.json")
RULES = Path(__file__).with_name("realcheck_india_rules.json")
RULE = json.loads(RULES.read_text(encoding="utf-8"))
P = RULE["params"]                     # fixed before any outcome was computed, never tuned
FIELDS = RULE["data"]["ledger_fields"]
ITEMS = [k for k in RULE["data"]["items"] if not k.startswith("19.")]
LEDGERS = [k for k in RULE["data"]["items"] if k.startswith("19.")]
MONTHS = ("April", "May", "June", "July", "August", "September", "October", "November", "December", "January",
          "February", "March")
CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
LABELS = dict(
    primary="IFA tablets: full courses (1.2.4) per ANC registration (1.1), against the IFA ledger (19.6)",
    placebo_calcium="Placebo, calcium: full courses (1.2.5) per ANC registration, in the same months, with the calcium "
                    "ledger (19.16) stocked",
    placebo_timing="Placebo, timing: the EMPTY marker moved 6 months later",
    lag_one_month="Secondary: the care month one month after the marker",
    ifa_syrup="Secondary: children given IFA syrup (9.9), against its ledger (19.9)",
    albendazole="Secondary: children given albendazole (9.10), against its ledger (19.15)")


def rules_sha256():
    """SHA-256 of the rules file with LF line endings, so a CRLF checkout (git's autocrlf) hashes the same."""
    return hashlib.sha256(RULES.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _in_use(led):
    """[N] bool: all five fields of each ledger-year [N, 5, 12] reported every month, and something distributed in
    at least 6 of the 12."""
    return np.isfinite(led).all((1, 2)) & ((led[:, 3] > 0).sum(1) >= P["in_use_min_months_distributed"])


def _marks(led, prev_close):
    """[N, 12] EMPTY and STOCKED months of each ledger-year [N, 5, 12]. prev_close [N]: the Total Stock of the month
    before April, NaN where that month is not in the analysed files."""
    avail, before = led[:, 0] + led[:, 1], np.c_[prev_close, led[:, 4, :-1]]
    with np.errstate(invalid="ignore"):
        return (avail == 0) & (before == 0), avail >= P["stocked_multiple"] * np.median(led[:, 3], 1)[:, None]


def _z(y, ref, floor, rel):
    """[N, 12] robust z, (y - median) / (1.4826 MAD), of each month against its row's ref months; a ref month is left
    out of its own reference. rel: the MAD floor is a share of the median, not absolute."""
    base = np.where(ref[:, None, :] & ~np.eye(12, dtype=bool), y[:, None, :], np.nan)    # [N, month, reference month]
    med = np.nanmedian(base, 2)
    mad = np.nanmedian(np.abs(base - med[..., None]), 2)
    return (y - med) / (P["mad_scale"] * np.maximum(mad, floor * (np.abs(med) if rel else 1)))


def _num(x):
    """A JSON-safe statistic: rounded, "inf" for an infinite ratio, None where it is undefined."""
    return None if np.isnan(x) else "inf" if np.isinf(x) else round(float(x), 4)


def _stats(n, states, in_use, scored, role):
    """Pooled shares flagged, their rate ratio with its bootstrap CI over districts, and the decision rule's label.
    n [D, 4]: each district's EMPTY months, those flagged, STOCKED months, those flagged."""
    b, d = P["bootstrap"], P["decision"]
    ratio = lambda t: (t[..., 1] / t[..., 0]) / (t[..., 3] / t[..., 2])   # p_E / p_S: 0/0, or no EMPTY month, is NaN
    with np.errstate(divide="ignore", invalid="ignore"):
        tot = n.sum(0)
        pe, ps, rr = tot[1] / tot[0], tot[3] / tot[2], ratio(tot)
        rng = np.random.default_rng(b["seed"])
        boot = ratio(n[rng.integers(0, len(n), (b["resamples"], len(n)))].sum(1)) if len(n) else np.array([])
    boot = boot[~np.isnan(boot)]
    lo, hi = np.percentile(boot, b["percentiles"], method=b["method"]) if boot.size else (np.nan, np.nan)
    has = n[:, 0] > 0
    nd, ns = int(has.sum()), len({s for s, h in zip(states, has) if h})
    if not tot[0]:
        label = "inconclusive"
    elif (tot[0] >= d["min_empty_months"] and nd >= d["min_districts"] and ns >= d["min_states"]
          and rr >= d["support_rr"] and lo > d["support_ci_lower_above"]):
        label = "support"
    elif hi < d["refute_ci_upper_below"] or pe <= ps:
        label = "refuted"
    else:
        label = "inconclusive"
    out = dict(series_in_use=in_use, series_scored=scored, districts_scored=len(n), empty_months=int(tot[0]),
               empty_flagged=int(tot[1]), stocked_months=int(tot[2]), stocked_flagged=int(tot[3]), districts=nd,
               states=ns, p_empty=_num(pe), p_stocked=_num(ps), rr=_num(rr), ci=[_num(lo), _num(hi)],
               resamples_used=int(boot.size), label=label)
    if role == "placebo":
        f = P["placebo_fires"]
        out["reading"] = ("no EMPTY months" if not tot[0] else "fires" if rr >= f["rr"] and lo > f["ci_lower_above"]
                          else "quiet" if lo <= 1 else "raised" if lo > 1 else "undefined")
    return out


def analyse(series, key, years):
    """One analysis of the rules (params.analyses[key]) on the district-years of the given financial years."""
    a, s = P["analyses"][key], [x for x in series if x["fy"] in years]
    arr = lambda k: np.array([x[k] for x in s], float)             # a blank (None) is NaN
    led = arr(a["ledger"])
    use = _in_use(led)
    # April looks back to the March before, when that year is analysed too
    prev = np.array([x["prev_close"][a["ledger"]] if x["fy"] != years[0] else None for x in s], float)
    empty, stocked = _marks(led, prev)
    if "also_stocked" in a:                                        # calcium: its ledger in use and STOCKED as well
        c = arr(a["also_stocked"])
        use &= _in_use(c)
        both = _marks(c, np.full(len(s), np.nan))[1]
        empty, stocked = empty & both, stocked & both
    empty, stocked = empty & use[:, None], stocked & use[:, None]
    anc = arr(P["gap_guard_item"])
    with np.errstate(divide="ignore", invalid="ignore"):
        y = arr(a["num"]) / arr(a["den"]) if a["den"] else arr(a["num"])
        med = np.nanmedian(np.where(np.isnan(anc).all(1, keepdims=True), 0, anc), 1, keepdims=True)
        kept = (anc > 0) & (anc >= P["gap_guard_share_of_median"] * med) & np.isfinite(y)
        ref = stocked & kept
        ok = use & (ref.sum(1) >= P["min_stocked_months"])        # a scored district-year
        z = np.full(y.shape, np.nan)
        z[ok] = _z(y[ok], ref[ok], a["floor"], a["floor_relative"])
        flag = z <= P["flag_z_at_or_below"]
    # every district's months in a row, so a marker can move into the next year
    dist = sorted({(x["state"], x["district"]) for x in s})
    pos, T = {k: i for i, k in enumerate(dist)}, 12 * len(years)
    di = np.array([pos[x["state"], x["district"]] for x in s], int)
    g = np.zeros((4, len(dist), T), bool)                          # EMPTY, STOCKED, scored, flagged
    for i, x in enumerate(s):
        t = 12 * years.index(x["fy"])
        g[:, di[i], t:t + 12] = empty[i], stocked[i], kept[i] & ok[i], flag[i]
    E0, S0, sc, fl = g
    k = a.get("marker_shift_months", a.get("outcome_lag_months", 0))
    on = lambda m: np.pad(m, ((0, 0), (k, 0)))[:, :T]            # each marker k months on, same district
    E, S = (on(E0) & S0 & sc, S0 & sc) if "marker_shift_months" in a else (on(E0) & sc, on(S0) & sc)
    n = np.stack([E.sum(1), (E & fl).sum(1), S.sum(1), (S & fl).sum(1)], 1)
    boot = np.isin(np.arange(len(dist)), di[ok])                  # the bootstrap's districts: a scored district-year
    return _stats(n[boot], [dist[j][0] for j in np.flatnonzero(boot)], int(use.sum()), int(ok.sum()), a["role"])


def results(series):
    """Every analysis of the rules, on v0 and on v1: {version: {"years": [...], analysis: statistics}}."""
    return {v: dict(years=ys, **{k: analyse(series, k, ys) for k in P["analyses"]}) for v, ys in P["years"].items()}


def _pct(k, n):
    return f"{k:,} of {n:,}" + (f" ({k / n:.1%})" if n else "")


def _ratio(r):
    rr, (lo, hi) = r["rr"], r["ci"]
    f = lambda x: "undefined" if x is None else "infinite" if x == "inf" else f"{x:.2f}"
    return f"{f(rr)} (95% CI {f(lo)} to {f(hi)})"


def summary():
    """What the UI needs, from the cached file alone (no network): the headline, the table rows, the decision, the
    caveats and the amendments."""
    d = json.loads(DATA.read_text(encoding="utf-8"))
    r = results(d["series"])
    v, fy = P["headline"], lambda ys: f"FY {ys[0][:4]}-{ys[-1][-2:]}"
    p = r[v]["primary"]
    headline = (f"{fy(r[v]['years'])}, pre-registered decision: {p['label']}. In the {p['districts_scored']} districts whose "
                f"IFA stock ledger was in use, {_pct(p['empty_flagged'], p['empty_months'])} months in which the district's "
                f"own register showed no IFA tablets to issue were flagged for fewer full IFA courses per woman registered "
                f"for antenatal care, against {_pct(p['stocked_flagged'], p['stocked_months'])} well-stocked months: rate "
                f"ratio {_ratio(p)}.")
    for k in r:                                     # the rules: if v0 and v1 disagree, the headline says so
        if r[k]["primary"]["label"] != p["label"]:
            headline += f" {fy(r[k]['years'])} alone: {r[k]['primary']['label']}, rate ratio {_ratio(r[k]['primary'])}."
    headline += " Placebos: " + "; ".join(f"{k.split('_')[1]} {r[v][k]['reading']}, rate ratio {_ratio(r[v][k])}"
                                          for k, a in P["analyses"].items() if a["role"] == "placebo") + "."
    return dict(source=dict(d["source"], fetched=d["fetched"], files=d["files"]),
                rules=dict(file=RULES.name, sha256=d["rules_sha256"], unchanged=d["rules_sha256"] == rules_sha256(),
                           written_at=d["rules_written_at"], flag=f"z <= {P['flag_z_at_or_below']}",
                           decision=RULE["rules"]["8_decision"]),
                headline=headline, headline_version=v, decision={k: r[k]["primary"]["label"] for k in r},
                years={k: r[k]["years"] for k in r},
                rows=[dict(key=k, name=LABELS[k], role=a["role"], **{ver: r[ver][k] for ver in r})
                      for k, a in P["analyses"].items()],
                coverage=d["coverage"], caveats=d["caveats"], amendments=d["amendments"])


def _html_rows(f):
    """A SAS-HTML file's data rows as [district, code, name, type, 65 values], None where a rowspan above covers the
    cell. Streamed: one state's file runs to 260 MB."""
    buf = ""
    for chunk in iter(lambda: f.read(1 << 24), b""):
        *trs, buf = (buf + chunk.decode("cp1252", "replace")).split("</tr>")
        for tr in trs:
            r = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip() for c in CELL.findall(tr)]
            if len(r) == 14:
                assert tuple(r[1:13]) == MONTHS, r
            elif len(r) == 65:
                assert all(c.startswith("Total") for c in r[::5]), r
            elif len(r) in (66, 68, 69):
                r = [None] * (69 - len(r)) + r                    # a rowspan covers the district, or code and name
                r[1] = r[1] and r[1].strip("'")
                yield r
            else:
                assert len(r) == 5, r                              # the 'District' label row


def _xlsx_rows(f):
    """The same rows from the one real .xlsx among the files (Telangana, FY 2017-18). It stores codes that look like
    numbers as numbers (9.10 reads 9.1), so those come back as floats; a blank is an absent cell."""
    x = zipfile.ZipFile(io.BytesIO(f.read()))
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ss = ["".join(t.text or "" for t in si.iter(ns + "t"))
          for si in ET.fromstring(x.read("xl/sharedStrings.xml")).iter(ns + "si")]
    for _, el in ET.iterparse(x.open("xl/worksheets/sheet1.xml")):
        if el.tag != ns + "row":
            continue
        r = [None] * 69
        for c in el.iter(ns + "c"):
            v, col = c.find(ns + "v"), 0
            for ch in re.match("[A-Z]+", c.get("r")).group():
                col = 26 * col + ord(ch) - 64
            if v is not None and col <= 69:
                r[col - 1] = (re.sub(r"\s+", " ", ss[int(v.text)]).strip() if c.get("t") == "s"
                              else float(v.text) if col == 2 else v.text)
        el.clear()
        if r[4] == "April":
            assert tuple(r[4:64:5]) == MONTHS, r
        elif str(r[4]).startswith("Total ["):
            assert all(c.startswith("Total") for c in r[4:64:5]), r
        elif r[3]:
            yield r


def _status(z, m):
    """The 'Status As On' date a file is headed with."""
    if m.endswith(".xlsx"):
        head = zipfile.ZipFile(io.BytesIO(z.read(m))).read("xl/sharedStrings.xml")
    else:
        head = z.open(m).read(1 << 16)
    return re.search(rb"Status As On: ([^<]+?)\s*<", head).group(1).decode()


def _value(v):
    if v is None or v == "":
        return None
    x = float(v)
    return int(x) if x.is_integer() else x


def _extract(path):
    """One year's zip: {(state, district): {item: [12 months], ledger: [5 fields][12 months]}} for the rules' items
    and ledgers, from the Total column; the names printed for them; the files' status dates."""
    out, names, status = {}, {}, set()
    with zipfile.ZipFile(path) as z:
        for m in z.namelist():
            state = Path(m).stem
            if state in P["exclude_files"]:
                continue
            status.add(_status(z, m))
            district = code = name = None
            for r in (_xlsx_rows if m.endswith(".xlsx") else _html_rows)(z.open(m)):
                if r[0]:
                    district = r[0]
                    assert (state, district) not in out, (state, district)
                    out[state, district] = {}
                if r[1] is not None:
                    code, name = r[1], r[2]
                w = next((w for w in RULE["data"]["items"] if code == w
                          or isinstance(code, float) and w.count(".") == 1 and code == float(w)), None)
                if w is None:
                    continue
                vals = [_value(v) for v in r[4:64:5]]
                if r[3] == "TOTAL":
                    out[state, district][w] = vals
                else:
                    out[state, district].setdefault(w, [None] * 5)[FIELDS.index(r[3])] = vals
                names.setdefault(w, set()).add(name)
    return out, {k: sorted(v) for k, v in names.items()}, sorted(status)


def _ledger(rec, code):
    return [f or [None] * 12 for f in rec.get(code, [None] * 5)]


def _download(url, path):
    """One official zip, whole (the portal ignores byte ranges): about 60 MB, a timeout, two retries."""
    part = path.with_suffix(".part")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=600) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f)
            return part.replace(path)
        except OSError:
            if attempt == 2:
                raise
            time.sleep(10)


def fetch(folder):
    """Rebuild DATA from the official HMIS zips in folder, downloading any that is missing: the files' hashes, the
    coverage, the district-years whose IFA, IFA syrup or albendazole ledger is in use, and the results. DATA's
    hand-written parts (about, source, caveats, amendments) are kept."""
    d, files, got = json.loads(DATA.read_text(encoding="utf-8")), [], {}
    years = P["years"]["v1"]
    for fy in years:
        path = Path(folder) / f"c2_all_states_districts_across_months_{fy}.zip"
        url = RULE["data"]["url_pattern"].format(fy=fy)
        if not path.exists():
            _download(url, path)
        files.append(dict(fy=fy, url=url, bytes=path.stat().st_size,
                          sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                          fetched=date.fromtimestamp(path.stat().st_mtime).isoformat()))
        got[fy] = _extract(path)
        print(f"{fy}: {len(got[fy][0])} districts", flush=True)
    marked = sorted({a["ledger"] for a in P["analyses"].values()}, key=LEDGERS.index)   # ledgers with EMPTY months
    series, coverage = [], {}
    for i, fy in enumerate(years):
        ex, names, status = got[fy]
        use = {L: {k for k, rec in ex.items() if _in_use(np.array([_ledger(rec, L)], float))[0]} for L in LEDGERS}
        coverage[fy] = dict(states=len({s for s, _ in ex}), districts=len(ex), status_as_on=status, item_names=names,
                            ledger_in_use={L: len(v) for L, v in use.items()})
        prev = got[years[i - 1]][0] if i else {}
        for k in sorted(set().union(*(use[L] for L in marked))):
            series.append(dict(state=k[0], district=k[1], fy=fy, **{c: ex[k].get(c, [None] * 12) for c in ITEMS},
                               **{L: _ledger(ex[k], L) for L in LEDGERS},
                               prev_close={L: _ledger(prev.get(k, {}), L)[4][11] for L in marked}))
    d.update(fetched=max(f["fetched"] for f in files), files=files, rules_sha256=rules_sha256(),
             rules_written_at=RULE["written_at"], coverage=coverage, results=results(series), series=series)
    text = re.sub(r"\n\s+(?=[-\d\]\[n])", "", json.dumps(d, indent=1))     # a district-year's months to a line
    DATA.write_text(text + "\n", encoding="utf-8", newline="\n")


def main():
    ap = argparse.ArgumentParser(description="Pre-registered premise check on India's public HMIS district-month data.")
    ap.add_argument("--fetch", metavar="DIR",
                    help="rebuild the JSON from the official HMIS zips in DIR, downloading any that is missing")
    args = ap.parse_args()
    if args.fetch:
        fetch(args.fetch)
    s = summary()
    print(f"REAL Indian records, premise check only - {s['source']['dataset']}, fetched {s['source']['fetched']}.\n"
          f"  Rules fixed before any outcome was computed: {s['rules']['file']}, written {s['rules']['written_at']},\n"
          f"  sha256 {s['rules']['sha256']}" + ("" if s["rules"]["unchanged"] else " (THE FILE HAS CHANGED SINCE)") + ".\n"
          f"  A month is flagged when its robust z against its district-year's own STOCKED months is {P['flag_z_at_or_below']}"
          f" or below.\n\n{s['headline']}\n")
    for v, ys in s["years"].items():
        print(f"{v}: FY {', '.join(ys)}" + (" (the headline)" if v == s["headline_version"] else ""))
        print(f"  {'analysis':34}{'EMPTY months flagged':25}{'STOCKED months flagged':25}{'districts':10}{'states':7}"
              f"{'rate ratio':34}result (a placebo's reading)")
        for row in s["rows"]:
            r = row[v]
            print(f"  {row['key'] + ' (' + row['role'] + ')':34}{_pct(r['empty_flagged'], r['empty_months']):25}"
                  f"{_pct(r['stocked_flagged'], r['stocked_months']):25}{r['districts']:<10}{r['states']:<7}{_ratio(r):34}"
                  f"{r.get('reading', r['label'])}")
        print()
    print("\n".join("  - " + c for c in s["caveats"]))
    for a in s["amendments"]:
        print(f"  - amendment ({a['at']}): {a['what']} {a['why']}")


if __name__ == "__main__":
    main()
