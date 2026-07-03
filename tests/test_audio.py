"""Characterization tests for audio.py — the pure G.711/resampling leaf.

These pin down current behavior so the M2+ refactors can prove they changed
nothing on the audio path.
"""
import numpy as np

import audio


# ---------------------------------------------------------------- mu-law codec
def test_round_trip_sine_snr():
    t = np.linspace(0, 1, 8000, endpoint=False)
    pcm = (np.sin(2 * np.pi * 440 * t) * 20000).astype(np.int16)
    decoded = audio.ulaw_to_pcm16(audio.pcm16_to_ulaw(pcm))
    err = decoded.astype(np.float64) - pcm.astype(np.float64)
    snr_db = 10 * np.log10(np.mean(pcm.astype(np.float64) ** 2) / np.mean(err**2))
    assert snr_db > 30  # mu-law delivers ~35-40 dB on speech-level signals


def test_encode_length_and_decode_dtype():
    pcm = np.arange(-1000, 1000, 10, dtype=np.int16)
    ulaw = audio.pcm16_to_ulaw(pcm)
    assert len(ulaw) == len(pcm)
    decoded = audio.ulaw_to_pcm16(ulaw)
    assert decoded.dtype == np.int16
    assert len(decoded) == len(pcm)


def test_silence_round_trips_to_exact_zero():
    pcm = np.zeros(160, dtype=np.int16)
    assert audio.pcm16_to_ulaw(pcm) == b"\xff" * 160
    assert np.all(audio.ulaw_to_pcm16(b"\xff" * 160) == 0)


def test_sign_symmetry():
    pcm = np.array([100, 1000, 10000, 30000], dtype=np.int16)
    pos = audio.ulaw_to_pcm16(audio.pcm16_to_ulaw(pcm))
    neg = audio.ulaw_to_pcm16(audio.pcm16_to_ulaw(-pcm))
    assert np.array_equal(pos, -neg)


def test_extremes_clip_without_overflow():
    pcm = np.array([32767, -32768], dtype=np.int16)
    decoded = audio.ulaw_to_pcm16(audio.pcm16_to_ulaw(pcm))
    assert decoded[0] > 30000
    assert decoded[1] < -30000


# ------------------------------------------------------------------ resampling
def test_resample_24k_to_8k_length():
    pcm = np.zeros(2400, dtype=np.int16)
    assert len(audio.resample(pcm, 24000, 8000)) == 800


def test_resample_same_rate_is_identity():
    pcm = np.array([1, 2, 3], dtype=np.int16)
    assert np.array_equal(audio.resample(pcm, 8000, 8000), pcm)


def test_resample_empty():
    assert len(audio.resample(np.array([], dtype=np.int16), 24000, 8000)) == 0


def test_resample_preserves_dc_level():
    pcm = np.full(2400, 5000, dtype=np.int16)
    out = audio.resample(pcm, 24000, 8000)
    assert np.all(np.abs(out.astype(np.int32) - 5000) <= 1)


# --------------------------------------------------------------- Vobiz framing
def test_vobiz_frames_are_160_bytes():
    # 960 samples @ 24k -> 320 @ 8k -> 320 mu-law bytes -> exactly 2 frames
    pcm = np.zeros(960, dtype=np.int16)
    frames = audio.pcm16_to_vobiz_frames(pcm, src_rate=24000)
    assert len(frames) == 2
    assert all(len(f) == audio.FRAME_BYTES for f in frames)


def test_vobiz_frames_tail_may_be_short():
    # Current behavior: a trailing partial frame is emitted as-is, not padded.
    pcm = np.zeros(990, dtype=np.int16)  # -> 330 mu-law bytes
    frames = audio.pcm16_to_vobiz_frames(pcm, src_rate=24000)
    assert [len(f) for f in frames] == [160, 160, 10]


# ------------------------------------------------------------------------- RMS
def test_rms_silence_and_constant():
    assert audio.rms(np.zeros(160, dtype=np.int16)) == 0.0
    assert abs(audio.rms(np.full(160, 1000, dtype=np.int16)) - 1000.0) < 1e-6


def test_rms_empty():
    assert audio.rms(np.array([], dtype=np.int16)) == 0.0
