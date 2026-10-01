"""Shelf confirmation by voice: a pharmacist says whether a medicine is finished.

The filter asks one question when it is unsure: "is <drug> finished on your shelf?"
The PHC pharmacist or community health officer answers out loud, in Odia, Malayalam,
Hindi or English. Gemini on Vertex AI listens to the clip and fills a fixed form:
empty / available / unclear, the cause if they gave one, what they said and its
English translation. Only "empty" and "available" are evidence for the filter;
"unclear" means ask again or tap the button.

The same Gemini client writes the district officer's brief for an alarm (write_brief):
the evidence in two or three sentences and a next step, in English or one of the 15
scheduled languages Gemini documents. check() holds each brief to the evidence's numbers
and its language's script, and speak() reads a brief aloud with Gemini-TTS.

    python -m anumaan.voice answer.webm --drug amoxicillin_500 [--language or]

Needs GOOGLE_GENAI_USE_VERTEXAI=true, GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION
(global) and application-default credentials; read from .env if present.
"""
import argparse
import io
import json
import mimetypes
import os
import re
import sys
import time
import unicodedata
import wave
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
# Vertex's shared Gemini quota answers 429 in bursts (seen on this project), so a 429 gets
# two more tries, 2 s apart then 4 s; everything else fails fast.
RETRY = types.HttpRetryOptions(attempts=3, initialDelay=2, maxDelay=8, httpStatusCodes=[429])
TIMEOUT_MS = 30_000
# ponytail: the model's self-reported confidence is uncalibrated; calibrate this cut
# on labelled clips per language before trusting it
MIN_CONFIDENCE = 0.6
ENV = Path(__file__).resolve().parent.parent / ".env"
answered = {}     # model -> when this process last had an answer from it (app/main.py's /api/google)

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


def _generate(client, contents, schema=None, model=None, timeout=TIMEOUT_MS, **config):
    """One Gemini call, JSON-schema when given a schema; every way of getting no answer becomes VoiceUnavailable."""
    if schema:
        config.update(response_mime_type="application/json", response_schema=schema, temperature=0)
    model = model or os.environ.get("ANUMAAN_MODEL", "gemini-3.7-flash")
    try:
        resp = client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(**config, http_options=types.HttpOptions(timeout=timeout,
                                                                                        retry_options=RETRY)))
    except GoogleAuthError as e:
        raise VoiceUnavailable(_HOWTO.format(why=type(e).__name__)) from e
    except errors.APIError as e:     # the original error stays chained for the logs
        if e.code in (401, 403):
            raise VoiceUnavailable(f"Vertex AI refused the call ({e.code}): check billing, the "
                                   "aiplatform API and roles/aiplatform.user") from e
        if e.code == 404:     # a retired or mistyped model id
            raise VoiceUnavailable(f"Vertex AI has no model {model} for this project (404): set ANUMAAN_MODEL "
                                   "(text) or ANUMAAN_TTS_MODEL (voice) to a current one") from e
        raise VoiceUnavailable(f"Gemini call failed ({e.code} {e.status}): {e.message}") from e
    except httpx.TransportError as e:     # timeouts, refused or dropped connections
        raise VoiceUnavailable(f"Gemini did not answer ({type(e).__name__}, limit {timeout // 1000} s)") from e
    answered[model] = time.time()
    return resp


def label_voice(audio: bytes, mime: str, drug: str, language: str | None = None, client=None) -> dict:
    """One spoken answer -> dict(answer, cause, transcript, transcript_en, language, confidence)."""
    mime = mime.split(";")[0].strip().lower()     # browsers send "audio/webm;codecs=opus"
    if not audio or len(audio) > MAX_BYTES or not mime.startswith("audio/"):
        raise ValueError(f"need an audio/* clip under {MAX_BYTES >> 20} MB, got {mime} ({len(audio)} bytes)")
    hint = f" They were expected to speak {language}." if language else ""
    resp = _generate(client or _client(), [types.Part.from_bytes(data=audio, mime_type=mime),
                                           PROMPT.format(drug=drug.replace("_", " "), hint=hint)], Answer)
    try:
        out = Answer.model_validate(resp.parsed).model_dump()
    except ValidationError:     # off-schema or empty reply: never guess a shelf state
        return dict(answer="unclear", cause="unknown", transcript=getattr(resp, "text", None) or "",
                    transcript_en="", language=language or "", confidence=0.0)
    out["confidence"] = min(max(out["confidence"], 0.0), 1.0)
    if out["confidence"] < MIN_CONFIDENCE:
        out["answer"] = "unclear"
    return out


