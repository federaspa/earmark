"""Last-resort filing engine: the Anthropic API returns structured JSON and this code writes the
files. Only used when both Copilot CLI and Claude Code CLI failed."""

import re
from datetime import date
from pathlib import Path

import anthropic

from app.filer import INDEX, EngineError, transcript_kind
from app.transcript import DATE

TOOL = {
    "name": "file_meeting",
    "description": "File the meeting note into the vault.",
    "input_schema": {
        "type": "object",
        "properties": {
            "folder": {"type": "string",
                       "description": "Vault-relative folder: '<Project>/Meetings', 'General/Meetings' or 'Inbox'."},
            "filename": {"type": "string", "description": "e.g. 'Meeting Acme 19-11-2026.md'"},
            "note_markdown": {"type": "string",
                              "description": "Full note. If the file already exists, only the part to append "
                                             "(starting with '---')."},
            "previous_note": {"type": ["string", "null"],
                              "description": "Vault-relative path of the previous note in the series, or null."},
            "index_section": {"type": "string",
                              "description": "Text of the '## ' index heading to add the entry under, or 'Inbox'."},
            "index_line": {"type": "string", "description": "'- [[Note name]] — one-sentence summary'"},
        },
        "required": ["folder", "filename", "note_markdown", "previous_note", "index_section", "index_line"],
    },
}


VOICE_TOOL = {
    "name": "file_voice_note",
    "description": "Create or update the notes this voice note belongs in (one per topic).",
    "input_schema": {
        "type": "object",
        "properties": {
            "notes": {"type": "array", "minItems": 1, "maxItems": 5, "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Vault-relative path of the note, existing or "
                                                              "new, e.g. 'Portal/Backend/Login.md'."},
                    "note_markdown": {"type": "string",
                                      "description": "The full note after the change. For an existing note: every "
                                                     "existing line kept unchanged, new content added where it belongs."},
                    "index_section": {"type": ["string", "null"],
                                      "description": "Text of the '## ' index heading to add the note under, "
                                                     "'Inbox', or null if the note is already in the index."},
                    "index_line": {"type": ["string", "null"],
                                   "description": "'- [[Note name]] — one-sentence summary', or null."},
                },
                "required": ["path", "note_markdown", "index_section", "index_line"],
            }},
        },
        "required": ["notes"],
    },
}
CONTEXT_LIMIT = 400_000  # characters of vault notes sent with a voice note


def _all_notes(vault: Path) -> str:
    """Every note's content (a voice note may belong in any of them), up to CONTEXT_LIMIT."""
    parts, size = [], 0
    for p in sorted(vault.rglob("*.md")):
        if any(part.startswith(".") for part in p.relative_to(vault).parts) or p.name == INDEX:
            continue
        text = f"=== {p.relative_to(vault)} ===\n{p.read_text()}"
        if size + len(text) > CONTEXT_LIMIT:
            break
        parts.append(text)
        size += len(text)
    return "\n\n".join(parts)


def _context(vault: Path) -> str:
    """Index, every note path, project overviews and recent meeting notes."""
    paths = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*.md")
                   if not any(part.startswith(".") for part in p.parts))
    parts = [f"=== {INDEX} ===\n{(vault / INDEX).read_text()}",
             "=== All notes ===\n" + "\n".join(paths)]
    for p in sorted(vault.iterdir()):
        overview = p / f"{p.name}.md"
        if overview.exists():
            parts.append(f"=== {overview.relative_to(vault)} ===\n{overview.read_text()}")
        meetings = p / "Meetings"
        if meetings.is_dir():
            for note in sorted(meetings.glob("*.md"))[-3:]:
                parts.append(f"=== {note.relative_to(vault)} ===\n{note.read_text()}")
    return "\n\n".join(parts)


