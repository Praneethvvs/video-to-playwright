"""Transcript parsing.

Every case here is a real file shape that broke something: the Teams .docx that arrived as six
garbled blocks, and the mm:ss .vtt that silently became plain lines and fed the WEBVTT header to
an agent as though a human had said it.
"""

import zipfile

import pytest

from testboard import transcripts

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


def test_hours_are_optional_in_a_cue(tmp_path):
    """WebVTT makes HH optional and ffmpeg, Whisper and several exporters omit it."""
    parsed = transcripts.parse(write(tmp_path, "a.vtt", VTT_WITHOUT_HOURS))
    assert len(parsed.cues) == 2
    assert parsed.has_timestamps
    assert parsed.duration == pytest.approx(9.0)
    assert parsed.cues[0]["speaker"] == "Jane Doe"
    assert "WEBVTT" not in parsed.text          # the header is not narration


def test_hours_still_work(tmp_path):
    parsed = transcripts.parse(write(tmp_path, "a.vtt", VTT_WITH_HOURS))
    assert len(parsed.cues) == 1
    assert parsed.has_timestamps                # one fully-timed cue is timestamped
    assert parsed.note == ""                    # and is not warned about


def test_a_cue_file_that_cannot_be_read_says_so(tmp_path):
    """Silently treating its raw text as narration is how junk reaches the prompt."""
    parsed = transcripts.parse(write(tmp_path, "a.vtt", VTT_UNREADABLE))
    assert "no cues could be read" in parsed.note
    assert ".vtt" in parsed.note


def test_seconds_parses_both_stamp_shapes():
    assert transcripts._seconds("00:00:04.500") == pytest.approx(4.5)
    assert transcripts._seconds("01:30.000") == pytest.approx(90.0)
    assert transcripts._seconds("1:02:03.000") == pytest.approx(3723.0)
    assert transcripts._seconds("nonsense") is None


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
    parsed = transcripts.parse(path)
    texts = [c["text"] for c in parsed.cues]
    assert "First sentence." in texts
    assert "Second sentence." in texts
    assert not any("First sentence.Second" in t for t in texts)


def test_docx_speaker_and_offset_are_lifted(tmp_path):
    path = _docx(tmp_path / "t.docx", [["Jane Doe   0:10", None, "Hello."]])
    parsed = transcripts.parse(path)
    spoken = [c for c in parsed.cues if c["text"] == "Hello."]
    assert spoken and spoken[0]["speaker"] == "Jane Doe"
    assert spoken[0]["start"] == "00:00:10"


def test_the_speaker_name_does_not_swallow_the_speech(tmp_path):
    """The header regex was DOTALL, so the name matched across the newline into the first line."""
    path = _docx(tmp_path / "t.docx", [["Jane Doe   0:10", None, "Do not eat this line."]])
    parsed = transcripts.parse(path)
    assert all("Do not eat this line" not in (c["speaker"] or "") for c in parsed.cues)


def test_word_metadata_never_reaches_the_text(tmp_path):
    """Stripping every tag pulled revision ids and table geometry into real sentences."""
    path = tmp_path / "t.docx"
    xml = ('<?xml version="1.0"?><w:document><w:body>'
           '<w:p w:rsidR="621792274320"><w:r><w:t>A real sentence.</w:t></w:r></w:p>'
           '</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    parsed = transcripts.parse(path)
    assert [c["text"] for c in parsed.cues] == ["A real sentence."]


def test_a_corrupt_docx_returns_nothing_rather_than_raising(tmp_path):
    path = tmp_path / "t.docx"
    path.write_bytes(b"not a zip at all")
    assert transcripts.parse(path).cues == []


@pytest.mark.parametrize("name,video,transcript", [
    ("a.mp4", True, False), ("a.WEBM", True, False), ("a.vtt", False, True),
    ("a.docx", False, True), ("a.exe", False, False), ("a", False, False),
])
def test_file_kinds(name, video, transcript):
    assert transcripts.is_video(name) is video
    assert transcripts.is_transcript(name) is transcript
