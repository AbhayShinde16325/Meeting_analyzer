"""Microphone discovery and capture.

SoundCard is imported lazily so the desktop application can still start (and
show a helpful message) on a machine where the optional audio dependency is
missing.  Capture always runs on a background thread; callers receive numpy
frames through a callback and must not touch UI objects from that thread.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

AudioCallback = Callable[[np.ndarray], None]
ErrorCallback = Callable[[str], None]


class AudioDeviceError(RuntimeError):
    """Raised when device discovery or capture cannot be initialised."""


@dataclass(frozen=True)
class AudioDevice:
    id: str
    name: str
    channels: int
    is_default: bool = False
    is_loopback: bool = False

    @property
    def label(self) -> str:
        suffix = " (default)" if self.is_default else ""
        return f"{self.name}{suffix}"


def _soundcard():
    try:
        import soundcard as sc  # type: ignore import-not-found
    except Exception as exc:  # pragma: no cover - depends on host audio stack
        raise AudioDeviceError(
            "The 'soundcard' package or Windows audio backend is unavailable. "
            "Install the project requirements and verify your audio devices."
        ) from exc
    return sc


def _device_channels(device) -> int:
    channels = getattr(device, "channels", None)
    if isinstance(channels, int) and channels > 0:
        return channels
    return 1


def list_microphones() -> list[AudioDevice]:
    """Return available (non-loopback) microphone input devices."""

    sc = _soundcard()
    try:
        default = sc.default_microphone()
    except Exception:  # no device or unavailable audio service
        default = None
    default_id = getattr(default, "id", None) if default is not None else None

    devices: list[AudioDevice] = []
    for mic in sc.all_microphones(include_loopback=False):
        device_id = str(getattr(mic, "id", getattr(mic, "name", "")))
        name = str(getattr(mic, "name", device_id))
        devices.append(
            AudioDevice(
                id=device_id,
                name=name,
                channels=max(1, _device_channels(mic)),
                is_default=(device_id == default_id),
            )
        )
    if not devices:
        raise AudioDeviceError("No microphone input devices were found.")
    return devices


class MicrophoneCapture:
    """Continuously capture a microphone into a caller-provided callback."""

    def __init__(
        self,
        *,
        device_id: str | None = None,
        sample_rate: int = 48_000,
        channels: int = 1,
        block_frames: int = 1_024,
    ) -> None:
        self.device_id = device_id
        self.sample_rate = int(sample_rate)
        self.channels = max(1, int(channels))
        self.block_frames = max(128, int(block_frames))

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._recorder_cm = None
        self._recorder = None
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        on_audio: AudioCallback,
        on_error: ErrorCallback | None = None,
    ) -> None:
        if self.is_running:
            raise AudioDeviceError("Microphone capture is already running.")

        sc = _soundcard()
        try:
            mic = (
                sc.get_microphone(id=self.device_id, include_loopback=False)
                if self.device_id
                else sc.default_microphone()
            )
        except Exception as exc:
            raise AudioDeviceError(
                "The selected microphone is unavailable. Choose another device."
            ) from exc
        if mic is None:
            raise AudioDeviceError("No microphone is available for recording.")

        # Open the stream synchronously so device/permission errors are visible
        # before the UI switches into a "recording" state.
        try:
            self._recorder_cm = mic.recorder(
                samplerate=self.sample_rate,
                channels=self.channels,
                blocksize=self.block_frames,
            )
            self._recorder = self._recorder_cm.__enter__()
        except Exception as exc:
            self._recorder_cm = None
            self._recorder = None
            raise AudioDeviceError(
                "Could not open the microphone. It may be in use or muted."
            ) from exc

        self._stop_event.clear()
        self._last_error = None
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(on_audio, on_error),
            name="microphone-capture",
            daemon=True,
        )
        self._thread.start()
        logger.info("Microphone capture started at %s Hz.", self.sample_rate)

    def _capture_loop(self, on_audio: AudioCallback, on_error: ErrorCallback | None) -> None:
        try:
            while not self._stop_event.is_set():
                data = self._recorder.record(numframes=self.block_frames)
                if data is not None and getattr(data, "size", 0) > 0:
                    on_audio(np.asarray(data, dtype=np.float32))
        except Exception as exc:  # pragma: no cover - hardware dependent
            self._last_error = f"Microphone capture failed: {exc}"
            logger.error("Microphone capture failed: %s", exc)
            if on_error is not None:
                try:
                    on_error(self._last_error)
                except Exception:
                    pass
        finally:
            self._close_recorder()

    def _close_recorder(self) -> None:
        cm, self._recorder_cm = self._recorder_cm, None
        self._recorder = None
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                logger.debug("Error while closing microphone stream.", exc_info=True)

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10.0)
            if thread.is_alive():
                # The capture thread still owns the stream; it closes the
                # recorder itself when it eventually exits.
                logger.warning("Microphone capture thread did not stop in time.")
                return
        self._thread = None
        self._close_recorder()
        logger.info("Microphone capture stopped.")