def insert_index_line(index: str, section: str, line: str, today: str) -> str:
    lines = index.splitlines()
    if section == "Inbox" and "## Inbox" not in lines:
        at = lines.index("## Vault Conventions")
        lines[at:at] = ["## Inbox", "", line, "", "---", ""]
    else:
        start = lines.index(f"## {section}")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        sub = next((i for i in range(start, end) if lines[i].startswith("**Meetings**")), start)
        last = sub
        for i in range(sub + 1, end):
            if lines[i].startswith("- "):
                last = i
            elif lines[i].strip() and last != sub:
                break
        if last == sub:  # empty sub-section: put the entry after its heading and a blank line
            lines[sub + 1:sub + 1] = ["", line]
        else:
            lines.insert(last + 1, line)
    text = "\n".join(lines) + "\n"
    return re.sub(r"\*Last updated: [^*]*\*", f"*Last updated: {today}*", text, count=1)


def add_next_link(text: str, stem: str) -> str:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(("Previous:", "Related:")):
            lines[i] = f"{line.rstrip()} · Next: [[{stem}]]"
            break
    else:
        tags = next(i for i, line in enumerate(lines) if line.startswith("#") and not line.startswith("# "))
        lines[tags + 1:tags + 1] = ["", f"Next: [[{stem}]]"]
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _ask(prompt: str, context: str, transcript: Path, tool: dict, cfg: dict) -> dict:
    message = (f"{prompt}\n\nYou have no file tools here. Instead, answer by calling {tool['name']}; "
               "the system will write the files and update the index for you.\n\n"
               f"{context}\n\n=== Transcript ===\n{transcript.read_text()}")
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=cfg["filer"]["api_model"], max_tokens=16000, tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": message}],
        timeout=cfg["filer"]["timeout_min"] * 60)
    return next(b.input for b in resp.content if b.type == "tool_use")


def write_voice_note(vault: Path, data: dict, today: str) -> str:
    """Apply a file_voice_note answer. verify_voice_note checks the result afterwards."""
    filed = []
    for item in data["notes"]:
        rel = Path(item["path"].strip("/"))
        if ".." in rel.parts or rel.suffix != ".md" or len(rel.parts) < 2 or rel.name == INDEX:
            raise EngineError(f"API chose an invalid path: {rel}")
        note = vault / rel
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(item["note_markdown"].strip() + "\n")
        index = vault / INDEX
        if item["index_section"] and item["index_line"] and f"[[{note.stem}]]" not in index.read_text():
            index.write_text(insert_index_line(index.read_text(), item["index_section"], item["index_line"], today))
        filed.append(f"FILED: {rel.as_posix()}")
    return "\n".join(filed)


def run(prompt: str, vault: Path, cfg: dict) -> str:
    transcript = Path(re.search(r"The transcript to file is: (.+)", prompt).group(1).strip())
    today = date.today().strftime(DATE)
    if transcript_kind(transcript) == "voice-note":
        context = f"=== {INDEX} ===\n{(vault / INDEX).read_text()}\n\n{_all_notes(vault)}"
        return write_voice_note(vault, _ask(prompt, context, transcript, VOICE_TOOL, cfg), today)
    data = _ask(prompt, _context(vault), transcript, TOOL, cfg)

    folder = data["folder"].strip("/")
    if ".." in folder or not (folder == "Inbox" or folder.endswith("/Meetings")):
        raise EngineError(f"API chose an invalid folder: {folder}")
    note = vault / folder / Path(data["filename"]).name
    note.parent.mkdir(parents=True, exist_ok=True)
    if note.exists():
        note.write_text(note.read_text().rstrip("\n") + "\n\n" + data["note_markdown"].strip() + "\n")
    else:
        note.write_text(data["note_markdown"].strip() + "\n")

    if data["previous_note"]:
        prev = vault / data["previous_note"]
        if prev.exists() and f"[[{note.stem}]]" not in prev.read_text():
            prev.write_text(add_next_link(prev.read_text(), note.stem))

    index = vault / INDEX
    if f"[[{note.stem}]]" not in index.read_text():
        index.write_text(insert_index_line(index.read_text(), data["index_section"],
                                           data["index_line"], today))
    return f"FILED: {note.relative_to(vault)}"
