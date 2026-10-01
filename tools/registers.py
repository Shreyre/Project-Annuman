"""SYNTHETIC register photos with an answer key, to measure the paper-PHC intake (anumaan/intake.py).

One PHC-day of a simulator run becomes 1-2 OPD register pages and 1-2 dispensing register
pages, handwritten in Kalam (tools/fonts, SIL OFL 1.1) and 'photographed': the page at a
slant on a table, uneven light, blur, sensor noise, JPEG. One slant of hand a page; repeated
values are often written as dittos, and now and then a value is struck out and written again.
The day's feeds.stream message is the answer key, with the true line behind every row, so a
read is scored exactly. About one patient in seven has a diagnosis the grammar does not
track, which a read must leave out. Latin script only: Kalam has no Odia or Malayalam glyphs,
and this Pillow has no raqm, so it cannot shape Devanagari. These are not real registers.

    python tools/registers.py eval --seeds 5-9 --n 7 --add 6:60:S1-W1-P5 --log calls.jsonl --out anumaan/intake_eval.json
    python tools/registers.py score anumaan/intake_eval.json     # re-score the saved reads: no Gemini call
    python tools/registers.py render --seed 6 --day 60 --phc S1-W1-P5 --eval anumaan/intake_eval.json --out tools/samples/
"""
import argparse
import hashlib
import io
import json
import os
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from functools import cache
from itertools import accumulate
from math import sqrt
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from anumaan import crg as G, feeds, intake, sim, voice  # noqa: E402

HAND = ROOT / "tools" / "fonts" / "Kalam-Regular.ttf"
ROWS = 30              # rows a register page holds; two pages a register at most
OTHER_SHARE = 0.15     # patients with an untracked diagnosis, per tracked one
# how often a value repeated from the row above is written as a ditto, and how often a row has a
# value struck out and written again: set by hand for realism, not tuned
DITTO, FIX = 0.3, 0.04
CAVEAT = "synthetic pages rendered in a handwriting font; not real registers"
# how staff write each id: what the model has to map back to the grammar's ids
DX = {"fever": ["Fever", "Viral fever", "PUO", "F/O fever"], "pneumonia": ["Pneumonia", "LRTI", "CAP"],
      "acute_diarrhoea": ["AGE", "Diarrhoea", "Loose motions"], "hypertension": ["HTN", "High BP", "Hypertension"],
      "type2_diabetes": ["DM-2", "T2DM", "Diabetes"], "uti": ["UTI", "Burning micturition"],
      "anaemia_pregnancy": ["ANC - anaemia", "Anaemia (preg)", "ANC, Hb 8.9"]}
RX = {"amoxicillin_500": ["Cap Amoxy 500", "Cap Amoxicillin 500"], "erythromycin_250": ["Tab Erythro 250"],
      "doxycycline_100": ["Cap Doxy 100"], "amlodipine_5": ["Tab Amlo 5", "Tab Amlodipine 5mg"],
      "nitrofurantoin_100": ["Tab Nitrofurantoin 100"], "trimethoprim_sulphamethoxazole_ds": ["Tab Septran DS"],
      "fosfomycin_3g": ["Fosfomycin 3g"], "norfloxacin_400": ["Tab Norflox 400"], "levofloxacin_250": ["Tab Levoflox 250"],
      "ifa_tab": ["Tab IFA", "IFA (red)"], "metformin_500": ["Tab Metformin 500", "Tab Metf 500"],
      "paracetamol_500": ["Tab PCM 500", "Tab Paracetamol 500"], "ors_sachet": ["ORS pkt", "ORS sachet"]}
# untracked diagnoses and what they are usually given: (medicine, dose, days, quantity)
OTHER = {"Scabies": ("Oint Permethrin 5%", "", 0, 1), "Cough/cold": ("Tab CPM 4", "1 TDS", 3, 9),
         "URTI": ("Tab Cetirizine 10", "1 OD", 5, 5), "Acidity": ("Tab Pantop 40", "1 OD", 5, 5),
         "Worms": ("Tab Albendazole 400", "1 stat", 1, 1), "Skin allergy": ("Tab Cetirizine 10", "1 OD", 5, 5),
         "Headache": ("Tab Diclofenac 50", "1 BD", 3, 6), "Dog bite": ("Inj ARV", "0.1 ml ID", 1, 1),
         "Conjunctivitis": ("Cipro eye drops", "1 drop QID", 0, 1)}
