import fcntl
import shutil
from pathlib import Path

import pytest

from app import config, filer, filer_api

INDEX = """# Notes Index

*Last updated: 09-11-2026*

---

## Client Portal

**Specs**

- [[Functional Analysis]] — spec.

**Meetings** (`Portal/Meetings/`)

- [[Meeting Acme 07-09-2026]] — docs.

---

## Vault Conventions

- Notes are written in English.
"""

PREV = "# Meeting Acme — 07-09-2026\n\n#client-portal #meeting #acme\n\nPrevious: [[Meeting Acme 20-07-2026]]\n\n---\n\nBody.\n"
NOTE = "# Meeting Acme — 19-11-2026\n\n#client-portal #meeting #acme\n\nPrevious: [[Meeting Acme 07-09-2026]]\n\n---\n\n## Summary\nX.\n"
LINE = "- [[Meeting Acme 19-11-2026]] — demo."


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "Portal/Meetings").mkdir(parents=True)
    (v / "000 Index.md").write_text(INDEX)
    (v / "Portal/Meetings/Meeting Acme 07-09-2026.md").write_text(PREV)
    (v / "Portal/Functional Analysis.md").write_text("# FA\n\n#spec\n")
    return v


@pytest.fixture
def snap(vault):
    before, saved = filer.snapshot(vault), filer.backup(vault)
    yield before, saved
    shutil.rmtree(saved)


def good_run(v: Path):
    (v / "Portal/Meetings/Meeting Acme 19-11-2026.md").write_text(NOTE)
    idx = v / "000 Index.md"
    idx.write_text(filer_api.insert_index_line(idx.read_text(), "Client Portal", LINE, "19-11-2026"))
    prev = v / "Portal/Meetings/Meeting Acme 07-09-2026.md"
    prev.write_text(filer_api.add_next_link(prev.read_text(), "Meeting Acme 19-11-2026"))


def test_verify_accepts_good_run(vault, snap):
    good_run(vault)
    r = filer.verify(vault, *snap)
    assert r.ok, r.errors
    assert r.note == "Portal/Meetings/Meeting Acme 19-11-2026.md"


def test_index_insert_and_next_link(vault, snap):
    good_run(vault)
    idx = (vault / "000 Index.md").read_text()
    assert "*Last updated: 19-11-2026*" in idx
    assert "- [[Meeting Acme 07-09-2026]] — docs.\n" + LINE + "\n" in idx
    prev = (vault / "Portal/Meetings/Meeting Acme 07-09-2026.md").read_text()
    assert "Previous: [[Meeting Acme 20-07-2026]] · Next: [[Meeting Acme 19-11-2026]]" in prev


def test_index_insert_creates_inbox_section():
    out = filer_api.insert_index_line(INDEX, "Inbox", "- [[Meeting X 19-11-2026]] — x.", "19-11-2026")
    assert "## Inbox\n\n- [[Meeting X 19-11-2026]] — x.\n\n---\n\n## Vault Conventions" in out


@pytest.mark.parametrize("breakage, message", [
    (lambda v: (v / "Portal/Meetings/Meeting Acme 19-11-2026.md").write_text("no heading\n#a\n"), "H1"),
    (lambda v: (v / "Portal/Meetings/Meeting Acme 19-11-2026.md").write_text("# T\n\nno tags\n"), "tags"),
    (lambda v: (v / "Portal/Functional Analysis.md").unlink(), "deleted"),
    (lambda v: (v / "Portal/Functional Analysis.md").write_text("rewritten"), "beyond its Previous/Next"),
    (lambda v: (v / "000 Index.md").write_text((v / "000 Index.md").read_text().replace("— spec.", "— x.")),
     "other lines changed"),
])
def test_verify_rejects_bad_runs(vault, snap, breakage, message):
    good_run(vault)
    breakage(vault)
    r = filer.verify(vault, *snap)
    assert not r.ok
    assert any(message in e for e in r.errors), r.errors


def test_verify_rejects_note_outside_meetings(vault, snap):
    (vault / "Portal/Meeting Acme 19-11-2026.md").write_text(NOTE)
    idx = vault / "000 Index.md"
    idx.write_text(idx.read_text() + LINE + "\n")
    r = filer.verify(vault, *snap)
    assert any("Meetings" in e for e in r.errors)


def test_verify_rejects_missing_index_entry(vault, snap):
    (vault / "Portal/Meetings/Meeting Acme 19-11-2026.md").write_text(NOTE)
    r = filer.verify(vault, *snap)
    assert any("Index" in e for e in r.errors)


