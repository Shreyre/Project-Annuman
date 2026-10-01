"""Paper-PHC intake: Gemini reads photos of a PHC's OPD register and dispensing register
for one day into the same PHC-day message the live feed takes (feeds.stream / feeds.absorb).

read() makes one Gemini call; to_message() joins the dispensing lines to the OPD lines by
OPD number, builds the message and lists the lines a person should check. Nothing here
applies anything: the app shows the message and the flagged lines, and a person confirms.
The schema has no field for names, ages or any other patient identifier.

Accuracy on SYNTHETIC pages: anumaan/intake_eval.json (python tools/registers.py eval).
"""
import re
from types import SimpleNamespace
from typing import Literal

from google.genai import types
from pydantic import BaseModel, ValidationError

from anumaan import crg as G, feeds
from anumaan.voice import VoiceUnavailable, _client, _generate

IX = G.index(G.load())
MAX_IMAGES, MAX_BYTES = 4, 5 << 20
# a four-page read ran past the voice check's 30 s on the first tuning call (a 504 from Vertex);
# the tuning reads (seeds 0-4, 3-4 pages) then took 41-49 s
TIMEOUT_MS = 120_000
# ponytail: not tuned: every line of the tuning reads (seeds 0-4) came back at confidence 1.0, so
# the model's own confidence says nothing yet; calibrate it on real pages. 0.6 is the voice check's
MIN_CONFIDENCE = 0.6
# (condition, medicine) -> the guideline's units a day, for its course and each substitute
DOSE = {(IX["conds"][c], IX["drugs"][d]): u for (c, p), (upd, _, subs) in IX["course"].items()
        for d, u in [(p, upd), *((s, su) for s, su, _ in subs)]}
IMPLIED = {d: c for c, d in DOSE if sum(d2 == d for _, d2 in DOSE) == 1}    # medicines only one condition uses
# what each condition covers, as registers write it: added after a tuning page (seeds 0-4) read
# "LRTI" as other twice. SYNTHETIC caveat: tools/registers.py writes these same abbreviations
MEANS = {"fever": "fever or pyrexia (PUO, viral fever)",
         "pneumonia": "pneumonia or a lower respiratory tract infection (LRTI, CAP)",
         "acute_diarrhoea": "acute diarrhoea or gastroenteritis (AGE, loose motions)",
         "hypertension": "high blood pressure (HTN)", "type2_diabetes": "type 2 diabetes (DM, T2DM)",
         "uti": "urinary tract infection or its symptoms (burning micturition)",
         "anaemia_pregnancy": "anaemia in pregnancy (an ANC visit with low Hb)"}
_conditions = "\n".join(f"  {c}: {MEANS.get(c, c)}" for c in IX["conds"])

PROMPT = f"""These photos are one day's registers from a Primary Health Centre in India: pages of
the OPD register (one row per patient) and of the dispensing or prescription register (one
row per medicine given or asked for). Read every handwritten row on every page, in order.
A ditto mark (", do or -do-) means the same as the row above: write that value out. Where a
value is struck out and written again, use the new value. Never copy patient names, ages,
addresses or phone numbers.

opd: one entry per OPD register row.
- opd_no: the OPD number as written.
- written_as: the diagnosis as written.
- condition: which of these it is, or "other" for any other diagnosis:
{_conditions}
- confidence: 0 to 1, how sure you are of this row.

dispensing: one entry per dispensing register row.
- opd_no: the OPD number as written.
- written_as: the medicine as written.
- drug: which of these it is: {", ".join(IX["drugs"])}; "other" for any other medicine or strength.
- days: the days of treatment written; 0 if none.
- units: the quantity given, in tablets, capsules or sachets; 0 if none was given.
- not_available: true if the row records the medicine as not available (N/A, NA, not
  available, out of stock) instead of giving it.
- confidence: 0 to 1, how sure you are of this row."""


class OpdLine(BaseModel):
    # field order is generation order: read what is written first, then map it
    opd_no: str
    written_as: str
    condition: Literal[(*IX["conds"], "other")]
    confidence: float


class DispensingLine(BaseModel):
    opd_no: str
    written_as: str
    drug: Literal[(*IX["drugs"], "other")]
    days: int
    units: float
    not_available: bool
    confidence: float


class Registers(BaseModel):
    opd: list[OpdLine]
    dispensing: list[DispensingLine]


