"""File transcripts from inbox/ into the Obsidian vault.

For each transcript, one at a time under a lock: snapshot the vault, ask an engine (Copilot CLI,
then Claude Code CLI, then the Anthropic API) to write the note and update the index, verify the
result in code, and roll the vault back before trying the next engine if anything is
wrong. Successful transcripts move to processed/, failed ones to failed/.

A meeting transcript becomes a new meeting note (or fills in a prep note). A voice note
(source: voice-note) updates the existing note it belongs in, or becomes a new note.

    uv run python -m app.filer            # everything in inbox/
    uv run python -m app.filer FILE ...   # specific files
"""

import fcntl
import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app import config, teams_import
from app.transcript import DATE, write_atomic

log = logging.getLogger("filer")

INDEX = "000 Index.md"
TRANSCRIPT_SUFFIXES = {".md", ".vtt", ".docx"}
SKIP_DIRS = {".obsidian", ".trash", ".git"}
COPILOT_TOOLS = "view,glob,rg,apply_patch,create,edit"
CLAUDE_TOOLS = "Read,Write,Edit,Glob,Grep"
TAGS_LINE = re.compile(r"^#[\w/-]+(\s+#[\w/-]+)*\s*$")
FILED_LINE = re.compile(r"^FILED:\s*(.+?)\s*$", re.M)
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
CHECKBOX = re.compile(r"^(\s*[-*] )\[[ xX]\]")


class EngineError(Exception):
    pass


# ---------------------------------------------------------------- vault snapshot / rollback

def snapshot(vault: Path) -> dict[str, str]:
    """Map every vault file (relative path) to its sha256."""
    out = {}
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name == ".DS_Store":
                continue
            p = Path(root, name)
            out[p.relative_to(vault).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def backup(vault: Path) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="vault-backup-"))
    shutil.copytree(vault, dest / "v", ignore=shutil.ignore_patterns(*SKIP_DIRS))
    return dest


def rollback(vault: Path, saved: Path, before: dict[str, str]) -> None:
    """Restore the vault to the backed-up state (only touching files that changed)."""
    after = snapshot(vault)
    for rel in after.keys() - before.keys():
        (vault / rel).unlink()
    for rel, digest in before.items():
        if after.get(rel) != digest:
            (vault / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved / "v" / rel, vault / rel)
    # Remove directories the engine created that are now empty.
    for root, dirs, files in os.walk(vault, topdown=False):
        p = Path(root)
        if p != vault and not any(p.iterdir()) and not (saved / "v" / p.relative_to(vault)).exists():
            p.rmdir()


# ---------------------------------------------------------------- verification

@dataclass
class Result:
    ok: bool
    note: str | None = None
    errors: list[str] = field(default_factory=list)