def test_verify_filled_existing_prep_note(tmp_path):
    v = tmp_path / "v"
    (v / "Portal/Meetings").mkdir(parents=True)
    (v / "000 Index.md").write_text(INDEX)
    prep = v / "Portal/Meetings/Meeting Acme 12-11-2026.md"
    prep.write_text("# Meeting Acme — 12-11-2026\n\n#client-portal #meeting #acme\n\nTalk about: PR 53.\n")
    before, saved = filer.snapshot(v), filer.backup(v)
    prep.write_text(prep.read_text() + "\n---\n\n## Summary\nDone.\n")
    idx = v / "000 Index.md"
    idx.write_text(filer_api.insert_index_line(idx.read_text(), "Client Portal",
                                               "- [[Meeting Acme 12-11-2026]] — x.", "12-11-2026"))
    assert filer.verify(v, before, saved, "FILED: Portal/Meetings/Meeting Acme 12-11-2026.md").ok
    # ...but rewriting the prep content is rejected
    prep.write_text("# Meeting Acme — 12-11-2026\n\n#client-portal #meeting #acme\n\n## Summary\nDone.\n")
    r = filer.verify(v, before, saved, "")
    assert any("existing content" in e for e in r.errors)
    shutil.rmtree(saved)


def test_rollback_restores_everything(vault, snap):
    before, saved = snap
    good_run(vault)
    (vault / "New/Meetings").mkdir(parents=True)
    (vault / "New/Meetings/x.md").write_text("x")
    (vault / "Portal/Functional Analysis.md").unlink()
    filer.rollback(vault, saved, before)
    assert filer.snapshot(vault) == before
    assert not (vault / "New").exists()


def test_move_to_avoids_overwriting(tmp_path):
    dest = tmp_path / "processed"
    for _ in range(2):
        f = tmp_path / "19-11-2026 1000.md"
        f.write_text("t")
        filer.move_to(f, dest)
    assert sorted(p.name for p in dest.iterdir()) == ["19-11-2026 1000 (2).md", "19-11-2026 1000.md"]


