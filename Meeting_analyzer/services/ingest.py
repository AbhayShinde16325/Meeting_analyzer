"""Recording ingest: store the original audio and its 16 kHz mono derivative.

The uploaded source audio is preserved as the original meeting file. For browser
and other non-canonical formats, we transcode to a canonical 16-bit PCM WAV
before storing/deriving audio used by the existing analyzer pipeline.
"""

from __future__ import annotations

import audioop  # stdlib resampler (Python 3.11; removed only in 3.13+)
import json
import logging
import mimetypes
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from db import lo

logger = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16_000
CANONICAL_SAMPLE_RATE = 48_000
CANONICAL_CHANNELS = 2
SUPPORTED_AUDIO_EXTENSIONS = {
    ".wav", ".webm", ".mp3", ".m4a", ".ogg", ".oga", ".opus", ".mp4", ".aac", ".flac"
}


class IngestError(RuntimeError):
    """Raised when a recording cannot be ingested."""


def _safe_unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to remove temporary file: %s", path, exc_info=True)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise IngestError(f"Required audio tool not found: {cmd[0]}") from exc


def _ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise IngestError(
            "Audio conversion requires ffmpeg on the server. Install ffmpeg and retry "
            "(Ubuntu: sudo apt install ffmpeg; Docker: install ffmpeg in the backend image)."
        )
    return path


def _ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def _probe_audio(source: Path) -> dict[str, Any]:
    """Detect actual audio format/streams; falls back when ffprobe unavailable."""
    ffprobe = _ffprobe_path()
    if ffprobe:
        result = _run([
            ffprobe, "-v", "error", "-show_entries",
            "format=format_name,duration,size:stream=index,codec_type,codec_name,sample_rate,channels",
            "-of", "json", str(source),
        ])
        if result.returncode == 0 and result.stdout.strip():
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise IngestError("Could not parse ffprobe output for uploaded audio.") from exc
            streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "audio"]
            if not streams:
                raise IngestError("Uploaded file does not contain an audio stream.")
            fmt = payload.get("format", {})
            primary = streams[0]
            duration = fmt.get("duration")
            try:
                duration_ms = int(float(duration) * 1000) if duration is not None else None
            except (TypeError, ValueError):
                duration_ms = None
            try:
                sample_rate = int(primary.get("sample_rate") or 0) or None
            except (TypeError, ValueError):
                sample_rate = None
            try:
                channels = int(primary.get("channels") or 0) or None
            except (TypeError, ValueError):
                channels = None
            format_name = str(fmt.get("format_name") or "")
            codec_name = str(primary.get("codec_name") or "")
            return {
                "format_name": format_name,
                "codec_name": codec_name,
                "sample_rate": sample_rate,
                "channels": channels,
                "duration_ms": duration_ms,
            }

    # Fallback: use wave for WAV, else minimal detection by extension/signature.
    try:
        with wave.open(str(source), "rb") as reader:
            return {
                "format_name": "wav",
                "codec_name": f"pcm_s{reader.getsampwidth() * 8}le",
                "sample_rate": reader.getframerate(),
                "channels": reader.getnchannels(),
                "duration_ms": round(reader.getnframes() * 1000 / reader.getframerate()) if reader.getframerate() else None,
            }
    except wave.Error:
        pass

    header = source.read_bytes()[:16]
    ext = source.suffix.lower()
    if header.startswith(bytes.fromhex("1a45dfa3")):
        return {"format_name": "matroska,webm", "codec_name": "", "sample_rate": None, "channels": None, "duration_ms": None}
    if header.startswith(b"OggS"):
        return {"format_name": "ogg", "codec_name": "", "sample_rate": None, "channels": None, "duration_ms": None}
    if header.startswith(b"ID3") or header[:2] in (bytes.fromhex("fffb"), bytes.fromhex("fff3"), bytes.fromhex("fff2")):
        return {"format_name": "mp3", "codec_name": "", "sample_rate": None, "channels": None, "duration_ms": None}
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return {"format_name": "mp4,m4a", "codec_name": "", "sample_rate": None, "channels": None, "duration_ms": None}
    if header.startswith(b"fLaC"):
        return {"format_name": "flac", "codec_name": "", "sample_rate": None, "channels": None, "duration_ms": None}
    if ext in SUPPORTED_AUDIO_EXTENSIONS:
        return {"format_name": ext.lstrip("."), "codec_name": "", "sample_rate": None, "channels": None, "duration_ms": None}
    raise IngestError("Unsupported or unrecognized audio format. Upload WAV, WEBM/Opus, MP3, M4A, or OGG.")


