"""Compile treatment-guideline PDFs into a cited Care-to-Resource Grammar with Gemini.

    GEMINI_API_KEY=... python grammar/compile_crg.py stw_pneumonia.pdf stw_diarrhoea.pdf -o grammar/crg/compiled.json

(or set GOOGLE_GENAI_USE_VERTEXAI=true, GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION for Vertex AI)

Pass 1 extracts every first-line drug course with its guideline-permitted
substitutes and a page citation. Pass 2 re-reads the PDF and drops any rule the
text does not support. The output loads with anumaan.crg.load().
"""
import argparse
import json
import os
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import BaseModel

MODEL = os.environ.get("ANUMAAN_MODEL", "gemini-3.7-flash")


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
    condition_id: str       # snake_case, e.g. pneumonia
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


EXTRACT = """You are compiling a machine-readable care-to-resource grammar from an Indian
standard treatment guideline. For every condition treatable at a Primary Health Centre,
list each FIRST-LINE adult outpatient drug course: drug_id as snake_case generic name plus
strength (e.g. amoxicillin_500), dosage unit, units per day, number of days, and the
substitutes the guideline itself permits if the first-line drug is unavailable or
contraindicated. Cite the page number and a short verbatim quote for each course.
Only include what the document states; never fill gaps from general knowledge."""

VERIFY = """For each rule below, re-read the attached guideline and decide whether the
document supports the drug, the units per day, the duration and the substitutes as written.
Mark supported=false and explain in `issue` if any part is not in the text.
RULES:
"""


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
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("-o", "--out", default="grammar/crg/compiled.json")
    a = ap.parse_args()
    client = genai.Client()
    crg = {"version": f"compiled-{MODEL}", "source_note": "Gemini-compiled from: " + ", ".join(map(str, a.pdfs)),
           "drugs": {}, "conditions": {}, "rejected": []}
    for path in a.pdfs:
        grammar, bad = compile_pdf(client, path)
        for c in grammar.conditions:
            for co in c.courses:
                if (c.condition_id, co.drug_id) in bad:
                    crg["rejected"].append(dict(condition=c.condition_id, drug=co.drug_id,
                                                issue=bad[(c.condition_id, co.drug_id)], source=str(path)))
                    continue
                crg["drugs"].setdefault(co.drug_id, {"unit": co.unit})
                for s in co.substitutes:
                    crg["drugs"].setdefault(s.drug_id, {"unit": co.unit})
                crg["conditions"].setdefault(c.condition_id, {"courses": []})["courses"].append(dict(
                    drug=co.drug_id, share=1.0, units_per_day=co.units_per_day, days=co.days,
                    substitutes=[s.model_dump(exclude={"drug_id"}) | {"drug": s.drug_id} for s in co.substitutes],
                    citation=dict(source=Path(path).name, page=co.citation.page, quote=co.citation.quote)))
    Path(a.out).write_text(json.dumps(crg, indent=2, ensure_ascii=False), encoding="utf-8")
    n = sum(len(v["courses"]) for v in crg["conditions"].values())
    print(f"{n} rules kept, {len(crg['rejected'])} rejected by the verifier -> {a.out}")


if __name__ == "__main__":
    main()