FREQ = {1: "OD", 2: "BD", 3: "TDS", 4: "QID"}
NAMES = dict(M="Ramesh Anil Suresh Rajesh Mahesh Deepak Vijay Manoj Arjun Ravi Sanjay Prakash".split(),
             F="Sunita Kavita Meena Pooja Asha Rekha Lata Geeta Anita Savita Radha Priya".split())
AGES = dict(anaemia_pregnancy=(18, 35), hypertension=(35, 80), type2_diabetes=(35, 80))    # else 3-80


def rows_of(m, rnd):
    """One PHC-day message -> (OPD rows, dispensing rows), each a dict of the truth behind the
    row plus the cells written on the page. Each slip and not-available slip goes to a patient
    of its condition, in arrival order."""
    pts = [c for c, n in m["diagnoses"].items() for _ in range(n)]
    extra = min(max(1, round(OTHER_SHARE * len(pts))), 2 * ROWS - len(pts))
    if extra < 0:
        raise ValueError(f"{len(pts)} patients do not fit two register pages")
    pts += [None] * extra
    rnd.shuffle(pts)
    opd, disp, waiting = [], [], {}
    for k, c in enumerate(pts, 1):
        no, sex = f"{k:03d}", "F" if c == "anaemia_pregnancy" else rnd.choice("MF")
        said = rnd.choice(DX[c]) if c else rnd.choice(sorted(OTHER))
        opd.append(dict(opd_no=no, condition=c or "other", written_as=said,
                        cells=(no, f"{rnd.choice(NAMES[sex])} {rnd.choice('KSMRPD')}.",
                               f"{rnd.randint(*AGES.get(c, (3, 80)))}/{sex}", said)))
        waiting.setdefault(c or said, []).append(no)

    def line(key, drug, written, dose, days, units, na=False):
        no = waiting[key].pop(0)
        disp.append(dict(opd_no=no, drug=drug, written_as=written, days=days, units=units, not_available=na,
                         cells=(no, written, dose, str(days) if days else "",
                                rnd.choice(["N/A", "NA", "Not avl"]) if na else f"{units:g}")))

    for c, d, days, units in m["slips"]:
        per_day = intake.DOSE.get((c, d), units / max(days, 1))
        line(c, d, rnd.choice(RX[d]), f"1 {FREQ[per_day]}" if per_day in FREQ else f"{per_day:g}/day", days, units)
    for c, d in m["not_available"]:
        line(c, d, rnd.choice(RX[d]), "", 0, 0, na=True)
    for said in [k for k in waiting if k in OTHER]:
        for _ in list(waiting[said]):
            if rnd.random() < 0.8:
                line(said, "other", *OTHER[said])
    disp.sort(key=lambda r: r["opd_no"])
    return opd, disp


@cache
def _font(size, hand=True):
    return ImageFont.truetype(str(HAND), size) if hand else ImageFont.load_default(size)