def _meaningful(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def _is_subsequence(small: list[str], big: list[str]) -> bool:
    it = iter(big)
    return all(any(line == other for other in it) for line in small)


def _links_to(text: str, stem: str) -> bool:
    return re.search(r"\[\[(?:[^\]|]*/)?" + re.escape(stem) + r"(\|[^\]]*)?\]\]", text) is not None


def _note_location_ok(rel: str) -> bool:
    parts = Path(rel).parts
    return (len(parts) >= 3 and parts[-2] == "Meetings") or (len(parts) == 2 and parts[0] == "Inbox")


def _strip_next_link(text: str, stem: str) -> str:
    pattern = r"\s*·?\s*Next:\s*\[\[(?:[^\]|]*/)?" + re.escape(stem) + r"(\|[^\]]*)?\]\]"
    return re.sub(pattern, "", text)


_WIKILINK = re.compile(r"\[\[([^\]|]*)(\|[^\]]*)?\]\]")


def _only_link_change(old_text: str, new_text: str, stem: str) -> bool:
    """True if new_text differs from old_text only by a link to the new note: either a
    "Next: [[stem]]" added, or one wikilink on the link line re-pointed to [[stem]]."""
    old_lines, new_lines = _meaningful(old_text), _meaningful(new_text)
    if _meaningful(_strip_next_link(new_text, stem)) == old_lines:
        # Adding a Next link is only right if there wasn't one already (else it must be re-pointed).
        return not any(l.startswith(("Previous:", "Next:", "Related:")) and "Next:" in l for l in old_lines)
    if len(old_lines) != len(new_lines):
        return False
    changed = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
    if len(changed) != 1:
        return False
    a, b = changed[0]
    if not a.startswith(("Previous:", "Next:", "Related:")):
        return False
    old_links, new_links = _WIKILINK.findall(a), _WIKILINK.findall(b)
    if _WIKILINK.sub("[[]]", a) != _WIKILINK.sub("[[]]", b) or len(old_links) != len(new_links):
        return False
    diffs = [(x[0], y[0]) for x, y in zip(old_links, new_links) if x != y]
    return len(diffs) == 1 and Path(diffs[0][1]).name == stem


def _changes(vault: Path, before: dict[str, str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """(added, deleted, modified, basic errors) since the snapshot."""
    after = snapshot(vault)
    added = sorted(after.keys() - before.keys())
    deleted = sorted(before.keys() - after.keys())
    modified = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    errors = []
    if deleted:
        errors.append(f"files deleted: {deleted}")
    if any(not rel.endswith(".md") for rel in added + modified):
        errors.append(f"non-markdown files changed: {[r for r in added + modified if not r.endswith('.md')]}")
    return added, deleted, modified, errors


def _claimed_all(engine_output: str) -> list[str]:
    return [m.strip("`'\" ") for m in FILED_LINE.findall(engine_output)]


def _claimed(engine_output: str) -> str | None:
    m = _claimed_all(engine_output)
    return m[-1] if m else None


def _check_note_format(rel: str, text: str, check_name: bool = True) -> list[str]:
    errors = []
    lines = _meaningful(text)
    if not lines or not lines[0].startswith("# "):
        errors.append("note does not start with an H1")
    if not any(TAGS_LINE.match(line) for line in lines[:4]):
        errors.append("note has no #tags line after the H1")
    if check_name and ISO_DATE.search(Path(rel).stem):
        errors.append(f"note name has a YYYY-MM-DD date; dates must be DD-MM-YYYY: {rel}")
    return errors


def _check_index(old_index: str, new_index: str, stem: str) -> list[str]:
    """The index must link the note and keep all its other lines (except *Last updated*)."""
    errors = []
    if not _links_to(new_index, stem):
        errors.append(f"000 Index.md does not mention [[{stem}]]")
    kept = [l for l in _meaningful(old_index) if not l.startswith("*Last updated") and not _links_to(l, stem)]
    if not _is_subsequence(kept, _meaningful(new_index)):
        errors.append("000 Index.md had other lines changed or removed")
    return errors


def verify(vault: Path, before: dict[str, str], saved: Path, engine_output: str = "") -> Result:
    """Check that the engine filed a meeting with exactly the allowed changes and nothing else."""
    added, deleted, modified, errors = _changes(vault, before)
    old = lambda rel: (saved / "v" / rel).read_text(encoding="utf-8")
    new = lambda rel: (vault / rel).read_text(encoding="utf-8")

    # Which file is the meeting note? A new note, or an existing (prep) note that was filled in.
    filled_existing = False
    if len(added) == 1:
        note = added[0]
    elif len(added) > 1:
        return Result(False, None, errors + [f"more than one new file: {added}"])
    else:
        claimed = _claimed(engine_output)
        candidates = [r for r in modified if r != INDEX and _note_location_ok(r)]
        note = claimed if claimed in candidates else (candidates[0] if len(candidates) == 1 else None)
        if note is None:
            return Result(False, None, errors + ["no meeting note was created"])
        filled_existing = True

    stem = Path(note).stem
    if not _note_location_ok(note):
        errors.append(f"note is not in a */Meetings/ folder or Inbox/: {note}")
    if filled_existing:
        errors += _check_note_format(note, new(note), check_name=False)
        if not _is_subsequence(_meaningful(old(note)), _meaningful(new(note))):
            errors.append("existing content of the note was changed")
    else:
        errors += _check_note_format(note, new(note))

    if INDEX not in modified:
        errors.append("000 Index.md was not updated")
    else:
        errors += _check_index(old(INDEX), new(INDEX), stem)

    # At most the two neighbours in the series: the previous note (Next link added or re-pointed)
    # and, when the note is inserted before an existing later note, that note (Previous re-pointed).
    others = [r for r in modified if r not in (INDEX, note)]
    if len(others) > 2:
        errors.append(f"too many existing notes modified: {others}")
    for rel in others:
        if not _only_link_change(old(rel), new(rel), stem):
            errors.append(f"{rel} was changed beyond its Previous/Next link to the new note")

    return Result(not errors, note, errors)


def _unticked(text: str) -> list[str]:
    return [CHECKBOX.sub(r"\1[ ]", line) for line in _meaningful(text)]


MAX_VOICE_NOTES, MAX_NEW_VOICE_NOTES = 5, 3


def verify_voice_note(vault: Path, before: dict[str, str], saved: Path, engine_output: str = "") -> Result:
    """Check that the engine created or updated a few notes (plus the index). An updated note must
    keep all its lines; only ticking a to-do is allowed besides adding content."""
    added, deleted, modified, errors = _changes(vault, before)
    old = lambda rel: (saved / "v" / rel).read_text(encoding="utf-8")
    new = lambda rel: (vault / rel).read_text(encoding="utf-8")

    updated = [r for r in modified if r != INDEX]
    notes = added + updated
    if not notes:
        return Result(False, None, errors + ["no note was created or updated"])
    if len(notes) > MAX_VOICE_NOTES or len(added) > MAX_NEW_VOICE_NOTES:
        return Result(False, None, errors + [
            f"too many notes touched (max {MAX_VOICE_NOTES}, {MAX_NEW_VOICE_NOTES} new): "
            f"new={added} updated={updated}"])
    unexpected = [c for c in _claimed_all(engine_output) if c not in notes and c != INDEX]
    if unexpected:
        errors.append(f"engine says it filed {unexpected} but changed {notes}")

    for note in notes:
        if len(Path(note).parts) < 2:
            errors.append(f"note is not inside a folder: {note}")
        if note in added:
            if "Meetings" in Path(note).parts[:-1]:
                errors.append(f"new voice note put in a Meetings folder: {note}")
            errors += _check_note_format(note, new(note))
        elif not _is_subsequence(_unticked(old(note)), _unticked(new(note))):
            errors.append(f"existing content of {note} was changed")

    if INDEX in modified:
        kept = [l for l in _meaningful(old(INDEX)) if not l.startswith("*Last updated")
                and not any(_links_to(l, Path(n).stem) for n in notes)]
        if not _is_subsequence(kept, _meaningful(new(INDEX))):
            errors.append("000 Index.md had other lines changed or removed")
    for note in notes:
        if not _links_to(new(INDEX), Path(note).stem):
            errors.append(f"000 Index.md does not mention [[{Path(note).stem}]]")
    return Result(not errors, ", ".join(notes), errors)


VERIFIERS = {"meeting": verify, "voice-note": verify_voice_note}


def transcript_kind(transcript: Path) -> str:
    """"voice-note" if the transcript's frontmatter says source: voice-note, else "meeting"."""
    head = transcript.read_text(encoding="utf-8").split("\n---", 1)[0]
    return "voice-note" if re.search(r"^source:\s*voice-note\s*$", head, re.M) else "meeting"


# ---------------------------------------------------------------- engines

def build_prompt(transcript: Path, kind: str = "meeting", owner: str = "") -> str:
    """The prompt for this kind of transcript, plus prompts/local.md (examples from the real
    vault, kept out of the repository) if it exists."""
    text = config.PROMPTS[kind].read_text(encoding="utf-8")
    if config.LOCAL_PROMPT.exists():
        text += "\n\n" + config.LOCAL_PROMPT.read_text(encoding="utf-8")
    return (text.replace("{transcript_path}", str(transcript)).replace("{today}", date.today().strftime(DATE))
            .replace("{owner}", owner or "the vault owner"))


def _run(cmd: list[str], cwd: Path, timeout: float) -> str:
    """Run a CLI in its own process group so a timeout kills the whole tree."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise EngineError(f"timed out after {timeout / 60:.0f} min")
    if proc.returncode != 0:
        raise EngineError(f"exit code {proc.returncode}: {out[-2000:]}")
    return out


def run_copilot(prompt: str, vault: Path, cfg: dict) -> str:
    cmd = ["copilot", "-p", prompt, "-s",
           f"--available-tools={COPILOT_TOOLS}", "--allow-all-tools", "--deny-tool=shell",
           "--add-dir", str(config.INBOX), "--disallow-temp-dir", "--disable-builtin-mcps",
           "--no-custom-instructions", "--no-ask-user"]
    if cfg["filer"]["copilot_model"]:
        cmd += ["--model", cfg["filer"]["copilot_model"]]
    return _run(cmd, vault, cfg["filer"]["timeout_min"] * 60)


def run_claude(prompt: str, vault: Path, cfg: dict) -> str:
    cmd = ["claude", "-p", prompt, "--tools", CLAUDE_TOOLS, "--allowedTools", CLAUDE_TOOLS,
           "--add-dir", str(config.INBOX), "--setting-sources", "project", "--strict-mcp-config"]
    if cfg["filer"]["claude_model"]:
        cmd += ["--model", cfg["filer"]["claude_model"]]
    return _run(cmd, vault, cfg["filer"]["timeout_min"] * 60)


def run_api(prompt: str, vault: Path, cfg: dict) -> str:
    from app import filer_api  # imported lazily: only needed as a last resort
    return filer_api.run(prompt, vault, cfg)


ENGINES = {"copilot": run_copilot, "claude": run_claude, "api": run_api}


def available_engines() -> list[str]:
    names = os.environ.get("FILER_ENGINES", "copilot,claude,api").split(",")
    out = []
    for name in names:
        if name in ("copilot", "claude") and not shutil.which(name):
            continue
        if name == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
            continue
        out.append(name)
    return out


# ---------------------------------------------------------------- per-file processing

def move_to(path: Path, folder: Path) -> Path:
    folder.mkdir(exist_ok=True)
    dest, n = folder / path.name, 2
    while dest.exists():
        dest, n = folder / f"{path.stem} ({n}){path.suffix}", n + 1
    shutil.move(path, dest)
    return dest


def notify(title: str, message: str) -> None:
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                   check=False, capture_output=True)


def wait_until_stable(path: Path, quiet: float = 2.0, max_wait: float = 120.0) -> None:
    """Wait until the file has stopped growing (e.g. a .docx still being copied in)."""
    deadline = time.time() + max_wait
    last = None
    while time.time() < deadline:
        st = path.stat()
        if (st.st_size, st.st_mtime) == last and time.time() - st.st_mtime >= quiet:
            return
        last = (st.st_size, st.st_mtime)
        time.sleep(quiet / 2)


def convert_teams(path: Path) -> Path:
    """Turn a Teams .vtt/.docx into an inbox .md transcript; the original goes to processed/."""
    md = path.with_suffix(".md")
    n = 2
    while md.exists():
        md, n = path.with_name(f"{path.stem} ({n}).md"), n + 1
    write_atomic(md, teams_import.convert(path))
    move_to(path, config.PROCESSED)
    log.info("converted %s -> %s", path.name, md.name)
    return md


def file_transcript(transcript: Path, vault: Path, cfg: dict) -> str | None:
    """Try each engine until one passes verification. Returns the note path, or None."""
    kind = transcript_kind(transcript)
    prompt, check = build_prompt(transcript, kind, cfg["filer"].get("owner", "")), VERIFIERS[kind]
    forced = set(os.environ.get("FILER_FORCE_FAIL", "").split(","))
    for engine in available_engines():
        before, saved = snapshot(vault), backup(vault)
        t0 = time.time()
        note = None
        try:
            if engine in forced:
                raise EngineError("forced failure (FILER_FORCE_FAIL)")
            out = ENGINES[engine](prompt, vault, cfg)
            result = check(vault, before, saved, out)
            if result.ok:
                note = result.note
            else:
                log.warning("engine=%s duration=%.0fs verification failed: %s",
                            engine, time.time() - t0, "; ".join(result.errors))
        except Exception as e:  # noqa: BLE001 - any engine failure means "try the next one"
            log.warning("engine=%s duration=%.0fs failed: %s", engine, time.time() - t0, e)
        finally:
            if note is None:
                rollback(vault, saved, before)
            shutil.rmtree(saved, ignore_errors=True)
        if note:
            log.info("OK engine=%s kind=%s duration=%.0fs note=%s", engine, kind, time.time() - t0, note)
            return note
    return None


def process(path: Path, vault: Path, cfg: dict) -> bool:
    log.info("processing %s (vault=%s)", path.name, vault)
    wait_until_stable(path)
    try:
        if path.suffix.lower() in (".vtt", ".docx"):
            path = convert_teams(path)
    except Exception as e:  # noqa: BLE001
        log.error("could not convert %s: %s", path.name, e)
        move_to(path, config.FAILED)
        notify("Meeting filer", f"Could not read {path.name}; moved to failed/.")
        return False
    note = file_transcript(path, vault, cfg)
    if note:
        move_to(path, config.PROCESSED)
        return True
    dest = move_to(path, config.FAILED)
    log.error("all engines failed for %s -> %s", path.name, dest)
    notify("Meeting filer", f"Filing {path.name} failed after all retries; moved to failed/.")
    return False


def pending() -> list[Path]:
    """Inbox files, oldest first (names are DD-MM-YYYY, which doesn't sort by date)."""
    return sorted((p for p in config.INBOX.iterdir()
                   if p.is_file() and not p.name.startswith(".")
                   and p.suffix.lower() in TRANSCRIPT_SUFFIXES),
                  key=lambda p: (p.stat().st_mtime, p.name))


def setup_logging() -> None:
    config.LOGS.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(config.LOGS / "filer.log"), logging.StreamHandler()])


def main(argv: list[str]) -> int:
    setup_logging()
    config.load_env()
    cfg = config.load()
    vault = cfg["vault"]["path"]
    if not (vault / INDEX).exists():
        log.error("vault has no %s: %s", INDEX, vault)
        return 2
    lock = open(config.LOGS / ".filer.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another filer run is active; it will pick up new files")
        return 0
    failures = 0
    if argv:
        for p in argv:
            failures += not process(Path(p).resolve(), vault, cfg)
    else:
        # Keep going until the inbox is empty, so files that arrive mid-run are not missed.
        while todo := pending():
            failures += not process(todo[0], vault, cfg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
