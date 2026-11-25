"""Convert Teams transcripts (.vtt and .docx) into the shared transcript format."""

import re
from datetime import datetime
from pathlib import Path

from docx import Document

from app.transcript import Segment, render

_TS = r"(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)"
_CUE = re.compile(rf"^{_TS}\s*-->\s*{_TS}")
_VOICE = re.compile(r"<v\s+([^>]+)>(.*?)(?:</v>|$)", re.S)
_TAG = re.compile(r"</?[^>]+>")
# "Mario Rossi   0:03" or "Mario Rossi   1:02:03" (newer Teams .docx)
_DOCX_SPEAKER = re.compile(r"^(.+?)\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*$")
_DATE_IN_NAME = re.compile(r"(?<!\d)(\d\d)-(\d\d)-(20\d\d)(?!\d)")  # DD-MM-YYYY
_ISO_DATE_IN_NAME = re.compile(r"(20\d\d)-?(\d\d)-?(\d\d)")


def _secs(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


def _clock(text: str) -> float:
    parts = [float(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def merge_consecutive(segments: list[Segment]) -> list[Segment]:
    """Join consecutive segments by the same speaker (Teams splits sentences into many cues)."""
    out: list[Segment] = []
    for seg in segments:
        if out and out[-1].speaker == seg.speaker:
            out[-1].text += " " + seg.text
        else:
            out.append(Segment(seg.start, seg.speaker, seg.text))
    return out


def parse_vtt(text: str) -> list[Segment]:
    segments, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        m = _CUE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        start = _secs(*m.groups()[:3])
        i += 1
        body = []
        while i < len(lines) and lines[i].strip():
            body.append(lines[i].strip())
            i += 1
        raw = " ".join(body)
        v = _VOICE.search(raw)
        speaker, words = (v.group(1).strip(), v.group(2)) if v else ("Unknown", raw)
        words = _TAG.sub("", words).strip()
        if words:
            segments.append(Segment(start, speaker, words))
    return merge_consecutive(segments)


def parse_docx_paragraphs(paras: list[str]) -> tuple[list[Segment], datetime | None]:
    """Teams .docx: a header (title, date, duration), then 'Name  m:ss' lines each followed by text.
    Older exports use 'h:mm:ss.fff --> h:mm:ss.fff' / name / text triplets instead."""
    segments: list[Segment] = []
    started = None
    speaker, start, i = None, 0.0, 0
    paras = [p.strip() for p in paras]
    while i < len(paras):
        p = paras[i]
        cue = _CUE.match(p)
        sm = _DOCX_SPEAKER.match(p)
        if cue and i + 2 < len(paras):
            segments.append(Segment(_secs(*cue.groups()[:3]), paras[i + 1], paras[i + 2]))
            i += 3
            continue
        # A text line can also end in a time ("... at 10:30"); a real speaker line has a short
        # name, a timestamp that doesn't go backwards, and is followed by text.
        if (sm and len(sm.group(1).split()) <= 5 and _clock(sm.group(2)) >= start
                and i + 1 < len(paras) and paras[i + 1]):
            speaker, start = sm.group(1).strip(), _clock(sm.group(2))
        elif p and speaker:
            segments.append(Segment(start, speaker, p))
        elif p and started is None:
            started = _parse_header_date(p)
        i += 1
    return merge_consecutive(segments), started


def _parse_header_date(text: str) -> datetime | None:
    cleaned = re.sub(r"\s+", " ", text.replace(",", "")).strip()
    for fmt in ("%B %d %Y %I:%M%p", "%B %d %Y %I:%M %p", "%d %B %Y %H:%M", "%B %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            pass
    return None


def _date_from_name(path: Path) -> datetime | None:
    if m := _DATE_IN_NAME.search(path.stem):
        day, month, year = m.groups()
    elif m := _ISO_DATE_IN_NAME.search(path.stem):
        year, month, day = m.groups()
    else:
        return None
    try:
        return datetime(int(year), int(month), int(day))
    except ValueError:
        return None


def convert(path: Path) -> str:
    """Return the transcript text for a Teams .vtt or .docx file."""
    started = None
    if path.suffix.lower() == ".vtt":
        segments = parse_vtt(path.read_text(encoding="utf-8-sig"))
    else:
        doc = Document(str(path))
        segments, started = parse_docx_paragraphs([p.text for p in doc.paragraphs])
    if not segments:
        raise ValueError(f"no transcript lines found in {path.name}")
    started = started or _date_from_name(path) or datetime.fromtimestamp(path.stat().st_mtime)
    duration = round(segments[-1].start / 60)
    return render(started, duration, "teams", "", segments)
