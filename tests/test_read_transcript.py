"""Transcript parsing, which is step 4 of the workflow and the step with the most ways to be
quietly wrong.

Every case here is a real file shape that broke something: the Teams .docx that arrived as six
garbled blocks with no speaker attribution, and the mm:ss .vtt that matched no cues at all,
silently became plain lines, and fed the WEBVTT header to an agent as though a human had said it.

A wrong transcript does not look like a failure. It looks like a transcript, and every assertion
derived from it inherits the error.
"""

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import read_transcript as rt  # noqa: E402

VTT_WITH_HOURS = """WEBVTT

00:00:01.000 --> 00:00:04.500
<v Jane Doe>So this is the indicators screen.
"""

VTT_WITHOUT_HOURS = """WEBVTT

1
00:01.000 --> 00:04.500
<v Jane Doe>So this is the indicators screen.

2
00:04.500 --> 00:09.000
<v Jane Doe>You should see the economic indicators grid.
"""

VTT_UNREADABLE = """WEBVTT

this file has a header and no cues at all
just prose that happens to be in a .vtt
"""


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- cue files --------------------------------------------------------------------------------
def test_hours_are_optional_in_a_cue(tmp_path):
    """WebVTT makes HH optional, and ffmpeg, Whisper and several exporters omit it.

    Requiring it meant a valid .vtt produced *zero* utterances and fell through to plain lines —
    a total parse failure that reported itself as a transcript with no timestamps.
    """
    result = rt.read(write(tmp_path, "a.vtt", VTT_WITHOUT_HOURS))
    assert len(result["utterances"]) == 2
    assert result["has_timestamps"]
    assert result["utterances"][0]["speaker"] == "Jane Doe"
    assert all("WEBVTT" not in c["text"] for c in result["utterances"])


def test_hours_still_work(tmp_path):
    result = rt.read(write(tmp_path, "a.vtt", VTT_WITH_HOURS))
    assert len(result["utterances"]) == 1
    assert result["has_timestamps"]             # one fully-timed cue is timestamped
    assert result["note"] == ""                 # and is not warned about


def test_a_cue_file_that_cannot_be_read_says_so(tmp_path):
    """"No times in this export" and "we could not read this file" are different problems.

    They produce an identical-looking list of untimed lines, so the note is the only thing that
    tells them apart — and only one of them is the user's fault.
    """
    result = rt.read(write(tmp_path, "a.vtt", VTT_UNREADABLE))
    assert "no cues could be read" in result["note"]
    assert ".vtt" in result["note"]


def test_srt_comma_stamps_parse(tmp_path):
    srt = "1\n00:00:01,000 --> 00:00:04,500\nA line of speech.\n"
    result = rt.read(write(tmp_path, "a.srt", srt))
    assert [c["text"] for c in result["utterances"]] == ["A line of speech."]
    assert result["has_timestamps"]


def test_seconds_parses_both_stamp_shapes():
    assert rt._seconds("00:00:04.500") == pytest.approx(4.5)
    assert rt._seconds("01:30.000") == pytest.approx(90.0)
    assert rt._seconds("1:02:03.000") == pytest.approx(3723.0)
    assert rt._seconds("nonsense") is None


# --- Word documents ---------------------------------------------------------------------------
def _docx(path, paragraphs):
    """A minimal Word document: the parts the parser actually reads."""
    body = "".join(
        "<w:p><w:r>"
        + "".join(f"<w:t>{line}</w:t>" if line else "<w:br/>" for line in para)
        + "</w:r></w:p>"
        for para in paragraphs
    )
    xml = f'<?xml version="1.0"?><w:document><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return path


def test_docx_line_breaks_separate_utterances(tmp_path):
    """A <w:br/> sits between runs, so missing it ran every spoken line together."""
    path = _docx(tmp_path / "t.docx", [
        ["Jane Doe   0:10", None, "First sentence.", None, "Second sentence."],
    ])
    texts = [c["text"] for c in rt.read(path)["utterances"]]
    assert "First sentence." in texts
    assert "Second sentence." in texts
    assert not any("First sentence.Second" in t for t in texts)


def test_docx_speaker_and_offset_are_lifted(tmp_path):
    """Teams prints "Name   0:10" at the head of a turn. That is a real timestamp."""
    path = _docx(tmp_path / "t.docx", [["Jane Doe   0:10", None, "Hello."]])
    spoken = [c for c in rt.read(path)["utterances"] if c["text"] == "Hello."]
    assert spoken and spoken[0]["speaker"] == "Jane Doe"
    assert spoken[0]["start"] == "00:00:10"


def test_the_speaker_name_does_not_swallow_the_speech(tmp_path):
    """The header regex was DOTALL, so the name matched across the newline into the speech."""
    path = _docx(tmp_path / "t.docx", [["Jane Doe   0:10", None, "Do not eat this line."]])
    cues = rt.read(path)["utterances"]
    assert all("Do not eat this line" not in (c["speaker"] or "") for c in cues)


def test_the_speaker_carries_to_later_lines_of_the_same_turn(tmp_path):
    path = _docx(tmp_path / "t.docx", [["Jane Doe   0:10", None, "One.", None, "Two."]])
    cues = [c for c in rt.read(path)["utterances"] if c["text"] in ("One.", "Two.")]
    assert [c["speaker"] for c in cues] == ["Jane Doe", "Jane Doe"]


def test_only_the_first_line_of_a_turn_is_given_the_offset(tmp_path):
    """Teams says when the turn started, not when each line did. Inventing the rest is worse."""
    path = _docx(tmp_path / "t.docx", [["Jane Doe   0:10", None, "One.", None, "Two."]])
    cues = [c for c in rt.read(path)["utterances"] if c["text"] in ("One.", "Two.")]
    assert [c["start"] for c in cues] == ["00:00:10", None]


def test_word_metadata_never_reaches_the_text(tmp_path):
    """Stripping every tag pulled revision ids and table geometry into real sentences."""
    path = tmp_path / "t.docx"
    xml = ('<?xml version="1.0"?><w:document><w:body>'
           '<w:p w:rsidR="621792274320"><w:r><w:t>A real sentence.</w:t></w:r></w:p>'
           '</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    assert [c["text"] for c in rt.read(path)["utterances"]] == ["A real sentence."]


def test_entities_are_unescaped(tmp_path):
    path = _docx(tmp_path / "t.docx", [["Click &quot;Add&quot; &amp; wait &lt;here&gt;."]])
    assert rt.read(path)["utterances"][0]["text"] == 'Click "Add" & wait <here>.'


def test_a_corrupt_docx_returns_nothing_rather_than_raising(tmp_path):
    path = tmp_path / "t.docx"
    path.write_bytes(b"not a zip at all")
    assert rt.read(path)["utterances"] == []


# --- plain text and triage --------------------------------------------------------------------
def test_plain_text_becomes_one_utterance_per_line(tmp_path):
    result = rt.read(write(tmp_path, "a.txt", "First line.\n\nSecond line.\n"))
    assert [c["text"] for c in result["utterances"]] == ["First line.", "Second line."]
    assert not result["has_timestamps"]


@pytest.mark.parametrize("text,tag", [
    ("You should see the grid", "expectation"),
    ("The prerequisite is an open case", "precondition"),
    ("You cannot apply it twice", "rule"),
    ("Then I click the button", "action"),
])
def test_classification_is_a_triage_aid(text, tag):
    assert tag in rt.classify(text)
