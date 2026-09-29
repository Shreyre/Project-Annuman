"""Voice confirmation with an injected fake Gemini client: no network, no credentials."""
import os
from types import SimpleNamespace

import httpx
import pytest
from google.auth.exceptions import DefaultCredentialsError
from google.genai import errors

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
