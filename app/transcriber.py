"""Transcribe a recording (others.wav + me.wav, or only me.wav for a voice note) into an inbox
transcript with mlx-whisper.

Each channel is transcribed separately, junk segments are dropped, "Me" segments that are just
the speakers' echo of "Others" are removed, and both are merged by timestamp.
"""

import json
import logging
import os
import re
import shutil
import struct
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

from app import config
from app.transcript import Segment, inbox_name, render, write_atomic

log = logging.getLogger("transcriber")

SAMPLE_RATE = 16000
# Phrases Whisper is known to hallucinate on silence or noise.
HALLUCINATIONS = [
    "sottotitoli creati dalla comunità amara.org", "sottotitoli a cura di", "amara.org",
    "thanks for watching", "thank you for watching", "subscribe to", "grazie per la visione",
    "iscriviti al canale",
]
# Short fillers Whisper produces from noise; dropped only when it isn't confident about them.
SHORT_HALLUCINATIONS = {"thank you", "thank you very much", "thanks", "you", "bye", "okay",
                        "grazie", "grazie a tutti", "grazie mille", "ciao"}


def load_wav(path: Path) -> np.ndarray:
    """16 kHz mono 16-bit WAV -> float32 in [-1, 1]. Tolerates a header with wrong sizes
    (e.g. after a crash) by reading whatever frames are on disk."""
    data = path.read_bytes()
    fmt, start = data.find(b"fmt "), data.find(b"data", 12, 4096)
    if data[:4] != b"RIFF" or fmt < 0 or start < 0:
        raise ValueError(f"{path.name}: not a WAV file")
    channels, rate = struct.unpack_from("<HI", data, fmt + 10)
    bits = struct.unpack_from("<H", data, fmt + 22)[0]
    if (rate, channels, bits) != (SAMPLE_RATE, 1, 16):
        raise ValueError(f"{path.name}: expected 16 kHz mono 16-bit, got {rate} Hz {channels}ch {bits}-bit")
    raw = data[start + 8:]  # ignore the size fields: they may be stale after a crash
    raw = raw[: len(raw) // 2 * 2]
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def is_junk(seg: dict) -> bool:
    text = seg["text"].strip()
    low = text.lower().strip(" .!")
    if not re.search(r"\w", text):
        return True
    if seg.get("avg_logprob", 0) < -1.0:
        return True
    if seg.get("compression_ratio", 0) > 2.4:  # repetitive text
        return True
    if seg.get("no_speech_prob", 0) > 0.6 and seg.get("avg_logprob", 0) < -0.5:
        return True
    if any(h in low for h in HALLUCINATIONS):
        return True
    if low.strip(" .,!?") in SHORT_HALLUCINATIONS and seg.get("avg_logprob", 0) < -0.5:
        return True
    words = low.split()
    if len(words) >= 6 and len(set(words)) <= len(words) / 4:  # "ok ok ok ok ok ok"
        return True
    return False


def drop_repeats(segments: list[Segment]) -> list[Segment]:
    """Drop a segment identical to the previous one from the same speaker (Whisper loops)."""
    out: list[Segment] = []
    for seg in segments:
        if out and out[-1].speaker == seg.speaker and out[-1].text.lower() == seg.text.lower():
            continue
        out.append(seg)
    return out


def drop_echo(me: list[tuple[Segment, float]], others: list[tuple[Segment, float]],
              threshold: float = 0.6) -> list[tuple[Segment, float]]:
    """Without headphones the mic hears the call too. Drop a "Me" segment that overlaps in time
    with a similar "Others" segment. Items are (segment, end_time)."""
    kept = []
    for seg, end in me:
        echo = any(o.start < end + 1 and seg.start < o_end + 1
                   and SequenceMatcher(None, seg.text.lower(), o.text.lower()).ratio() >= threshold
                   for o, o_end in others)
        if not echo:
            kept.append((seg, end))
    return kept


def merge(me: list[tuple[Segment, float]], others: list[tuple[Segment, float]]) -> list[Segment]:
    me = drop_echo(me, others)
    segments = sorted([s for s, _ in me] + [s for s, _ in others], key=lambda s: s.start)
    return drop_repeats(segments)


def speech_chunks(audio: np.ndarray, max_len: float = 25.0, min_gap: float = 0.6,
                  pad: float = 0.25) -> list[tuple[int, int]]:
    """Energy-based voice activity detection: (start, end) sample ranges of speech, split at
    pauses and at most max_len seconds long. Whisper never sees long silences (where it
    hallucinates), and each chunk gets its own language detection and accurate timestamps."""
    frame = SAMPLE_RATE * 30 // 1000
    n = audio.size // frame
    if n == 0:
        return []
    rms = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1))
    # Above the noise floor, but capped relative to the loud parts in case the channel is mostly speech.
    noise, loud = np.percentile(rms, 10), np.percentile(rms, 95)
    speech = rms > max(min(noise * 3, loud * 0.1), 0.003)
    regions, start = [], None
    for i, on in enumerate(np.append(speech, False)):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if regions and (start - regions[-1][1]) * 0.03 < min_gap:
                regions[-1] = (regions[-1][0], i)
            else:
                regions.append((start, i))
            start = None
    regions = [(a * frame, b * frame) for a, b in regions if (b - a) * 0.03 >= 0.2]

    limit, p = int(max_len * SAMPLE_RATE), int(pad * SAMPLE_RATE)
    chunks: list[tuple[int, int]] = []
    for a, b in regions:
        while b - a > limit:  # one very long stretch without pauses: hard split
            chunks.append((a, a + limit))
            a += limit
        if chunks and b - chunks[-1][0] <= limit and a - chunks[-1][1] < SAMPLE_RATE:
            chunks[-1] = (chunks[-1][0], b)
        else:
            chunks.append((a, b))
    return [(max(0, a - p), min(audio.size, b + p)) for a, b in chunks]


