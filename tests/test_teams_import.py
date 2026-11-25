from datetime import datetime
from pathlib import Path

import pytest
from docx import Document

from app import teams_import

VTT = """WEBVTT

0f3c1a/12-0
00:00:03.120 --> 00:00:05.430
<v Mario Rossi>Buongiorno a tutti.</v>

0f3c1a/12-1
00:00:05.500 --> 00:00:07.000
<v Mario Rossi>Iniziamo con la demo.</v>

0f3c1a/13-0
00:01:02.000 --> 00:01:04.000
<v Alex Moretti>Sure, let me share my screen.</v>

00:01:05.000 --> 00:01:06.000
no voice tag here
"""


def test_parse_vtt_keeps_speakers_and_merges_consecutive():
    segs = teams_import.parse_vtt(VTT)
    assert [(s.start, s.speaker) for s in segs] == [
        (3.12, "Mario Rossi"), (62.0, "Alex Moretti"), (65.0, "Unknown")]
    assert segs[0].text == "Buongiorno a tutti. Iniziamo con la demo."


def test_convert_vtt_renders_transcript(tmp_path):
    f = tmp_path / "Acme sync 2026-11-19.vtt"
    f.write_text(VTT)
    out = teams_import.convert(f)
    assert out.startswith("---\nstarted: 19-11-2026 00:00\n")
    assert "source: teams" in out
    assert "[00:00:03] Mario Rossi: Buongiorno a tutti. Iniziamo con la demo." in out
    assert "[00:01:02] Alex Moretti: Sure, let me share my screen." in out


@pytest.mark.parametrize("name", ["Acme sync 19-11-2026.vtt", "Acme sync 2026-11-19.vtt", "Acme sync 20261119.vtt"])
def test_date_from_name_accepts_both_orders(tmp_path, name):
    assert teams_import._date_from_name(tmp_path / name) == datetime(2026, 11, 19)


def test_parse_docx_new_format():
    paras = ["Acme weekly", "November 19, 2026, 10:02AM", "45m 12s", "",
             "Mario Rossi   0:03", "Buongiorno.", "Mario Rossi   0:07", "Iniziamo.",
             "Alex Moretti   1:02:03", "We meet again at 10:30"]
    segs, started = teams_import.parse_docx_paragraphs(paras)
    assert started.isoformat() == "2026-11-19T10:02:00"
    assert [(s.start, s.speaker, s.text) for s in segs] == [
        (3, "Mario Rossi", "Buongiorno. Iniziamo."),
        (3723, "Alex Moretti", "We meet again at 10:30")]


def test_parse_docx_old_cue_format():
    paras = ["0:0:1.000 --> 0:0:4.000", "Anna Bianchi", "Hello.",
             "0:0:5.000 --> 0:0:8.000", "Alex Moretti", "Hi Anna."]
    segs, _ = teams_import.parse_docx_paragraphs(paras)
    assert [(s.start, s.speaker) for s in segs] == [(1, "Anna Bianchi"), (5, "Alex Moretti")]


def test_convert_docx_file(tmp_path: Path):
    doc = Document()
    for p in ["Weekly", "November 19, 2026, 3:30PM", "Anna Bianchi   0:05", "Ciao a tutti."]:
        doc.add_paragraph(p)
    f = tmp_path / "Weekly.docx"
    doc.save(f)
    out = teams_import.convert(f)
    assert "started: 19-11-2026 15:30" in out
    assert "[00:00:05] Anna Bianchi: Ciao a tutti." in out
