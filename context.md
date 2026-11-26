# TeamMeetingAI — Agent Context

Updated: 2026-11-25. This handoff is for a coding agent continuing the speaker-identification implementation. Read before changing code. Preserve existing work; do not commit or push unless asked.

## Application overview

TeamMeetingAI is a meeting recorder/analyzer with a React/Vite client, FastAPI/Python backend, and PostgreSQL. The root Dockerfile builds the frontend and serves its static output through the API. docker-compose.yml runs the unified app plus local PostgreSQL; the app may instead use Supabase through DATABASE_URL. Backend source is in Meeting_analyzer/; browser UI is in client/.

Recording uploads audio through the browser. The backend stores original audio and a derived 16 kHz mono WAV as PostgreSQL Large Objects. Transcription stores text and diarized segments in meeting_analyzer.transcripts. Background jobs are run by JobRunner and TranscriptionPipeline. The meeting details UI loads transcripts, jobs, speaker labels, and profiles through API calls. Profile names are rendered as overlays; original transcript segments must not be rewritten.

## Existing speaker schema and flow

The checked-in schema is plural and in meeting_analyzer. Migration 0002_speaker_profiles creates:

- speaker_profiles: UUID, unique display_name, timestamps.
- speaker_embeddings: one row per (transcript_id, local_label), JSONB vector, source speech seconds, meeting/transcript IDs, optional profile_id.
- speaker_assignments: one row per (transcript_id, local_label), optional profile_id, confidence, and status (suggested, confirmed, rejected).

No schema migration was added in this work. The models/migration describe expected schema; the live Supabase schema was not inspected. Verify it before assuming parity.

Profiling is opt-in via TEAMMEETINGAI_VOICE_PROFILING=1 and is queued after transcription; a manual API request can enqueue it for an existing transcript. The worker reads diarized segments and derived_audio_16k from PostgreSQL large-object storage, writes a temporary WAV, then calls the profile service. Resemblyzer preprocesses audio and computes segment embeddings, aggregated to one vector per diarized label subject to minimum speech and maximum segment settings. Confirmed profile embeddings are averaged into centroids, then cosine similarity is used to rank profiles. Manual confirmation links the meeting embedding to its profile for future matching. UI overlays confirmed names and displays local labels for Unknown speakers.

## Matching decisions implemented

- Profiling remains opt-in. The project .env had TEAMMEETINGAI_VOICE_PROFILING=1 when checked; never print secrets from .env.
- TEAMMEETINGAI_VOICE_MATCH_THRESHOLD defaults to 0.70.
- TEAMMEETINGAI_VOICE_MATCH_AMBIGUITY_MARGIN defaults to 0.05. A top match must exceed the runner-up by at least this margin; if there is no competitor, meeting threshold suffices.
- Clear high-score matches are persisted as confirmed, with the embedding linked to the profile. Low scores and close ties stay unassigned (Unknown).
- Confirmed and rejected user decisions must survive retries. Only embeddings joined to confirmed assignments are reference data.
- Reprofiling upserts one embedding row per transcript/label. Active profiling jobs should be reused instead of duplicated.
- Never edit source transcript segments. Profiling failures should not fail transcription.

## Workspace changes already made

- Meeting_analyzer/requirements.txt: adds resemblyzer>=0.1.4,<0.2 and setuptools<81 for pkg_resources compatibility.
- Root Dockerfile: adds build-essential and python3-dev because webrtcvad requires compilation.
- Meeting_analyzer/config.py: adds ambiguity-margin setting and environment parsing.
- Meeting_analyzer/services/speaker_profiles.py: automatic confirmed matching, ambiguity handling, embedding upserts, confirmed-only references, manual profile validation/linking.
- Meeting_analyzer/services/pipeline.py: reuses active profiling job on transcription-triggered enqueue.
- Meeting_analyzer/api/routers/speakers.py: returns an existing active profiling job when already queued/running.
- .env.example: documents profiling and thresholds.
- Meeting_analyzer/tests/test_speaker_api.py: updates/extends assertions for automatic match reuse, low-score Unknown, ambiguity, and retry preservation.
- CONTEXT_HANDOFF.md: shorter operational handoff.
- .commandcode/ is untracked and unrelated to this speaker change. Do not edit/delete without checking its owner/purpose.

## Verification state

Before Docker work, `pytest -q Meeting_analyzer/tests/test_speaker_profiles.py Meeting_analyzer/tests/test_speaker_api.py` reported 19 passed and 17 skipped. Skipped PostgreSQL integration tests mean persistence was not validated against a live test DB. `git diff --check` passed at that point.

The first `docker compose build app` failed compiling the webrtcvad native extension due to missing compiler tools. After adding build tools, the second build successfully installed Resemblyzer 0.1.4, setuptools 79.0.1, Torch 2.15.0, and built webrtcvad. BuildKit reached export/unpack, but the user interrupted the turn and the session is no longer available. Final image export, app recreation from that image, and in-container Resemblyzer runtime verification are unconfirmed.

Before that build finished, an app-only recreation from the old image was observed; tma-app stayed up and inspection showed Resemblyzer absent. Do not infer the running container now uses the new image. Docker daemon access intermittently returned Access is denied in this agent environment. The user explicitly authorized rebuilding/recreating only the relevant app service.

## Recommended next steps

1. Inspect git status/diff before editing; preserve all unrelated changes.
2. Retry Docker access. Confirm latest image build state, then recreate only app (never down or remove volumes):

   ```powershell
   docker compose up -d --no-deps --force-recreate app
   docker compose ps
   docker compose logs --tail 50 app
   ```

3. Verify import and encoder initialization in the actual app container:

   ```powershell
   docker compose exec -T app python -c "import resemblyzer, torch; from resemblyzer import VoiceEncoder; print('imports OK', torch.__version__); e=VoiceEncoder(verbose=False); print('encoder initialized')"
   ```

   Initialization may download pretrained weights. Report this separately. Avoid running a real profile job unless application DB writes are intended and authorized.
4. Check startup logs/health, rerun git diff --check and targeted tests. State clearly if integration tests remain skipped.
5. Review potential gaps before claiming completion: bounds validation for thresholds, meaningful poor-audio detection (current filter only screens too-short and zero-norm vectors), stale assignments after changed transcript/audio, API enqueue atomicity, retry UX/path, and calibration on labeled golden speaker data. No labeled speaker calibration set was implemented or verified; do not claim calibration.

## Safety and scope

- The root .env contains secrets. Never print values into tool output or this document.
- Supabase can be configured by DATABASE_URL; Compose also defines local db. Avoid migrations/profile jobs unless DB side effects are intended.
- Never run docker compose down -v, delete volumes, reset the DB, or discard workspace changes.
- Keep matching conservative: no identity assignment for low-confidence, ambiguous, too-short, or unusable audio.