def _detect_language(chunk: np.ndarray, cfg: dict) -> str:
    """Most likely language among the configured ones (short chunks are otherwise often
    misdetected as a random language)."""
    import mlx.core as mx
    from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
    from mlx_whisper.transcribe import ModelHolder

    model = ModelHolder.get_model(cfg["transcriber"]["model"], mx.float16)
    mel = log_mel_spectrogram(chunk, n_mels=model.dims.n_mels, padding=N_SAMPLES)
    _, probs = model.detect_language(pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16))
    allowed = cfg["transcriber"].get("languages") or list(probs)
    return max(allowed, key=lambda lang: probs.get(lang, 0.0))


def _offline_if_cached(repo: str) -> None:
    """Once the model is downloaded, stop Hugging Face from checking for updates: with
    HF_HUB_OFFLINE nothing leaves the Mac during transcription. Must run before mlx_whisper
    (and so huggingface_hub) is imported."""
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache/huggingface")) / "hub"
    snapshots = hub / f"models--{repo.replace('/', '--')}" / "snapshots"
    if any(snapshots.glob("*/config.json")):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")


def transcribe_channel(audio: np.ndarray, speaker: str, cfg: dict) -> tuple[list[tuple[Segment, float]], str]:
    """Returns ([(segment, end), ...], languages found, most frequent first)."""
    chunks = speech_chunks(audio)
    if not chunks:
        log.info("%s channel is silent, skipping", speaker)
        return [], ""
    _offline_if_cached(cfg["transcriber"]["model"])
    import mlx_whisper  # slow import; only needed here

    kept, total, languages = [], 0, {}
    for a, b in chunks:
        chunk = audio[a:b]
        language = cfg["transcriber"]["language"] or _detect_language(chunk, cfg)
        result = mlx_whisper.transcribe(
            chunk, path_or_hf_repo=cfg["transcriber"]["model"], language=language,
            condition_on_previous_text=False,
            word_timestamps=True,  # accurate segment starts (otherwise often the chunk start)
            verbose=None)
        offset = a / SAMPLE_RATE
        for s in result["segments"]:
            total += 1
            if not is_junk(s):
                kept.append((Segment(offset + s["start"], speaker, s["text"].strip()), offset + s["end"]))
                languages[language] = languages.get(language, 0) + 1
    found = ",".join(sorted(languages, key=languages.get, reverse=True))
    log.info("%s: %d chunks, %d/%d segments kept, languages=%s", speaker, len(chunks), len(kept), total, found)
    return kept, found


def transcribe_recording(folder: Path, cfg: dict | None = None) -> Path:
    """Transcribe recordings/<ts>/ into inbox/ and delete the WAVs once the transcript is safe."""
    cfg = cfg or config.load()
    session = json.loads((folder / "session.json").read_text()) if (folder / "session.json").exists() else {}
    started = datetime.fromisoformat(session["started"]) if "started" in session else \
        datetime.fromtimestamp(folder.stat().st_mtime)

    channels, languages, duration = {}, [], 0.0
    for name, speaker in (("others.wav", "Others"), ("me.wav", "Me")):
        path = folder / name
        if not path.exists():
            channels[speaker] = []
            continue
        audio = load_wav(path)
        duration = max(duration, audio.size / SAMPLE_RATE)
        channels[speaker], lang = transcribe_channel(audio, speaker, cfg)
        languages += [l for l in lang.split(",") if l and l not in languages]

    segments = merge(channels["Me"], channels["Others"])
    if not segments:
        raise RuntimeError(f"no speech found in {folder}")
    source = "voice-note" if session.get("mode") == "voice-note" else "recorder"
    text = render(started, round(duration / 60), source, ",".join(languages), segments)
    out = inbox_name(config.INBOX, started)
    write_atomic(out, text)  # fsynced before the audio is removed
    shutil.rmtree(folder)
    log.info("wrote %s (%d segments), deleted %s", out.name, len(segments), folder.name)
    return out


def setup_logging() -> None:
    config.LOGS.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(config.LOGS / "transcriber.log"), logging.StreamHandler()])