def _is_canonical_wav(source: Path, probe: dict[str, Any] | None = None) -> bool:
    try:
        with wave.open(str(source), "rb") as reader:
            return (
                reader.getsampwidth() == 2
                and reader.getnchannels() in (1, 2)
                and reader.getframerate() > 0
            )
    except wave.Error:
        return False


def _transcode_to_wav(source: Path, target: Path) -> Path:
    """Convert supported audio formats to 16-bit PCM WAV via ffmpeg."""
    ffmpeg = _ffmpeg_path()
    cmd = [
        ffmpeg, "-y", "-v", "error", "-i", str(source),
        "-vn", "-ac", str(CANONICAL_CHANNELS), "-ar", str(CANONICAL_SAMPLE_RATE),
        "-c:a", "pcm_s16le", str(target),
    ]
    result = _run(cmd)
    if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 44:
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip().replace("\n", " ")[:300]
        raise IngestError(
            "Uploaded file could not be converted to WAV. Ensure it is a valid audio recording "
            f"(WAV/WEBM/MP3/M4A/OGG). Details: {detail}"
        )
    # Validate output is readable WAV for downstream pipeline.
    try:
        with wave.open(str(target), "rb") as reader:
            if reader.getsampwidth() != 2 or reader.getnchannels() not in (1, 2):
                raise IngestError("Converted audio is not a valid 16-bit PCM WAV.")
    except wave.Error as exc:
        raise IngestError(f"Converted audio is not a valid WAV file: {exc}") from exc
    return target


def derive_16k_mono(source: Path, target: Path) -> dict[str, Any]:
    """Convert a 16-bit PCM WAV to 16 kHz mono; return audio metadata."""
    with wave.open(str(source), "rb") as reader:
        channels = reader.getnchannels()
        sample_rate = reader.getframerate()
        sample_width = reader.getsampwidth()
        frames = reader.getnframes()
        raw = reader.readframes(frames)

    if sample_width != 2:
        raise IngestError("Only 16-bit PCM WAV recordings are supported.")
    if channels not in (1, 2):
        raise IngestError(f"Unexpected channel count {channels}; expected mono or stereo.")

    if channels == 2:
        raw = audioop.tomono(raw, 2, 0.5, 0.5)
    if sample_rate != TARGET_SAMPLE_RATE:
        raw, _ = audioop.ratecv(raw, 2, 1, sample_rate, TARGET_SAMPLE_RATE, None)

    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(TARGET_SAMPLE_RATE)
        writer.writeframes(raw)

    return {
        "sample_rate": TARGET_SAMPLE_RATE,
        "channels": 1,
        "duration_ms": round(frames * 1000 / sample_rate) if sample_rate else 0,
    }


def _normalized_mime(mime_type: str | None, filename: str | None) -> str:
    mime = (mime_type or "").split(";")[0].strip().lower()
    if mime:
        return mime
    guessed, _ = mimetypes.guess_type(filename or "")
    return (guessed or "application/octet-stream").lower()


