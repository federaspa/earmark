"""Build a fake two-channel recording with macOS `say` for manual transcriber tests:
Alex (English) on the mic, the others (Italian, then English) on the system channel,
plus a quiet echo of the others leaking into the mic.

    uv run python -m tests.make_sample_recording   # -> recordings/<now>/
"""

import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np

from app import config, transcriber
from tests.test_transcriber import write_wav

LINES = [  # (seconds, channel, voice, text)
    (1, "me", "Samantha", "Good morning everyone. Let's start with the login page."),
    (7, "others", "Alice", "Buongiorno Alex. Abbiamo finito i test sul login, "
                           "il resto arriva a fine mese."),
    (17, "me", "Samantha", "Perfect. I will run the full test suite next week."),
    (23, "others", "Daniel", "Can you also add the setup steps to the user guide?"),
    (29, "me", "Alice", "Sì, certo. Lo aggiungo entro venerdì."),
]


def main() -> None:
    sr, total = transcriber.SAMPLE_RATE, 40 * transcriber.SAMPLE_RATE
    channels = {"me": np.zeros(total), "others": np.zeros(total)}
    with tempfile.TemporaryDirectory() as tmp:
        for i, (t, channel, voice, text) in enumerate(LINES):
            wav = Path(tmp) / f"{i}.wav"
            subprocess.run(["say", "-v", voice, "-o", str(wav), "--data-format=LEI16@16000", text], check=True)
            clip = transcriber.load_wav(wav)
            channels[channel][int(t * sr):int(t * sr) + clip.size] += clip
    channels["me"] += 0.25 * channels["others"]  # speakers leaking into the mic
    channels["me"] += np.random.default_rng(0).normal(0, 0.002, total)  # room noise
    started = datetime.now().replace(microsecond=0)
    folder = config.RECORDINGS / started.strftime("%d-%m-%Y %H%M%S")
    folder.mkdir(parents=True)
    for name, audio in channels.items():
        write_wav(folder / f"{name}.wav", np.clip(audio, -1, 1))
    (folder / "session.json").write_text(json.dumps({"started": started.isoformat(), "backend": "say"}))
    print(folder)


if __name__ == "__main__":
    main()