# The brief's languages: English and the 15 of India's 22 scheduled languages that Gemini documents
# ("All the Gemini models can understand and respond in the following languages",
# https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/models, updated 2026-09-28). Bodo, Dogri,
# Kashmiri, Konkani, Maithili, Sanskrit and Santali are not on that list, so they are not offered.
# letters: the Unicode block most of a brief's letters must come from (check). voice, voice_stage: the
# Gemini-TTS locale that reads it aloud and its launch stage, from the language table at
# https://docs.cloud.google.com/text-to-speech/docs/gemini-tts (updated 2026-09-24); None = no voice. That
# table has Bangla only as bn-BD and Urdu only as ur-PK. Cloud Text-to-Speech also has GA voices for
# bn, gu, kn, ml, pa and ur-IN (https://docs.cloud.google.com/text-to-speech/docs/list-voices-and-types),
# but texttospeech.googleapis.com is not enabled on this project; Gemini-TTS needs only Vertex AI.
LANGUAGE_INFO = {
    "en": dict(name="English", native="English", script="Latin", letters=(0x0041, 0x024F), voice="en-IN", voice_stage="GA"),
    "as": dict(name="Assamese", native="অসমীয়া", script="Assamese", letters=(0x0980, 0x09FF), voice=None, voice_stage=None),
    "bn": dict(name="Bengali", native="বাংলা", script="Bengali", letters=(0x0980, 0x09FF), voice="bn-BD", voice_stage="GA"),
    "gu": dict(name="Gujarati", native="ગુજરાતી", script="Gujarati", letters=(0x0A80, 0x0AFF), voice="gu-IN", voice_stage="Preview"),
    "hi": dict(name="Hindi", native="हिन्दी", script="Devanagari", letters=(0x0900, 0x097F), voice="hi-IN", voice_stage="GA"),
    "kn": dict(name="Kannada", native="ಕನ್ನಡ", script="Kannada", letters=(0x0C80, 0x0CFF), voice="kn-IN", voice_stage="Preview"),
    "ml": dict(name="Malayalam", native="മലയാളം", script="Malayalam", letters=(0x0D00, 0x0D7F), voice="ml-IN", voice_stage="Preview"),
    "mni-Mtei": dict(name="Manipuri", native="ꯃꯤꯇꯩꯂꯣꯟ", script="Meetei Mayek", letters=(0xABC0, 0xABFF), voice=None, voice_stage=None),
    "mr": dict(name="Marathi", native="मराठी", script="Devanagari", letters=(0x0900, 0x097F), voice="mr-IN", voice_stage="GA"),
    "ne": dict(name="Nepali", native="नेपाली", script="Devanagari", letters=(0x0900, 0x097F), voice="ne-NP", voice_stage="Preview"),
    "or": dict(name="Odia", native="ଓଡ଼ିଆ", script="Odia", letters=(0x0B00, 0x0B7F), voice="or-IN", voice_stage="Preview"),
    "pa": dict(name="Punjabi", native="ਪੰਜਾਬੀ", script="Gurmukhi", letters=(0x0A00, 0x0A7F), voice="pa-IN", voice_stage="Preview"),
    # Sindhi is written in Devanagari too; Perso-Arabic is a choice, as Meetei Mayek (not Bengali) is for Manipuri
    "sd": dict(name="Sindhi", native="سنڌي", script="Perso-Arabic", letters=(0x0600, 0x06FF), voice="sd-IN", voice_stage="Preview"),
    "ta": dict(name="Tamil", native="தமிழ்", script="Tamil", letters=(0x0B80, 0x0BFF), voice="ta-IN", voice_stage="GA"),
    "te": dict(name="Telugu", native="తెలుగు", script="Telugu", letters=(0x0C00, 0x0C7F), voice="te-IN", voice_stage="GA"),
    "ur": dict(name="Urdu", native="اردو", script="Perso-Arabic", letters=(0x0600, 0x06FF), voice="ur-PK", voice_stage="Preview"),
}
LANGUAGES = {k: v["name"] for k, v in LANGUAGE_INFO.items()}
# ponytail: a set cut, not tuned (ids and medicine names stay in Latin letters, so a good brief is under
# 100%); set it per language from native-reviewed briefs
MIN_SCRIPT = 0.6

BRIEF = """You brief a District Health Officer in India about one medicine at one Primary
Health Centre. Anumaan infers the real shelf from the care being given (diagnoses, dispensing
slips, the stock register and the warehouse ledger); the facts below are its output on
SYNTHETIC demo data. Write in {language}:
- summary: two or three plain sentences: what is happening, and the evidence for it.
- next_step: one sentence: what the officer should do now.
Use only these facts. Do not invent numbers, names or dates; keep medicine and facility ids
as written. Copy numbers from the facts: do not round them or work out new ones.
FACTS: {facts}"""


class Brief(BaseModel):
    summary: str
    next_step: str


