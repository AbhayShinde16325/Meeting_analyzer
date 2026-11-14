# TeamMeetingAI

TeamMeetingAI is a **local-first desktop meeting recorder and analyzer** for
Windows. It records a meeting's microphone and system audio, stores everything
on the local computer, and provides a desktop UI for meeting history, playback,
transcripts, and structured notes.

This is the V1 MVP. There is no server, no cloud database, no synchronization,
and no automatic upload.

## Current status

Implemented in this repository:

- PySide6 desktop application with local profile selection.
- **Files-only storage**: every meeting lives in its own folder inside a
  user-chosen **meeting library** directory — the recording WAVs, a
  human-readable `transcript.md`, `analysis.md`, and a machine-readable
  `meeting.json` that is the source of truth. There is no database.
- First-run **library setup dialog**: on the first launch the app asks for the
  folder where meetings should be stored and validates write access before
  saving the choice.
- Manual meeting recording with a clear recording indicator and elapsed timer.
- Separate microphone and system-audio (WASAPI loopback) capture via SoundCard.
- Safe WAV finalization and file cleanup.
- One-time **migration** from the previous SQLite storage: existing meetings,
  audio files, and local profiles are converted into meeting folders
  automatically on the first start, and the old `teammeetingai.db` is renamed
  to `teammeetingai.db.migrated-<timestamp>` as a backup.
- Meeting dashboard with local keyword search across titles, summaries,
  transcripts, and action-item text.
- Meeting details screen with audio playback, transcript, overview, decisions,
  risks, follow-ups, and action items.
- Separate **Transcribe recording** and **Analyse transcript** buttons so the
  two stages can be run independently; transcription needs the audio file,
  analysis needs only the saved transcript.
- **Re-transcribe**: when a transcript already exists, the button becomes
  **Re-transcribe** and asks for confirmation because it replaces the
  transcript **and deletes the saved analysis** (so stale notes can never
  disagree with the new transcript).
- **Me / Remote speaker labels**: transcription runs on the two separated
  tracks recorded during the meeting - the microphone track is labelled
  `Me` and the system-audio track (remote participants) is labelled
  `Remote`. Segments are merged in timestamp order. If a track file is
  missing (older recordings) the mixed recording is transcribed without
  labels. **Use headphones for correct labels**: with speakers, your
  microphone also picks up the remote voices.
- **Cancellation**: a **Cancel** button appears while processing. Cancellation
  is cooperative - the meeting returns to its previous status, no partial
  transcript is saved, and cancelling a re-transcription keeps the old
  transcript and analysis. Closing the app during processing asks first and
  cancels cleanly.
- Local transcription wired through **Faster-Whisper** (lazy model loading,
  configurable model/device/compute type, first-run weight download into the
  app data directory).
- Meeting analysis behind a backend switch: `none` (default,
  transcription-only) or the Groq cloud API behind an explicit opt-in flag.
- Background processing workflow for transcription and analysis as independent
  stages; a successful transcription is never marked failed because analysis is
  unavailable.
- Strict validation of model-produced JSON before it is saved.
- Deleting a meeting removes its entire meeting folder (audio, transcript,
  analysis, and record).

