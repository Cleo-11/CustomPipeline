"""Tests for the RIFF chunk walk in providers/tts/sarvam.py (defect D9).

The old parser assumed sample rate at byte 24 and data at byte 44; a legal
WAV with a LIST chunk before data silently produced garbage audio.
"""
import struct

import numpy as np
import pytest

from providers.tts.sarvam import _parse_wav


def _wav(pcm: np.ndarray, rate: int, channels: int = 1, extra_chunk: bytes = b"") -> bytes:
    data = pcm.astype("<i2").tobytes()
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * 2, channels * 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += extra_chunk
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_standard_44_byte_wav():
    pcm = np.arange(100, dtype=np.int16)
    rate, out = _parse_wav(_wav(pcm, 24000))
    assert rate == 24000
    assert np.array_equal(out, pcm)


def test_wav_with_list_chunk_before_data():
    # D9 regression: fixed-offset parsing read the LIST chunk as audio.
    pcm = np.arange(100, dtype=np.int16)
    info = b"LIST" + struct.pack("<I", 10) + b"INFOhello\x00"
    rate, out = _parse_wav(_wav(pcm, 22050, extra_chunk=info))
    assert rate == 22050
    assert np.array_equal(out, pcm)


def test_stereo_takes_left_channel():
    interleaved = np.array([1, -1, 2, -2, 3, -3], dtype=np.int16)
    rate, out = _parse_wav(_wav(interleaved, 24000, channels=2))
    assert rate == 24000
    assert np.array_equal(out, np.array([1, 2, 3], dtype=np.int16))


def test_rejects_non_riff_payload():
    with pytest.raises(ValueError):
        _parse_wav(b"MP3\x00 definitely not a wav file....")


def test_rejects_wav_without_data_chunk():
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    raw = b"RIFF" + struct.pack("<I", len(body)) + body
    with pytest.raises(ValueError):
        _parse_wav(raw)
