import struct

import numpy as np

from app import transcriber
from app.transcript import Segment


def seg(start, speaker, text, end=None):
    return Segment(start, speaker, text), end if end is not None else start + 3


def test_merge_orders_by_timestamp_and_labels():
    me = [seg(0, "Me", "Hi all."), seg(10, "Me", "Let's start.")]
    others = [seg(4, "Others", "Ciao Alex.")]
    out = transcriber.merge(me, others)
    assert [(s.start, s.speaker) for s in out] == [(0, "Me"), (4, "Others"), (10, "Me")]


def test_merge_drops_echo_of_others_on_mic():
    others = [seg(20, "Others", "Abbiamo finito i test sul login.")]
    me = [seg(20.4, "Me", "abbiamo finito i test sul login"),  # echo
          seg(25, "Me", "Ok, thanks.")]
    out = transcriber.merge(me, others)
    assert [s.speaker for s in out] == ["Others", "Me"]
    assert out[1].text == "Ok, thanks."


def test_merge_keeps_similar_text_far_apart_in_time():
    others = [seg(20, "Others", "Yes, exactly.")]
    me = [seg(300, "Me", "Yes, exactly.")]
    assert len(transcriber.merge(me, others)) == 2


def test_merge_drops_consecutive_repeats():
    me = [seg(0, "Me", "Thank you."), seg(3, "Me", "thank you."), seg(6, "Me", "Bye.")]
    assert [s.text for s in transcriber.merge(me, [])] == ["Thank you.", "Bye."]


def s(text, logprob=-0.2, cr=1.2, nsp=0.1):
    return {"text": text, "avg_logprob": logprob, "compression_ratio": cr, "no_speech_prob": nsp}


def test_is_junk():
    assert not transcriber.is_junk(s("Parliamo della pull request."))
    assert transcriber.is_junk(s("  ...  "))
    assert transcriber.is_junk(s("Sottotitoli creati dalla comunità Amara.org"))
    assert transcriber.is_junk(s("Thanks for watching!"))
    assert transcriber.is_junk(s("something", logprob=-1.4))
    assert transcriber.is_junk(s("la la la la la la la la", cr=3.0))
    assert transcriber.is_junk(s("ok ok ok ok ok ok ok ok"))
    assert transcriber.is_junk(s("hmm", nsp=0.9, logprob=-0.7))
    assert transcriber.is_junk(s(" Thank you.", logprob=-0.63))
    assert transcriber.is_junk(s(" You", logprob=-0.91))
    assert not transcriber.is_junk(s("Grazie a tutti.", logprob=-0.2))


def write_wav(path, samples, data_size=None):
    pcm = (np.asarray(samples) * 32767).astype("<i2").tobytes()
    size = len(pcm) if data_size is None else data_size
    header = (b"RIFF" + struct.pack("<I", 36 + size) + b"WAVEfmt " +
              struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) + b"data" + struct.pack("<I", size))
    path.write_bytes(header + pcm)


def test_load_wav_reads_crashed_file_with_stale_header(tmp_path):
    f = tmp_path / "me.wav"
    write_wav(f, np.full(16000, 0.5), data_size=0)  # header never updated
    audio = transcriber.load_wav(f)
    assert audio.size == 16000
    assert abs(audio.mean() - 0.5) < 0.01


def tone(seconds, amp=0.2):
    t = np.arange(int(seconds * 16000)) / 16000
    return amp * np.sin(2 * np.pi * 220 * t)


def test_speech_chunks_split_at_pauses_with_offsets():
    quiet = lambda sec: np.random.default_rng(1).normal(0, 0.001, int(sec * 16000))
    audio = np.concatenate([quiet(2), tone(3), quiet(5), tone(4), quiet(2)])
    chunks = [(a / 16000, b / 16000) for a, b in transcriber.speech_chunks(audio)]
    assert len(chunks) == 2
    assert abs(chunks[0][0] - 1.75) < 0.1 and abs(chunks[0][1] - 5.25) < 0.1
    assert abs(chunks[1][0] - 9.75) < 0.1 and abs(chunks[1][1] - 14.25) < 0.1


def test_speech_chunks_short_pause_kept_together_and_long_speech_split():
    audio = np.concatenate([tone(3), np.zeros(4000), tone(3)])  # 0.25 s pause
    assert len(transcriber.speech_chunks(audio)) == 1
    assert len(transcriber.speech_chunks(tone(60))) == 3  # 25 + 25 + 10 s


def test_speech_chunks_silence():
    assert transcriber.speech_chunks(np.zeros(16000 * 5)) == []
