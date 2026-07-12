# Changelog

All notable changes to OpenShorts are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — 2026-07-12

### Added

- **Evidence-based Smart layout planning**: scene analysis now combines MediaPipe faces with YOLO people counts and duration-aware sampling. Stable single-person shots use a calm crop, stable two-person shots use a fixed Opus-style split, and groups or uncertain shots stay wide. Per-scene logs include time ranges, people counts, confidence and the number of artificial camera switches.
- **Safe Bounce captions**: short words only change highlight color; scale animation runs only when the event is long enough to complete cleanly.
- Regression coverage for real Clip-10 layout planning, HOLD/LOST tracking semantics, portrait/square/original geometry, legacy layer migration, subtitle timing, odd-size H.264 output and full-render recovery.

### Changed

- **Clear output formats**: 9:16 is the explicit default, with Original and 1:1 as alternatives. Legacy `auto` and `horizontal` job values remain compatible. Speaker Zoom and Wide remain available under Advanced while Smart is the recommended default.
- **Clip layer state v2** tracks one clean source and one composed render per logical clip. Auto Edit operates on clean pixels and reapplies subtitle/hook layers once; translation clears obsolete language captions while retaining hooks.
- All H.264 re-encode paths produce broadly compatible `yuv420p` output and safely pad odd source dimensions to even axes.

### Fixed

- **Gemini-Code-Review-Befunde aus PR #1** (beide bestätigt und behoben):
  - **Untertitel-Position „Oben" landete rechts statt oben**: Der SRT-Burn-Pfad (`build_subtitle_filter`) übersetzte `top` in ASS-Alignment-Code 6 — in ASS v4.00+ bedeutet das „Mitte rechts". Jetzt wird Code 8 (oben zentriert) verwendet, identisch mit dem Karaoke-Pfad (`generate_ass`). *(EN: `top` subtitle alignment mapped to ASS code 6 = middle right in v4.00+; now 8 = top center, consistent with `generate_ass`.)*
  - **Lost Update bei parallelen Clip-Operationen desselben Jobs**: `clip_layers.json` und `metadata.json` gelten für den ganzen Job, gesperrt wurde aber nur pro Clip. Liefen zwei Clips parallel (z. B. Auto-Edit auf Clip 0, Untertitel auf Clip 1), überschrieb die zuletzt fertige Operation den gespeicherten Stand der anderen. Neu: Der Clip-Lock bleibt über den gesamten Vorgang bestehen und FFmpeg-Encodes verschiedener Clips laufen weiterhin parallel — aber jede Lese-/Schreibsequenz auf den job-weiten Dateien wird durch einen kurzen Job-Lock serialisiert: frisch laden, nur den eigenen `clips[clip_index]`-Eintrag mergen, alle anderen Clip- und Legacy-Einträge unverändert erhalten, atomar speichern. Die v1→v2-Migration wird unter demselben Lock sofort persistiert, und `metadata.json` wird vor dem Schreiben stets frisch von der Platte gelesen statt einem Snapshot von vor dem minutenlangen Encode zu vertrauen. *(EN: job-wide state files were guarded only by per-clip locks, so concurrent clip operations lost each other's updates; all read-modify-write cycles on `clip_layers.json` and `metadata.json` are now serialized by a short job-level lock that merges only the operation's own clip entry.)*
  - 5 neue Regressionstests: ASS-Alignment-Mapping beider Burn-Pfade, paralleler Zwei-Clip-Commit ohne Datenverlust, sofortige Migrations-Persistenz mit Kopiersemantik.
