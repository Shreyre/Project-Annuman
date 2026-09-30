"""Do the two shortage fingerprints show in REAL dispensing records when a shortage is declared?

Everything else in Anumaan is scored on a simulator, because India publishes no dispensing data.
England does: the NHSBSA English Prescribing Dataset (EPD), what was dispensed each month against
every GP practice's prescriptions. For two shortages that were officially declared there (Creon
capsules 2024, Oestrogel 2022) and two medicines with none (amlodipine, azathioprine), this reads
per integrated care board (ICB, the district) and month:
  cut short     quantity per item of the index product falls: courses rationed
  substituting  the substitutes' share of items, substitutes / (index + substitutes), rises
Each month is a robust z-score against that ICB's own 12 months before the first official notice;
those 12 are each left out and scored against the other eleven, which says how often the rule fires
with no shortage declared. An ICB is flagged past Z, and the shortage is WIDE when 2+ ICBs are
flagged in each of 2+ NHS regions: triage.classify's NATIONAL rule.

    python -m anumaan.realcheck            # the table, from realcheck_england.json; no network
    python -m anumaan.realcheck --fetch    # re-download (150 queries, about 12 minutes) and rewrite it

What it does NOT show: it tests the premise, not the filter, which never runs here. Monthly ICB totals,
not patient records; English pharmacies, not Indian PHCs; two hand-picked shortages. A fingerprint that
an official notice ordered is not early warning. The JSON's caveats say the rest; summary() returns them.
"""
import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import numpy as np

DATA = Path(__file__).with_name("realcheck_england.json")
API = "https://opendata.nhsbsa.net/api/3/action/datastore_search_sql"
SQL = ("SELECT {area} AS icb, REGIONAL_OFFICE_CODE AS region, BNF_CODE AS code, BNF_DESCRIPTION AS name, "
       "SUM(ITEMS) AS items, SUM(TOTAL_QUANTITY) AS qty FROM `{table}` "
       "WHERE BNF_CHEMICAL_SUBSTANCE = '{substance}' GROUP BY icb, region, code, name")
# Fixed before the data was looked at, never tuned.
Z, BASE = 3.0, 12   # an ICB is flagged at 3 robust z from its own 12 months before the first official notice
FLOOR = 0.01        # on the MAD, so a flat baseline cannot explode z: 1% of the median (quantity per item), 1 point (share)


def scores(x, rel):
    """[I, M] robust z, (x - median) / (1.4826 MAD), of x [I, M] against each ICB's first BASE months: those each
    left out and scored against the others, the later months against all of them. rel: the MAD floor is a share
    of the median, not absolute."""
    def z(x, base):
        med = np.nanmedian(base, 1, keepdims=True)
        mad = np.nanmedian(np.abs(base - med), 1, keepdims=True)
        return (x - med) / (1.4826 * np.maximum(mad, FLOOR * (np.abs(med) if rel else 1)))
    base = x[:, :BASE]
    return np.hstack([z(base[:, k:k + 1], np.delete(base, k, 1)) for k in range(BASE)] + [z(x[:, BASE:], base)])


def wide(flag, region):
    """[M] bool: the month's flagged ICBs number 2+ in each of 2+ regions. triage.classify's NATIONAL
    rule, with an ICB for the warehouse and an NHS region for the state. flag: [I, M] bool."""
    return np.array([(np.unique(np.asarray(region)[f], return_counts=True)[1] >= 2).sum() >= 2 for f in flag.T])


def _study(s, region):
    """One medicine: ICBs flagged per month for each fingerprint, the months the wide rule was met, and how
    often both fire where no shortage was declared (a shortage's baseline months, a placebo's whole window)."""
    items, qty, subs = (np.array(s[k], float) for k in ("index_items", "index_qty", "substitute_items"))
    quiet = slice(None) if s["placebo"] else slice(BASE)
    out = dict(name=s["name"], notices=s["notices"], earlier_reports=s.get("earlier_reports", []), months=s["months"],
               notice_month=s["notice"], icbs=len(items), unit=s["unit"], ration=(qty.sum(0) / items.sum(0)).round(2).tolist(),
               sub_share=(subs.sum(0) / (items + subs).sum(0)).round(4).tolist())
    # a fall in quantity per item is a rise in its negative; an ICB with no items that month is NaN, never flagged
    for key, x, rel in (("cut_short", -qty / items, True), ("substituting", subs / (items + subs), False)):
        flag = scores(x, rel) >= Z
        hit = wide(flag, region)
        out[key] = dict(flagged=flag.sum(0).tolist(), wide_months=[m for m, h in zip(s["months"], hit) if h],
                        first_wide=next((m for m, h in zip(s["months"][BASE:], hit[BASE:]) if h), None),
                        false_flags=dict(months=int(hit[quiet].size), icb_rate=round(float(flag[:, quiet].mean()), 4),
                                         wide=[m for m, h in zip(s["months"][quiet], hit[quiet]) if h]))
    return out


