"""Recording orchestration and safe local WAV file management.

The microphone and system-audio tracks are captured separately, written to
temporary WAV files, and then mixed into one final WAV for playback,
transcription, and storage metadata.  A recording is only reported as
successful after every stream is closed and the final file has been verified.

Files are written into the meeting's library folder using fixed names
(``audio.wav``, ``mic.wav``, ``system.wav``); ``meeting.json`` stores the
relative file names.  Deleting a meeting removes the whole folder.
"""

from __future__ import annotations

import logging
import os
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from audio.microphone import AudioDeviceError, MicrophoneCapture
from audio.system_audio import SystemAudioCapture
from config import Settings

logger = logging.getLogger(__name__)

_TARGET_CHANNELS = 2
_SAMPLE_WIDTH = 2  # 16-bit PCM

# Canonical file names inside a meeting folder.
AUDIO_FILENAME = "audio.wav"
MIC_FILENAME = "mic.wav"
SYSTEM_FILENAME = "system.wav"


class RecordingError(RuntimeError):
    """Raised when a recording cannot be started or finalised safely."""


@dataclass(frozen=True)
class RecordingResult:
    meeting_id: str
    relative_path: str
    absolute_path: Path
    duration_seconds: float
    size_bytes: int
    sample_rate: int
    channels: int
    format: str = "wav"
    microphone_relative_path: str | None = None
    system_relative_path: str | None = None


class _WavWriter:
    """Incremental 16-bit PCM WAV writer used by a single capture thread."""

    def __init__(self, path: Path, sample_rate: int, channels: int) -> None:
        self.path = path
        self.sample_rate = int(sample_rate)
        self.channels = max(1, int(channels))
        self.frames_written = 0
        self._wave = wave.open(str(path), "wb")
        self._wave.setnchannels(self.channels)
        self._wave.setsampwidth(_SAMPLE_WIDTH)
        self._wave.setframerate(self.sample_rate)

    def write(self, data: np.ndarray) -> None:
        frames = np.asarray(data, dtype=np.float32)
        if frames.ndim == 1:
            frames = frames.reshape(-1, 1)
        if frames.ndim != 2 or frames.shape[0] == 0:
            return
        if frames.shape[1] != self.channels:
            frames = _convert_channels(frames, self.channels)
        pcm = (np.clip(frames, -1.0, 1.0) * 32767.0).astype("<i2")
        self._wave.writeframes(pcm.tobytes())
        self.frames_written += pcm.shape[0]

    def close(self) -> None:
        if self._wave is not None:
            self._wave.close()
            self._wave = None

    @property
    def duration_seconds(self) -> float:
        return self.frames_written / float(self.sample_rate) if self.sample_rate else 0.0