- **Review follow-ups**: layered legacy edits/translations are recovered before the original, every unmigrated v1 clip entry is retained, interrupted replacement detections restart their stabilization timer, Square/Original fallback renders recover after missing metadata, and forced Wide/Zoom layouts skip detector work that cannot affect their result.
- **Smart Split stability**: long/high-confidence split scenes are no longer overwritten by neighboring layouts; detector HOLD can no longer be bypassed by YOLO fallback; tracker thresholds use seconds rather than assumed frame counts; tracker state resets at real source cuts.
- **Subtitle flicker and duplication**: word timelines are sorted, clipped and de-duplicated, ASS centisecond carry and adjacent boundaries are exact, overlapping Whisper words cannot overlap across blocks, and subtitle/hook/edit/translation sequences always render presentation layers once from the clean source.
- **Format geometry**: two-axis crops prevent portrait-to-square stretching, GENERAL uses cover/contain geometry without negative slices, and square two-person layouts render side by side.
- **Auto Edit transitions**: touching zoom effects use non-overlapping frame ranges and `zoom_in` eases back to the base crop instead of snapping at the final frame.
- **GitHub Actions backend tests**: CI now installs the complete lightweight test dependency set, so application-level regression tests can import the FastAPI backend without pulling in the large video/ML stack. Removed three obsolete manual hook verification scripts; the maintained automated pytest suite remains intact.
- **Persistent clip versions**: Auto Edit now updates the in-memory result, metadata and `job_state.json` atomically, matching subtitles, hooks and translations. Refresh, ZIP download, resume and social publishing now use the exact version shown in the UI.
- **Safe stalled-job resume**: a heartbeat-stalled worker is terminated before it can keep a queue slot or race a resumed worker. Execution IDs prevent an old supervisor from overwriting the new queued state, and resume now lets the backend select the correct checkpoint instead of always forcing Gemini analysis.
- **Verified completion**: worker summary events no longer expose `completed` early. The log stream is drained and metadata/video artifacts are validated before the final status is published or S3 backup starts.
- **Race-free clip tools**: Auto Edit, subtitles, hooks and translation are serialized per clip and use unique temporary/output names. Concurrent requests can no longer overwrite each other's files.
- **Request validation and errors**: negative clip indices are rejected for all clip operations, expected HTTP errors keep their correct status, and unexpected failures return stable user-facing messages while technical details remain in server logs.
- **Non-blocking thumbnail download**: the direct Thumbnail Studio URL path now runs yt-dlp outside the API event loop.
- **Restart recovery**: Thumbnail Studio sessions, thumbnail publish jobs and SaaS generation jobs are persisted atomically and recovered after a server restart. Interrupted external jobs return an explicit recoverable failure instead of disappearing.
- **Connection recovery**: temporary status-poll failures no longer turn a potentially running server job into a local failure. Polling continues sequentially with backoff and automatically clears the warning after reconnection.
- **API-key deletion**: Gemini, Upload-Post, ElevenLabs and fal.ai keys now have explicit delete controls and empty values are removed from `localStorage` instead of reappearing after reload.
- Added 14 regression tests for version persistence, negative indices, completion ordering, stalled-process resume, auxiliary-job recovery, thumbnail event-loop safety, per-clip operation locks and preserved HTTP error statuses.

## [2.0.0] — 2026-07-08

Major overhaul release: new output formats, watermarking, a completely reworked AI Auto-Edit,
modern karaoke subtitles, a reliability pass over the whole pipeline, and a test suite with CI.

### Added

**Output & Rendering**
- **Output format selection** (Opus-Clip style): choose **Auto (smart)**, **9:16** (Shorts/Reels with speaker tracking), **16:9** (original, no reframing) or **1:1** (square) when starting a job. Auto detects sources that already match the target aspect (e.g. phone footage) and passes them through untouched instead of re-cropping. The chosen format is persisted per job (`render_config.json`), so resumed jobs keep rendering identically.
- **Watermark**: a subtle, centered watermark (default: OpenShorts logo at ~8% opacity) is blended into every rendered clip directly inside the frame loop — zero extra encode passes. Fully configurable via `.env`: `WATERMARK_TEXT` (e.g. your channel name), `WATERMARK_IMAGE` (custom PNG), `WATERMARK_OPACITY`, `WATERMARK_WIDTH_FRACTION`, or `WATERMARK_ENABLED=0` to disable.

**Auto-Edit v2 (complete rework)**
- Gemini no longer writes raw FFmpeg filter strings. It now returns a structured **edit decision list** (what / when / how strong), and a deterministic builder (`edit_builder.py`) converts it into a guaranteed-valid filter chain.
- 7 effect types: `zoom_in`, `punch_in`, `zoom_pulse`, `color_pop`, `bw_moment`, `flash`, `vignette`.
- Hard safety limits: max zoom 1.15, no overlapping zooms, zoom window anchored with facial headroom, max 12 edits, max 2 flashes.
- **Caption-safe editing**: if the clip already has burned-in subtitles or a hook, all zoom effects are automatically blocked so text always stays visible.
- 2-second FFmpeg dry-run before the full encode, with one Gemini self-repair round-trip on failure.

**Subtitles**
- Modern **karaoke captions** with word-level highlighting (active word in a custom color).
- **11 preset looks**: TikTok, Reels, Shorts Pop, Gold Glow, Neon, Cyber, Karaoke, Minimal, Beast, Boxed, Classic.
- Visual effects per preset: **glow**, **pop** (scale-in per word), **box**; plus dim control for non-active words, UPPERCASE toggle, font size slider (14–40), custom fonts and colors.
- **Bulk subtitles**: configure a style once and apply it to all clips of a job in one click.
- **Download all clips as ZIP.**

**Gemini pipeline**
- Two-stage analysis: transcript windows are scored first, then only the shortlist gets the expensive detail pass — better clips at lower cost.
- Structured output via Pydantic `response_schema` (no more JSON-parse failures), split temperatures per strategy, word-boundary snapping of clip cuts, transcript-language enforcement for all generated text, viral hook playbook, hashtag & diversity rules.
- Models configurable per task via `.env` (`GEMINI_MODEL`, `GEMINI_MODEL_ANALYSIS`, `GEMINI_MODEL_EDITOR`, `GEMINI_MODEL_THUMBNAIL`, `GEMINI_MODEL_IMAGE`, `GEMINI_MODEL_SAAS`), optional thinking control (`GEMINI_THINKING_SCORE`), full cost tracking including thinking tokens.
- Editor video uploads use `media_resolution=low` (~70 tokens/frame) — large video-input cost cut with no quality loss for edit decisions.