def normalize_uploaded_audio(source: Path, *, original_filename: str | None = None, mime_type: str | None = None) -> dict[str, Any]:
    """Return canonical WAV path for an uploaded audio file, converting if needed.

    Caller owns cleanup of returned temp_converted_path (if not None).
    """
    source = Path(source)
    probe = _probe_audio(source)
    canonical = source
    temp_converted = None
    if not _is_canonical_wav(source, probe):
        temp_converted = source.with_name(source.stem + ".canonical.wav")
        canonical = _transcode_to_wav(source, temp_converted)
    # actual props
    with wave.open(str(canonical), "rb") as wf:
        sr, ch, n = wf.getframerate(), wf.getnchannels(), wf.getnframes()
        duration_ms = round(n * 1000 / sr) if sr else 0
    return {
        "canonical_path": canonical,
        "temp_converted_path": temp_converted,
        "probe": probe,
        "duration_ms": duration_ms,
        "sample_rate": sr,
        "channels": ch,
        "mime_type": _normalized_mime(mime_type, original_filename or source.name),
    }


def ingest_recording(
    conn: Connection,
    *,
    user_id: str,
    title: str,
    original_path: Path,
    duration_ms: int,
    sample_rate: int,
    channels: int,
    mime_type: str = "audio/wav",
    original_filename: str | None = None,
) -> str:
    """Create meeting row and store original + derived 16k mono audio as LOs."""
    original_path = Path(original_path)
    if not original_path.is_file():
        raise IngestError("The recording file is missing.")
    if original_path.stat().st_size <= 44:
        raise IngestError("The recording file is empty.")

    display_name = original_filename or original_path.name
    original_mime = _normalized_mime(mime_type, display_name)
    probe = _probe_audio(original_path)

    upload_path = original_path
    converted_path: Path | None = None
    derived_path: Path | None = None
    try:
        if not _is_canonical_wav(original_path, probe):
            converted_path = original_path.with_name(original_path.stem + ".converted.wav")
            upload_path = _transcode_to_wav(original_path, converted_path)

        # Use probed metadata when client metadata is missing/generic.
        effective_duration_ms = duration_ms or int(probe.get("duration_ms") or 0)
        effective_sample_rate = sample_rate or int(probe.get("sample_rate") or 0) or CANONICAL_SAMPLE_RATE
        effective_channels = channels or int(probe.get("channels") or 0) or CANONICAL_CHANNELS

        # For canonical/stored WAV, read actual properties to preserve pipeline correctness.
        stored_mime = original_mime
        try:
            with wave.open(str(upload_path), "rb") as reader:
                effective_sample_rate = reader.getframerate() or effective_sample_rate
                effective_channels = reader.getnchannels() or effective_channels
                wav_frames = reader.getnframes()
                wav_duration_ms = round(wav_frames * 1000 / reader.getframerate()) if reader.getframerate() else 0
                if wav_duration_ms:
                    effective_duration_ms = wav_duration_ms
                stored_mime = "audio/wav"
        except wave.Error:
            pass

        meeting_id = conn.execute(
            text(
                "INSERT INTO meeting_analyzer.meetings (user_id, title, status, duration_ms) VALUES "
                "(:user_id, :title, 'recorded', :duration_ms) RETURNING id"
            ),
            {"user_id": user_id, "title": title, "duration_ms": effective_duration_ms},
        ).scalar_one()

        original_oid, original_sha, original_size = lo.create_lo_from_file(conn, upload_path)
        conn.execute(
            text(
                "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, "
                "size_bytes, mime_type, duration_ms, sample_rate, channels, "
                "original_filename) VALUES (gen_random_uuid(), :meeting_id, "
                "'original_audio_48k', :oid, :sha, :size, :mime, :duration_ms, "
                ":sample_rate, :channels, :filename)"
            ),
            {
                "meeting_id": meeting_id,
                "oid": original_oid,
                "sha": original_sha,
                "size": original_size,
                "mime": stored_mime,
                "duration_ms": effective_duration_ms,
                "sample_rate": effective_sample_rate,
                "channels": effective_channels,
                "filename": display_name,
            },
        )

        derived_path = upload_path.with_name(upload_path.stem + "-16k-mono.wav")
        derived_meta = derive_16k_mono(upload_path, derived_path)
        derived_oid, derived_sha, derived_size = lo.create_lo_from_file(conn, derived_path)
        conn.execute(
            text(
                "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, "
                "size_bytes, mime_type, duration_ms, sample_rate, channels, "
                "original_filename) VALUES (gen_random_uuid(), :meeting_id, "
                "'derived_audio_16k', :oid, :sha, :size, 'audio/wav', :duration_ms, "
                "16000, 1, :filename)"
            ),
            {
                "meeting_id": meeting_id,
                "oid": derived_oid,
                "sha": derived_sha,
                "size": derived_size,
                "duration_ms": derived_meta["duration_ms"] or effective_duration_ms,
                "filename": derived_path.name,
            },
        )

        conn.execute(
            text(
                "INSERT INTO meeting_analyzer.processing_jobs (id, meeting_id, job_type, status, message) "
                "VALUES (gen_random_uuid(), :meeting_id, 'transcription', 'queued', "
                "'Queued for transcription')"
            ),
            {"meeting_id": meeting_id},
        )
        conn.commit()
        logger.info(
            "Ingested meeting %s (%s -> %s, %d Hz, %d ch).",
            meeting_id, probe.get("format_name"), upload_path.suffix.lower(), effective_sample_rate, effective_channels,
        )
        return str(meeting_id)
    finally:
        _safe_unlink(derived_path)
        if converted_path is not None and converted_path != original_path:
            _safe_unlink(converted_path)