class MeetingRecorder:
    """Capture microphone + system audio and produce one finalised WAV file."""

    def __init__(
        self,
        settings: Settings,
        *,
        microphone_id: str | None = None,
        system_device_id: str | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.microphone_id = microphone_id
        self.system_device_id = system_device_id
        self.on_error = on_error

        self._meeting_id: str | None = None
        self._meeting_dir: Path | None = None
        self._microphone: MicrophoneCapture | None = None
        self._system_audio: SystemAudioCapture | None = None
        self._mic_writer: _WavWriter | None = None
        self._system_writer: _WavWriter | None = None
        self._started_monotonic: float | None = None
        self._started_at: float | None = None
        self._track_errors: list[str] = []

        self._final_path: Path | None = None
        self._mic_path: Path | None = None
        self._system_path: Path | None = None
        self._result: RecordingResult | None = None

    # -- state ---------------------------------------------------------

    @property
    def meeting_id(self) -> str | None:
        return self._meeting_id

    @property
    def is_recording(self) -> bool:
        return self._started_monotonic is not None

    @property
    def elapsed_seconds(self) -> float:
        if self._started_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_monotonic)

    @property
    def track_errors(self) -> list[str]:
        return list(self._track_errors)

    # -- start ---------------------------------------------------------

    def start(self, meeting_id: str, meeting_dir: Path | None = None) -> None:
        if self.is_recording:
            raise RecordingError("A recording is already in progress.")
        if not meeting_id:
            raise RecordingError("A meeting ID is required to start recording.")

        # Audio files live inside the meeting's library folder; when no folder
        # is supplied (tests, standalone use) fall back to the recordings dir.
        if meeting_dir is not None:
            target_dir = Path(meeting_dir)
        else:
            target_dir = self.settings.recordings_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        self._meeting_id = meeting_id
        self._meeting_dir = target_dir
        self._result = None
        self._track_errors.clear()

        self._final_path = _unique_path(target_dir / AUDIO_FILENAME)
        self._mic_path = _unique_path(target_dir / MIC_FILENAME)
        self._system_path = _unique_path(target_dir / SYSTEM_FILENAME)

        try:
            self._mic_writer = _WavWriter(
                self._mic_path,
                self.settings.audio_sample_rate,
                self.settings.microphone_channels,
            )
            self._system_writer = _WavWriter(
                self._system_path,
                self.settings.audio_sample_rate,
                self.settings.system_channels,
            )
        except Exception as exc:
            self._cleanup_files()
            raise RecordingError(f"Could not create recording files: {exc}") from exc

        self._microphone = MicrophoneCapture(
            device_id=self.microphone_id,
            sample_rate=self.settings.audio_sample_rate,
            channels=self.settings.microphone_channels,
            block_frames=self.settings.audio_block_frames,
        )
        self._system_audio = SystemAudioCapture(
            device_id=self.system_device_id,
            sample_rate=self.settings.audio_sample_rate,
            channels=self.settings.system_channels,
            block_frames=self.settings.audio_block_frames,
        )

        try:
            self._microphone.start(self._write_microphone, self._report_error)
        except Exception as exc:
            self._teardown_streams()
            self._cleanup_files()
            raise RecordingError(str(exc)) from exc

        try:
            self._system_audio.start(self._write_system_audio, self._report_error)
        except Exception as exc:
            self._teardown_streams()
            self._cleanup_files()
            raise RecordingError(str(exc)) from exc

        self._started_monotonic = time.monotonic()
        self._started_at = time.time()
        logger.info("Recording started for meeting %s.", meeting_id)

    # -- capture callbacks --------------------------------------------

    def _write_microphone(self, data: np.ndarray) -> None:
        writer = self._mic_writer
        if writer is not None:
            writer.write(data)

    def _write_system_audio(self, data: np.ndarray) -> None:
        writer = self._system_writer
        if writer is not None:
            writer.write(data)

    def _report_error(self, message: str) -> None:
        if message not in self._track_errors:
            self._track_errors.append(message)
        logger.error("Recording track error: %s", message)
        if self.on_error is not None:
            try:
                self.on_error(message)
            except Exception:
                pass

    # -- stop ----------------------------------------------------------

    def stop(self) -> RecordingResult:
        if not self.is_recording or self._meeting_id is None:
            raise RecordingError("No recording is in progress.")

        meeting_id = self._meeting_id
        elapsed = self.elapsed_seconds
        self._started_monotonic = None

        self._teardown_streams()
        self._close_writers()

        mic_frames = self._mic_writer.frames_written if self._mic_writer else 0
        system_frames = self._system_writer.frames_written if self._system_writer else 0
        if mic_frames == 0 and system_frames == 0:
            self._cleanup_files()
            raise RecordingError(
                "No audio was captured. Check the selected microphone and "
                "system-audio devices, then try again."
            )

        assert self._final_path is not None
        try:
            _mix_tracks(
                self._final_path,
                self._mic_path,
                self._system_path,
                sample_rate=self.settings.audio_sample_rate,
                channels=_TARGET_CHANNELS,
            )
        except Exception as exc:
            logger.error("Failed to finalise recording for meeting %s.", meeting_id)
            raise RecordingError(f"Could not finalise the recording: {exc}") from exc

        try:
            size_bytes = self._final_path.stat().st_size
        except OSError as exc:
            raise RecordingError("The final recording file could not be verified.") from exc
        if size_bytes <= 44:  # empty WAV header
            self._cleanup_files()
            raise RecordingError("The final recording is empty or incomplete.")

        duration = max(mic_frames, system_frames) / float(
            self.settings.audio_sample_rate
        )
        if duration <= 0:
            duration = elapsed

        self._result = RecordingResult(
            meeting_id=meeting_id,
            relative_path=self._final_path.name,
            absolute_path=self._final_path,
            duration_seconds=duration,
            size_bytes=size_bytes,
            sample_rate=self.settings.audio_sample_rate,
            channels=_TARGET_CHANNELS,
            microphone_relative_path=self._mic_path.name if self._mic_path else None,
            system_relative_path=self._system_path.name if self._system_path else None,
        )
        logger.info(
            "Recording stopped for meeting %s (%.1f seconds, %s bytes).",
            meeting_id,
            duration,
            size_bytes,
        )
        return self._result

    def cancel(self) -> None:
        """Abort a recording and remove any partial files."""

        self._started_monotonic = None
        self._teardown_streams()
        self._close_writers()
        self._cleanup_files()
        self._meeting_id = None
        self._result = None
        logger.info("Recording cancelled.")

    # -- cleanup -------------------------------------------------------

    def _teardown_streams(self) -> None:
        for capture in (self._microphone, self._system_audio):
            if capture is None:
                continue
            try:
                capture.stop()
            except Exception:
                logger.debug("Error stopping audio capture.", exc_info=True)
        self._microphone = None
        self._system_audio = None

    def _close_writers(self) -> None:
        for writer in (self._mic_writer, self._system_writer):
            if writer is None:
                continue
            try:
                writer.close()
            except Exception:
                logger.debug("Error closing WAV writer.", exc_info=True)

    def _cleanup_files(self) -> None:
        # Close open WAV writers before unlinking, otherwise Windows keeps the
        # file locked and the partial recording cannot be removed.
        self._close_writers()
        for path in (self._final_path, self._mic_path, self._system_path):
            if path is None:
                continue
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                logger.debug("Could not remove partial recording file %s", path)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _unique_path(path: Path) -> Path:
    """Return *path* or a numbered alternative without overwriting files."""

    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RecordingError("Could not choose a unique recording filename.")


