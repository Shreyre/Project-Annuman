"""Shelf confirmation by voice: a pharmacist says whether a medicine is finished.

The filter asks one question when it is unsure: "is <drug> finished on your shelf?"
The PHC pharmacist or community health officer answers out loud, in Odia, Malayalam,
Hindi or English. Gemini on Vertex AI listens to the clip and fills a fixed form:
empty / available / unclear, the cause if they gave one, what they said and its
English translation. Only "empty" and "available" are evidence for the filter;
"unclear" means ask again or tap the button.

    python -m anumaan.voice answer.webm --drug amoxicillin_500 [--language or]

Needs GOOGLE_GENAI_USE_VERTEXAI=true, GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION
(global) and application-default credentials; read from .env if present.
"""
import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Literal

import httpx     # google-genai's transport; its timeouts and connection errors surface raw
from google import genai
from google.auth.exceptions import GoogleAuthError
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

ANSWERS = ("empty", "available", "unclear")
CAUSES = ("indent_pending", "warehouse_short", "expired_or_damaged", "demand_up",
          "not_prescribed", "other", "unknown")
MAX_BYTES = 8 << 20      # a spoken answer is seconds long; refuse anything this big
# google-genai's default is no timeout and no retries: a stalled connection blocks forever.
# ponytail: one attempt, 30 s; add HttpOptions(retry_options=...) if 429s show up in the field
TIMEOUT_MS = 30_000
# ponytail: the model's self-reported confidence is uncalibrated; calibrate this cut
# on labelled clips per language before trusting it
MIN_CONFIDENCE = 0.6
ENV = Path(__file__).resolve().parent.parent / ".env"

PROMPT = """A pharmacist or community health officer at a Primary Health Centre in India
was asked: "Is {drug} finished on your shelf?" They may answer in Odia, Malayalam,
Hindi, English or a mix.{hint} Listen to the recording and fill every field:
- transcript: their words, in the script of the language they spoke.
- transcript_en: an English translation.
- language: ISO 639-1 code of the language spoken (or, ml, hi, en).
- answer: "empty" if they say none is left on the shelf; "available" if they say some
  is still there; "unclear" if you cannot tell, or they talk about a different medicine.
- cause: why it ran short, only if they say so: indent_pending (ordered, not delivered),
  warehouse_short (the warehouse has none), expired_or_damaged, demand_up (more patients
  than usual), not_prescribed (in stock but doctors are not prescribing it), other;
  "unknown" if they give no reason.
- confidence: 0 to 1, how sure you are of the answer field."""


class Answer(BaseModel):
    # field order is generation order: transcribe and translate first, then decide
    transcript: str
    transcript_en: str
    language: str
    answer: Literal[ANSWERS]
    cause: Literal[CAUSES]
    confidence: float


class VoiceUnavailable(RuntimeError):
    """Gemini gave no answer (not configured, refused, failed or timed out); fall back to the buttons."""


def load_env(path=ENV):
    """KEY=VALUE lines from .env into os.environ; the real environment wins. Never prints values."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        k, sep, v = line.strip().removeprefix("export ").partition("=")
        if sep and k.strip() and not k.startswith("#"):
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_HOWTO = ("Gemini voice is not configured ({why}): set GOOGLE_GENAI_USE_VERTEXAI=true, "
          "GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION=global, and run "
          "`gcloud auth application-default login` (on Cloud Run: a service account with "
          "roles/aiplatform.user)")


def _client():
    load_env()
    try:
        return genai.Client()     # reads GOOGLE_GENAI_USE_VERTEXAI / _CLOUD_PROJECT / _CLOUD_LOCATION
    except (ValueError, GoogleAuthError) as e:
        raise VoiceUnavailable(_HOWTO.format(why=type(e).__name__)) from e


def label_voice(audio: bytes, mime: str, drug: str, language: str | None = None, client=None) -> dict:
    """One spoken answer -> dict(answer, cause, transcript, transcript_en, language, confidence)."""
    mime = mime.split(";")[0].strip().lower()     # browsers send "audio/webm;codecs=opus"
    if not audio or len(audio) > MAX_BYTES or not mime.startswith("audio/"):
        raise ValueError(f"need an audio/* clip under {MAX_BYTES >> 20} MB, got {mime} ({len(audio)} bytes)")
    client = client or _client()
    hint = f" They were expected to speak {language}." if language else ""
    try:
        resp = client.models.generate_content(
            model=os.environ.get("ANUMAAN_MODEL", "gemini-3.7-flash"),
            contents=[types.Part.from_bytes(data=audio, mime_type=mime),
                      PROMPT.format(drug=drug.replace("_", " "), hint=hint)],
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=Answer, temperature=0,
                                               http_options=types.HttpOptions(timeout=TIMEOUT_MS)))
    except GoogleAuthError as e:
        raise VoiceUnavailable(_HOWTO.format(why=type(e).__name__)) from e
    except errors.APIError as e:     # the original error stays chained for the logs
        if e.code in (401, 403):
            raise VoiceUnavailable(f"Vertex AI refused the call ({e.code}): check billing, the "
                                   "aiplatform API and roles/aiplatform.user") from e
        raise VoiceUnavailable(f"Gemini call failed ({e.code} {e.status}): {e.message}") from e
    except httpx.TransportError as e:     # timeouts, refused or dropped connections
        raise VoiceUnavailable(f"Gemini did not answer ({type(e).__name__}, limit {TIMEOUT_MS // 1000} s)") from e
    try:
        out = Answer.model_validate(resp.parsed).model_dump()
    except ValidationError:     # off-schema or empty reply: never guess a shelf state
        return dict(answer="unclear", cause="unknown", transcript=getattr(resp, "text", None) or "",
                    transcript_en="", language=language or "", confidence=0.0)
    out["confidence"] = min(max(out["confidence"], 0.0), 1.0)
    if out["confidence"] < MIN_CONFIDENCE:
        out["answer"] = "unclear"
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path")
    ap.add_argument("--drug", required=True)
    ap.add_argument("--language")
    ap.add_argument("--mime", help="default: guessed from the file name")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")     # Odia / Malayalam script on a Windows pipe
    # mimetypes calls .webm video/webm; a voice note is audio
    mime = a.mime or (mimetypes.guess_type(a.path)[0] or "audio/webm").replace("video/", "audio/")
    try:
        print(json.dumps(label_voice(Path(a.path).read_bytes(), mime, a.drug, a.language),
                         ensure_ascii=False, indent=2))
    except VoiceUnavailable as e:
        sys.exit(f"voice unavailable: {e}")