def summary():
    """The whole check as a JSON-serialisable dict, from the cached file alone (no network)."""
    d = json.loads(DATA.read_text(encoding="utf-8"))
    region = [d["regions"][i] for i in sorted(d["regions"])]      # array rows are ICBs in sorted code order
    done = [(s["placebo"], _study(s, region)) for s in d["studies"]]
    return dict(source=dict(d["source"], fetched=d["fetched"]), caveats=d["caveats"],
                rule=dict(z=Z, mad_floor=FLOOR, baseline_months=BASE, wide="2+ flagged ICBs in each of 2+ NHS regions"),
                shortages=[r for p, r in done if not p], placebos=[r for p, r in done if p])


def _rows(substance, month):
    """One medicine, one month of the EPD: items and quantity per area and product. One request at a time, a pause
    before each, a timeout, two retries. Areas are STP_CODE up to April 2022, ICB_CODE after: the same 42 codes."""
    table = "EPD_" + month.replace("-", "")
    sql = SQL.format(area="ICB_CODE" if month >= "2022-05" else "STP_CODE", table=table, substance=substance)
    url = API + "?" + urllib.parse.urlencode({"resource_id": table, "sql": sql})
    for attempt in range(3):
        time.sleep(1 + 9 * attempt)
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return json.load(r)["result"]["result"]["records"]
        except Exception:
            if attempt == 2:
                raise


def fetch():
    """Re-download every study DATA defines and rewrite it. The file's "about" says how a study's substance,
    notice, index and substitutes pick the months and the products; what is fetched replaces what was there.
    ponytail: nothing is kept on disk between queries, so a failed run starts over; cache if the portal is flaky."""
    d = json.loads(DATA.read_text(encoding="utf-8"))
    for s in d["studies"]:                                      # BASE months before the notice month, it, BASE after
        s["months"] = np.arange(np.datetime64(s["notice"]) - BASE, np.datetime64(s["notice"]) + BASE + 1).astype(str).tolist()
    got = [[_rows(s["substance"], m) for m in s["months"]] for s in d["studies"]]
    d["regions"] = dict(sorted((r["icb"], r["region"]) for study in got for rows in study for r in rows if r["icb"] != "-"))
    icbs = list(d["regions"])
    for s, study in zip(d["studies"], got):
        tot = np.zeros((3, len(icbs), len(study)))             # index items, index quantity, substitute items
        names = dict(index=set(), substitute=set(), other=set())
        for k, rows in enumerate(study):
            for r in rows:
                w = s["index"].get(r["code"][-2:])
                kind = "index" if w else "substitute" if re.search(s["substitutes"], r["name"]) else "other"
                names[kind].add(r["name"])
                if r["icb"] != "-" and kind != "other":        # "-": practices the EPD could not place
                    i = icbs.index(r["icb"])
                    tot[0 if w else 2, i, k] += r["items"]
                    tot[1, i, k] += (w or 0) * r["qty"]
        s.update({f"{k}_names": sorted(v) for k, v in names.items()},
                 **dict(zip(("index_items", "index_qty", "substitute_items"), tot.round().astype(int).tolist())))
    d.update(fetched=date.today().isoformat(), query=SQL)
    text = re.sub(r"\n\s+(?=[-\d\]])", "", json.dumps(d, indent=1))     # one ICB's months to a line
    DATA.write_text(text + "\n", encoding="utf-8", newline="\n")


def main():
    ap = argparse.ArgumentParser(description="Premise check on real dispensing data for England.")
    ap.add_argument("--fetch", action="store_true", help="re-download from the NHSBSA Open Data Portal and rewrite the JSON")
    if ap.parse_args().fetch:
        fetch()
    s = summary()
    print(f"REAL dispensing data, premise check only - {s['source']['dataset']}, fetched {s['source']['fetched']}.\n"
          f"  Flagged: an ICB's robust z against its own {BASE} months before the first official notice passes {Z:g}. Wide: 2+\n"
          f"  flagged ICBs in each of 2+ NHS regions. First wide and most ICBs count from the notice month on (a placebo\n"
          f"  borrows its shortage's). No shortage declared: those {BASE} months, each left out in turn; all {2 * BASE + 1} of a placebo.\n")
    print(f"{'medicine':47}{'notice':9}{'fingerprint':14}{'first wide':15}{'most ICBs flagged':19}flagged with no shortage declared")
    for r in s["shortages"] + s["placebos"]:
        for key in ("cut_short", "substituting"):
            f, after, ff = r[key], r[key]["flagged"][BASE:], r[key]["false_flags"]
            first = f"{f['first_wide']} (+{r['months'].index(f['first_wide']) - BASE})" if f["first_wide"] else "never"
            peak = f"{max(after)}/{r['icbs']}" + (f" in {r['months'][BASE + after.index(max(after))]}" if max(after) else "")
            head = f"{r['name']:47}{r['notice_month']:9}" if key == "cut_short" else " " * 56
            print(f"{head}{key.replace('_', ' '):14}{first:15}{peak:19}{ff['icb_rate']:.1%} of ICB-months, wide in "
                  f"{len(ff['wide'])} of {ff['months']} months" + (": " + " ".join(ff["wide"]) if ff["wide"] else ""))
    print("\n" + "\n".join("  - " + c for c in s["caveats"]))


if __name__ == "__main__":
    main()
