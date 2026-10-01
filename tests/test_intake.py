"""Paper-PHC intake with an injected fake Gemini client: no network, no credentials."""
from types import SimpleNamespace

import numpy as np
import pytest

from anumaan import crg as G, feeds, intake as I, sim

JPEG, PNG, WEBP = b"\xff\xd8\xff\xe0" + bytes(64), b"\x89PNG\r\n\x1a\n" + bytes(64), b"RIFF\0\0\0\0WEBPVP8 " + bytes(64)


class Fake:
    """Stands in for genai.Client: records the request, returns what it is given."""

    def __init__(self, reply):
        self.reply, self.calls = reply, []
        self.models = self

    def generate_content(self, **kw):
        self.calls.append(kw)
        return self.reply


def perfect(m):
    """What a faultless read of a PHC-day's pages says: an OPD line a patient, a dispensing line a slip."""
    opd, disp, nos = [], [], {}
    for c, n in m["diagnoses"].items():
        for _ in range(n):
            opd.append(dict(opd_no=f"{len(opd) + 1:03d}", written_as=c, condition=c, confidence=0.9))
            nos.setdefault(c, []).append(opd[-1]["opd_no"])
    for c, d, days, units in m["slips"]:
        disp.append(dict(opd_no=nos[c].pop(), written_as=d, drug=d, days=days, units=units, not_available=False, confidence=0.9))
    for c, d in m["not_available"]:
        disp.append(dict(opd_no=nos[c].pop(), written_as=d, drug=d, days=0, units=0, not_available=True, confidence=0.9))
    return dict(opd=opd, dispensing=disp)