def _store_audio_file(conn, meeting_id: str, path: Path, *, kind: str, mime_type: str, filename: str, duration_ms: int | None = None, sample_rate: int | None = None, channels: int | None = None) -> None:
    oid, sha256, size = lo.create_lo_from_file(conn, path)
    conn.execute(text(
        "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, size_bytes, mime_type, "
        "duration_ms, sample_rate, channels, original_filename) VALUES (gen_random_uuid(), :meeting_id, :kind, "
        ":oid, :sha, :size, :mime, :duration_ms, :sample_rate, :channels, :filename)"
    ), {
        "meeting_id": meeting_id, "kind": kind, "oid": oid, "sha": sha256, "size": size,
        "mime": mime_type, "duration_ms": duration_ms, "sample_rate": sample_rate, "channels": channels,
        "filename": filename,
    })


def ingest_multi_source_recording(
    conn: Connection,
    *,
    user_id: str,
    title: str,
    sources: dict[str, Path],
    source_meta: dict[str, dict[str, Any]] | None = None,
    duration_ms: int = 0,
    notes: str | None = None,
) -> str:
    """Ingest synchronized multi-source capture. Stores original sources + canonical/mixed/derived audio."""
    if not sources:
        raise IngestError("At least one audio source is required.")
    source_meta = source_meta or {}
    normalized: dict[str, Path] = {}
    converted: list[Path] = []
    derived: Path | None = None
    mixed_path: Path | None = None
    try:
        # Normalize each provided source to canonical WAV and store original source file.
        for name, path in sources.items():
            p = Path(path)
            if not p.is_file() or p.stat().st_size <= 44:
                raise IngestError(f"Source '{name}' is missing or empty.")
            probe = _probe_audio(p)
            out = p
            if not _is_canonical_wav(p, probe):
                out = p.with_name(p.stem + f".{name}.canonical.wav")
                _transcode_to_wav(p, out)
                converted.append(out)
            normalized[name] = out

        # Create mixed master (prefer system+mic mix, else any available source).
        mix_sources = [normalized[k] for k in ("microphone", "system", "browser") if k in normalized]
        if not mix_sources:
            mix_sources = list(normalized.values())
        mixed_path = mix_sources[0].with_name("mixed-master.wav")
        if len(mix_sources) == 1:
            # copy as canonical mixed master
            shutil.copyfile(mix_sources[0], mixed_path)
        else:
            _mix_wav_files(mix_sources, mixed_path)

        with wave.open(str(mixed_path), "rb") as wf:
            mix_rate, mix_ch, mix_frames = wf.getframerate(), wf.getnchannels(), wf.getnframes()
            mix_duration_ms = round(mix_frames * 1000 / mix_rate) if mix_rate else duration_ms

        meeting_id = conn.execute(text(
            "INSERT INTO meeting_analyzer.meetings (user_id, title, status, duration_ms, notes, capture_mode, capture_manifest) "
            "VALUES (:user_id, :title, 'recorded', :duration_ms, :notes, :capture_mode, CAST(:manifest AS jsonb)) RETURNING id"
        ), {
            "user_id": user_id, "title": title, "duration_ms": mix_duration_ms or duration_ms, "notes": notes,
            "capture_mode": "multi_source", "manifest": json.dumps({"sources": list(normalized.keys()), "source_meta": source_meta}),
        }).scalar_one()

        # Store original uploaded sources (preserve as uploaded/transcoded canonical source for reliability).
        kind_map = {"microphone": "source_microphone", "system": "source_system", "browser": "source_browser"}
        for name, out in normalized.items():
            meta = source_meta.get(name, {})
            _store_audio_file(conn, str(meeting_id), out, kind=kind_map.get(name, "mixed_master"),
                              mime_type="audio/wav", filename=meta.get("original_filename") or f"{name}.wav",
                              duration_ms=meta.get("duration_ms"), sample_rate=meta.get("sample_rate"), channels=meta.get("channels"))

        # Store mixed master as original_audio_48k for compatibility with existing pipeline/UI.
        _store_audio_file(conn, str(meeting_id), mixed_path, kind="original_audio_48k", mime_type="audio/wav",
                          filename="mixed-master.wav", duration_ms=mix_duration_ms, sample_rate=mix_rate, channels=mix_ch)
        _store_audio_file(conn, str(meeting_id), mixed_path, kind="mixed_master", mime_type="audio/wav",
                          filename="mixed-master.wav", duration_ms=mix_duration_ms, sample_rate=mix_rate, channels=mix_ch)

        derived = mixed_path.with_name(mixed_path.stem + "-16k-mono.wav")
        derive_16k_mono(mixed_path, derived)
        with wave.open(str(derived), "rb") as wf:
            d_rate, d_ch, d_frames = wf.getframerate(), wf.getnchannels(), wf.getnframes()
            d_ms = round(d_frames * 1000 / d_rate) if d_rate else mix_duration_ms
        _store_audio_file(conn, str(meeting_id), derived, kind="derived_audio_16k", mime_type="audio/wav",
                          filename=derived.name, duration_ms=d_ms, sample_rate=d_rate, channels=d_ch)

        conn.execute(text(
            "INSERT INTO meeting_analyzer.processing_jobs (id, meeting_id, job_type, status, message) "
            "VALUES (gen_random_uuid(), :meeting_id, 'transcription', 'queued', 'Queued for transcription')"
        ), {"meeting_id": meeting_id})
        conn.commit()
        return str(meeting_id)
    finally:
        _safe_unlink(derived)
        _safe_unlink(mixed_path)
        for p in converted:
            _safe_unlink(p)


def _mix_wav_files(paths: list[Path], target: Path) -> None:
    arrays = []
    sr = None
    max_len = 0
    for p in paths:
        with wave.open(str(p), "rb") as wf:
            if wf.getsampwidth() != 2:
                raise IngestError("Only 16-bit PCM WAV sources can be mixed.")
            rate = wf.getframerate(); ch = wf.getnchannels(); n = wf.getnframes()
            raw = wf.readframes(n)
        if sr is None:
            sr = rate
        if rate != sr:
            # resample to first sample rate
            raw, _ = audioop.ratecv(raw, 2, ch, rate, sr, None)
            rate = sr
        arr = np.frombuffer(raw, dtype='<i2').astype(np.float32)
        if ch > 1:
            arr = arr.reshape(-1, ch).mean(axis=1)
        arrays.append(arr)
        max_len = max(max_len, len(arr))
    if sr is None:
        raise IngestError("No audio sources to mix.")
    mix = np.zeros(max_len, dtype=np.float32)
    for arr in arrays:
        mix[:len(arr)] += arr
    mix = np.clip(mix, -32768.0, 32767.0).astype('<i2')
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), 'wb') as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr); wf.writeframes(mix.tobytes())