def page(title, head, widths, lines, rnd):
    """One A4 register page at 150 dpi: printed heading and rules, handwritten cells."""
    W, H, x0, y0, rh = 1240, 1754, 60, 130, 50
    img = Image.new("RGB", (W, H), (246, 242, 230))
    d = ImageDraw.Draw(img)
    xs = list(accumulate(widths, initial=x0))
    d.text((x0, 45), title, font=_font(34, False), fill=(40, 40, 40))
    for i, h in enumerate(head):
        d.text((xs[i] + 8, y0 + 12), h, font=_font(24, False), fill=(60, 60, 60))
    for r in range(ROWS + 2):
        d.line([(x0, y0 + r * rh), (xs[-1], y0 + r * rh)], fill=(150, 160, 190), width=2)
    for x in xs:
        d.line([(x, y0), (x, y0 + (ROWS + 1) * rh)], fill=(150, 160, 190), width=2)
    d.text((x0, H - 48), "SYNTHETIC: rendered by tools/registers.py from a simulator; not a real register",
           font=_font(18, False), fill=(120, 120, 120))
    ink = rnd.choice([(20, 30, 110), (25, 25, 25)])       # a blue or black ballpoint, one pen a page
    slant = rnd.uniform(-0.1, 0.25)                       # and one hand
    for r, cells in enumerate(lines, 1):
        for i, text in enumerate(cells):
            tile = Image.new("L", (widths[i] + 60, rh + 30), 0)
            pen, font, x = ImageDraw.Draw(tile), _font(rnd.randint(26, 31)), 10
            if isinstance(text, tuple):                   # (struck out, written again)
                gone, text = text
                pen.text((x, 6), gone, font=font, fill=255)
                pen.line([(x - 3, 27), (x + font.getlength(gone) + 3, 23)], fill=255, width=3)
                x += font.getlength(gone) + 14
            pen.text((x, 6), text, font=font, fill=255)
            tile = tile.transform(tile.size, Image.Transform.AFFINE, (1, slant, -slant * tile.height / 2, 0, 1, 0),
                                  Image.Resampling.BICUBIC)
            tile = tile.rotate(rnd.uniform(-2.5, 2.5), resample=Image.Resampling.BICUBIC)
            img.paste(Image.new("RGB", tile.size, ink), (xs[i] + rnd.randint(0, 8), y0 + r * rh + rnd.randint(-10, 0)), tile)
    return img


def written(rows, dittos, fixes, rnd):
    """A page's cells as a hand writes them: a ditto for a value repeated from the row above (in
    the columns dittos), and in some rows one value (in fixes) struck out and written again."""
    out, above, said = [], None, {x for xs in DX.values() for x in xs} | set(OTHER)
    for r in rows:
        cells = list(r["cells"])
        for i in dittos:
            if above and cells[i] and cells[i] == above[i] and rnd.random() < DITTO:
                cells[i] = rnd.choice(['"', "do", "-do-"])
        i = rnd.choice(fixes)
        if rnd.random() < FIX and cells[i] == r["cells"][i] and (cells[i].isdigit() or cells[i] in said):
            cells[i] = (str(int(cells[i]) + rnd.choice([1, 2, 10])) if cells[i].isdigit()
                        else rnd.choice(sorted(said - {cells[i]})), cells[i])
        above = r["cells"]
        out.append(cells)
    return out


def photograph(img, rnd):
    """A phone photo of the page -> JPEG bytes: the page at a slant on a table (each corner
    moved up to 3.5% of the page's size), uneven light, blur, sensor noise, JPEG quality 55-80.
    The noise is Pillow's effect_noise, drawn from C rand(): a page repeats byte for byte only
    when pages are rendered in the same order in a fresh process."""
    W, H, pad = *img.size, 70
    corners = [(0, 0), (W, 0), (W, H), (0, H)]
    seen = [(x + pad + rnd.uniform(-1, 1) * 0.035 * W, y + pad + rnd.uniform(-1, 1) * 0.035 * H) for x, y in corners]
    A, b = [], []
    for (x, y), (u, v) in zip(seen, corners):     # PIL maps each photo pixel back to the page
        A += [[x, y, 1, 0, 0, 0, -x * u, -y * u], [0, 0, 0, x, y, 1, -x * v, -y * v]]
        b += [u, v]
    img = img.transform((W + 2 * pad, H + 2 * pad), Image.Transform.PERSPECTIVE, tuple(np.linalg.solve(A, b)),
                        Image.Resampling.BICUBIC, fillcolor=(90, 80, 70))
    shade = Image.linear_gradient("L").rotate(rnd.choice([0, 90, 180, 270])).resize(img.size)
    img = Image.composite(img, ImageEnhance.Brightness(img).enhance(0.7), shade)
    img = img.filter(ImageFilter.GaussianBlur(rnd.uniform(0.5, 1.2)))
    img = Image.blend(img, Image.effect_noise(img.size, 12).convert("RGB"), 0.06)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=rnd.randint(55, 80))
    return out.getvalue()


