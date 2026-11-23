"""Evaluate the golden-audio pack against the production processing path.

Runs every datapoint in ``golden_audio_synthetic`` through the *exact*
production wiring — Sarvam Saaras v4 (translit, diarization) primary with the
local Faster-Whisper fallback, then the Groq analysis backend — and scores the
results with the metrics from ``golden_metrics.py``.

Usage (from the repository root):

    .venv\\Scripts\\python.exe scripts\\evaluate_golden.py            # full run
    .venv\\Scripts\\python.exe scripts\\evaluate_golden.py --skip-analysis
    .venv\\Scripts\\python.exe scripts\\evaluate_golden.py --only EN01 HI04
    .venv\\Scripts\\python.exe scripts\\evaluate_golden.py --dry-run  # no network

Outputs (default ``golden_audio_synthetic/results/``):
    metrics.json  - full per-datapoint detail including transcripts
    report.md     - aggregated tables plus the real-meeting transcript

Cloud cost note: a full run creates one Sarvam batch job and one Groq
analysis per datapoint. API keys are read from the environment/.env and are
never logged or written to the results.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import golden_metrics as gm  # noqa: E402  (path set above)

REAL_MEETING_FILE = ROOT / "golden_audio_synthetic" / (
    "(Audio) Screen Recording 2026-11-20 101607.m4a"
)
REAL_MEETING_ID = "REAL01"
REAL_MEETING_EXPECTED_SPEAKERS = 3

logger = logging.getLogger("evaluate_golden")


# -- dataset ------------------------------------------------------------------


def load_datapoints(only: list[str] | None) -> tuple[list[dict], dict[str, str]]:
    """Return manifest datapoints plus the romanized Hindi references."""

    manifest_path = ROOT / "golden_audio_synthetic" / "manifest.json"
    entries: list[dict] = json.loads(manifest_path.read_text(encoding="utf-8"))
    romanized_path = ROOT / "golden_audio_synthetic" / "hindi_romanized.json"
    romanized_raw = json.loads(romanized_path.read_text(encoding="utf-8"))
    romanized = {k: v for k, v in romanized_raw.items() if not k.startswith("_")}

    datapoints = [
        entry
        for entry in entries
        if not only or entry["id"] in only
    ]
    return datapoints, romanized


def wav_duration(path: Path) -> float | None:
    """Audio duration in seconds for WAV files; ``None`` when unreadable."""

    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
        return frames / rate if rate else None
    except (wave.Error, EOFError, OSError):
        return None


def any_audio_duration(path: Path) -> float | None:
    """Duration via ``wave`` first, then PyAV (m4a), else ``None``."""

    duration = wav_duration(path)
    if duration is not None:
        return duration
    try:
        import av  # faster-whisper dependency; only needed for m4a

        with av.open(str(path)) as container:
            return float(container.duration / 1_000_000) if container.duration else None
    except Exception:
        return None


# -- production wiring --------------------------------------------------------


def build_production_services():
    """Wire exactly what ``api.app`` wires at server startup."""

    from ai.fallback_provider import FallbackTranscriptionProvider
    from ai.faster_whisper_provider import FasterWhisperProvider
    from ai.provider_factory import build_analysis_provider
    from ai.sarvam_provider import SarvamTranscriptionProvider
    from ai.summarization import MeetingSummarizer
    from ai.transcription import TranscriptionService
    from config import load_settings

    settings = load_settings()
    primary = SarvamTranscriptionProvider(settings)
    fallback = FasterWhisperProvider(settings)
    service = TranscriptionService(
        settings,
        provider=FallbackTranscriptionProvider(primary, fallback),
    )
    try:
        analysis_provider = build_analysis_provider(settings)
    except Exception as exc:
        logger.warning("Analysis backend unavailable: %s", exc)
        analysis_provider = None
    summarizer = (
        MeetingSummarizer(settings, provider=analysis_provider)
        if analysis_provider is not None
        else None
    )
    return settings, service, summarizer


# -- evaluation ---------------------------------------------------------------


def evaluate_datapoint(service, entry: dict) -> dict:
    """Transcribe one datapoint through the production path and score it."""

    path = ROOT / "golden_audio_synthetic" / entry["file"]
    language = entry.get("language", "english")
    reference = entry.get("reference_text", "")
    expected_speaker_count = len(entry.get("speakers", []))
    expected_utterances = [
        str(speaker.get("utterance", ""))
        for speaker in entry.get("speakers", [])
        if speaker.get("utterance")
    ]

    result: dict[str, object] = {"id": entry["id"], "scenario": entry["scenario"]}
    started = time.perf_counter()
    try:
        transcription = service.transcribe(path)
    except Exception as exc:
        result["status"] = "error"
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        result["latency_s"] = round(time.perf_counter() - started, 2)
        result["provider"] = _last_provider_name(service)
        result["fallback"] = _fallback_info(service)
        return result

    latency = time.perf_counter() - started
    duration = any_audio_duration(path)
    text = transcription.text

    fallback_info = _fallback_info(service)
    if fallback_info:
        # The fallback (Whisper) produced the most recent result.
        provider_used = "faster-whisper"
    else:
        provider_used = _last_provider_name(service)

    # Hindi: pick the reference form matching the returned script.
    if language == "hindi":
        if gm.has_devanagari(text):
            compare_reference = reference
        else:
            compare_reference = ROMANIZED_REFERENCES.get(entry["id"], reference)
    else:
        compare_reference = reference

    segments = list(transcription.segments or [])
    ts_metrics = gm.timestamp_metrics(segments, duration)
    spk_metrics = gm.speaker_metrics(
        segments, expected_utterances, expected_speaker_count
    )
    duplication_ratio = gm.duplicate_segment_ratio(segments)
    out_of_order = gm.out_of_order_count(segments)

    language_ok = None
    detected = (transcription.language or "").lower()
    if detected:
        language_ok = detected.startswith(language[:2])

    wer_value = gm.wer(compare_reference, text)
    structure_value = ts_metrics["duration_coverage"]
    if structure_value is None:
        structure_value = 0.0

    result.update(
        {
            "status": "ok",
            "provider": provider_used,
            "fallback": fallback_info,
            "detected_language": transcription.language,
            "language_ok": language_ok,
            "script_has_devanagari": gm.has_devanagari(text),
            "wer": round(wer_value, 3),
            "cer": round(gm.cer(compare_reference, text), 3),
            "key_term_recall": round(gm.key_term_recall(compare_reference, text), 3),
            "latency_s": round(latency, 2),
            "audio_duration_s": round(duration, 2) if duration else None,
            "rtf": (
                round(latency / duration, 3) if duration and duration > 0 else None
            ),
            "score": gm.composite_score(
                wer_value=wer_value,
                key_term_recall_value=float(
                    gm.key_term_recall(compare_reference, text)
                ),
                language_ok=language_ok,
                diarization_accuracy=spk_metrics["attribution_accuracy"],
                structure_value=float(structure_value),
            ),
            "transcript": text,
        }
    )
    result.update(ts_metrics)
    result.update(spk_metrics)
    result["duplicate_segment_ratio"] = duplication_ratio
    result["out_of_order_count"] = out_of_order
    return result


def _last_provider_name(service) -> str:
    provider = getattr(getattr(service, "_provider", None), "last_provider", None)
    name = getattr(provider, "provider_name", None)
    return str(name) if name else "unknown"


def _fallback_info(service) -> dict[str, str | None] | None:
    provider = getattr(service, "_provider", None)
    info = getattr(provider, "fallback_info", None)
    if info is None:
        return None
    fallback_from, reason = info()
    if not fallback_from:
        return None
    return {"fallback_from": fallback_from, "reason": reason}


def evaluate_analysis(summarizer, entry: dict, transcript: str) -> dict:
    """Run the Groq analysis stage and score it against the reference."""

    reference = entry.get("reference_text", "")
    scenario = entry.get("scenario", "")
    result: dict[str, object] = {}
    try:
        analysis = summarizer.analyze(transcript)
    except Exception as exc:
        return {"analysis_valid": False, "analysis_error": str(exc)}

    result["analysis_valid"] = True
    result["analysis"] = analysis

    action_items = analysis.get("action_items") or []
    # Hallucination checks: references contain no spoken names, so every
    # owner must be null; deadlines must be one of the date words in the
    # reference.
    hallucinated_owners = sum(
        1
        for item in action_items
        if isinstance(item, dict) and item.get("owner") not in (None, "")
    )
    reference_words = set(gm.normalize_text(reference).split())
    date_words = {"friday", "thursday", "tomorrow", "today", "shukrawar",
                  "guruwar", "kal", "aaj"}
    invented_deadlines = 0
    for item in action_items:
        if not isinstance(item, dict):
            continue
        deadline = item.get("deadline")
        if not deadline:
            continue
        deadline_words = set(gm.normalize_text(str(deadline)).split())
        if not deadline_words & (reference_words | date_words):
            invented_deadlines += 1

    result["action_item_count"] = len(action_items)
    result["hallucinated_owners"] = hallucinated_owners
    result["invented_deadlines"] = invented_deadlines

    if scenario == "task_manager_commands":
        result["expected_action_items"] = 3
    if scenario == "overlapping_speech":
        result["captured_conflict"] = bool(
            analysis.get("decisions") or analysis.get("risks_or_blockers")
        )
    if scenario == "fast_informal_speech":
        result["captured_question"] = bool(analysis.get("open_questions"))
    return result


# -- robustness checks --------------------------------------------------------


def robustness_checks(service, scratch: Path) -> list[dict]:
    """Hostile-input pass/fail checks; files are generated, never golden."""

    from ai.transcription import NoSpeechError, TranscriptionError

    results: list[dict] = []

    def record(name: str, outcome: str, detail: str) -> None:
        results.append(
            {
                "check": name,
                "outcome": outcome,
                "passed": outcome in {"rejected", "handled"},
                "detail": detail,
            }
        )

    # 1. Pure silence -> must be rejected as no-speech / empty.
    silent_path = scratch / "robustness_silent.wav"
    with wave.open(str(silent_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\x00\x00" * 22050)
    try:
        service.transcribe(silent_path)
        record("silent_wav", "accepted", "silent audio produced a transcript")
    except NoSpeechError as exc:
        record("silent_wav", "rejected", str(exc))
    except TranscriptionError as exc:
        record("silent_wav", "rejected", str(exc))
    except Exception as exc:
        record("silent_wav", "crash", f"{type(exc).__name__}: {exc}")

    # 2. Truncated WAV (header + cut mid-frame) -> typed error or clean result.
    source = ROOT / "golden_audio_synthetic" / "english" / "EN01.wav"
    truncated_path = scratch / "robustness_truncated.wav"
    try:
        with wave.open(str(source), "rb") as src:
            params = src.getparams()
            frames = src.readframes(min(src.getnframes(), 4096))
        with wave.open(str(truncated_path), "wb") as dst:
            dst.setparams(params)
            dst.writeframes(frames[: len(frames) // 2])
        try:
            service.transcribe(truncated_path)
            record("truncated_wav", "handled", "transcribed the audible prefix")
        except (TranscriptionError, NoSpeechError) as exc:
            record("truncated_wav", "rejected", str(exc))
        except Exception as exc:
            record("truncated_wav", "crash", f"{type(exc).__name__}: {exc}")
    except (wave.Error, OSError) as exc:
        record("truncated_wav", "skipped", f"could not build the file: {exc}")

    # 3. Zero-byte file -> must be rejected as empty.
    empty_path = scratch / "robustness_empty.wav"
    empty_path.write_bytes(b"")
    try:
        service.transcribe(empty_path)
        record("empty_file", "accepted", "empty file produced a transcript")
    except (TranscriptionError, NoSpeechError) as exc:
        record("empty_file", "rejected", str(exc))
    except Exception as exc:
        record("empty_file", "crash", f"{type(exc).__name__}: {exc}")

    return results


# -- reporting ----------------------------------------------------------------


def aggregate_table(rows: list[dict]) -> str:
    """Markdown table for the per-datapoint metrics."""

    header = (
        "| id | scenario | provider | WER | CER | key-terms | lang ok | "
        "spk err | attrib | dup | ooo | cov | RTF | score |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    lines = [header]
    for row in rows:
        lines.append(
            "| {id} | {scenario} | {provider} | {wer} | {cer} | {ktr} | {lang} | "
            "{spk} | {attr} | {dup} | {ooo} | {cov} | {rtf} | {score} |".format(
                id=row.get("id"),
                scenario=row.get("scenario"),
                provider=row.get("provider"),
                wer=_fmt(row.get("wer")),
                cer=_fmt(row.get("cer")),
                ktr=_fmt(row.get("key_term_recall")),
                lang=_fmt_bool(row.get("language_ok")),
                spk=_fmt(row.get("speaker_count_error")),
                attr=_fmt(row.get("attribution_accuracy")),
                dup=_fmt(row.get("duplicate_segment_ratio")),
                ooo=_fmt(row.get("out_of_order_count")),
                cov=_fmt(row.get("duration_coverage")),
                rtf=_fmt(row.get("rtf")),
                score=_fmt(row.get("score")),
            )
        )
    return "\n".join(lines)


def _fmt(value) -> str:
    if value is None:
        return "-"
    return str(value)


def _fmt_bool(value) -> str:
    if value is None:
        return "-"
    return "yes" if value else "NO"


def build_report(metrics: dict, analysis_enabled: bool) -> str:
    rows = [
        item
        for item in metrics["datapoints"]
        if item.get("status") == "ok" and item["id"] != REAL_MEETING_ID
    ]
    real = next(
        (item for item in metrics["datapoints"] if item["id"] == REAL_MEETING_ID),
        None,
    )
    lines: list[str] = []
    lines.append("# Golden-audio evaluation report")
    lines.append("")
    lines.append(f"Generated: {metrics['generated_at']}")
    lines.append(f"Path: production (Sarvam primary -> Whisper fallback)"
                 + (" + Groq analysis" if analysis_enabled else " (analysis skipped)"))
    lines.append("")

    errors = [
        item for item in metrics["datapoints"]
        if item.get("status") == "error"
    ]
    lines.append(f"Datapoints: {len(metrics['datapoints'])} "
                 f"(ok: {len(rows)}, errors: {len(errors)})")
    lines.append("")

    for language in ("english", "hindi"):
        subset = [
            item for item in rows
            if item["id"].startswith("EN" if language == "english" else "HI")
        ]
        if not subset:
            continue
        lines.append(f"## {language.capitalize()} ({len(subset)} samples)")
        lines.append("")
        lines.append(aggregate_table(subset))
        lines.append("")
        wer_values = [item["wer"] for item in subset]
        scores = [item["score"] for item in subset]
        lines.append(
            f"- Mean WER: {sum(wer_values) / len(wer_values):.3f} "
            f"(pass bar: {'0.30' if language == 'english' else '0.45'})"
        )
        lines.append(
            f"- Mean composite score: {sum(scores) / len(scores):.3f}"
        )
        fallbacks = sum(1 for item in subset if item.get("fallback"))
        lines.append(f"- Whisper fallback used: {fallbacks}/{len(subset)}")
        duplication = [
            float(item.get("duplicate_segment_ratio") or 0.0) for item in subset
        ]
        out_of_order = sum(
            int(item.get("out_of_order_count") or 0) for item in subset
        )
        lines.append(
            f"- Duplicated segments (dup ratio mean): {sum(duplication) / len(duplication):.3f}"
        )
        lines.append(f"- Out-of-order timestamps (total): {out_of_order}")
        lines.append("")

    lines.append("## Per-scenario breakdown")
    lines.append("")
    scenarios: dict[str, list[dict]] = {}
    for item in rows:
        scenarios.setdefault(str(item["scenario"]), []).append(item)
    lines.append("| scenario | n | mean WER | mean score |")
    lines.append("|---|---|---|---|")
    for scenario, subset in sorted(scenarios.items()):
        lines.append(
            f"| {scenario} | {len(subset)} "
            f"| {sum(i['wer'] for i in subset) / len(subset):.3f} "
            f"| {sum(i['score'] for i in subset) / len(subset):.3f} |"
        )
    lines.append("")

    robustness = metrics.get("robustness") or []
    if robustness:
        lines.append("## Robustness (hostile inputs)")
        lines.append("")
        lines.append("| check | result | detail |")
        lines.append("|---|---|---|")
        for item in robustness:
            verdict = "PASS" if item["passed"] else "FAIL"
            lines.append(
                f"| {item['check']} | {verdict} ({item['outcome']}) "
                f"| {item['detail']} |"
            )
        lines.append("")

    if analysis_enabled:
        lines.append("## Groq analysis stage")
        lines.append("")
        analysed = metrics.get("analysis") or {}
        valid = sum(1 for item in analysed.values() if item.get("analysis_valid"))
        lines.append(f"- Schema-valid analyses: {valid}/{len(analysed)}")
        # The "owners must be null" / "deadlines must be reference date words"
        # assertions only hold for synthetic datapoints, whose references
        # contain no spoken names and no dates beyond the known date words.
        # The real meeting may legitimately have spoken names, so it is
        # excluded from the must-be-zero totals.
        synthetic_analysis = {
            key: item
            for key, item in analysed.items()
            if key != REAL_MEETING_ID
        }
        halluc = sum(
            int(item.get("hallucinated_owners", 0) or 0)
            for item in synthetic_analysis.values()
        )
        invented = sum(
            int(item.get("invented_deadlines", 0) or 0)
            for item in synthetic_analysis.values()
        )
        lines.append(
            f"- Hallucinated owners (synthetic datapoints, must be 0): {halluc}"
        )
        lines.append(
            f"- Invented deadlines (synthetic datapoints, must be 0): {invented}"
        )
        real_analysis = analysed.get(REAL_MEETING_ID, {})
        real_owners = [
            item.get("owner")
            for item in real_analysis.get("analysis", {}).get("action_items", [])
            if isinstance(item, dict) and item.get("owner")
        ]
        if real_owners:
            lines.append(
                "- Real-meeting attributed owners (verify manually against "
                f"the transcript): {', '.join(str(o) for o in real_owners)}"
            )
        lines.append("")

    if real:
        lines.append("## Real meeting (no reference — manual review)")
        lines.append("")
        if real.get("status") == "ok":
            lines.append(
                f"- Provider: {real.get('provider')}, RTF: {real.get('rtf')}, "
                f"segments: {real.get('segment_count')}, "
                f"speakers found: {real.get('found_speakers', '-')} "
                f"(expected ~{REAL_MEETING_EXPECTED_SPEAKERS})"
            )
            lines.append(f"- Detected language: {real.get('detected_language')}")
            lines.append(
                f"- Duplicated segments: {real.get('duplicate_segment_ratio', 0)}, "
                f"out-of-order timestamps: {real.get('out_of_order_count', 0)}"
            )
            lines.append("")
            lines.append("### Transcript")
            lines.append("")
            lines.append("```")
            lines.append(str(real.get("transcript", "")))
            lines.append("```")
            analysis = (metrics.get("analysis") or {}).get(REAL_MEETING_ID, {})
            if analysis.get("analysis_valid"):
                lines.append("")
                lines.append("### Analysis (raw JSON)")
                lines.append("")
                lines.append("```json")
                lines.append(
                    json.dumps(analysis["analysis"], ensure_ascii=False, indent=2)
                )
                lines.append("```")
        else:
            lines.append(f"- FAILED: {real.get('error_type')}: {real.get('error')}")
        lines.append("")

    return "\n".join(lines)


# -- entry point ---------------------------------------------------------------

ROMANIZED_REFERENCES: dict[str, str] = {}


def main() -> int:
    global ROMANIZED_REFERENCES

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-analysis", action="store_true",
                        help="skip the Groq analysis stage")
    parser.add_argument("--only", nargs="*", metavar="ID",
                        help="evaluate only these datapoint ids")
    parser.add_argument("--results-dir", type=Path, default=None,
                        help="output directory (default: golden_audio_synthetic/results)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list datapoints and wiring without network calls")
    parser.add_argument("--skip-robustness", action="store_true",
                        help="skip the hostile-input checks")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
    logging.getLogger("alembic").setLevel(logging.WARNING)

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    datapoints, romanized = load_datapoints(args.only)
    ROMANIZED_REFERENCES = romanized

    _settings, service, summarizer = build_production_services()
    analysis_enabled = not args.skip_analysis and summarizer is not None

    real_datapoint = {
        "id": REAL_MEETING_ID,
        "language": "english",
        "scenario": "real_meeting",
        # Relative to the golden pack root, like every manifest "file" value.
        "file": str(REAL_MEETING_FILE.relative_to(ROOT / "golden_audio_synthetic")),
        "speakers": [
            {"speaker_id": f"SPEAKER_{i}", "utterance": ""}
            for i in range(1, REAL_MEETING_EXPECTED_SPEAKERS + 1)
        ],
    }
    if not args.only or REAL_MEETING_ID in args.only:
        datapoints = datapoints + [real_datapoint]

    print(f"Datapoints: {len(datapoints)}")
    for entry in datapoints:
        print(f"  {entry['id']:<8} {entry['scenario']:<28} {entry['file']}")
    fallback_wrapper = getattr(service, "_provider", None)
    print(f"Transcription: {getattr(fallback_wrapper, 'provider_name', '?')} "
          f"(primary model: {getattr(fallback_wrapper, 'model_name', '?')})")
    print(f"Analysis: {'configured' if summarizer is not None else 'not configured'}"
          f"{'' if analysis_enabled else ' (stage disabled)'}")
    if args.dry_run:
        print("Dry run: no audio will be processed.")
        return 0

    results_dir = args.results_dir or (ROOT / "golden_audio_synthetic" / "results")
    results_dir.mkdir(parents=True, exist_ok=True)

    datapoint_results: list[dict] = []
    analysis_results: dict[str, dict] = {}
    for entry in datapoints:
        print(f"[{entry['id']}] transcribing ({entry['scenario']})...")
        result = evaluate_datapoint(service, entry)
        datapoint_results.append(result)
        if result.get("status") == "ok":
            print(
                f"[{entry['id']}] provider={result.get('provider')} "
                f"wer={result.get('wer')} score={result.get('score')}"
            )
            if analysis_enabled:
                print(f"[{entry['id']}] analysing with Groq...")
                analysis_results[entry["id"]] = evaluate_analysis(
                    summarizer, entry, str(result.get("transcript", ""))
                )
        else:
            print(f"[{entry['id']}] FAILED: {result.get('error_type')}")

    robustness: list[dict] = []
    if not args.skip_robustness:
        print("Running robustness checks (3 hostile inputs)...")
        with tempfile.TemporaryDirectory(prefix="tma-golden-") as scratch:
            robustness = robustness_checks(service, Path(scratch))

    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "path": "production (sarvam primary, whisper fallback)",
        "analysis_enabled": analysis_enabled,
        "analysis_model": (
            summarizer.provider_model_name if summarizer is not None else None
        ),
        "datapoints": datapoint_results,
        "analysis": analysis_results,
        "robustness": robustness,
    }

    metrics_path = results_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = build_report(metrics, analysis_enabled)
    report_path = results_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")

    print(f"Wrote {metrics_path}")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