def _numbers(text):
    """The numbers in text as ASCII, whatever digits they are written in (Odia, Tamil, Devanagari, ...)."""
    text = "".join(str(d) if (d := unicodedata.decimal(ch, None)) is not None else ch for ch in text)
    return re.findall(r"\d+(?:\.\d+)?", re.sub(r"(?<=\d),(?=\d)", "", text))     # 4,036 is one number


def check(brief: dict, facts: dict, language: str) -> list[str]:
    """What is wrong with a brief, [] if nothing: a number that is not in the facts (a fraction may
    come back x100, as a percentage), or too few letters in the language's script (it came back in
    English, or in another script). Numbers written as words and the wrong language in the right
    script both pass."""
    text, info = f"{brief['summary']} {brief['next_step']}", LANGUAGE_INFO[language]
    allowed = {round(float(x) * k, 6) for x in _numbers(json.dumps(facts, ensure_ascii=False)) for k in (1, 100)}
    new = [x for x in _numbers(text) if round(float(x), 6) not in allowed]
    out = [f"numbers not in the evidence: {', '.join(dict.fromkeys(new))}"] if new else []
    (lo, hi), letters = info["letters"], [ord(ch) for ch in text if ch.isalpha()]
    share = sum(lo <= c <= hi for c in letters) / max(len(letters), 1)
    if share < MIN_SCRIPT:
        out.append(f"only {share:.0%} of the letters are in {info['script']} script")
    return out


def write_brief(facts: dict, language: str = "en", client=None) -> dict:
    """Anumaan's evidence for one PHC x medicine -> dict(summary, next_step) in the officer's language,
    plus problems=[...] when the brief fails check(): show the English brief instead of that one."""
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(LANGUAGES)}")
    info = LANGUAGE_INFO[language]
    name = info["name"] if language == "en" else f"{info['name']} ({info['script']} script)"
    resp = _generate(client or _client(), [BRIEF.format(language=name, facts=json.dumps(facts))], Brief)
    try:
        out = Brief.model_validate(resp.parsed).model_dump()
    except ValidationError as e:     # never show a half-formed brief
        raise VoiceUnavailable("Gemini returned no usable brief") from e
    if problems := check(out, facts, language):
        out["problems"] = problems
    return out


# Gemini-TTS through Vertex AI's generateContent: the aiplatform API and roles/aiplatform.user the brief
# already uses, nothing more (https://docs.cloud.google.com/text-to-speech/docs/gemini-tts). The voice_stage
# marks above are for this GA model; gemini-3.1-flash-tts-preview has only Preview voices. The lifecycle
# page (https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions, 2026-09-29)
# retires gemini-2.5-flash on 20 Oct 2026 and lists no TTS model: if this one goes too, set ANUMAAN_TTS_MODEL.
TTS_MODEL = "gemini-2.5-flash-tts"
SPEAKER = "Kore"          # one of Gemini-TTS's 30 voices, for every language; the locale sets the language
TTS_MAX_BYTES = 8000      # Vertex AI's cap on a Gemini-TTS request
# Speech took about as long to make as to play (1 Oct: 7.4 s for 7.85 s of Malayalam, 6.8 s for 7.61 s
# of Hindi), so a whole brief can run past TIMEOUT_MS
TTS_TIMEOUT_MS = 120_000


def speak(text: str, language: str, client=None) -> tuple[bytes, str]:
    """A brief read aloud -> (WAV bytes, "audio/wav"). ValueError for a language with no voice."""
    voice = LANGUAGE_INFO.get(language, {}).get("voice")
    if not voice:
        raise ValueError(f"no voice for {language!r}; these have one: "
                         f"{', '.join(k for k, v in LANGUAGE_INFO.items() if v['voice'])}")
    say = f"Say the following in a clear, calm voice: {text.strip()}"
    if not text.strip() or len(say.encode()) > TTS_MAX_BYTES:
        raise ValueError(f"need some text, under {TTS_MAX_BYTES} bytes in all")
    resp = _generate(client or _client(), say, model=os.environ.get("ANUMAAN_TTS_MODEL", TTS_MODEL),
                     timeout=TTS_TIMEOUT_MS, speech_config=types.SpeechConfig(
                         language_code=voice, voice_config=types.VoiceConfig(
                             prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=SPEAKER))))
    try:
        audio = next(p.inline_data for p in resp.candidates[0].content.parts if p.inline_data and p.inline_data.data)
    except (AttributeError, IndexError, TypeError, StopIteration) as e:     # blocked or empty reply
        raise VoiceUnavailable("Gemini-TTS returned no audio") from e
    rate = re.search(r"rate=(\d+)", audio.mime_type or "")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:     # Vertex sends bare 16-bit mono PCM; a WAV header makes it playable
        w.setparams((1, 2, int(rate.group(1)) if rate else 24000, 0, "NONE", "not compressed"))
        w.writeframes(audio.data)
    return buf.getvalue(), "audio/wav"


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
