# Changelog

All notable changes to OpenShorts are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — 2026-07-12

### Added

- **Faktenbasierte Smart-Layout-Planung**: Die Szenenanalyse kombiniert jetzt MediaPipe-Gesichter mit YOLO-Personenzählungen und dauerabhängigem Sampling. Stabile Einzelpersonen-Szenen bekommen einen ruhigen Zoom, stabile Zwei-Personen-Szenen den festen Split im Opus-Stil, Gruppen oder unsichere Szenen bleiben in der Weitwinkel-Ansicht. Die Logs zeigen pro Szene Zeitbereich, Personenzahl, Konfidenz und die Zahl künstlicher Kamerawechsel.
- **Safe-Bounce-Untertitel**: Kurze Wörter wechseln nur die Highlight-Farbe; die Skalierungs-Animation läuft nur, wenn das Wort lang genug eingeblendet ist, um sie sauber abzuschließen.
- Regressionstests für die echte Clip-10-Layout-Planung, die HOLD/LOST-Tracker-Semantik, Hochkant-/Quadrat-/Original-Geometrie, die Legacy-Layer-Migration, Untertitel-Timing, H.264-Ausgabe bei ungeraden Auflösungen und die Wiederherstellung kompletter Renderings.

### Changed

- **Klare Ausgabeformate**: 9:16 ist der explizite Standard, Original und 1:1 sind Alternativen. Alte `auto`- und `horizontal`-Werte bleiben kompatibel. Speaker-Zoom und Weitwinkel bleiben unter „Erweitert" verfügbar, Smart ist die empfohlene Voreinstellung.
- **Clip-Layer-Zustand v2**: Pro logischem Clip werden eine saubere Quelle und ein zusammengesetztes Rendering verwaltet. Auto-Edit arbeitet auf sauberen Pixeln und legt Untertitel-/Hook-Ebenen genau einmal wieder auf; eine Übersetzung entfernt veraltete Sprach-Untertitel und behält den Hook.
- Alle H.264-Re-Encodes erzeugen breit kompatibles `yuv420p` und polstern ungerade Quellauflösungen sicher auf gerade Achsen auf.

### Fixed

- **Gemini-Code-Review-Befunde aus PR #1** (beide bestätigt und behoben):
  - **Untertitel-Position „Oben" landete rechts statt oben**: Der SRT-Burn-Pfad (`build_subtitle_filter`) übersetzte `top` in ASS-Alignment-Code 6 — in ASS v4.00+ bedeutet das „Mitte rechts". Jetzt wird Code 8 (oben zentriert) verwendet, identisch mit dem Karaoke-Pfad (`generate_ass`). *(EN: `top` subtitle alignment mapped to ASS code 6 = middle right in v4.00+; now 8 = top center, consistent with `generate_ass`.)*
  - **Lost Update bei parallelen Clip-Operationen desselben Jobs**: `clip_layers.json` und `metadata.json` gelten für den ganzen Job, gesperrt wurde aber nur pro Clip. Liefen zwei Clips parallel (z. B. Auto-Edit auf Clip 0, Untertitel auf Clip 1), überschrieb die zuletzt fertige Operation den gespeicherten Stand der anderen. Neu: Der Clip-Lock bleibt über den gesamten Vorgang bestehen und FFmpeg-Encodes verschiedener Clips laufen weiterhin parallel — aber jede Lese-/Schreibsequenz auf den job-weiten Dateien wird durch einen kurzen Job-Lock serialisiert: frisch laden, nur den eigenen `clips[clip_index]`-Eintrag mergen, alle anderen Clip- und Legacy-Einträge unverändert erhalten, atomar speichern. Die v1→v2-Migration wird unter demselben Lock sofort persistiert, und `metadata.json` wird vor dem Schreiben stets frisch von der Platte gelesen statt einem Snapshot von vor dem minutenlangen Encode zu vertrauen. *(EN: job-wide state files were guarded only by per-clip locks, so concurrent clip operations lost each other's updates; all read-modify-write cycles on `clip_layers.json` and `metadata.json` are now serialized by a short job-level lock that merges only the operation's own clip entry.)*
  - 5 neue Regressionstests: ASS-Alignment-Mapping beider Burn-Pfade, paralleler Zwei-Clip-Commit ohne Datenverlust, sofortige Migrations-Persistenz mit Kopiersemantik.