@pytest.fixture
def dirs(tmp_path, monkeypatch, vault):
    for name in ("INBOX", "PROCESSED", "FAILED", "LOGS"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(config, name, d)
    monkeypatch.setattr(config, "load", lambda: {"vault": {"path": vault}, "filer": {}})
    monkeypatch.setattr(filer, "setup_logging", lambda: None)
    return tmp_path


def test_lock_prevents_overlapping_runs(dirs, monkeypatch):
    (config.INBOX / "a.md").write_text("t")
    called = []
    monkeypatch.setattr(filer, "process", lambda *a: called.append(a) or True)
    with open(config.LOGS / ".filer.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert filer.main([]) == 0
    assert called == []


def test_success_moves_to_processed_failure_to_failed(dirs, monkeypatch):
    (config.INBOX / "ok.md").write_text("t")
    (config.INBOX / "bad.md").write_text("t")
    (config.INBOX / ".hidden.md.tmp").write_text("t")
    monkeypatch.setattr(filer, "wait_until_stable", lambda p: None)
    monkeypatch.setattr(filer, "notify", lambda *a: None)
    monkeypatch.setattr(filer, "file_transcript", lambda p, v, c: "x.md" if p.name == "ok.md" else None)
    assert filer.main([]) == 1
    assert [p.name for p in config.PROCESSED.iterdir()] == ["ok.md"]
    assert [p.name for p in config.FAILED.iterdir()] == ["bad.md"]
    assert [p.name for p in config.INBOX.iterdir()] == [".hidden.md.tmp"]


def test_verify_insertion_between_existing_notes(vault):
    later = vault / "Portal/Meetings/Meeting Acme 12-11-2026.md"
    later.write_text("# Meeting Acme — 12-11-2026\n\n#acme\n\nPrevious: [[Meeting Acme 07-09-2026]]\n\nAgenda.\n")
    prev = vault / "Portal/Meetings/Meeting Acme 07-09-2026.md"
    prev.write_text(prev.read_text().replace("Previous: [[Meeting Acme 20-07-2026]]",
                                             "Previous: [[Meeting Acme 20-07-2026]] · Next: [[Meeting Acme 12-11-2026]]"))
    before, saved = filer.snapshot(vault), filer.backup(vault)
    stem = "Meeting Acme 11-11-2026"
    (vault / f"Portal/Meetings/{stem}.md").write_text(NOTE)
    idx = vault / "000 Index.md"
    idx.write_text(idx.read_text() + f"- [[{stem}]] — x.\n")
    prev.write_text(prev.read_text().replace("Next: [[Meeting Acme 12-11-2026]]", f"Next: [[{stem}]]"))
    later.write_text(later.read_text().replace("Previous: [[Meeting Acme 07-09-2026]]", f"Previous: [[{stem}]]"))
    r = filer.verify(vault, before, saved)
    assert r.ok, r.errors
    # re-pointing a link to some other note, or a second change on the line, is rejected
    later.write_text(later.read_text().replace(f"Previous: [[{stem}]]", "Previous: [[Something else]]"))
    assert not filer.verify(vault, before, saved).ok
    shutil.rmtree(saved)


def test_only_link_change_rejects_body_edits():
    old = "# T\n\n#a\n\nPrevious: [[A]]\n\nBody.\n"
    assert filer._only_link_change(old, old.replace("[[A]]", "[[A]] · Next: [[N]]"), "N")
    assert filer._only_link_change(old, old.replace("[[A]]", "[[N]]"), "N")
    assert not filer._only_link_change(old, old.replace("Body.", "Edited."), "N")
    has_next = old.replace("[[A]]", "[[A]] · Next: [[B]]")
    assert not filer._only_link_change(has_next, has_next.replace("[[B]]", "[[B]] · Next: [[N]]"), "N")
    assert not filer._only_link_change(old, old.replace("[[A]]", "[[N]]").replace("Body.", "X."), "N")


# ---------------------------------------------------------------- voice notes

OCR = "# OCR\n\n#client-portal #ocr\n\n## Ideas\n- Try Tesseract.\n\n## TODO\n- [ ] Benchmark on scans\n"


@pytest.fixture
def voice_vault(vault):
    (vault / "Portal/Pipeline").mkdir()
    (vault / "Portal/Pipeline/OCR.md").write_text(OCR)
    idx = vault / "000 Index.md"
    idx.write_text(idx.read_text().replace("- [[Functional Analysis]] — spec.",
                                           "- [[Functional Analysis]] — spec.\n- [[OCR]] — OCR stage."))
    return vault


def test_transcript_kind(tmp_path):
    t = tmp_path / "t.md"
    t.write_text("---\nstarted: 13-11-2026 10:00\nsource: voice-note\n---\n\n[00:00:01] Me: hi\n")
    assert filer.transcript_kind(t) == "voice-note"
    t.write_text("---\nstarted: 13-11-2026 10:00\nsource: recorder\n---\n\n[00:00:01] Me: source: voice-note\n")
    assert filer.transcript_kind(t) == "meeting"


def test_voice_note_update_existing_note(voice_vault):
    before, saved = filer.snapshot(voice_vault), filer.backup(voice_vault)
    ocr = voice_vault / "Portal/Pipeline/OCR.md"
    ocr.write_text(OCR.replace("- Try Tesseract.\n", "- Try Tesseract.\n- Try a smaller model on CPU.\n")
                   .replace("- [ ] Benchmark", "- [x] Benchmark"))
    r = filer.verify_voice_note(voice_vault, before, saved, "FILED: Portal/Pipeline/OCR.md")
    assert r.ok, r.errors
    assert r.note == "Portal/Pipeline/OCR.md"
    # rewording an existing line is rejected
    ocr.write_text(OCR.replace("- Try Tesseract.", "- Tesseract is out."))
    assert any("existing content" in e for e in filer.verify_voice_note(voice_vault, before, saved).errors)
    shutil.rmtree(saved)


def test_voice_note_new_note(voice_vault):
    before, saved = filer.snapshot(voice_vault), filer.backup(voice_vault)
    (voice_vault / "Portal/Evaluation Ideas.md").write_text("# Evaluation Ideas\n\n#client-portal\n\n- A.\n")
    idx = voice_vault / "000 Index.md"
    idx.write_text(filer_api.insert_index_line(idx.read_text(), "Client Portal",
                                               "- [[Evaluation Ideas]] — ideas.", "13-11-2026"))
    assert filer.verify_voice_note(voice_vault, before, saved, "FILED: Portal/Evaluation Ideas.md").ok
    shutil.rmtree(saved)


def test_voice_note_spread_over_several_notes(voice_vault):
    before, saved = filer.snapshot(voice_vault), filer.backup(voice_vault)
    (voice_vault / "Portal/Pipeline/OCR.md").write_text(OCR + "- [ ] New item\n")
    fa = voice_vault / "Portal/Functional Analysis.md"
    fa.write_text(fa.read_text() + "\n- New open point.\n")
    r = filer.verify_voice_note(voice_vault, before, saved,
                                "FILED: Portal/Pipeline/OCR.md\nFILED: Portal/Functional Analysis.md")
    assert r.ok, r.errors
    # ...but rewriting one of them is still rejected
    fa.write_text("# Functional Analysis\n\n#spec\n")
    assert any("Functional Analysis" in e for e in filer.verify_voice_note(voice_vault, before, saved).errors)
    shutil.rmtree(saved)


def test_voice_note_too_many_new_notes(voice_vault):
    before, saved = filer.snapshot(voice_vault), filer.backup(voice_vault)
    for i in range(4):
        (voice_vault / f"Portal/Idea {i}.md").write_text(f"# Idea {i}\n\n#a\n")
    assert any("too many" in e for e in filer.verify_voice_note(voice_vault, before, saved).errors)
    shutil.rmtree(saved)


@pytest.mark.parametrize("breakage, message", [
    # nothing filed besides the existing edit being reverted
    (lambda v: (v / "Portal/Pipeline/OCR.md").write_text(OCR), "no note"),
    # new note not in the index
    (lambda v: ((v / "Inbox").mkdir(), (v / "Inbox/Thought.md").write_text("# T\n\n#idea\n")), "does not mention"),
    # ISO date in the name of a new note
    (lambda v: ((v / "Portal/Pipeline/OCR.md").write_text(OCR),
                (v / "Portal/Ideas 2026-11-13.md").write_text("# I\n\n#a\n")), "DD-MM-YYYY"),
    # new voice note in a Meetings folder
    (lambda v: ((v / "Portal/Pipeline/OCR.md").write_text(OCR),
                (v / "Portal/Meetings/Thoughts.md").write_text("# T\n\n#a\n")), "Meetings folder"),
])
def test_voice_note_rejects_bad_runs(voice_vault, breakage, message):
    before, saved = filer.snapshot(voice_vault), filer.backup(voice_vault)
    (voice_vault / "Portal/Pipeline/OCR.md").write_text(OCR + "- [ ] New item\n")
    breakage(voice_vault)
    r = filer.verify_voice_note(voice_vault, before, saved)
    assert not r.ok
    assert any(message in e for e in r.errors), r.errors
    shutil.rmtree(saved)


def test_voice_note_new_note_must_be_indexed(voice_vault):
    before, saved = filer.snapshot(voice_vault), filer.backup(voice_vault)
    (voice_vault / "Inbox").mkdir()
    (voice_vault / "Inbox/Thought.md").write_text("# Thought\n\n#idea\n\n- x\n")
    r = filer.verify_voice_note(voice_vault, before, saved)
    assert any("does not mention" in e for e in r.errors)
    shutil.rmtree(saved)


def test_api_write_voice_note(voice_vault):
    out = filer_api.write_voice_note(voice_vault, {"notes": [
        {"path": "Inbox/Thought.md", "note_markdown": "# Thought\n\n#idea\n\n- x\n",
         "index_section": "Inbox", "index_line": "- [[Thought]] — x."},
        {"path": "Portal/Pipeline/OCR.md", "note_markdown": OCR + "- [ ] y\n",
         "index_section": None, "index_line": None}]}, "13-11-2026")
    assert out == "FILED: Inbox/Thought.md\nFILED: Portal/Pipeline/OCR.md"
    idx = (voice_vault / "000 Index.md").read_text()
    assert "## Inbox\n\n- [[Thought]] — x." in idx and "*Last updated: 13-11-2026*" in idx
    with pytest.raises(filer.EngineError):
        filer_api.write_voice_note(voice_vault, {"notes": [{"path": "../x.md", "note_markdown": "x",
                                                            "index_section": None, "index_line": None}]},
                                   "13-11-2026")


def test_meeting_note_with_iso_date_rejected(vault, snap):
    (vault / "Portal/Meetings/Meeting Acme 2026-11-19.md").write_text(NOTE)
    idx = vault / "000 Index.md"
    idx.write_text(idx.read_text() + "- [[Meeting Acme 2026-11-19]] — x.\n")
    assert any("DD-MM-YYYY" in e for e in filer.verify(vault, *snap).errors)


def test_pending_oldest_first(dirs):
    import os
    for name, t in (("01-12-2026 0900.md", 200), ("30-11-2026 0900.md", 100)):
        (config.INBOX / name).write_text("t")
        os.utime(config.INBOX / name, (t, t))
    assert [p.name for p in filer.pending()] == ["30-11-2026 0900.md", "01-12-2026 0900.md"]