def render(m, draw=True):
    """A PHC-day message -> ([(page name, JPEG bytes)], answer key); draw=False skips the pictures
    (bytes None) for a key alone. The key holds no names or ages."""
    rnd = random.Random(f"{m['phc']} {m['date']}")
    opd, disp = rows_of(m, rnd)
    pages = []
    for name, title, head, widths, rows, dittos, fixes in (
            ("opd", "OPD Register", ("OPD No", "Name", "Age/Sex", "Diagnosis"), (130, 330, 150, 510), opd, [3], [3]),
            ("disp", "Dispensing Register", ("OPD No", "Medicine", "Dose", "Days", "Qty"), (130, 430, 190, 120, 250),
             disp, [1, 2, 3, 4], [3, 4])):
        for k in range(0, max(len(rows), 1), ROWS):
            title_ = f"{title}  -  {m['phc']}  -  {m['date']}"
            cells = written(rows[k:k + ROWS], dittos, fixes, rnd)
            pages.append((f"{name}_{k // ROWS + 1}.jpg", photograph(page(title_, head, widths, cells, rnd), rnd) if draw else None))
    strip = lambda rows: [{k: v for k, v in r.items() if k != "cells"} for r in rows]
    key = dict(message={k: m[k] for k in ("kind", "date", "phc", "diagnoses", "slips", "not_available")},
               opd=strip(opd), dispensing=strip(disp), pages=[n for n, _ in pages],
               note=f"SYNTHETIC: {CAVEAT}. message is the simulator's PHC-day; opd and dispensing are the rows on the pages")
    return pages, key


@cache
def _run(seed):
    return sim.simulate(seed=seed)


def message_of(seed, t, phc):
    return next(m for m in feeds.stream(_run(seed))(t) if m["kind"] == "phc" and m["phc"] == phc)


def pick(seeds, n):
    """n PHC-days by a fixed rule, cycling through the seeds: after the 30-day warm-up, at least
    one diagnosis and room on two pages, alternately with and without a not-available slip."""
    rnd, out = random.Random(f"pick {seeds}"), []
    for i in range(n):
        run = _run(seeds[i % len(seeds)])
        na, per_day = {(t, f) for t, f, *_ in run.na}, run.dx.sum(2)
        cands = [(t, f) for t in range(30, len(run.dx)) for f in range(len(run.facilities))
                 if 0 < per_day[t, f] < 2 * ROWS and ((t, f) in na) == (i % 2 == 0)]
        t, f = rnd.choice(cands)
        out.append((seeds[i % len(seeds)], t, run.facilities[f]))
    return out


# ---------- scoring ----------

def _line(reg, x):
    """What must be read right for a line to count as right."""
    no = intake._no(x["opd_no"])
    if reg == "opd":
        return no, x["condition"]
    if x["drug"] == "other" or x["not_available"]:
        return no, x["drug"], x["not_available"]
    return no, x["drug"], False, int(x["days"]), round(float(x["units"]), 3)