def _convert_channels(frames: np.ndarray, channels: int) -> np.ndarray:
    if channels == 1:
        return frames.mean(axis=1, keepdims=True)
    if frames.shape[1] == 1:
        return np.repeat(frames, channels, axis=1)
    if frames.shape[1] > channels:
        return frames[:, :channels]
    missing = channels - frames.shape[1]
    return np.pad(frames, ((0, 0), (0, missing)), mode="constant")


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != _SAMPLE_WIDTH:
            raise RecordingError("Unexpected audio sample width.")
        channels = source.getnchannels()
        sample_rate = source.getframerate()
        raw = source.readframes(source.getnframes())
    if not raw:
        return np.zeros((0, channels), dtype=np.float32), sample_rate
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    return pcm.reshape(-1, channels), sample_rate


def _mix_tracks(
    final_path: Path,
    mic_path: Path | None,
    system_path: Path | None,
    *,
    sample_rate: int,
    channels: int,
) -> None:
    tracks: list[np.ndarray] = []
    for path in (mic_path, system_path):
        if path is None or not path.exists():
            continue
        frames, track_rate = _read_wav(path)
        if track_rate != sample_rate:
            raise RecordingError("Audio tracks use mismatched sample rates.")
        if frames.size:
            tracks.append(_convert_channels(frames, channels))
    if not tracks:
        raise RecordingError("No audio tracks were available to mix.")

    length = max(track.shape[0] for track in tracks)
    mixed = np.zeros((length, channels), dtype=np.float32)
    for track in tracks:
        mixed[: track.shape[0]] += track
    np.clip(mixed, -1.0, 1.0, out=mixed)

    temp_path = final_path.with_name(f"{final_path.stem}.tmp{final_path.suffix}")
    try:
        with wave.open(str(temp_path), "wb") as destination:
            destination.setnchannels(channels)
            destination.setsampwidth(_SAMPLE_WIDTH)
            destination.setframerate(sample_rate)
            destination.writeframes(
                (mixed * 32767.0).astype("<i2").tobytes()
            )
        os.replace(temp_path, final_path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