- **Review-Nacharbeiten**: Layer-tragende Legacy-Edits/-Übersetzungen werden vor dem Original wiederhergestellt, jeder nicht migrierte v1-Clip-Eintrag bleibt erhalten, unterbrochene Ersatzsprecher-Erkennungen starten ihren Stabilisierungs-Timer neu, Quadrat-/Original-Fallback-Renderings funktionieren auch ohne Metadaten, und erzwungene Weitwinkel-/Zoom-Layouts überspringen Detektor-Arbeit, die ihr Ergebnis nicht beeinflussen kann.
- **Stabiler Smart-Split**: Lange bzw. hochsichere Split-Szenen werden nicht mehr von benachbarten Layouts überschrieben; der HOLD-Zustand des Trackers kann nicht mehr vom YOLO-Fallback umgangen werden; Tracker-Schwellen rechnen in Sekunden statt in angenommenen Frame-Zahlen; der Tracker-Zustand wird an echten Szenenschnitten zurückgesetzt.
- **Untertitel-Flackern und -Duplikate**: Wort-Timelines werden sortiert, begrenzt und dedupliziert, ASS-Centisekunden-Überträge und angrenzende Wortgrenzen sind exakt, überlappende Whisper-Wörter können nicht mehr über Blöcke hinweg kollidieren, und Untertitel-/Hook-/Edit-/Übersetzungs-Abläufe rendern Präsentationsebenen immer genau einmal von der sauberen Quelle.
- **Format-Geometrie**: Zwei-Achsen-Crops verhindern Verzerrungen von Hochkant zu Quadrat, die Weitwinkel-Ansicht nutzt Cover/Contain-Geometrie ohne negative Ausschnitte, und quadratische Zwei-Personen-Layouts rendern nebeneinander.
- **Auto-Edit-Übergänge**: Aneinandergrenzende Zoom-Effekte nutzen überlappungsfreie Frame-Bereiche, und `zoom_in` gleitet zum Basis-Ausschnitt zurück statt am letzten Frame zu springen.
- **GitHub-Actions-Backend-Tests**: Die CI installiert jetzt das vollständige leichte Test-Abhängigkeits-Set, sodass Anwendungs-Regressionstests das FastAPI-Backend importieren können, ohne den großen Video-/ML-Stack zu ziehen. Drei veraltete manuelle Hook-Prüfskripte entfernt; die gepflegte automatische pytest-Suite bleibt bestehen.
- **Persistente Clip-Versionen**: Auto-Edit aktualisiert In-Memory-Ergebnis, Metadaten und `job_state.json` jetzt atomar — genau wie Untertitel, Hooks und Übersetzungen. Aktualisieren, ZIP-Download, Fortsetzen und Social-Publishing verwenden exakt die im UI angezeigte Version.
- **Sicheres Fortsetzen hängender Jobs**: Ein Worker mit ausgebliebenem Heartbeat wird beendet, bevor er einen Queue-Slot blockieren oder mit einem fortgesetzten Worker kollidieren kann. Execution-IDs verhindern, dass ein alter Supervisor den neuen Warteschlangen-Status überschreibt, und beim Fortsetzen wählt das Backend den richtigen Checkpoint statt immer die Gemini-Analyse zu erzwingen.
- **Verifizierter Abschluss**: Worker-Zusammenfassungen setzen den Status nicht mehr zu früh auf `completed`. Der Log-Stream wird vollständig geleert und Metadaten-/Video-Artefakte werden geprüft, bevor der finale Status veröffentlicht wird oder das S3-Backup startet.
- **Kollisionsfreie Clip-Werkzeuge**: Auto-Edit, Untertitel, Hooks und Übersetzung werden pro Clip serialisiert und verwenden eindeutige Temp-/Ausgabenamen. Parallele Anfragen können sich nicht mehr gegenseitig Dateien überschreiben.
- **Anfrage-Validierung und Fehler**: Negative Clip-Indizes werden bei allen Clip-Operationen abgelehnt, erwartete HTTP-Fehler behalten ihren korrekten Status, und unerwartete Fehler liefern stabile Nutzermeldungen, während technische Details in den Server-Logs bleiben.
- **Nicht blockierender Thumbnail-Download**: Der direkte Thumbnail-Studio-URL-Pfad führt yt-dlp jetzt außerhalb des API-Event-Loops aus.
- **Neustart-Wiederherstellung**: Thumbnail-Studio-Sitzungen, Thumbnail-Publish-Jobs und SaaS-Generierungsjobs werden atomar gespeichert und nach einem Server-Neustart wiederhergestellt. Unterbrochene externe Jobs melden einen expliziten, behebbaren Fehler statt zu verschwinden.
- **Verbindungs-Wiederherstellung**: Temporäre Fehler beim Status-Polling machen aus einem möglicherweise laufenden Server-Job keinen lokalen Fehlschlag mehr. Das Polling läuft sequentiell mit Backoff weiter und entfernt die Warnung nach der Wiederverbindung automatisch.
- **API-Key-Löschung**: Gemini-, Upload-Post-, ElevenLabs- und fal.ai-Schlüssel haben jetzt explizite Löschen-Buttons, und leere Werte werden aus `localStorage` entfernt statt nach dem Neuladen wieder aufzutauchen.
- 14 neue Regressionstests für Versions-Persistenz, negative Indizes, Abschluss-Reihenfolge, das Fortsetzen hängender Prozesse, die Wiederherstellung von Hilfsjobs, Thumbnail-Event-Loop-Sicherheit, Clip-Locks und erhaltene HTTP-Fehlerstatus.

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
