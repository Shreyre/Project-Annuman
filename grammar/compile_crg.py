"""Compile treatment-guideline PDFs into a cited Care-to-Resource Grammar with Gemini.

    python grammar/compile_crg.py first.pdf second.pdf -o grammar/crg/compiled.json

Needs GOOGLE_GENAI_USE_VERTEXAI=true, GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION in the
environment (or GEMINI_API_KEY). The PDFs we compile are listed in grammar/sources/urls.txt.

Only the seed grammar's tracer conditions are compiled, under its condition and drug ids, so
the output is a drop-in for the simulator. List the PDFs in priority order: when two give a
course for the same condition and drug, the first PDF wins.

Pass 1 extracts each tracer condition's first-line course with its guideline-permitted
substitutes and a page citation. Pass 2 re-reads the PDF and drops any rule the text does
not support. The output loads with anumaan.crg.load().
"""
import argparse
import json
import os
import re
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import BaseModel

MODEL = os.environ.get("ANUMAAN_MODEL", "gemini-3.7-flash")
SEED = json.loads((Path(__file__).parent / "crg" / "tracer.json").read_text(encoding="utf-8"))


class Citation(BaseModel):
    page: int
    quote: str


class Substitute(BaseModel):
    drug_id: str
    units_per_day: float
    days: int


class Course(BaseModel):
    drug_id: str            # snake_case generic name + strength, e.g. amoxicillin_500
    unit: str               # tab / cap / sachet / ml / vial
    units_per_day: float
    days: int
    substitutes: list[Substitute]
    citation: Citation


class Condition(BaseModel):
    condition_id: str       # one of the seed's tracer conditions, e.g. pneumonia
    courses: list[Course]


class Grammar(BaseModel):
    conditions: list[Condition]


class Check(BaseModel):
    condition_id: str
    drug_id: str
    supported: bool
    issue: str


class Checks(BaseModel):
    checks: list[Check]


EXTRACT = """You are compiling a machine-readable care-to-resource grammar from a national
standard treatment guideline. Cover only these Primary Health Centre tracer conditions, with
exactly these condition_id values: fever (acute undifferentiated fever), pneumonia
(community-acquired, adult outpatient), acute_diarrhoea, hypertension, type2_diabetes,
uti (uncomplicated), anaemia_pregnancy (mild or moderate anaemia diagnosed in pregnancy).
Skip any of them the document does not cover.

For each, list the course the guideline says to give every adult outpatient with it first
(the pregnant woman for anaemia_pregnancy). Symptomatic treatment counts when it is the
treatment (paracetamol for fever), and so do fluids dispensed as sachets (ORS). Leave out
optional add-ons, second-line escalation, and drugs only for children, severe cases or
comorbidities.
- drug_id: reuse one of DRUGS when the generic name and strength match exactly; otherwise
  snake_case generic name plus strength in mg without the unit (amoxicillin_500), other units
  written out (fosfomycin_3g), or the dosage form when there is no single strength (ors_sachet).
- unit (tab / cap / sachet / ml / vial) and units_per_day in that unit; one ORS sachet makes
  1 litre.
- days: the stated duration. For long-term or monitored regimens, the stated review or
  follow-up interval (review after 4 weeks -> 28); for symptomatic treatment, the day by which
  the guideline says to refer or review if it persists.
- substitutes: only those the guideline itself permits if the first-line drug is unavailable
  or contraindicated.
- citation: the page of the PDF file counting its first page as 1 (not the number printed on
  the page), and a short verbatim quote.
Only include what the document states; never fill gaps from general knowledge.
DRUGS: """ + ", ".join(sorted(SEED["drugs"]))

VERIFY = """For each rule below, re-read the attached guideline and decide whether the
document supports the drug, the units per day, the duration and the substitutes as written.
Unit conversions (litres of ORS to 1-litre sachets) and durations taken from a stated review,
follow-up or referral interval count as supported.
Mark supported=false and explain in `issue` if any part is not in the text.
RULES:
"""


def norm(drug_id):
    """amoxicillin_500mg -> amoxicillin_500: mg is the implied unit in drug ids."""
    return re.sub(r"(?<=\d)mg(?=_|$)", "", drug_id)


def ask(client, pdf, prompt, schema):
    part = types.Part.from_bytes(data=pdf, mime_type="application/pdf")
    resp = client.models.generate_content(
        model=MODEL, contents=[part, prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema))
    return resp.parsed


def compile_pdf(client, path):
    pdf = Path(path).read_bytes()
    grammar = ask(client, pdf, EXTRACT, Grammar)
    rules = [(c.condition_id, co.drug_id, co.units_per_day, co.days, [s.drug_id for s in co.substitutes])
             for c in grammar.conditions for co in c.courses]
    checks = ask(client, pdf, VERIFY + json.dumps(rules), Checks)
    bad = {(k.condition_id, k.drug_id): k.issue for k in checks.checks if not k.supported}
    return grammar, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", help="in priority order: the first PDF wins a condition+drug pair")
    ap.add_argument("-o", "--out", default="grammar/crg/compiled.json")
    a = ap.parse_args()
    client = genai.Client()
    crg = {"version": f"compiled-{MODEL}",
           "source_note": "Gemini-compiled from: " + ", ".join(Path(p).name for p in a.pdfs),
           "drugs": {}, "conditions": {}, "rejected": [], "skipped": []}
    first = {}     # (condition, drug) -> the PDF that supplied it
    for path in a.pdfs:
        src = Path(path).name
        grammar, bad = compile_pdf(client, path)
        for c in grammar.conditions:
            for co in c.courses:
                key, drug = (c.condition_id, co.drug_id), norm(co.drug_id)
                dropped = dict(condition=c.condition_id, drug=drug, source=src)
                if key in bad:
                    crg["rejected"].append(dropped | dict(issue=bad[key]))
                elif c.condition_id not in SEED["conditions"]:
                    crg["skipped"].append(dropped | dict(why="not a tracer condition"))
                elif (c.condition_id, drug) in first:
                    crg["skipped"].append(dropped | dict(why=f"{first[c.condition_id, drug]} already gives it"))
                else:
                    first[c.condition_id, drug] = src
                    crg["drugs"].setdefault(drug, {"unit": co.unit})
                    for s in co.substitutes:
                        crg["drugs"].setdefault(norm(s.drug_id), {"unit": co.unit})
                    crg["conditions"].setdefault(c.condition_id, {"courses": []})["courses"].append(dict(
                        drug=drug, share=1.0, units_per_day=co.units_per_day, days=co.days,
                        substitutes=[dict(drug=norm(s.drug_id), units_per_day=s.units_per_day, days=s.days)
                                     for s in co.substitutes],
                        citation=dict(source=src, page=co.citation.page, quote=co.citation.quote)))
    Path(a.out).write_text(json.dumps(crg, indent=2, ensure_ascii=False), encoding="utf-8")
    n = sum(len(v["courses"]) for v in crg["conditions"].values())
    missing = sorted(set(SEED["conditions"]) - set(crg["conditions"]))
    print(f"{n} rules kept for {len(crg['conditions'])}/{len(SEED['conditions'])} tracer conditions "
          f"(missing: {', '.join(missing) or 'none'}); {len(crg['rejected'])} rejected by the verifier, "
          f"{len(crg['skipped'])} skipped -> {a.out}")


if __name__ == "__main__":
    main()