**Reliability & job management**
- **Pre-flight quality gate**: before a job starts, a fast probe (`quality_probe.py`) checks which resolution YouTube actually offers. If it is below `QUALITY_GATE_MIN_HEIGHT` (default 720p), the UI shows a popup — process anyway or fix cookies first (with step-by-step incognito export instructions) — instead of silently burning 20+ minutes on a 360p source.
- **Self-learning ETA**: each phase's real duration per video-second is persisted (`.phase_stats.json`, median of last 10 jobs), the current phase is extrapolated from its live measured rate, and a one-time total estimate ("Estimated total processing time: ~X min") is announced right after download and shown in the dashboard.
- **Clean stop/cancel**: cancel endpoint kills worker processes server-side and frees the queue slot; recovery banner offers resume-or-restart choice after crashes.
- **Keepalive heartbeat** thread and **Windows standby prevention** (`SetThreadExecutionState`) — long silent transcriptions no longer trigger false "stalled" states or freeze when the laptop sleeps.
- **Resume hardening**: jobs that died mid-transcription (no checkpoints yet) now resume from the source video instead of crashing; resume resets the elapsed clock.
- One-time notice (instead of a repeating alert) when the Upload-Post key has no profiles yet.

**Project infrastructure**
- Test suite: **63 pytest tests** covering subtitle engine, clip selection/snapping, edit builder, hooks and translation helpers.
- **GitHub Actions CI**: backend tests, frontend lint + build, Docker build.
- `start.bat` for local Windows startup without Docker.
- `.env.example` documenting all new configuration (watermark, Whisper, Gemini models/thinking, quality gate).

### Changed
- **yt-dlp**: removed all hardcoded `player_client` overrides — yt-dlp's curated default clients are the ones that still serve HD without PO tokens. Restores up to 4K downloads (verified 2160p) where only 360p was available before.
- Whisper default model is now `small` (noticeably better German than `base`), with tuned transcription parameters (`beam_size=5`, VAD filter, no cross-segment conditioning against hallucinations); configurable via `WHISPER_MODEL` / `WHISPER_DEVICE` / `WHISPER_COMPUTE`.
- Recommended default analysis model: `gemini-3.1-flash-lite` (cheaper **and** stronger than 2.5 Flash).
- `-movflags +faststart` on all rendered outputs — downloaded clips play instantly everywhere.
- Frontend performance: `React.lazy` code splitting (initial JS 372 KB → 245 KB), GZip on API responses (status polling 249 KB → 30 KB), optimized logo (266 KB → 7 KB), reduced polling churn and log payloads.

### Fixed
- Subtitle word-gluing ("ichhabe" instead of "ich habe"): whisper continuation fragments are now merged into the previous word using the leading-space token signal.
- Gray/muddy dimmed subtitle text: semi-transparent fill blended with the libass outline; replaced with fully opaque RGB dimming — colors now match the preview exactly.
- Double subtitles when re-subtitling an already subtitled clip (prefix stripping now applies to both bulk and manual paths).
- Enlarged/fullscreen clip preview looked cropped and wrong: `object-cover` was cutting the video both in the card and in native fullscreen; previews now letterbox (`object-contain`) and a global CSS rule enforces contain in fullscreen. Works for all output formats.
- Resume crash ("Process failed with exit code 1") when a job died before any checkpoint was written.
- Runtime display counting frozen hours (e.g. "ETA 2m at 5h24m runtime") — elapsed clock resets on resume, ETA is now measurement-based.
- Zombie FFmpeg/worker processes after cancel or crash; encoder subprocess cleanup on aborted frame loops.
- Frontend memory leaks, polling races after completion, unbounded job-state growth, `datetime.utcnow` deprecations, missing `raise_for_status` checks, thumbnail upload size limits, title-slug collisions.
- Hook overlays: emoji rendering (color emoji font runs), text measurement, and positioning fixes.

### Security
- Color/font/number inputs sanitized before entering ASS subtitle files and FFmpeg filter strings (injection hardening).
- API keys remain client-side encrypted; YouTube cookies (`*_cookies.txt`), runtime stats and local notes are excluded from the repository via `.gitignore`.

---

## [1.x] — earlier

Initial platform: Clip Generator (Gemini viral-moment detection, dual-mode 9:16 reframing with
MediaPipe/YOLOv8 tracking), AI Shorts UGC pipeline, YouTube Studio (thumbnails/titles/descriptions),
ElevenLabs dubbing, Upload-Post social publishing, S3 backup, Docker setup.