def score(read, key):
    """One read against its answer key -> Counter of counts (summed over days by summarise)."""
    truth, ix, P = key["message"], intake.IX, intake.IX["primaries"]
    m, review = intake.to_message(read, truth["phc"], truth["date"])
    flagged = {(r["register"], r["line"]) for r in review}
    s = Counter(days=1, dx_true=sum(truth["diagnoses"].values()), cells=len(P),
                dx_abs_error=sum(abs(m["diagnoses"].get(c, 0) - truth["diagnoses"].get(c, 0)) for c in ix["conds"]))
    slip = lambda x: (x[0], x[1], int(x[2]), round(float(x[3]), 3))
    for name, a, b in (("slips", map(slip, m["slips"]), map(slip, truth["slips"])),
                       ("na", map(tuple, m["not_available"]), map(tuple, truth["not_available"]))):
        a, b = Counter(a), Counter(b)
        s[f"{name}_read"], s[f"{name}_true"], s[f"{name}_right"] = sum(a.values()), sum(b.values()), sum((a & b).values())

    def counts(msg):            # the per-medicine counts the filter takes (crg.aggregate)
        dx = np.zeros((1, 1, len(ix["conds"])), int)
        for c, n in msg["diagnoses"].items():
            dx[0, 0, ix["ci"][c]] = n
        return G.aggregate(ix, dx, [(0, 0, ix["ci"][c], ix["di"][d], int(n), float(u)) for c, d, n, u in msg["slips"]],
                           [(0, 0, ix["ci"][c], ix["di"][d]) for c, d in msg["not_available"]])
    got, want = counts(m), counts(truth)
    for k in ("N", *G.CATS):
        err = np.abs(got[k] - want[k])[0, 0, P]
        s[f"count_{k}_abs_error"], s[f"count_{k}_true"] = float(err.sum()), float(want[k][0, 0, P].sum())
        s[f"count_{k}_cells_wrong"] = int((err > 1e-9).sum())
    for reg in ("opd", "dispensing"):
        left = Counter(_line(reg, r) for r in key[reg])
        for i, x in enumerate(read[reg]):
            right, f = left[_line(reg, x)] > 0, (reg, i) in flagged
            left[_line(reg, x)] -= right
            s.update({f"{reg}_lines": 1, f"{reg}_right": right, "flagged": f, "wrong": not right,
                      "wrong_flagged": f and not right, "accepted": not f, "accepted_wrong": not (f or right)})
        s[f"{reg}_rows"] = len(key[reg])
        s["rows_missed"] += sum(v for v in left.values() if v > 0)
    other = Counter((intake._no(r["opd_no"]), r["condition"] == "other") for r in key["opd"])
    for x in read["opd"]:
        no, said_other = intake._no(x["opd_no"]), x["condition"] == "other"
        s["untracked_read_as_untracked"] += said_other and other[(no, True)] > 0
        s["tracked_read_as_untracked"] += said_other and other[(no, False)] > 0
        s["untracked_read_as_tracked"] += not said_other and other[(no, True)] > 0
    s["untracked_rows"] = sum(v for (_, o), v in other.items() if o)
    s.update(f"flag_{f}" for r in review for f in r["flags"])
    names = {n for ns in NAMES.values() for n in ns}      # every name on the pages comes from this list
    s["names_copied"] = sum(any(n in x["written_as"] for n in names) for reg in ("opd", "dispensing") for x in read[reg])
    return s


def wilson(k, n, z=1.96):
    """95% Wilson score interval for k of n."""
    if not n:
        return None
    p, h = k / n, z * z / n
    mid, half = (p + h / 2) / (1 + h), z * sqrt(p * (1 - p) / n + h / (4 * n)) / (1 + h)
    return [round(max(mid - half, 0.0), 3), round(min(mid + half, 1.0), 3)]


def rate(k, n):
    k, n = int(k), int(n)
    return dict(k=k, n=n, rate=round(k / n, 3) if n else None, ci95=wilson(k, n))


def summarise(days):
    s = sum((Counter(d["score"]) for d in days), Counter())
    per_day = [d["score"].get("dx_abs_error", 0) for d in days]
    lines = s["opd_lines"] + s["dispensing_lines"]
    return dict(
        phc_days=len(days), diagnoses=int(s["dx_true"]), lines_read=int(lines),
        diagnosis_count_error=dict(total=int(s["dx_abs_error"]), per_day_mean=round(float(np.mean(per_day)), 2),
                                   per_day_max=int(max(per_day)), per_100_diagnoses=round(100 * s["dx_abs_error"] / s["dx_true"], 2)),
        opd_lines_right=rate(s["opd_right"], s["opd_lines"]),
        dispensing_lines_right=rate(s["dispensing_right"], s["dispensing_lines"]),
        dispensed_lines_exact=dict(recall=rate(s["slips_right"], s["slips_true"]), precision=rate(s["slips_right"], s["slips_read"])),
        not_available=dict(recall=rate(s["na_right"], s["na_true"]), precision=rate(s["na_right"], s["na_read"])),
        filter_counts={k: dict(abs_error=s[f"count_{k}_abs_error"], true_total=s[f"count_{k}_true"],
                               medicine_days_exact=rate(s["cells"] - s[f"count_{k}_cells_wrong"], s["cells"]))
                       for k in ("N", *G.CATS)},
        rows_missed=rate(s["rows_missed"], s["opd_rows"] + s["dispensing_rows"]),
        review=dict(lines_flagged=rate(s["flagged"], lines), wrong_lines_flagged=rate(s["wrong_flagged"], s["wrong"]),
                    by_flag={k[5:]: int(v) for k, v in sorted(s.items()) if k.startswith("flag_")}),
        auto_accepted_wrong=rate(s["accepted_wrong"], s["accepted"]),
        untracked=dict(rows=int(s["untracked_rows"]), read_as_untracked=rate(s["untracked_read_as_untracked"], s["untracked_rows"]),
                       tracked_read_as_untracked=int(s["tracked_read_as_untracked"]),
                       untracked_read_as_tracked=int(s["untracked_read_as_tracked"])),
        lines_with_a_patient_name=int(s["names_copied"]),
        seconds=dict(median=round(float(np.median([d["seconds"] for d in days])), 1),
                     max=round(float(max(d["seconds"] for d in days)), 1)))


