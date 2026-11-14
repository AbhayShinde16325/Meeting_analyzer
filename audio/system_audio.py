"""Windows system-audio (WASAPI loopback) discovery and capture.

SoundCard exposes loopback endpoints as microphones that mirror a playback
device.  The user selects the speaker/headset that Google Meet plays through;
we then capture that endpoint's loopback stream on a background thread.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import numpy as np

from audio.microphone import (
    AudioCallback,
    AudioDevice,
    AudioDeviceError,
    ErrorCallback,
    _soundcard,
)

logger = logging.getLogger(__name__)


def list_loopback_devices() -> list[AudioDevice]:
    """Return playback devices that can be captured through loopback."""

    sc = _soundcard()
    try:
        default_speaker = sc.default_speaker()
    except Exception:
        default_speaker = None
    default_name = str(getattr(default_speaker, "name", "")) if default_speaker else ""

    devices: list[AudioDevice] = []
    for speaker in sc.all_speakers():
        name = str(getattr(speaker, "name", ""))
        if not name:
            continue
        channels = int(getattr(speaker, "channels", 2) or 2)
        devices.append(
            AudioDevice(
                id=name,
                name=name,
                channels=max(1, channels),
                is_default=(name == default_name),
                is_loopback=True,
            )
        )
    if not devices:
        raise AudioDeviceError(
            "No system-audio playback devices were found. "
            "Connect a speaker or headset and refresh the device list."
        )
    return devices


class SystemAudioCapture:
    """Continuously capture the selected playback device via loopback."""

    def __init__(
        self,
        *,
        device_id: str | None = None,
        sample_rate: int = 48_000,
        channels: int = 2,
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
            raise AudioDeviceError("System-audio capture is already running.")

        sc = _soundcard()
        try:
            if self.device_id:
                loopback = sc.get_microphone(
                    id=self.device_id, include_loopback=True
                )
            else:
                speaker = sc.default_speaker()
                if speaker is None:
                    raise AudioDeviceError("No default playback device was found.")
                loopback = sc.get_microphone(
                    id=str(speaker.name), include_loopback=True
                )
        except AudioDeviceError:
            raise
        except Exception as exc:
            raise AudioDeviceError(
                "The selected playback device cannot be captured through "
                "loopback. Choose another speaker or headset."
            ) from exc
        if loopback is None:
            raise AudioDeviceError("System-audio loopback is unavailable.")

        try:
            self._recorder_cm = loopback.recorder(
                samplerate=self.sample_rate,
                channels=self.channels,
                blocksize=self.block_frames,
            )
            self._recorder = self._recorder_cm.__enter__()
        except Exception as exc:
            self._recorder_cm = None
            self._recorder = None
            raise AudioDeviceError(
                "Could not open system-audio loopback. The playback device may "
                "be in use, disabled, or unsupported."
            ) from exc

        self._stop_event.clear()
        self._last_error = None
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(on_audio, on_error),
            name="system-audio-capture",
            daemon=True,
        )
        self._thread.start()
        logger.info("System-audio capture started at %s Hz.", self.sample_rate)

    def _capture_loop(self, on_audio: AudioCallback, on_error: ErrorCallback | None) -> None:
        try:
            while not self._stop_event.is_set():
                data = self._recorder.record(numframes=self.block_frames)
                if data is not None and getattr(data, "size", 0) > 0:
                    on_audio(np.asarray(data, dtype=np.float32))
        except Exception as exc:  # pragma: no cover - hardware dependent
            self._last_error = f"System-audio capture failed: {exc}"
            logger.error("System-audio capture failed: %s", exc)
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
                logger.debug("Error while closing loopback stream.", exc_info=True)

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10.0)
            if thread.is_alive():
                logger.warning("System-audio capture thread did not stop in time.")
                return
        self._thread = None
        self._close_recorder()
        logger.info("System-audio capture stopped.")
