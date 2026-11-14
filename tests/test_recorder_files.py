"""WAV writing, mixing, filename, and channel-conversion tests."""

from __future__ import annotations

import wave

import numpy as np

from audio.recorder import (
    _WavWriter,
    _convert_channels,
    _mix_tracks,
    _unique_path,
)


def _write_track(path, channels, frames, value):
    writer = _WavWriter(path, 48_000, channels)
    writer.write(np.full((frames, channels), value, dtype=np.float32))
    writer.close()
    return writer


def test_wav_writer_and_stereo_mix(tmp_path):
    mic_path = tmp_path / "mic.wav"
    system_path = tmp_path / "system.wav"
    final_path = tmp_path / "final.wav"

    mic_writer = _write_track(mic_path, 1, 4_800, 0.2)
    system_writer = _write_track(system_path, 2, 9_600, 0.1)

    _mix_tracks(final_path, mic_path, system_path, sample_rate=48_000, channels=2)

    assert mic_writer.frames_written == 4_800
    assert system_writer.frames_written == 9_600
    with wave.open(str(final_path), "rb") as final:
        assert final.getnchannels() == 2
        assert final.getframerate() == 48_000
        assert final.getsampwidth() == 2
        assert final.getnframes() == 9_600


def test_mix_with_only_one_track(tmp_path):
    mic_path = tmp_path / "mic.wav"
    final_path = tmp_path / "final.wav"
    _write_track(mic_path, 1, 1_000, 0.15)

    _mix_tracks(final_path, mic_path, None, sample_rate=48_000, channels=2)

    with wave.open(str(final_path), "rb") as final:
        assert final.getnchannels() == 2
        assert final.getnframes() == 1_000


def test_mix_rejects_missing_tracks(tmp_path):
    import pytest

    from audio.recorder import RecordingError

    with pytest.raises(RecordingError):
        _mix_tracks(
            tmp_path / "final.wav",
            tmp_path / "missing-mic.wav",
            tmp_path / "missing-system.wav",
            sample_rate=48_000,
            channels=2,
        )


def test_channel_conversions():
    mono = np.ones((10, 1), dtype=np.float32)
    stereo = _convert_channels(mono, 2)
    assert stereo.shape == (10, 2)
    assert np.allclose(stereo[:, 0], stereo[:, 1])

    surround = np.arange(20, dtype=np.float32).reshape(5, 4)
    assert _convert_channels(surround, 2).shape == (5, 2)
    assert _convert_channels(stereo, 1).shape == (10, 1)


def test_unique_path_does_not_overwrite(tmp_path):
    original = tmp_path / "meeting.wav"
    original.write_bytes(b"existing")
    chosen = _unique_path(original)
    assert chosen != original
    assert chosen.name == "meeting-1.wav"
    assert original.read_bytes() == b"existing"

