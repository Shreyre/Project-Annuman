"""Voice confirmation with an injected fake Gemini client: no network, no credentials."""
import io
import os
import wave
from types import SimpleNamespace

import httpx
import pytest
from google.auth.exceptions import DefaultCredentialsError
from google.genai import errors, types

from anumaan import voice as V


class Fake:
    """Stands in for genai.Client: records the request, returns or raises what it is given."""

    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []
        self.models = self

    def generate_content(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return self.reply


def test_parses_structured_answer():
    said = V.Answer(transcript="ଆମୋକ୍ସିସିଲିନ ସରିଯାଇଛି, ଇଣ୍ଡେଣ୍ଟ ଆସିନି", language="or",
                    transcript_en="Amoxicillin is finished, the indent has not come",
                    answer="empty", cause="indent_pending", confidence=0.93)
    fake = Fake(SimpleNamespace(parsed=said))
    out = V.label_voice(b"\x1aE\xdf\xa3 fake webm", "audio/webm;codecs=opus", "amoxicillin_500", "or", fake)
    assert out == said.model_dump()
    audio, prompt = fake.calls[0]["contents"]
    assert audio.inline_data.mime_type == "audio/webm" and "amoxicillin 500" in prompt
    assert fake.calls[0]["config"].response_schema is V.Answer
    # a dict reply with shaky confidence, or no parsed reply at all, is never shelf evidence
    shaky = V.label_voice(b"x", "audio/ogg", "ors", client=Fake(SimpleNamespace(parsed=dict(said.model_dump(), confidence=0.3))))
    assert shaky["answer"] == "unclear" and shaky["cause"] == "indent_pending"
    assert V.label_voice(b"x", "audio/ogg", "ors", client=Fake(SimpleNamespace(parsed=None, text="")))["answer"] == "unclear"
    with pytest.raises(ValueError):
        V.label_voice(b"x", "video/mp4", "ors", client=fake)


def test_every_gemini_failure_is_voice_unavailable():
    # no credentials, refused, bad request, server error, stalled connection: the caller only
    # needs one except clause to fall back to the buttons; the real error stays chained
    for exc in (DefaultCredentialsError("no ADC"), errors.ClientError(403, {"error": {"message": "billing disabled"}}),
                errors.ClientError(400, {"error": {"message": "bad"}}), errors.ServerError(503, {"error": {}}),
                httpx.ReadTimeout("stalled")):
        with pytest.raises(V.VoiceUnavailable) as e:
            V.label_voice(b"x", "audio/webm", "ors", client=Fake(exc=exc))
        assert e.value.__cause__ is exc
    fake = Fake(exc=httpx.ConnectError("refused"))
    with pytest.raises(V.VoiceUnavailable):
        V.label_voice(b"x", "audio/webm", "ors", client=fake)
    assert fake.calls[0]["config"].http_options.timeout == V.TIMEOUT_MS     # never the no-timeout default


def test_env_file_parser(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("# comment=no\nexport ANUMAAN_T1='a=b'\nANUMAAN_T2 = \"x\"\n\nANUMAAN_T3=kept\n", encoding="utf-8")
    monkeypatch.setattr(os, "environ", {"ANUMAAN_T3": "real"})    # restored after the test
    V.load_env(p)
    assert os.environ == {"ANUMAAN_T1": "a=b", "ANUMAAN_T2": "x", "ANUMAAN_T3": "real"}


def test_brief_asks_in_the_officers_language_and_never_returns_half_a_form():
    said = V.Brief(summary="ଆମୋକ୍ସିସିଲିନ ସରିଯାଇଛି", next_step="ପାଖ PHC ରୁ ପଠାନ୍ତୁ")
    fake = Fake(SimpleNamespace(parsed=said))
    assert V.write_brief({"medicine": "amoxicillin_500", "alarm": True}, "or", fake) == said.model_dump()
    (prompt,) = fake.calls[0]["contents"]
    assert "Odia" in prompt and "amoxicillin_500" in prompt and fake.calls[0]["config"].response_schema is V.Brief
    with pytest.raises(ValueError):
        V.write_brief({}, "xx", fake)
    with pytest.raises(V.VoiceUnavailable):
        V.write_brief({}, "en", Fake(SimpleNamespace(parsed=None)))


def test_every_language_has_a_name_a_script_and_no_translation_route():
    assert V.LANGUAGES == {k: v["name"] for k, v in V.LANGUAGE_INFO.items()} and {"en", "or", "hi", "ml"} <= set(V.LANGUAGES)
    assert not set(V.LANGUAGES) & {"brx", "doi", "gom", "kok", "ks", "mai", "sa", "sat"}     # Gemini does not list these
    for code, x in V.LANGUAGE_INFO.items():
        assert x["name"] and x["script"] and x["voice_stage"] in ("GA", "Preview", None) and (x["voice"] is None) == (x["voice_stage"] is None)
        assert V.check(dict(summary=x["native"], next_step=""), {}, code) == []     # the table's block holds its own name
    scheduled = [x for code, x in V.LANGUAGE_INFO.items() if code != "en"]
    # the counts the UI and README quote: written in 15, read aloud in 13, 8 of them with Preview voices
    assert (len(scheduled), sum(bool(x["voice"]) for x in scheduled), sum(x["voice_stage"] == "Preview" for x in scheduled)) == (15, 13, 8)


def test_check_holds_a_brief_to_the_evidence_numbers_and_the_language_script():
    facts = {"medicine": "paracetamol 500", "day": 113, "register_units": 4036, "inferred_confidence": 0.87,
             "last_14_days": {"cut_short": 92}}
    ta = dict(summary="பாராசிட்டமால் 500: கடந்த 14 நாட்களில் 92 முறை குறைக்கப்பட்டது, பதிவேட்டில் 4,036",
              next_step="87% உறுதி; நாள் ௧௧௩ முதல் சரிபார்க்கவும்")     # Tamil digits ௧௧௩ are 113; 87% is 0.87
    assert V.check(ta, facts, "ta") == []
    assert V.check(dict(ta, next_step="நாள் 115, 12 நாட்கள்"), facts, "ta") == ["numbers not in the evidence: 115, 12"]
    en = dict(summary="Paracetamol 500 was cut short 92 times in 14 days", next_step="Check the shelf.")
    assert V.check(en, facts, "en") == [] and V.check(en, facts, "ta") == ["only 0% of the letters are in Tamil script"]
    # Odia, Malayalam, Devanagari, Bengali, Tamil, Telugu, Kannada, Gujarati and Gurmukhi digits all read as 14
    assert V.check(dict(summary="day ୧୪ ൧൪ १४ ১৪ ௧௪ ౧౪ ೧೪ ૧૪ ੧੪", next_step=""), {"days": 14}, "en") == []
    assert V.check(dict(summary="day ୧୫", next_step=""), {"days": 14}, "en") == ["numbers not in the evidence: 15"]


def test_a_brief_that_fails_the_check_comes_back_marked_and_codes_not_offered_are_refused():
    said = V.Brief(summary="Amoxicillin ran out 15 days ago", next_step="Send 40 courses")
    fake = Fake(SimpleNamespace(parsed=said))
    out = V.write_brief({"medicine": "amoxicillin_500", "day": 113}, "ta", fake)
    assert out == dict(said.model_dump(), problems=["numbers not in the evidence: 15, 40",
                                                     "only 0% of the letters are in Tamil script"])
    (prompt,) = fake.calls[0]["contents"]
    assert "Write in Tamil (Tamil script)" in prompt and "Copy numbers from the facts" in prompt
    for code in ("doi", "kok", "brx", "sat", "xx"):
        with pytest.raises(ValueError):
            V.write_brief({}, code, fake)
    assert len(fake.calls) == 1                                    # refused before any Gemini call


def test_speak_asks_gemini_tts_for_the_language_and_voice_and_returns_wav(monkeypatch):
    monkeypatch.delenv("ANUMAAN_TTS_MODEL", raising=False)
    pcm = b"\x00\x10" * 2400                                        # 0.1 s of 16-bit mono at 24 kHz
    fake = Fake(types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[
        types.Part.from_bytes(data=pcm, mime_type="audio/L16;codec=pcm;rate=24000")]))]))
    audio, mime = V.speak("പാരസെറ്റമോൾ തീർന്നു", "ml", fake)
    with wave.open(io.BytesIO(audio)) as w:
        assert mime == "audio/wav" and (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)
        assert w.readframes(w.getnframes()) == pcm
    call = fake.calls[0]
    speech = call["config"].speech_config
    assert call["model"] == V.TTS_MODEL and call["contents"].endswith("പാരസെറ്റമോൾ തീർന്നു")
    assert speech.language_code == "ml-IN" and speech.voice_config.prebuilt_voice_config.voice_name == V.SPEAKER
    assert call["config"].http_options.timeout == V.TTS_TIMEOUT_MS > V.TIMEOUT_MS     # speech takes as long as it plays
    V.speak("আমোক্সিসিলিন শেষ", "bn", fake)
    assert fake.calls[1]["config"].speech_config.language_code == "bn-BD"      # Gemini-TTS has Bangla as bn-BD only
    for code, text in (("as", "x"), ("mni-Mtei", "x"), ("doi", "x"), ("xx", "x"), ("hi", "  "), ("hi", "क" * 3000)):
        with pytest.raises(ValueError):                             # no voice, not offered, nothing to say, too long
            V.speak(text, code, fake)
    assert len(fake.calls) == 2
    for reply in (SimpleNamespace(candidates=None), SimpleNamespace(candidates=[]),
                  types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[types.Part(text="no")]))])):
        with pytest.raises(V.VoiceUnavailable):
            V.speak("नमस्ते", "hi", Fake(reply))
    with pytest.raises(V.VoiceUnavailable) as e:                   # a retired model says which setting to change
        V.speak("नमस्ते", "hi", Fake(exc=errors.ClientError(404, {"error": {"message": "not found"}})))
    assert "ANUMAAN_TTS_MODEL" in str(e.value)