def test_bad_uploads_are_refused_before_any_call():
    fake = Fake(SimpleNamespace(parsed=I.Registers(opd=[], dispensing=[])))
    for bad in ([], [(JPEG, "image/jpeg")] * 5, [(b"", "image/jpeg")], [(JPEG, "image/gif")], [(b"GIF89a" + bytes(9), "image/gif")],
                [(PNG, "image/jpeg")], [(b"\xff\xd8\xff" + bytes(I.MAX_BYTES), "image/jpeg")], [("x", "image/jpeg")],
                [(JPEG, None)], "photo.jpg"):
        with pytest.raises(ValueError):
            I.read(bad, fake)
    assert fake.calls == []
    assert I.read([(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp; q=1")], fake) == dict(opd=[], dispensing=[])
    assert len(fake.calls) == 1


def test_a_read_becomes_the_days_message():
    day = feeds.stream(sim.simulate(seed=5, days=120))
    m = next(m for t in range(30, 120) for m in day(t) if m["kind"] == "phc" and m["not_available"] and m["slips"])
    fake = Fake(SimpleNamespace(parsed=I.Registers.model_validate(perfect(m))))
    got = I.read([(JPEG, "image/jpeg"), (PNG, "image/png")], fake)
    prompt, *photos = fake.calls[0]["contents"]
    assert prompt == I.PROMPT and [p.inline_data.mime_type for p in photos] == ["image/jpeg", "image/png"]
    assert fake.calls[0]["config"].response_schema is I.Registers and fake.calls[0]["config"].http_options.timeout == I.TIMEOUT_MS
    # nothing in the schema can carry a name, an age or any other identifier
    assert set(I.OpdLine.model_fields) == {"opd_no", "written_as", "condition", "confidence"}
    assert set(I.DispensingLine.model_fields) == {"opd_no", "written_as", "drug", "days", "units", "not_available", "confidence"}
    msg, review = I.to_message(got, m["phc"], m["date"])
    assert review == [] and "stock" not in msg
    assert msg["diagnoses"] == m["diagnoses"] and sorted(msg["slips"]) == sorted(m["slips"])
    assert sorted(msg["not_available"]) == sorted(m["not_available"])


def test_lines_to_check_are_flagged():
    opd = lambda no, c, conf=0.95: dict(opd_no=no, written_as=c, condition=c, confidence=conf)
    rx = lambda no, d, days, units, na=False: dict(opd_no=no, written_as=d, drug=d, days=days, units=units,
                                                   not_available=na, confidence=0.95)
    read = dict(opd=[opd("001", "fever"), opd("002", "other"), opd("003", "pneumonia", 0.3), opd("004", "uti"),
                     opd("005", "acute_diarrhoea")],
                dispensing=[rx("1", "paracetamol_500", 3, 12),                      # joins 001: the same number
                            rx("002", "other", 5, 5),
                            rx("003", "amoxicillin_500", 5, 15),
                            rx("004", "amoxicillin_500", 5, 15),                    # not the UTI course
                            rx("009", "ors_sachet", 3, 6),                          # no OPD line 009
                            rx("004", "nitrofurantoin_100", 0, 0, na=True),
                            rx("005", "ors_sachet", 3, 5),                          # 3 days of 2 sachets is 6
                            rx("004", "trimethoprim_sulphamethoxazole_ds", 3, 6)])  # the guideline's substitute
    msg, review = I.to_message(read, "S0-W0-P0", "2026-03-02")
    assert [(r["register"], r["line"], r["flags"]) for r in review] == [
        ("opd", 1, ["other"]), ("opd", 2, ["low_confidence"]), ("dispensing", 1, ["other"]),
        ("dispensing", 3, ["not_in_course"]), ("dispensing", 4, ["unjoined"]), ("dispensing", 6, ["units"])]
    assert all(r["why"] for r in review)
    assert msg["diagnoses"] == dict(fever=1, pneumonia=1, uti=1, acute_diarrhoea=1)
    assert sorted(msg["slips"]) == sorted([["fever", "paracetamol_500", 3, 12.0], ["pneumonia", "amoxicillin_500", 5, 15.0],
                                           ["uti", "amoxicillin_500", 5, 15.0], ["acute_diarrhoea", "ors_sachet", 3, 6.0],
                                           ["acute_diarrhoea", "ors_sachet", 3, 5.0],
                                           ["uti", "trimethoprim_sulphamethoxazole_ds", 3, 6.0]])
    assert msg["not_available"] == [["uti", "nitrofurantoin_100"]]
    with pytest.raises(ValueError):
        I.to_message(read, "S0-W0-P0", "02/03/2026")
    with pytest.raises(ValueError):
        I.to_message(dict(opd=[dict(opd_no="1")], dispensing=[]), "S0-W0-P0", "2026-03-02")


def test_a_message_without_stock_keeps_the_register_balance():
    run = sim.simulate(seed=5, days=40)
    blank = np.zeros_like
    live = sim.Run(run.ix, run.facilities, run.wh, run.st, blank(run.dx), [], [], blank(run.book), blank(run.receipts),
                   blank(run.wh_asked), blank(run.wh_got), np.full(run.wh_posted.shape, 40), None, None, None, None, None)
    obs, day = G.aggregate(live.ix, live.dx, [], []), feeds.stream(run)
    for t in range(39):
        for m in day(t):
            feeds.absorb(m, live, obs)
    # the stream's messages always carry the balances: they land exactly as before
    assert np.array_equal(live.book[:39], run.book[:39]) and np.array_equal(live.receipts[:39], run.receipts[:39])
    m = next(m for m in day(39) if m["kind"] == "phc")
    f, ors = run.facilities.index(m["phc"]), run.ix["di"]["ors_sachet"]
    paper = {k: v for k, v in m.items() if k not in ("stock", "receipts")}
    feeds.absorb(dict(paper, received={"ors_sachet": 50}), live, obs)
    assert live.book[38, f].any() and np.array_equal(live.book[39, f], live.book[38, f])
    assert live.receipts[39, f, ors] == 50 and live.receipts[39, f].sum() == 50
    assert not live.book[39, f + 1].any()                                 # nobody else's day is touched


def test_rendered_pages_score_against_their_answer_key():
    pytest.importorskip("PIL")
    from tools import registers as R
    m = R.message_of(6, 60, "S1-W1-P5")                                   # the UI's sample day
    pages, key = R.render(m)
    fake = Fake(SimpleNamespace(parsed=I.Registers(opd=[], dispensing=[])))
    I.read([(data, "image/jpeg") for _, data in pages], fake)             # the pages pass the upload checks
    assert 2 <= len(pages) <= I.MAX_IMAGES and len(fake.calls) == 1
    assert key["message"] == {k: m[k] for k in key["message"]}
    assert {k for r in key["opd"] for k in r} == {"opd_no", "condition", "written_as"}     # no names or ages
    read = {reg: [dict(r, confidence=1.0) for r in key[reg]] for reg in ("opd", "dispensing")}
    s = R.score(read, key)
    assert s["dx_abs_error"] == s["wrong"] == s["rows_missed"] == s["names_copied"] == 0
    assert s["slips_right"] == s["slips_true"] == s["slips_read"]
    assert s["flagged"] == s["flag_other"] == s["untracked_rows"] + sum(r["drug"] == "other" for r in key["dispensing"])
    # two misreads: days on a slip (review catches it: the quantity no longer fits), and a diagnosis
    # read confidently as another tracked one (review cannot see it)
    x = next(x for x in read["dispensing"] if x["drug"] != "other" and x["days"] > 1)
    x["days"] -= 1
    given = {x["opd_no"] for x in read["dispensing"]}
    y = next(y for y in read["opd"] if y["condition"] not in ("other", "fever") and y["opd_no"] not in given)
    y["condition"] = "fever"
    s = R.score(read, key)
    assert s["dx_abs_error"] == 2 and s["slips_right"] == s["slips_true"] - 1
    assert (s["wrong"], s["wrong_flagged"], s["accepted_wrong"]) == (2, 1, 1)
    assert R.summarise([dict(score=dict(s), seconds=1.0)])["auto_accepted_wrong"]["k"] == 1
