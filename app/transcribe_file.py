"""Transcribe audio by hand into inbox/.

    uv run python -m app.transcribe_file meeting.m4a          # any format afconvert can read
    uv run python -m app.transcribe_file recordings/<ts>/     # a recording folder (others.wav + me.wav)
"""

import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from app import config, transcriber
from app.transcript import inbox_name, render, write_atomic


def transcribe_audio(path: Path) -> Path:
    cfg = config.load()
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        # afconvert ships with macOS: 16 kHz mono 16-bit little-endian WAV.
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(path), str(wav)],
                       check=True)
        audio = transcriber.load_wav(wav)
    segments, language = transcriber.transcribe_channel(audio, "Speaker", cfg)
    if not segments:
        raise RuntimeError(f"no speech found in {path}")
    started = datetime.fromtimestamp(path.stat().st_mtime)
    text = render(started, round(audio.size / transcriber.SAMPLE_RATE / 60), "file", language,
                  transcriber.drop_repeats([s for s, _ in segments]))
    out = inbox_name(config.INBOX, started)
    write_atomic(out, text)
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    transcriber.setup_logging()
    path = Path(argv[0]).expanduser().resolve()
    out = transcriber.transcribe_recording(path) if path.is_dir() else transcribe_audio(path)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