def _kind(data):
    """What the bytes are, from their signature: the declared type alone is not trusted."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"


def check(images):
    """[(bytes, mime)] -> the same with each mime normalised; a bad upload raises ValueError.
    The app runs it before it spends a Gemini call; read() runs it again."""
    if not isinstance(images, (list, tuple)) or not 1 <= len(images) <= MAX_IMAGES:
        raise ValueError(f"send 1 to {MAX_IMAGES} photos of the register pages")
    out = []
    for data, mime in images:
        mime = str(mime or "").split(";")[0].strip().lower()
        if not isinstance(data, bytes) or len(data) > MAX_BYTES or _kind(data) != mime:
            raise ValueError(f"each photo must be a JPEG, PNG or WebP of at most {MAX_BYTES >> 20} MB; "
                             f"got {mime or 'no type'} ({len(data) if isinstance(data, bytes) else 0} bytes)")
        out.append((data, mime))
    return out


def read(images, client=None) -> dict:
    """Photos of one PHC-day's register pages, [(bytes, mime)] -> dict(opd=[...], dispensing=[...])
    as the Registers schema has them. A bad upload raises ValueError before any Gemini call;
    no usable answer raises voice.VoiceUnavailable."""
    parts = [types.Part.from_bytes(data=data, mime_type=mime) for data, mime in check(images)]
    # the instructions go before the images
    resp = _generate(client or _client(), [PROMPT, *parts], Registers, timeout=TIMEOUT_MS)
    try:
        out = Registers.model_validate(resp.parsed).model_dump()
    except ValidationError as e:     # never show half a register
        raise VoiceUnavailable("Gemini returned no usable read of the registers") from e
    for line in out["opd"] + out["dispensing"]:
        line["confidence"] = min(max(line["confidence"], 0.0), 1.0)
    return out


WHY = {"low_confidence": "Gemini was not sure of this line",
       "other": "not a condition or medicine Anumaan tracks, so it is left out",
       "unjoined": "no single OPD line has this OPD number; the condition is taken from the medicine",
       "not_in_course": "this medicine is not in the guideline course for the diagnosis",
       "units": "the quantity does not match the days at the guideline's daily dose"}


def _no(s):
    """OPD numbers as join keys: '007', '7' and ' 7.' are one number."""
    s = re.sub(r"\W", "", str(s)).upper()
    return s.lstrip("0") or s


def to_message(read, phc, date):
    """A read (read()'s output, or a person's correction of it) -> (message, review).

    message: the feeds.stream PHC message for that day: diagnoses, slips [condition, drug,
    days, units] and not_available [condition, drug]. It has no "stock": feeds.absorb keeps
    the register's last balance. review: the lines to check, OPD lines first, each
    dict(register, line, opd_no, written_as, flags, why); a line flagged "other" is left out.
    ValueError if the read is malformed or feeds.absorb would not take the message."""
    lines = Registers.model_validate(read).model_dump()      # pydantic's ValidationError is a ValueError
    flags = {}
    flag = lambda reg, i, f: flags.setdefault((reg, i), []).append(f)
    dx, conds_at = {}, {}
    for i, x in enumerate(lines["opd"]):
        conds_at.setdefault(_no(x["opd_no"]), set()).add(x["condition"])
        if x["confidence"] < MIN_CONFIDENCE:
            flag("opd", i, "low_confidence")
        if x["condition"] == "other":
            flag("opd", i, "other")
        else:
            dx[x["condition"]] = dx.get(x["condition"], 0) + 1
    slips, na = [], []
    for i, x in enumerate(lines["dispensing"]):
        d, key = x["drug"], _no(x["opd_no"])
        cs = conds_at.get(key, set()) if key else set()
        if x["confidence"] < MIN_CONFIDENCE:
            flag("dispensing", i, "low_confidence")
        if len(cs) == 1:
            (c,) = cs
        else:        # no OPD line with this number, or lines that disagree on the condition
            flag("dispensing", i, "unjoined")
            c = IMPLIED.get(d, "other")
        if "other" in (c, d):
            flag("dispensing", i, "other")
            continue
        dose = DOSE.get((c, d))
        if dose is None:
            flag("dispensing", i, "not_in_course")
        if x["not_available"]:
            bad = x["units"] > 0
        else:
            bad = x["days"] < 1 or x["units"] <= 0 or (dose is not None and abs(x["units"] - x["days"] * dose) > 1e-6)
        if bad:
            flag("dispensing", i, "units")
        if x["not_available"]:
            na.append([c, d])
        else:
            slips.append([c, d, int(x["days"]), float(x["units"])])
    m = dict(kind="phc", date=str(date), phc=str(phc), diagnoses=dx, slips=slips, not_available=na)
    # absorb's own parser, on a stand-in network of just this PHC: it checks and writes nothing
    feeds.absorb(m, SimpleNamespace(ix=IX, book=range(1 << 20), facilities=[m["phc"]]), None, skip=lambda *a: True)
    review = [dict(register=reg, line=i, opd_no=lines[reg][i]["opd_no"], written_as=lines[reg][i]["written_as"],
                   flags=fl, why="; ".join(WHY[f] for f in fl))
              for (reg, i), fl in sorted(flags.items(), key=lambda kv: (kv[0][0] != "opd", kv[0][1]))]
    return m, review