**Transcription is always local.** Meeting audio never leaves this computer.
When the Groq analysis backend is explicitly enabled, the meeting *transcript*
(not the audio) is sent to Groq for processing; the app shows a visible warning
whenever that mode is active. See [AI providers and backends](#ai-providers-and-backends).

## Requirements

- Windows 10 or Windows 11
- Python 3.11+
- A working microphone and playback device
- Google Meet (or any other meeting app) playing through the selected speakers
- For analysis: a Groq API key and model (cloud, opt-in) or a local adapter you
  wire yourself

## Setup

```powershell
cd D:\TeamMeetingAI
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
```

On Windows you can also double-click `run.bat`, which runs `main.py` with the
virtual-environment interpreter from the repository directory (no matter where
the shortcut is started from).

`requirements.txt` installs the core desktop application plus the Faster-Whisper
transcription engine and the Groq SDK. The first transcription downloads the
Whisper model weights (tens to hundreds of MB depending on model size) from
Hugging Face into `%LOCALAPPDATA%\TeamMeetingAI\models\whisper`. That download
contains model weights only; meeting audio and transcripts never leave the
machine during transcription.

## Development and testing

Install test tooling into the project virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Run the automated suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The tests use a temporary data directory and the Qt `offscreen` platform. They
never record real audio or touch your normal `%LOCALAPPDATA%` data.

For a manual end-to-end session in an isolated sandbox:

```powershell
$env:TEAMMEETINGAI_DATA_DIR = "C:\Users\ABHAY\AppData\Local\Temp\opencode\tma-sandbox"
$env:TEAMMEETINGAI_LIBRARY_DIR = "C:\Users\ABHAY\AppData\Local\Temp\opencode\tma-sandbox\library"
.\.venv\Scripts\python.exe main.py
```

Use a short, consented test recording rather than a private meeting. Delete the
sandbox directory to reset all local test data. Omit
`TEAMMEETINGAI_LIBRARY_DIR` to exercise the first-run library dialog instead.

## Using the application

1. On the first launch, choose the **meeting library folder** — the directory
   where every meeting folder will live (e.g. a Documents subfolder). The
   choice is validated for write access and remembered.
2. Enter or select a local profile.
3. Select **Start new recording**.
4. Enter a meeting title.
5. Choose the microphone and the speaker/headset that Google Meet uses.
6. Click **Start recording**. Confirm that all participants have consented.
7. Click **Stop recording** when the meeting ends.
8. Open the saved meeting and click **Transcribe recording**, then
   **Analyse transcript** (analysis requires a configured backend; with the
   default `none` backend the meeting stops at status `transcribed`).
9. To redo a transcription, click **Re-transcribe**. Confirm the dialog: the
   transcript is replaced and the saved analysis is deleted. Run
   **Analyse transcript** again afterwards.

Recording always requires an explicit user action. Closing the app during a
recording cancels and cleans up the partial files. While transcription or
analysis is running, a **Cancel** button is available; the meeting keeps its
previous state and a **Cancel** during processing never leaves a partial
transcript behind.

## Local data locations

Only two things are stored on disk:

1. **The application data directory** (default
   `%LOCALAPPDATA%\TeamMeetingAI\`) holds the log file, the Whisper model
   cache, and a small `config.json` with the chosen library location.
2. **The meeting library directory** (you choose it on first launch) holds
   everything else. Every meeting becomes one folder:

```text
<library>\
    profiles.json                     # local profile labels
    2026-11-13 10-26 Release planning [6bcae10c]\
        meeting.json                  # machine-readable record (source of truth)
        audio.wav                     # mixed recording used for playback/AI
        mic.wav                       # separate microphone track
        system.wav                    # separate system-audio track
        transcript.md                 # human-readable transcript (timestamps + Me/Remote)
        analysis.md                   # human-readable meeting notes
```

`meeting.json` is authoritative; the `.md` files are regenerated from it on
every save, so you can open, copy, or back up meeting folders freely. The
dashboard re-scans the library on refresh, so folders added or removed outside
the app are picked up. Deleting a meeting in the app deletes its whole folder.

Override the application data directory with:

```powershell
$env:TEAMMEETINGAI_DATA_DIR = "D:\TeamMeetingData"
```

Override (or pre-configure) the meeting library with:

```powershell
$env:TEAMMEETINGAI_LIBRARY_DIR = "D:\Meetings"
```

The environment variable always wins over the value saved in `config.json`.

## Changing the library location

You can change the folder where meetings are stored at any time from the
application UI:

1. In the dashboard header, click **Library…** (next to the user label).
2. The **Library settings** dialog shows your current library and lets you
   choose a different folder.  The dialog previews how many meetings the
   candidate folder already contains, so you can tell an empty new folder from
   one holding your existing meetings.
3. Select a folder.  The app validates write access before accepting the
   choice, then saves it to `config.json` and **hot-swaps** the library into
   every screen — no restart needed.
4. If your current profile exists in the new library, you stay logged in.
   Otherwise the app returns to the profile-selection screen.
5. All meetings remain where they are; the app simply points at the new
   folder.  To relocate meetings, move the whole library folder in Windows
   Explorer first, then switch the app to point at it.

**Guards** — the switch is refused while a recording is in progress, while
a transcription/analysis worker is running, or while the finalisation worker
is closing the current recording.  A clear warning dialog explains why.

**What does not happen** — meetings are never automatically moved or copied.
If you want your meetings in the new folder, move the library folder in
Windows Explorer first, then switch the app to point at it.

Other optional environment overrides:

| Variable | Default |
| --- | --- |
| `TEAMMEETINGAI_LIBRARY_DIR` | unset (asked on first launch, then saved in `config.json`) |
| `TEAMMEETINGAI_SAMPLE_RATE` | `48000` |
| `TEAMMEETINGAI_WHISPER_MODEL` | `small` (model size or local model directory) |
| `TEAMMEETINGAI_WHISPER_DEVICE` | `cpu` |
| `TEAMMEETINGAI_WHISPER_COMPUTE_TYPE` | `int8` |
| `TEAMMEETINGAI_WHISPER_LANGUAGE` | blank (auto-detect the spoken language) |
| `TEAMMEETINGAI_WHISPER_BEAM_SIZE` | `5` |
| `TEAMMEETINGAI_WHISPER_CPU_THREADS` | `0` (use the engine default) |
| `TEAMMEETINGAI_ANALYSIS_BACKEND` | `none` (`groq` enables cloud analysis) |
| `TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS` | unset (must be `1` to opt in) |
| `TEAMMEETINGAI_GROQ_MODEL` | blank (required when the backend is `groq`) |
| `TEAMMEETINGAI_GROQ_TIMEOUT` | `60` seconds |
| `TEAMMEETINGAI_GROQ_API_KEY` | blank (required when the backend is `groq`) |
| `TEAMMEETINGAI_OLLAMA_HOST` | `http://localhost:11434` (reserved for a future backend) |
| `TEAMMEETINGAI_OLLAMA_MODEL` | `qwen3:4b` (reserved for a future backend) |
| `TEAMMEETINGAI_LLM_TEMPERATURE` | `0.2` |
| `TEAMMEETINGAI_LLM_MAX_TRANSCRIPT_CHARS` | `40000` |

The Ollama settings are configuration provisions only. The `ollama` analysis
backend is deliberately not supported in this build and reports a clear error
if selected.

## AI providers and backends

Transcription always runs locally through `ai/faster_whisper_provider.py`,
which implements the `TranscriptionProvider` port in `ai/transcription.py`.
The model is loaded lazily on the first transcription, reused afterwards, and
supports either a Hugging Face model size or a local model directory via
`TEAMMEETINGAI_WHISPER_MODEL`.

Meeting analysis is selected at startup with
`TEAMMEETINGAI_ANALYSIS_BACKEND` (`ai/provider_factory.py`):

| Backend | Behaviour |
| --- | --- |
| `none` (default) | No analysis. A processed meeting stops at status `transcribed`. |
| `groq` | Sends the transcript (never the audio) to the Groq API. Requires `TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS=1`, `TEAMMEETINGAI_GROQ_MODEL`, and `TEAMMEETINGAI_GROQ_API_KEY`. |
| `ollama` | Reserved for a future local backend; reports a clear unsupported error. |

When the Groq backend is active the details screen shows a persistent warning
that the transcript leaves the computer, and the status bar replaces the
"all data stays on this computer" message. The API key is read from the
environment only; it is never stored in `Settings`, meeting records, logs, or
the UI. Unknown/invalid keys, rate limits, unavailable models, and network
timeouts are mapped to clear user-facing errors.

### Alternative providers

Both ports remain open for custom adapters:

```python
class TranscriptionProvider(Protocol):
    def transcribe(self, audio_path: Path, *, language=None, progress_cb=None
    ) -> TranscriptionResult: ...

class AnalysisProvider(Protocol):
    def analyze(self, *, system_prompt: str, user_prompt: str
    ) -> str | Mapping[str, Any]: ...
```

Pass instances into `MainWindow(transcription_provider=..., analysis_provider=...)`
in `main.py`; explicit providers override the environment-selected backend.

The expected analysis schema is:

```json
{
  "overview": "Short meeting summary",
  "key_points": [],
  "decisions": [],
  "action_items": [
    {
      "task": "Task description",
      "owner": null,
      "deadline": null,
      "evidence": "Relevant transcript excerpt or timestamp"
    }
  ],
  "open_questions": [],
  "risks_or_blockers": [],
  "follow_ups": []
}
```

The analysis prompt instructs the model that the transcript is untrusted data,
forbids inventing owners or deadlines, and the response is validated against
the schema in `ai/task_extractor.py` (with one retry) before anything is
stored.

## Architecture

```text
main.py                  Startup, library setup, window wiring, processing worker
config.py                Paths and application defaults
audio/
    microphone.py        Microphone discovery and capture
    system_audio.py      WASAPI loopback discovery and capture
    recorder.py          Recording orchestration and WAV finalization
ai/
    transcription.py     Transcription service + provider port
    faster_whisper_provider.py  Local Faster-Whisper transcription adapter
    groq_provider.py     Groq cloud analysis adapter (opt-in)
    provider_factory.py  Analysis-backend selection and configuration errors
    summarization.py     Prompting, provider port, response validation entry point
    task_extractor.py    JSON parsing and schema validation
storage/
    library.py           File-backed meeting library (records, search, markdown)
    migration.py         One-time legacy SQLite -> library conversion
ui/
    login.py             Local profile selection
    dashboard.py         Meeting list and search
    recorder.py          Manual recording controls
    meeting_details.py   Playback, transcript, analysis, action items
    library_setup.py     First-run library directory dialog
```

Audio and AI work runs on background threads. UI code never touches the
library directly — it goes through the small `ops.*` function API in
`storage/library.py`, which is safe to call from worker threads.

## Privacy and consent

- Recording is manual and requires the user to click Start.
- A persistent recording indicator and elapsed timer are displayed.
- The app reminds users to obtain consent and follow organizational policy.
- There is no telemetry, analytics, crash upload.
- Audio capture, transcription, and storage are fully local. Recording audio
  never leaves the computer.
- By default (analysis backend `none`) nothing is sent to any external service.
- When cloud analysis is explicitly enabled, the meeting transcript is sent to
  the configured provider (Groq) and the UI warns about this persistently. The
  API key is kept in an environment variable and is never logged or stored.
- Logs contain operational events and meeting IDs, not transcript content,
  audio, passwords, or tokens.
- Transcripts are treated as untrusted data. Spoken instructions in a meeting
  cannot cause the application to execute commands or access files.
- Model output is validated before storage.

## Meeting record and statuses

There is no database. `meeting.json` in each meeting folder holds the full
record: profile, title, status, timestamps, recording metadata, the transcript
with segments, the summary, and the action items. Markdown documents are
regenerated from it on every save.

Meeting status values:

```text
recording -> recorded -> transcribing -> analyzing -> completed
                                    |            \-> failed
                                    \-> transcribed (no analysis backend)
                                    \-> failed
```

## Troubleshooting

### No microphone or speaker devices

- Confirm Windows can see the devices in Sound settings.
- Check that the meeting app is playing through the selected playback device.
- Click **Refresh devices**.
- Install the requirements in the active virtual environment. SoundCard must be
  able to access the Windows audio backend.

### A recording has no system audio

- Select the same speaker/headset that Google Meet uses.
- Avoid switching playback devices during a recording.
- Bluetooth headsets sometimes expose multiple endpoints; try the active one.

### Echo or feedback

- Use headphones when possible. Capturing system audio through speakers can
  also capture the microphone output depending on the setup.

### Recording file missing

The details screen keeps the transcript and analysis readable and disables
playback/transcription when the audio file has been moved or deleted. If you
moved the whole library folder, update the library location (or set
`TEAMMEETINGAI_LIBRARY_DIR`) so the app points at the new place.

### Processing says no provider is configured

Transcription uses the Faster-Whisper package; if it is missing, install the
requirements into the active virtual environment. Analysis is unavailable by
default (`TEAMMEETINGAI_ANALYSIS_BACKEND=none`); meetings then stop at status
`transcribed`, which is expected. To enable analysis, set the backend to
`groq` with the opt-in flag, model, and API key as described above.

### Analysis backend misconfigured at startup

If `TEAMMEETINGAI_ANALYSIS_BACKEND` names an unsupported backend or the Groq
backend is enabled without the opt-in flag, the app refuses to start and shows
a configuration error instead of failing later during a meeting.

### First transcription is slow or fails to download the model

The first run downloads Whisper weights from Hugging Face. A slow or blocked
connection shows a clear model-loading error; pre-provision the model by
pointing `TEAMMEETINGAI_WHISPER_MODEL` at a local model directory instead.

## Known limitations

- Windows-only audio capture.
- Echo cancellation is not implemented.
- Real speaker identification is not implemented: labels come from the audio
  tracks (`Me` = microphone, `Remote` = system audio), so a user's real name is
  never attached automatically, and speakers without headphones can bleed into
  both tracks.
- Transcription runs the microphone and system tracks sequentially, so it takes
  roughly twice as long as transcribing a single file.
- The `ollama` analysis backend is reserved but not wired; no local-LLM
  analysis is available in this build.
- No automatic recording, calendar integration, cloud sync, or meeting bot.
- Action-item status changes are stored only in `meeting.json` (and mirrored
  into `analysis.md`); editing `transcript.md`/`analysis.md` by hand is not
  read back by the app.
- Packaging (PyInstaller) is the next step.
