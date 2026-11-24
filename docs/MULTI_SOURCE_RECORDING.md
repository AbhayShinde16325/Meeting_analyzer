# Multi-Source Recording (Implementation Status)

## Implemented in repo

- **React recording UI**: mic selection, mic meter, browser tab-audio capture via `getDisplayMedia` (fallback), recording timer, capture health warnings, and warning before uploading mic-only when tab audio was requested but missing.
- **Backend multi-source ingest endpoint**: `POST /meetings/multi-source` accepts `microphone`, `system`, `browser`, `mixed` files + `source_manifest`; stores source files, mixed master (`original_audio_48k` compatibility), derived 16k mono, and queues transcription.
- **Audio normalization**: browser formats (webm/opus, mp3, m4a, ogg, wav) probed and transcoded with ffmpeg to canonical WAV when needed; temp files cleaned.
- **Sarvam**: batch API with diarization enabled, automatic language detection by default (optional hint), segment timestamps/speaker labels preserved; optional language/confidence fields preserved when present.
- **Windows local capture agent**: `Meeting_analyzer/tools/windows_capture_agent.py` captures mic + WASAPI loopback on Windows machine and uploads synchronized sources to backend. **Must run on user PC, not remote Ubuntu server.**

## Architecture constraints

- No deprecated Google Meet Media API. 
- Backend remains FastAPI+Postgres+existing analyzer pipeline. 
- Original source recordings preserved where provided. 
- WASAPI loopback is Windows-local; Linux PipeWire/PulseAudio is future extension point.

## Validation workflow (required, not fully automated here)

Use real meetings and record ground truth participants/languages. Evaluate WER, Hindi/English/Hinglish accuracy, code-switching, speaker attribution, overlap, and missing-audio detection. Store results in a validation report; do not claim 100% accuracy.

## Not fully verified in this environment

- Real Google Meet 2/5/8-person E2E with remote participants.
- Actual WASAPI loopback on Windows hardware, Bluetooth/headset device switching.
- Sarvam live transcription quality on real Hindi/English/Hinglish meetings.
- Long-duration and overlapping speech quality benchmarks.
