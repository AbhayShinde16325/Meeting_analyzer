"""Recording ingest: convert uploaded audio to canonical Opus and store only Opus.

New recordings are converted immediately to Ogg Opus (32 kbps, mono, 16 kHz,
VBR, voip application), validated, and stored as a single ``audio_opus`` Large
Object. Temporary source/converted/decoded WAV files are deleted after ingest.

Legacy WAV artifacts (``original_audio_48k``, ``derived_audio_16k``) remain
readable for existing meetings but are not created for new ingests.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.engine import Connection

from db import lo

logger = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16_000
TARGET_CHANNELS = 1
TARGET_BITRATE_KBPS = 32
TARGET_CODEC = "opus"
TARGET_CONTAINER = "ogg"
TARGET_MIME = "audio/ogg"
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
            "Audio conversion requires ffmpeg on the server. Install ffmpeg and retry."
        )
    return path


def _ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def _probe_audio(source: Path) -> dict[str, Any]:
    """Detect audio format/streams via ffprobe, with lightweight fallbacks."""
    ffprobe = _ffprobe_path()
    if ffprobe:
        result = _run([
            ffprobe, "-v", "error", "-show_entries",
            "format=format_name,duration,size,bit_rate:stream=index,codec_type,codec_name,sample_rate,channels",
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
            bit_rate = fmt.get("bit_rate")
            try:
                bit_rate_bps = int(float(bit_rate)) if bit_rate is not None else None
            except (TypeError, ValueError):
                bit_rate_bps = None
            return {
                "format_name": str(fmt.get("format_name") or ""),
                "codec_name": str(primary.get("codec_name") or ""),
                "sample_rate": sample_rate,
                "channels": channels,
                "duration_ms": duration_ms,
                "bit_rate_bps": bit_rate_bps,
            }

    # Fallback for WAV when ffprobe unavailable.
    try:
        with wave.open(str(source), "rb") as reader:
            return {
                "format_name": "wav",
                "codec_name": f"pcm_s{reader.getsampwidth() * 8}le",
                "sample_rate": reader.getframerate(),
                "channels": reader.getnchannels(),
                "duration_ms": round(reader.getnframes() * 1000 / reader.getframerate()) if reader.getframerate() else None,
                "bit_rate_bps": None,
            }
    except wave.Error:
        pass

    header = source.read_bytes()[:16]
    ext = source.suffix.lower()
    if header.startswith(bytes.fromhex("1a45dfa3")):
        return {"format_name": "matroska,webm", "codec_name": "", "sample_rate": None, "channels": None, "duration_ms": None, "bit_rate_bps": None}
    if header.startswith(b"OggS"):
        return {"format_name": "ogg", "codec_name": "", "sample_rate": None, "channels": None, "duration_ms": None, "bit_rate_bps": None}
    if header.startswith(b"ID3") or header[:2] in (bytes.fromhex("fffb"), bytes.fromhex("fff3"), bytes.fromhex("fff2")):
        return {"format_name": "mp3", "codec_name": "", "sample_rate": None, "channels": None, "duration_ms": None, "bit_rate_bps": None}
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return {"format_name": "mp4,m4a", "codec_name": "", "sample_rate": None, "channels": None, "duration_ms": None, "bit_rate_bps": None}
    if header.startswith(b"fLaC"):
        return {"format_name": "flac", "codec_name": "", "sample_rate": None, "channels": None, "duration_ms": None, "bit_rate_bps": None}
    if ext in SUPPORTED_AUDIO_EXTENSIONS:
        return {"format_name": ext.lstrip("."), "codec_name": "", "sample_rate": None, "channels": None, "duration_ms": None, "bit_rate_bps": None}
    raise IngestError("Unsupported or unrecognized audio format. Upload WAV, WEBM/Opus, MP3, M4A, or OGG.")


def _normalized_mime(mime_type: str | None, filename: str | None) -> str:
    mime = (mime_type or "").split(";")[0].strip().lower()
    if mime:
        return mime
    guessed, _ = mimetypes.guess_type(filename or "")
    return (guessed or "application/octet-stream").lower()


def convert_to_canonical_opus(source: Path, target: Path) -> dict[str, Any]:
    """Convert any supported audio to canonical Ogg Opus and validate it."""
    ffmpeg = _ffmpeg_path()
    cmd = [
        ffmpeg, "-y", "-v", "error", "-i", str(source),
        "-vn", "-ac", str(TARGET_CHANNELS), "-ar", str(TARGET_SAMPLE_RATE),
        "-c:a", "libopus", "-b:a", f"{TARGET_BITRATE_KBPS}k",
        "-vbr", "on", "-application", "voip",
        str(target),
    ]
    result = _run(cmd)
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip().replace("\n", " ")[:300]
        raise IngestError(f"Uploaded audio could not be converted to Opus. Details: {detail}")
    return validate_canonical_opus(target)


def validate_canonical_opus(path: Path, *, expected_duration_ms: int | None = None) -> dict[str, Any]:
    """Validate an Opus artifact is non-empty, decodable, and canonical."""
    if not path.is_file() or path.stat().st_size <= 0:
        raise IngestError("Converted Opus file is empty.")
    probe = _probe_audio(path)
    codec = str(probe.get("codec_name") or "").lower()
    channels = int(probe.get("channels") or 0)
    duration_ms = int(probe.get("duration_ms") or 0)
    # Opus may be reported as 48kHz by ffprobe; accept 16k/48k.
    sample_rate = int(probe.get("sample_rate") or 0)
    if codec != "opus":
        raise IngestError(f"Converted audio codec is {codec or 'unknown'}, expected opus.")
    if channels != TARGET_CHANNELS:
        raise IngestError(f"Converted audio channels={channels}, expected {TARGET_CHANNELS}.")
    if sample_rate not in {TARGET_SAMPLE_RATE, 48000}:
        raise IngestError(f"Converted audio sample rate={sample_rate}, expected 16000 or 48000 for Opus.")
    if duration_ms <= 0:
        raise IngestError("Converted Opus duration is invalid.")
    if expected_duration_ms and expected_duration_ms > 0:
        # Allow small container/duration reporting drift.
        delta = abs(duration_ms - expected_duration_ms)
        if delta > max(2000, int(expected_duration_ms * 0.05)):
            logger.warning("Opus duration drift: expected=%sms actual=%sms", expected_duration_ms, duration_ms)

    # Decode check ensures the file is actually readable.
    ffmpeg = _ffmpeg_path()
    decode_check = _run([ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"])
    if decode_check.returncode != 0:
        detail = (decode_check.stderr or decode_check.stdout or "decode failed").strip().replace("\n", " ")[:300]
        raise IngestError(f"Converted Opus file failed decode validation: {detail}")

    return {
        "duration_ms": duration_ms,
        "sample_rate": TARGET_SAMPLE_RATE,
        "channels": TARGET_CHANNELS,
        "codec": TARGET_CODEC,
        "bitrate_kbps": TARGET_BITRATE_KBPS,
        "container": TARGET_CONTAINER,
        "mime_type": TARGET_MIME,
        "size_bytes": path.stat().st_size,
    }


def _store_audio_file(
    conn: Connection,
    meeting_id: str,
    path: Path,
    *,
    kind: str,
    mime_type: str,
    filename: str,
    duration_ms: int | None = None,
    sample_rate: int | None = None,
    channels: int | None = None,
    codec: str | None = None,
    bitrate_kbps: int | None = None,
    container: str | None = None,
    created_oids: list[int] | None = None,
) -> int:
    oid, sha256, size = lo.create_lo_from_file(conn, path)
    if created_oids is not None:
        created_oids.append(int(oid))
    conn.execute(text(
        """
        INSERT INTO meeting_analyzer.meeting_files (
            id, meeting_id, kind, lo_oid, sha256, size_bytes, mime_type,
            duration_ms, sample_rate, channels, original_filename, codec, bitrate_kbps, container
        ) VALUES (
            gen_random_uuid(), :meeting_id, :kind, :oid, :sha, :size, :mime,
            :duration_ms, :sample_rate, :channels, :filename, :codec, :bitrate_kbps, :container
        )
        """
    ), {
        "meeting_id": meeting_id, "kind": kind, "oid": oid, "sha": sha256, "size": size,
        "mime": mime_type, "duration_ms": duration_ms, "sample_rate": sample_rate,
        "channels": channels, "filename": filename, "codec": codec,
        "bitrate_kbps": bitrate_kbps, "container": container,
    })
    return int(oid)


def ingest_recording(
    conn: Connection,
    *,
    user_id: str,
    title: str,
    original_path: Path,
    duration_ms: int,
    sample_rate: int,
    channels: int,
    mime_type: str = "application/octet-stream",
    original_filename: str | None = None,
) -> str:
    """Ingest a recording as a single canonical Opus artifact.

    The uploaded source may be WAV/WEBM/MP3/M4A/OGG/etc. It is converted to
    Ogg Opus (32kbps, mono, 16kHz, VBR) and only that Opus LO is stored.
    """
    original_path = Path(original_path)
    if not original_path.is_file():
        raise IngestError("The recording file is missing.")
    if original_path.stat().st_size <= 44:
        raise IngestError("The recording file is empty.")

    display_name = original_filename or original_path.name
    probe = _probe_audio(original_path)
    effective_duration_ms = duration_ms or int(probe.get("duration_ms") or 0)

    created_oids: list[int] = []
    committed = False
    opus_path: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="tma-ingest-") as temp_dir:
            opus_path = Path(temp_dir) / "canonical-audio.ogg"
            meta = convert_to_canonical_opus(original_path, opus_path)
            if effective_duration_ms:
                meta["duration_ms"] = meta.get("duration_ms") or effective_duration_ms

            meeting_id = conn.execute(
                text(
                    "INSERT INTO meeting_analyzer.meetings (user_id, title, status, duration_ms) VALUES "
                    "(:user_id, :title, 'recorded', :duration_ms) RETURNING id"
                ),
                {"user_id": user_id, "title": title, "duration_ms": meta["duration_ms"]},
            ).scalar_one()

            _store_audio_file(
                conn, str(meeting_id), opus_path,
                kind="audio_opus",
                mime_type=meta["mime_type"],
                filename=Path(display_name).with_suffix(".ogg").name,
                duration_ms=meta["duration_ms"],
                sample_rate=meta["sample_rate"],
                channels=meta["channels"],
                codec=meta["codec"],
                bitrate_kbps=meta["bitrate_kbps"],
                container=meta["container"],
                created_oids=created_oids,
            )

            conn.execute(
                text(
                    "INSERT INTO meeting_analyzer.processing_jobs (id, meeting_id, job_type, status, message) "
                    "VALUES (gen_random_uuid(), :meeting_id, 'transcription', 'queued', 'Queued for transcription')"
                ),
                {"meeting_id": meeting_id},
            )
            conn.commit()
            committed = True
            logger.info(
                "Ingested meeting %s as Opus (%s -> %s, %dkbps, %dHz mono, %dms).",
                meeting_id, probe.get("format_name"), meta["container"], meta["bitrate_kbps"],
                meta["sample_rate"], meta["duration_ms"],
            )
            return str(meeting_id)
    except Exception:
        if not committed and created_oids:
            try:
                try:
                    conn.rollback()
                except Exception:
                    pass
                with conn.engine.begin() as cleanup:
                    for oid in created_oids:
                        try:
                            cleanup.execute(text("SELECT lo_unlink(:oid)"), {"oid": int(oid)})
                        except Exception:
                            logger.warning("Could not unlink orphan LO %s after ingest failure.", oid, exc_info=True)
            except Exception:
                logger.warning("LO cleanup after ingest failure failed.", exc_info=True)
        raise


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
    """Ingest multi-source capture as a single canonical Opus artifact.

    Source files are used only transiently to create a mixed canonical Opus.
    No source/original/derived WAV files are permanently stored.
    """
    if not sources:
        raise IngestError("At least one audio source is required.")
    source_meta = source_meta or {}
    created_oids: list[int] = []
    committed = False
    try:
        with tempfile.TemporaryDirectory(prefix="tma-multi-") as temp_dir:
            tmp = Path(temp_dir)
            normalized: dict[str, Path] = []
            # Normalize each source to temp WAV for mixing.
            normalized_map: dict[str, Path] = {}
            for name, path in sources.items():
                p = Path(path)
                if not p.is_file() or p.stat().st_size <= 44:
                    raise IngestError(f"Source '{name}' is missing or empty.")
                norm_wav = tmp / f"{name}.norm.wav"
                _transcode_to_wav_for_mix(p, norm_wav)
                normalized_map[name] = norm_wav

            mix_sources = [normalized_map[k] for k in ("microphone", "system", "browser", "mixed") if k in normalized_map]
            if not mix_sources:
                mix_sources = list(normalized_map.values())
            mixed_wav = tmp / "mixed-master.wav"
            if len(mix_sources) == 1:
                shutil.copyfile(mix_sources[0], mixed_wav)
            else:
                _mix_wav_files(mix_sources, mixed_wav)

            opus_path = tmp / "canonical-audio.ogg"
            meta = convert_to_canonical_opus(mixed_wav, opus_path)
            mix_duration_ms = meta.get("duration_ms") or duration_ms

            manifest = {
                "sources": list(normalized_map.keys()),
                "source_meta": source_meta,
                "canonical": {"kind": "audio_opus", "codec": "opus", "bitrate_kbps": TARGET_BITRATE_KBPS},
            }
            meeting_id = conn.execute(text(
                "INSERT INTO meeting_analyzer.meetings (user_id, title, status, duration_ms, notes, capture_mode, capture_manifest) "
                "VALUES (:user_id, :title, 'recorded', :duration_ms, :notes, :capture_mode, CAST(:manifest AS jsonb)) RETURNING id"
            ), {
                "user_id": user_id, "title": title, "duration_ms": mix_duration_ms, "notes": notes,
                "capture_mode": "multi_source", "manifest": json.dumps(manifest),
            }).scalar_one()

            _store_audio_file(
                conn, str(meeting_id), opus_path,
                kind="audio_opus", mime_type=meta["mime_type"], filename="audio.ogg",
                duration_ms=mix_duration_ms, sample_rate=meta["sample_rate"], channels=meta["channels"],
                codec=meta["codec"], bitrate_kbps=meta["bitrate_kbps"], container=meta["container"],
                created_oids=created_oids,
            )

            conn.execute(text(
                "INSERT INTO meeting_analyzer.processing_jobs (id, meeting_id, job_type, status, message) "
                "VALUES (gen_random_uuid(), :meeting_id, 'transcription', 'queued', 'Queued for transcription')"
            ), {"meeting_id": meeting_id})
            conn.commit()
            committed = True
            return str(meeting_id)
    except Exception:
        if not committed and created_oids:
            try:
                try:
                    conn.rollback()
                except Exception:
                    pass
                with conn.engine.begin() as cleanup:
                    for oid in created_oids:
                        try:
                            cleanup.execute(text("SELECT lo_unlink(:oid)"), {"oid": int(oid)})
                        except Exception:
                            logger.warning("Could not unlink orphan LO %s after ingest failure.", oid, exc_info=True)
            except Exception:
                logger.warning("LO cleanup after ingest failure failed.", exc_info=True)
        raise


def _transcode_to_wav_for_mix(source: Path, target: Path) -> Path:
    """Transcode any supported source to PCM WAV for temporary mixing."""
    ffmpeg = _ffmpeg_path()
    cmd = [
        ffmpeg, "-y", "-v", "error", "-i", str(source),
        "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(target),
    ]
    result = _run(cmd)
    if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 44:
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip().replace("\n", " ")[:300]
        raise IngestError(f"Audio source could not be prepared for mixing: {detail}")
    with wave.open(str(target), "rb") as wf:
        if wf.getnframes() == 0:
            raise IngestError("Audio source decoded to empty WAV.")
    return target


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
            import audioop
            raw, _ = audioop.ratecv(raw, 2, ch, rate, sr, None)
            ch = ch
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


# Compatibility helpers for older callers/tests. New ingests do not persist WAV.

def normalize_uploaded_audio(source: Path, *, original_filename: str | None = None, mime_type: str | None = None) -> dict[str, Any]:
    """Legacy helper: return audio probe/metadata without persisting WAV.

    New ingest stores Opus only. This function keeps a compatible shape for
    callers that still import it.
    """
    source = Path(source)
    probe = _probe_audio(source)
    return {
        "canonical_path": source,
        "temp_converted_path": None,
        "probe": probe,
        "duration_ms": int(probe.get("duration_ms") or 0),
        "sample_rate": int(probe.get("sample_rate") or 0),
        "channels": int(probe.get("channels") or 0),
        "mime_type": _normalized_mime(mime_type, original_filename or source.name),
    }


def derive_16k_mono(source: Path, target: Path) -> dict[str, Any]:
    """Legacy compatibility helper (not used for new Opus-only ingests)."""
    import audioop
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
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(TARGET_SAMPLE_RATE); writer.writeframes(raw)
    return {"sample_rate": TARGET_SAMPLE_RATE, "channels": 1, "duration_ms": round(frames * 1000 / sample_rate) if sample_rate else 0}