# ---------- the Gemini runs ----------

PROMPT_HASH = hashlib.sha256((intake.PROMPT + json.dumps(intake.Registers.model_json_schema(), sort_keys=True))
                             .encode()).hexdigest()[:12]


def _call(pages, log, gap, limit):
    """intake.read on the pages, `gap` s after the last logged call's end and never past `limit` calls."""
    calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    if len(calls) >= limit:
        sys.exit(f"{len(calls)} Gemini calls already logged in {log}: the budget is {limit}")
    if calls:
        time.sleep(max(0.0, calls[-1]["end"] + gap - time.time()))
    t0, err, out = time.time(), None, None
    try:
        out = intake.read([(b, "image/jpeg") for _, b in pages])
    except Exception as e:     # logged, then fatal: a failed call still counts against the budget
        err = f"{type(e).__name__}: {e}"
    end = time.time()
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(at=datetime.fromtimestamp(t0, timezone.utc).isoformat(timespec="seconds"),
                                 end=end, seconds=round(end - t0, 1), prompt=PROMPT_HASH, error=err)) + "\n")
    if err:
        sys.exit(f"Gemini call failed after {end - t0:.1f} s: {err}")
    return out, end - t0


def evaluate(picks, out, log, gap=20, limit=14, note="", tuning=()):
    """Render each PHC-day, read it (one Gemini call), score it; the JSON is rewritten after every
    day. Reads already in `out` under the same prompt and schema are reused, not re-read.
    tuning: earlier eval results (the tuning runs), listed alongside one line a read."""
    tuned = [dict(prompt_hash=t["prompt_hash"], seed=d["seed"], day=d["day"], phc=d["phc"], pages=d["pages"],
                  seconds=d["seconds"], lines=d["score"]["opd_lines"] + d["score"]["dispensing_lines"],
                  wrong=d["score"].get("wrong", 0)) for t in tuning for d in t["days"]]
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    saved = {(d["seed"], d["day"], d["phc"]): d for d in old.get("days", []) if old.get("prompt_hash") == PROMPT_HASH}
    voice.load_env()
    days, run_at = [], old.get("run_at") if saved else None
    for s, t, phc in picks:
        m = message_of(s, t, phc)
        pages, key = render(m)
        if (s, t, phc) in saved:
            read, secs = saved[(s, t, phc)]["read"], saved[(s, t, phc)]["seconds"]
        else:
            read, secs = _call(pages, log, gap, limit)
            run_at = datetime.now().astimezone().isoformat(timespec="minutes")
        days.append(dict(seed=s, day=t, phc=phc, date=m["date"], pages=len(pages), seconds=round(secs, 1),
                         score=dict(score(read, key)), read=read))
        calls = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        res = dict(caveat=CAVEAT, note=note, model=os.environ.get("ANUMAAN_MODEL", "gemini-3.7-flash"),
                   prompt_hash=PROMPT_HASH, min_confidence=intake.MIN_CONFIDENCE, timeout_s=intake.TIMEOUT_MS / 1000,
                   run_at=run_at, gemini_calls=dict(logged=len(calls), failed=sum(bool(c["error"]) for c in calls)),
                   tuning=tuned, summary=summarise(days), days=days)
        out.write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(f"seed {s} day {t} {phc}: {len(pages)} pages, {secs:.1f} s, {days[-1]['score'].get('wrong', 0)} wrong lines", flush=True)
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rd = sub.add_parser("render", help="one PHC-day's pages and answer key (sample.json) into a folder")
    rd.add_argument("--seed", type=int, required=True)
    rd.add_argument("--day", type=int, required=True)
    rd.add_argument("--phc", required=True)
    rd.add_argument("--out", type=Path, required=True)
    rd.add_argument("--eval", type=Path, help="attach this PHC-day's recorded read from an eval JSON")
    ev = sub.add_parser("eval", help="render, read with Gemini (one call a PHC-day) and score")
    ev.add_argument("--seeds", required=True, help="e.g. 5-9")
    ev.add_argument("--n", type=int, required=True, help="PHC-days drawn by pick()")
    ev.add_argument("--add", nargs="*", default=[], help="more PHC-days as SEED:DAY:PHC")
    ev.add_argument("--only", type=int, nargs="*", help="read just these PHC-days (0-based, in pick order)")
    ev.add_argument("--out", type=Path, required=True)
    ev.add_argument("--log", type=Path, required=True, help="JSON lines, one per Gemini call: the budget's ledger")
    ev.add_argument("--max-calls", type=int, default=14)
    ev.add_argument("--gap", type=float, default=20, help="seconds from one call's end to the next call")
    ev.add_argument("--timeout", type=float, help="seconds a read may take (default: intake.TIMEOUT_MS)")
    ev.add_argument("--note", default="")
    ev.add_argument("--tuning", type=Path, nargs="*", default=[], help="tuning eval JSONs to list alongside")
    sc = sub.add_parser("score", help="re-score the reads saved in an eval JSON, and write the scores back")
    sc.add_argument("path", type=Path)
    a = ap.parse_args()
    if a.cmd == "render":
        ev_ = json.loads(a.eval.read_text(encoding="utf-8")) if a.eval else dict(days=[])
        at, me = [(d["seed"], d["day"], d["phc"]) for d in ev_["days"]], (a.seed, a.day, a.phc)
        for s, t, p in at[:at.index(me)] if me in at else ():     # the eval's order: its exact pages (photograph)
            render(message_of(s, t, p))
        pages, key = render(message_of(*me))
        a.out.mkdir(parents=True, exist_ok=True)
        for name, data in pages:
            (a.out / name).write_bytes(data)
        key.update(seed=a.seed, day=a.day)
        if me in at:
            d = ev_["days"][at.index(me)]
            key["recorded_read"] = dict(read=d["read"], seconds=d["seconds"], model=ev_["model"],
                                        prompt_hash=ev_["prompt_hash"], run_at=ev_["run_at"])
        (a.out / "sample.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
        print(f"{len(pages)} pages and sample.json written to {a.out}")
    elif a.cmd == "eval":
        lo, _, hi = a.seeds.partition("-")
        picks = pick(list(range(int(lo), int(hi or lo) + 1)), a.n)
        picks += [(int(s), int(t), p) for s, t, p in (x.split(":") for x in a.add)]
        if a.only is not None:
            picks = [picks[i] for i in a.only]
        if a.timeout:     # a budgeted call is not spent on the app's limit; every read's seconds are saved
            intake.TIMEOUT_MS = int(a.timeout * 1000)
        tuning = [json.loads(p.read_text(encoding="utf-8")) for p in a.tuning]
        print(json.dumps(evaluate(picks, a.out, a.log, a.gap, a.max_calls, a.note, tuning)["summary"], indent=1))
    else:
        res = json.loads(a.path.read_text(encoding="utf-8"))
        for d in res["days"]:
            d["score"] = dict(score(d["read"], render(message_of(d["seed"], d["day"], d["phc"]), draw=False)[1]))
        res.update(min_confidence=intake.MIN_CONFIDENCE, summary=summarise(res["days"]))
        a.path.write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(json.dumps(res["summary"], indent=1))


if __name__ == "__main__":
    main()
