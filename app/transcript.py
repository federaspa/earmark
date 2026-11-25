"""The transcript file format shared by the transcriber, the Teams importers and the filer.

    ---
    started: 19-11-2026 10:00
    duration_min: 42
    source: recorder
    language: it
    ---

    [00:00:12] Me: ...
    [00:00:15] Others: ...

source is "recorder" (a call), "voice-note" (the owner talking alone), "teams" or "file".
Dates are DD-MM-YYYY everywhere a person reads them.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DATE = "%d-%m-%Y"


@dataclass
class Segment:
    start: float  # seconds from the start of the meeting
    speaker: str
    text: str


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def render(started: datetime, duration_min: int, source: str, language: str,
           segments: list[Segment]) -> str:
    head = (f"---\nstarted: {started.strftime(DATE + ' %H:%M')}\n"
            f"duration_min: {duration_min}\nsource: {source}\nlanguage: {language}\n---\n\n")
    return head + "".join(f"[{hms(s.start)}] {s.speaker}: {s.text}\n" for s in segments)


def write_atomic(path: Path, text: str) -> None:
    """Write via a hidden temp file + fsync + rename, so watchers never see a partial file."""
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def inbox_name(inbox: Path, started: datetime) -> Path:
    """inbox/DD-MM-YYYY HHMM.md, with a suffix if that name is taken."""
    base = started.strftime(DATE + " %H%M")
    path, n = inbox / f"{base}.md", 2
    while path.exists():
        path, n = inbox / f"{base} ({n}).md", n + 1
    return path
