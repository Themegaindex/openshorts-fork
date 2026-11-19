# Independent Security Research, Development & Hardening of an Open-Source Fork

**Projekt:** [Themegaindex/openshorts-fork](https://github.com/Themegaindex/openshorts-fork), basierend auf [mutonby/openshorts](https://github.com/mutonby/openshorts). **Vergleichsdatum:** 5. November 2026. Dies ist eine belegte Darstellung eines erweiterten Forks und eines KI-unterstützten lokalen Security-Projekts, keine Behauptung, die ursprüngliche Plattform entwickelt zu haben.

## 1. Vergleichsstände und Methodik

| Stand | Fixierter Commit | Bedeutung |
| --- | --- | --- |
| Logische übernommene Ausgangsbasis | Upstream [`8f4bc54`](https://github.com/mutonby/openshorts/commit/8f4bc542ce5bab8fdc55ce35836160484dffdd1f), im Fork `0ffe730ce12443e4fc2638113372bb1f18b05b96` | Beide haben denselben vollständigen Git-Tree `db0a251ea0adc16e8adf52996edd420937dd197c`; letzter übernommener Stand vor dem ersten Themegaindex-Entwicklungscommit |
| Veröffentlichter Fork / Audit-Basis | [`98afa9e`](https://github.com/Themegaindex/openshorts-fork/commit/98afa9ea5e9a842eb358834fe0cbd251d335573b), 2. November 2026 | Per GitHub-API und `git ls-remote` als Fork-`main` bestätigt |
| Aktueller Upstream-Snapshot | [`8945140`](https://github.com/mutonby/openshorts/commit/894514049a3c6210a7cd65676344f44371b3717a), 5. November 2026 | Per GitHub-API geprüft und in lokale Git-Objekte geladen; in dieser Untersuchung weder ausgecheckt noch als Anwendung gestartet |
| Lokale Arbeitskopie | Fork-Basis plus uncommittetes Audit-Hardening | Enthält `security.py`, `security_http.py`, Security-Tests, Dependency-Updates und Berichte. Diese Änderungen sind **noch nicht Bestandteil des veröffentlichten Fork-Commits**. |

Untersucht wurden Abstammung, Patch-Äquivalenzen, Dateidifferenzen, tatsächliche Moduldefinitionen, ausgewählte sicherheitsrelevante Upstream-Patches und die vorhandenen Audit-Belege. Die vollständigen Git-Daten liegen in [comparison.json](fork-evidence/comparison.json), die überprüften öffentlichen Referenzen in [github-provenance.json](fork-evidence/github-provenance.json). Es gab keinen Merge, Push oder Eingriff in fremde Systeme.

Der Graph-Merge-Base ist `d44dc3ed006cef7cf6895fe4849df1aeacfdb192` vom 23. Januar 2026. Die voneinander abweichenden Commit-IDs verbergen jedoch eine spätere identische Ausgangsbasis. Deshalb wird der tatsächliche Entwicklungsbeitrag gegen `0ffe730`/`8f4bc54` gemessen, nicht pauschal gegen den deutlich älteren Graph-Vorfahren.

## 2. Was die Historie tatsächlich belegt

| Messung | Nachgewiesenes Ergebnis | Einordnung |
| --- | ---: | --- |
| Nur auf Fork-Seite vorhandene Commit-IDs gegenüber aktuellem Upstream | 107 | **Keine** Behauptung von 107 eigenen Entwicklungsbeiträgen |
| Davon Autorname `juancarlos.cavero` | 17 | Übernommene Historie; alle 17 haben patch-äquivalente Upstream-Gegenstücke |
| Davon Autorname `Themegaindex` | 90 | 79 Nicht-Merge-Commits und 11 Merge-Commits, 8. Juli bis 2. November 2026 |
| Nur auf Upstream-Seite vorhandene Commit-IDs | 544 | Kein Beweis für 544 fehlende Funktionen oder Sicherheitslücken |
| Änderungen seit identischer übernommener Ausgangsbasis | 93 Dateien | 56 hinzugefügt, 34 verändert, 3 historische Verifikationsskripte entfernt |
| Git-Zeilenstatistik derselben Entwicklung | +29.505 / −2.882 | Einschließlich Tests, Dokumentation und generiertem Lockfile; keine Messung von Qualität, Aufwand oder ausschließlich manuell geschriebenem Code |
| Neue Python-Module im Root während dieser Entwicklung | 15 | Unten vollständig zugeordnet |
| Neue Einträge unter `tests/` im veröffentlichten Fork | 25 | 24 Testdateien und `conftest.py`; Dateien sind nicht mit Testfällen gleichzusetzen |

Die Autorinformationen in Git sind Metadaten. Sie stützen die Zuordnung zum Fork-Konto, beweisen aber weder alleinige Urheberschaft jedes Zeichens noch Arbeitsstunden oder eine vollständig manuelle Entwicklung. Lokales Audit und Dokumentation entstanden KI-unterstützt. Historische CI-/Testaussagen aus Commit- oder PR-Texten werden nicht als neue erfolgreiche Ausführung übernommen.

## 3. Übernommene Originalfunktionen

Bereits die identische März-Ausgangsbasis enthält Clip Generator, FastAPI-/React-Anwendung, YouTube-Downloads, automatische Hochkant-Neurahmung, Gemini-Analyse, ElevenLabs-Dubbing, YouTube Studio, UGC-/SaaS-Generierung, öffentliche Galerien, S3 und Upload-Post-Publishing. Diese Plattformfunktionen sind **keine vollständigen Neuentwicklungen des Fork-Inhabers**.

Belegbare Beispiele aus der übernommenen Historie:

- [`5c5db84`](https://github.com/Themegaindex/openshorts-fork/commit/5c5db84) von `juancarlos.cavero`: Dubbing und Projektsetup, einschließlich `translate.py`.
- [`bced8f3`](https://github.com/Themegaindex/openshorts-fork/commit/bced8f3): YouTube Studio, einschließlich `thumbnail.py` und `ThumbnailStudio.jsx`.
- [`5e490c2`](https://github.com/Themegaindex/openshorts-fork/commit/5e490c2) und [`00278b2`](https://github.com/Themegaindex/openshorts-fork/commit/00278b2): UGC-Pipeline, Low-Cost-Modus und Galerie, einschließlich `saasshorts.py` und `SaaShortsTab.jsx`.

Dass `saasshorts.py` seit der Ausgangsbasis verändert wurde, macht das gesamte Modul nicht zu einem eigenen ursprünglichen Beitrag. Gleiches gilt für die bestehenden UI-Komponenten und Anbieterintegrationen.

## 4. Nachweisbare Erweiterungen und erhebliche Änderungen

Die folgenden Beiträge sind durch tatsächliche Dateien und Commit-Diffs belegt. Sie beschreiben Entwicklung **in diesem Fork** und behaupten keine weltweite technische Neuheit. Aktueller Upstream hat teilweise vergleichbare oder daraus übernommene Funktionen.

| Bereich | Neue/erheblich veränderte Module | Konkreter Beitrag und Beleg |
| --- | --- | --- |
| Ausgabeformate und Wasserzeichen | Neu `video_formats.py`; erhebliche Änderungen an `main.py`, `app.py`, `MediaInput.jsx`, `ResultCard.jsx` | Auto/9:16/16:9/1:1, formatabhängige Geometrie, persistierte Renderkonfiguration, konfigurierbare Wasserzeichen: [`46a4102`](https://github.com/Themegaindex/openshorts-fork/commit/46a41024827414c04571dbcd029e72faef1f0423). Später exakte 1080×1920-/1080×1080-Ausgaben: [`61c64e1`](https://github.com/Themegaindex/openshorts-fork/commit/61c64e16c13b6525fcc3759db0ff86368e166a14). |
| Strukturierter Auto-Edit | Neu `edit_builder.py`; `editor.py` und zugehörige Tests überarbeitet | Modell liefert Edit-Entscheidungen; deterministischer Builder begrenzt Effekte/Zooms und schützt eingebrannte Captions. Ursprung [`46a4102`](https://github.com/Themegaindex/openshorts-fork/commit/46a41024827414c04571dbcd029e72faef1f0423). Der freie Reparatur-Fallback wurde erst im lokalen Security-Audit ebenfalls auf den Builder begrenzt. |
| Zweistufige Clip-Auswahl | Neu `clip_selection.py`, `gemini_worker.py`; erheblicher Umbau von `main.py` | Zeitfenster bewerten, Shortlist detaillieren, Schnitte an Wortgrenzen ausrichten, strukturierte Antworten und Kostenaufschlüsselung: [`46a4102`](https://github.com/Themegaindex/openshorts-fork/commit/46a41024827414c04571dbcd029e72faef1f0423), weitere Wiederherstellungs-/Fallbackkorrekturen in der späteren Historie. |
| Untertitel und Präsentationsebenen | `subtitles.py`, `hooks.py`, Subtitle-/Hook-UI erheblich verändert; neu `render_planning.py` | Karaoke, Presets, Effekte, einmaliges Rendern kombinierter Ebenen und wieder entfernbare Layer: [`3b3ed12`](https://github.com/Themegaindex/openshorts-fork/commit/3b3ed12e798a07d6dd79d2bf47a1059cc53fbcd9), [`32a8665`](https://github.com/Themegaindex/openshorts-fork/commit/32a8665ebb945cca14f2df93556ae14357cd6281), [`793d7a5`](https://github.com/Themegaindex/openshorts-fork/commit/793d7a56bd7095a992df11c00708086d882d7803). |
| Reframing und Szenenstabilität | Neu `speaker_tracking.py`, später `frame_sampler.py`; großer Umbau von `main.py` | Weniger Wechsel/Pendeln bei Sprecherwechseln, stabilisierte Layouts und zusammenhängendes Frame-Sampling: [`8404984`](https://github.com/Themegaindex/openshorts-fork/commit/8404984beed5233c31b2fc33b77372985b66fb96), [`5a2d65c`](https://github.com/Themegaindex/openshorts-fork/commit/5a2d65c574654489da213a2a4326deed8d6b3c41), [`3161921`](https://github.com/Themegaindex/openshorts-fork/commit/3161921). Keine neu gemessene Geschwindigkeitssteigerung wird hier behauptet. |
| Long Video / Smart-Auto | Neu `longform.py`, `media_audio.py`; Änderungen an Auswahl, Pipeline, API und UI | Zusätzliche Videomodi, Segment-/Reviewplanung, längere zusammengesetzte Ausgaben und passende Untertitel-Timelines: [`d341cdc`](https://github.com/Themegaindex/openshorts-fork/commit/d341cdc035d66ac28417383f64c5b357b6f4944d), [`7de35ea`](https://github.com/Themegaindex/openshorts-fork/commit/7de35eaa2854bcde03e2411757772283238b226f). |
| Job-Lebenszyklus und Parallelität | `app.py` erheblich verändert; neu `advisory_lock.py` nach vorheriger integrierter Lock-Lösung | Statuspersistenz, Versionen, Wiederaufnahme, Abbruch, Serialisierung von Clip-/Jobzuständen; Windows-Kill-on-close-Job-Objects: [`8229743`](https://github.com/Themegaindex/openshorts-fork/commit/8229743d47fe3d9d82e67cbefcc44389af9de868), [`95bea78`](https://github.com/Themegaindex/openshorts-fork/commit/95bea78529831937277967dd3d92ea76ea928540), [`c8202d8`](https://github.com/Themegaindex/openshorts-fork/commit/c8202d85627206cca670f8271f865658ff04aa05), spätere Modulextraktion in [`1cbe96f`](https://github.com/Themegaindex/openshorts-fork/commit/1cbe96fb2fe4f1c71c1856c51c0012a67581fe92). |
| ETA und Kalibrierung | `main.py`/UI geändert; neu `calibrate_eta.py` | Phasenbezogene und lernende Zeitabschätzung, lokales Kalibrierwerkzeug: [`ac39d4f`](https://github.com/Themegaindex/openshorts-fork/commit/ac39d4f), [`556928c`](https://github.com/Themegaindex/openshorts-fork/commit/556928c). Vorhandensein des Werkzeugs ist kein hier neu ausgeführter Benchmark. |
| Download-Sitzungen und Qualitätsprüfung | Neu `quality_probe.py`, später `youtube_download.py`; `main.py` geändert | Qualitätsabfrage, getrennte Auth-Versuche, optionaler Browserzugriff, Cookie-Snapshots im Speicher: [`46a4102`](https://github.com/Themegaindex/openshorts-fork/commit/46a41024827414c04571dbcd029e72faef1f0423), [`6ad8dd9`](https://github.com/Themegaindex/openshorts-fork/commit/6ad8dd9212d164f96ed159e6e631d0bc08293fcb). |
| GPU-/CPU-Laufzeit | Neu `whisper_runtime.py`, `video_encoding.py`; Pipeline und Tests geändert | Whisper mit GPU-/CPU-Fallback, NVENC mit CPU-Fallback, Encoder-/Decodierungsprüfung: [`ff36d96`](https://github.com/Themegaindex/openshorts-fork/commit/ff36d96542c72ff585f3853252b4b44305024b90), [`754e6af`](https://github.com/Themegaindex/openshorts-fork/commit/754e6afd58ad036d3390008ce436fa94d49c65b2). Reale GPU-Leistung bleibt außerhalb der hier ausgeführten Verifikation. |
| Test- und Entwicklungsinfrastruktur | `tests/`, leichte Test-Requirements, GitHub Actions, lokale Anweisungen/Skills, `start.bat`, Dokumentation | Regressionstests und CI-Konfiguration ab [`46a4102`](https://github.com/Themegaindex/openshorts-fork/commit/46a41024827414c04571dbcd029e72faef1f0423); begrenzte Issue-/Review-Automation [`bf534e7`](https://github.com/Themegaindex/openshorts-fork/commit/bf534e79444b2fe2c95eeeea54db288c8853b28e). Konfiguriertes CI ist kein Beweis für einen aktuellen grünen Lauf. |

Die 15 neuen Root-Module mit ihrem tatsächlichen Einführungscommit sind zusätzlich maschinenlesbar unter `new_root_modules_during_themegaindex_development` in [comparison.json](fork-evidence/comparison.json) aufgeführt. Besonders große Änderungen an `main.py` und `app.py` sind nachvollziehbar; die bestehenden Anwendungen und UI-Grundkomponenten bleiben übernommene und weiterentwickelte Arbeit.

## 5. Beiträge, die Upstream ausdrücklich übernommen hat

Der von `Themegaindex` eröffnete [Upstream-PR #50](https://github.com/mutonby/openshorts/pull/50) ist **geschlossen und nicht gemergt**. Es gibt keinen Merge-Commit dieses PRs. Die Aussage „PR #50 wurde gemergt“ wäre falsch.

Zwei spätere, auf aktuellem Upstream erreichbare Commits dokumentieren dagegen ausdrücklich eine angepasste Übernahme:

1. [`40151e1`](https://github.com/mutonby/openshorts/commit/40151e1708100327f647bdd190adcf4808838a60), 17. Juli 2026: übernimmt aus PR #50 Karaoke-Untertitel, deterministischen Auto-Edit, Emoji-Hooks, Retries und Tests; passt sie an das inzwischen veränderte Upstream an und führt Themegaindex als Co-Autor auf.
2. [`b3e4464`](https://github.com/mutonby/openshorts/commit/b3e44643cc1fd0cf3822f202726d451d2e0ef1e4), ebenfalls 17. Juli: nennt PR #50 als Quelle der zweistufigen Gemini-Auswahl mit Zeitfensterbewertung und Detailpass.

Zusätzlicher Codebeleg: `edit_builder.py` hat im Fork-Einführungscommit `46a4102` und im Upstream-Übernahmecommit `40151e1` exakt denselben Git-Blob `5e8c91d504b944965278d33182cf161422889231`. Das belegt eine konkrete Dateiübernahme zu diesem Zeitpunkt; es behauptet nicht, dass alle heutigen Upstream-Dateien identisch geblieben sind.

Eine belastbare Portfolio-Aussage lautet deshalb: **Entwicklungsbeiträge aus dem Fork wurden Upstream nachweislich übernommen und angepasst, obwohl der ursprüngliche PR selbst nicht gemergt wurde.** Sie sagt nichts über eine Übernahme sämtlicher Fork-Funktionen aus.

## 6. Tatsächlich gefundene und lokal korrigierte Schwachstellen

Die folgenden acht Anwendungslücken wurden im bereits durchgeführten Audit mit ungefährlichen lokalen Nachweisen bestätigt und für die dort getesteten Abläufe korrigiert. Es sind keine neu erfundenen CVEs und keine Behauptung zuvor unbekannter Zero-Days. Upstream hat verwandte Muster teilweise bereits früher korrigiert. Detaillierte Ursachen, Basis-Codezeilen, Fixes und Reproduktionsbefehle bleiben im [Auditbericht](SECURITY_AUDIT.md) und in [SECURITY_TESTING.md](SECURITY_TESTING.md) erhalten.

| ID | Einstufung / Kategorie | Tatsächliche lokale Korrektur | Historische Einordnung |
| --- | --- | --- | --- |
| S01 | High, CWE-22/73 | Speichergrenzen, ID-Prüfung, serverseitige Dateinamen, exklusive Upload-Dateierstellung, begrenzte Thumbnail-/Actor-Pfade | Gemischt: Thumbnail-Publish/-Generate sind übernommen; zusätzliche Jobzustände/IDs und deren Zugriffspfade wurden im Fork erweitert. |
| S02 | High, CWE-918 | Ziel-IP-Pinning für Website-/Mediendownloads, Redirect-Prüfung, öffentliche IPs, YouTube-Host-/Extractor-Grenze | Der ungeprüfte Scraper ist schon in `0ffe730:saasshorts.py:160` vorhanden. Die unten dokumentierte NAT64-Restlücke bleibt außerhalb der bisher bestandenen Regressionen offen. |
| S03 | High, CWE-79 | HTML-Text-/Attributkodierung, sichere URL-Schemes und JSON-LD-Serialisierung | Die unsicheren Galerie-Hashtags existieren bereits in `0ffe730:app.py:1770`; kein Beleg für eine vom Fork neu erfundene Galerie. |
| S04 | High, CWE-306/942 | Lokal-oder-Token-Zugriff, Host-/Origin-Prüfung, lokale Portbindungen | Übernommene API ohne Betreiber-Authentifizierung; der Fork ergänzt weitere Endpunkte und den Windows-Launcher. |
| S05 | Medium, CWE-200/552 | Medien-Allowlist statt Freigabe sämtlicher Ausgabedateien | Bereits in `0ffe730:app.py:181` wird der vollständige Ausgabeordner statisch gemountet. |
| S06 | Medium, CWE-400/770 | Begrenzte Upload-/Downloadstreams, Bodylimits, temporäre Dateien und weitere Timeouts | Vor allem übernommene Thumbnail-/Actor-/Downloadpfade mit späteren Änderungen. Kein pauschaler Nachweis, dass jede Variante vom Fork eingeführt wurde. |
| S07 | Medium, CWE-770 | 1–6 Actor-Optionen/Skripte, 30 Script-Segmente, Auftrags-/Anfragegrenzen und geteilte Parallelität | Unbegrenzte Optionen sind schon in `0ffe730:app.py:1505` und `0ffe730:saasshorts.py:724` nachweisbar. |
| S08 | Low, bedingte CWE-78 | Quotierter `start /D`-Pfad statt eingefügtem Verzeichnis in `cmd`-Text | `start.bat` wurde im Fork hinzugefügt. Ausnutzung setzt einen beeinflussbaren Checkout-Pfad und Start durch den Betreiber voraus. |

**Zusätzlich D01:** bekannte verwundbare Abhängigkeitsversionen, getrennt von den acht lokal nachgewiesenen Anwendungslücken. Die neue Toolchain und Python-Constraints beheben nicht automatisch die vorgefundene Runtime. Diese bleibt ein offener Befund.

Im Audit wurden 13 ausgewählte Sicherheitsassertions gegen den alten Fork-Commit tatsächlich ausgeführt und schlugen dort erwartungsgemäß fehl. Die korrigierte Security-Suite bestand mit **65 Fällen, einem Skip und keinem Fehler**. Die vollständige Suite bestand mit **632 Fällen, acht Skips und keinem Fehler**. Diese Zahlen stammen aus erhaltenen Ausführungsbelegen des vorherigen lokalen Audits; sie wurden nicht aus PR-Beschreibungen übernommen und in dieser Dokumentationserweiterung nicht erneut als neue Gesamtausführung ausgegeben.

## 7. Sicherheitsrelevanter Vergleich mit aktuellem Upstream

Upstream enthält inzwischen `security_utils.py`, `ssrf_guard.py`, `media_auth.py`, atomare JSON-Helfer sowie Cloud-Authentifizierung, Benutzerzuordnung und OAuth-/MCP-/Billing-Komponenten. Der Fork ist dagegen weiterhin eine BYOK-/Betreiberanwendung ohne diese Cloud-Architektur. Eine fehlende Datei bedeutet daher weder automatisch eine Lücke noch eine ohne Weiteres kompatible Korrektur.

| Schutzbereich | Geprüfter Upstream-Nachweis | Lokaler Fork nach Audit |
| --- | --- | --- |
| Öffentliche Downloadziele, interne IPs, Redirects | [`4bcdc36`](https://github.com/mutonby/openshorts/commit/4bcdc36a6f6c101297920a4c2876e16d2dd39470), [`cb522b3`](https://github.com/mutonby/openshorts/commit/cb522b3d53979c54bdd68c7be7e31bbdcb70296f) | Eigener, in Teilen entsprechender Schutz mit DNS-Pinning für die neuen HTTPX-Downloadhelper. Die genaue Abdeckung ist enger als die Upstream-Verbindungskontrolle. |
| Galerie-XSS | `4bcdc36` kodiert Galerie-Werte und verwirft ungeeignete URL-Schemes | S03 korrigiert dieselbe Problemklasse unabhängig im lokalen Renderer. |
| Vertrauliche Ausgabe-/Quelldateien | [`134ad54`](https://github.com/mutonby/openshorts/commit/134ad5431e68cb617ad9d967aa72d42f8f4e8175), [`87adf88`](https://github.com/mutonby/openshorts/commit/87adf88daaf8178bb8614bc4bbea90a1665e8257), [`de66923`](https://github.com/mutonby/openshorts/commit/de66923e7289d0a6ecac10b9bb7d8e2e6daee060) | JSON-/interne Dateitypen gesperrt und Betreiberzugriff begrenzt. Keine benutzerbezogenen signierten Freigabelinks oder Mandantenisolation. |
| Ressourcen und Aufträge | [`e441cae`](https://github.com/mutonby/openshorts/commit/e441cae7a91177fbccb0f529d602b89707fab18b) ergänzt unter anderem dauerhafte/benutzerbezogene Budgets | Lokale Größen-, Mengen- und Prozessgrenzen. Keine vergleichbare dauerhafte Kosten-/Benutzerabrechnung. |
| Secret-Minimierung und Protokolle | `4bcdc36` und [`b9aeaac`](https://github.com/mutonby/openshorts/commit/b9aeaac692dbe850ca06250339a03a3d7a01b44f) | Cookie-Inhalte werden bereits validiert und nicht ungeprüft ausgegeben; allgemeine Log-Redaktion und reduzierte Kindprozessumgebungen fehlen teilweise. |
| Atomare Metadaten | [`7e1d394`](https://github.com/mutonby/openshorts/commit/7e1d394e4406502063f746d6ed0345641661be3d) vom 4. November | API-Zustandsupdates nutzen bereits `_safe_write_json`; Worker-Metadaten über `_save_json_file` sind weiterhin nicht atomar. |

Die vollständigen SHAs und betroffenen Dateien stehen in [upstream-security-commits.json](fork-evidence/upstream-security-commits.json). Diese Tabelle ist ein gezielter Vergleich, kein vollständiger Security-Audit des heutigen Upstreams.

### Noch fehlende oder nur teilweise entsprechende Korrekturen

| Priorität für die Nacharbeit | Nachgewiesener Unterschied | Grenze des Nachweises / nächster Schritt |
| --- | --- | --- |
| Hoch: NAT64-Abdeckung prüfen | Upstream `4bcdc36` bewertet in `64:ff9b::/96` eingebettete IPv4-Adressen. Der lokale Helper akzeptiert derzeit `64:ff9b::7f00:1` und `64:ff9b::a00:1`, obwohl die eingebetteten IPv4-Ziele intern sind. | **Bestätigter Unterschied der Eingabepolicy**, keine behauptete erfolgreiche interne Verbindung. Ein SSRF-Pfad hängt von einer tatsächlich erreichbaren NAT64-Übersetzung ab; diese wurde nicht getestet. Als offener Restpunkt zu S02 behandeln. |
| Hoch: Egress-Abdeckung abgleichen | Upstream `cb522b3` prüft Verbindungen innerhalb von yt-dlp/Probes/Scrapes und prüft URL-Felder, die externen Downloadern übergeben werden. Der Fork prüft YouTube-Einstiegs-Hosts und seine HTTPX-Downloadhelper. | Kein gleichwertiger allgemeiner Connect-Time-Guard für yt-dlp/CDNs, FFmpeg oder SDKs nachgewiesen. Kein erfolgreicher Missbrauch eines echten Anbieters behauptet. Datenfluss und Unterprozesse vor einem kompatiblen Port prüfen. |
| Mittel: Credential-URL-Redaktion | Lokales `_make_log_entry` erhält einen künstlichen `user:password@host`-String unverändert; der Upstream-Scrubber entfernt diesen Teil. | Synthetischer lokaler Nachweis, kein tatsächlicher Secret-Fund. Eine zentrale Redaktionsgrenze vor Persistenz, Status und Supportlog fehlt. Bestehende Zugriffskontrolle ersetzt diese Minimierung nicht. |
| Mittel: Kindprozess-Secrets | Upstream baut über `child_env` eine reduzierte Worker-/Probe-Umgebung. Der Fork kopiert an den relevanten Stellen `os.environ`. | Zu breite Vererbung ist im Code belegt; es wurde kein echter Secret-Wert gelesen oder ausgegeben. Vor Änderung klären, welche Worker AWS-/Anbietervariablen wirklich benötigen. |
| Mittel: Worker-JSON atomar schreiben | Beim lokalen `_save_json_file` kann ein Leser während `json.dump` eine geleerte Zieldatei sehen; beim geprüften Upstream-Helfer bleibt der bisherige JSON-Inhalt lesbar. | Deterministische lokale Interleaving-Probe mit eigener Temp-Datei; keine echte Benutzerdatei verändert. Integritäts-/Verfügbarkeitsrestpunkt, nicht als zusätzlich behobene Schwachstelle gezählt. |
| Abhängig vom Betriebsmodell: Eigentümer, Freigaben und Budgets | Upstream ergänzt benutzerbezogene Medienfreigaben, private Actors, Quoten und OAuth-Scopes. Diese Architektur fehlt im Fork. | Für die dokumentierte Einzelbetreiber-Nutzung nicht pauschal eine neue Lücke. Für öffentlichen Mehrbenutzerbetrieb wesentliche offene Voraussetzungen. Cloud-Patches nicht als einfache Core-Cherry-Picks behandeln. |

Die ungefährlichen Vergleichsproben wurden tatsächlich ausgeführt: [targeted-probes.json](fork-evidence/targeted-probes.json), reproduzierbar mit [compare_fork_security.py](../tools/compare_fork_security.py). Keine echte Netzwerkverbindung und keine realen Zugangsdaten wurden dafür verwendet. Die Probe für malformed DNS zeigt unterschiedliche Fehlerklassen, aber **beide** liegen in der `ValueError`-Familie und brechen vor dem Zugriff ab. Daraus wird keine zusätzliche SSRF-Lücke erfunden. Der jüngste Upstream-IDNA-Fix [`d797e20`](https://github.com/mutonby/openshorts/commit/d797e20c4dafbda0e4ba1fe01eb2e185501a7b9a) ist deshalb vor allem ein Hinweis auf Fehlernormalisierung, kein hier nachgewiesener neuer Zugriffsbypass.

OAuth-/Login-/Social-Token-Patches wie `d97b809` oder `e049df9` betreffen Upstream-Funktionen, die der Fork in dieser Form nicht besitzt. Sie sind nicht einfach als fehlende verwundbare Funktionen des Forks aufzulisten.

### Abhängigkeitsvergleich

Ein neuer reiner Advisory-Abgleich des **fixierten aktuellen Upstream-Lockfiles** meldet 15 betroffene npm-Pakete: 11 High, 3 Moderate, 1 Low. Das lokale gehärtete Lockfile meldet im ebenfalls erneut ausgeführten Abgleich 0. Das veröffentlichte Fork-Lockfile hatte im vorigen Audit 13 betroffene Pakete. Quellen: [upstream-npm-audit.json](fork-evidence/upstream-npm-audit.json), [local-npm-audit.json](fork-evidence/local-npm-audit.json) und ursprüngliche Audit-Belege.

Das ist ein Versions-/Datenbankabgleich unterschiedlicher Abhängigkeitsbäume, kein Gesamt-Ranking der Sicherheit und kein Nachweis von 15 erfolgreich ausgenutzten Upstream-Lücken. Es wurden keine Upstream-Abhängigkeiten installiert und kein Upstream-Server angegriffen. Ein vollständiger Upstream-Python-/OS-/Container-Scan wurde nicht durchgeführt. Die vorhandene lokale Python-Runtime bleibt mit den zuvor gemeldeten 18 Paketen/163 Advisory-Einträgen offen.

## 8. Verifikation, offene Arbeit und Lizenz

Vor jeder Dokumentationsänderung wurden alle **69** vom vorherigen Audit-Manifest erfassten Datei-Hashes erfolgreich verglichen. Das bestätigt den Anschluss an genau den damaligen lokalen Stand. Unveränderte Produkt-/Testdateien und alte Belege werden abschließend erneut gegen diesen Stand geprüft; Dokumentationsänderungen werden separat ausgewiesen.

Neu in dieser Erweiterung tatsächlich ausgeführt: aktuelle Referenz-/PR-Abfragen, Git-Graph-/Tree-/Blob-/Patchvergleich, gezielte isolierte Helper-Proben, Upstream-/lokale npm-Advisory-Abfragen und Dokumentations-/Belegprüfung. **Nicht neu ausgeführt:** die vollständige Python-Suite, GPU-/Providerintegration, Docker-Builds oder Remote-CI. Die früheren erfolgreichen 632/65-Testläufe bleiben als solche erhalten. Docker, Runtime-Migration, echte Video-/GPU-Kompatibilität und die externe Claude-/Codex-Gatefolge bleiben ausstehend.

Die Vergleichslücken sind ein dokumentierter Nacharbeitsbestand und wurden hier nicht automatisch aus Upstream übernommen. Der heutige gezielte Vergleich erweitert die bekannten Restrisiken; er ist keine Behauptung, dass die ursprüngliche Untersuchung alle möglichen Randfälle abgedeckt habe. Die acht bisherigen Korrekturen und ihre bestandenen Regressionen bleiben nachprüfbar, insbesondere wird der Schutz von S02 nicht auf sämtliche IPv6-Übersetzungsnetze oder alle SDK-Verbindungen verallgemeinert.

`LICENSE` wurde nicht verändert. Fork, Arbeitskopie und aktuelles Upstream verwenden dort exakt denselben Blob `a5ad963b0d2f14faa6fb11625048155106bddb17` mit **Copyright (c) 2024 OpenShorts** und MIT-Text. Aktueller Upstream nennt in seinem [NOTICE](https://github.com/mutonby/openshorts/blob/894514049a3c6210a7cd65676344f44371b3717a/NOTICE) eine separate Lizenz für [`cloud/`](https://github.com/mutonby/openshorts/blob/894514049a3c6210a7cd65676344f44371b3717a/cloud/LICENSE). Diese Cloud-Komponenten wurden weder automatisch in den Fork kopiert noch als eigene Leistung ausgegeben. Bestehende Copyright-/Lizenzhinweise und frühere Dokumente bleiben erhalten; die ursprünglichen Audit-/Portfolio-Texte liegen zusätzlich unverändert im [Archiv](fork-evidence/README.md).

## 9. Reproduktion

Für einen **frischen, vollständigen Clone** zuerst den öffentlichen Fork und das Upstream-Remote einrichten. Diese drei Befehle sind die einmalige Einrichtung; keine Zugangsdaten sind erforderlich:

```powershell
git clone https://github.com/Themegaindex/openshorts-fork.git
cd openshorts-fork
git remote add upstream https://github.com/mutonby/openshorts.git
```

In einem bereits vorhandenen Clone nicht erneut klonen oder blind ein Remote überschreiben: `git remote get-url upstream` muss `https://github.com/mutonby/openshorts.git` zeigen. Nur wenn das Remote fehlt, den obigen `git remote add`-Befehl ausführen. `origin` zeigt in einem frischen Clone auf den öffentlichen Fork. Ein flacher Clone (`--depth`) reicht für die historischen Vergleiche nicht aus.

Anschließend von der Projektwurzel; der Fetch lädt den **fixierten Audit-Commit** statt eines künftig beweglichen `main` und führt keinen Checkout/Merge aus:

```powershell
git fetch --no-tags --no-write-fetch-head --no-recurse-submodules upstream 894514049a3c6210a7cd65676344f44371b3717a:refs/audit/security-20261105-upstream
git merge-base 98afa9ea5e9a842eb358834fe0cbd251d335573b 894514049a3c6210a7cd65676344f44371b3717a
git rev-list --left-right --count 98afa9ea5e9a842eb358834fe0cbd251d335573b...894514049a3c6210a7cd65676344f44371b3717a
git log --no-merges --format="%h %ad %an %s" --date=short 0ffe730ce12443e4fc2638113372bb1f18b05b96..98afa9ea5e9a842eb358834fe0cbd251d335573b
git diff --stat 0ffe730ce12443e4fc2638113372bb1f18b05b96 98afa9ea5e9a842eb358834fe0cbd251d335573b
git diff 0ffe730ce12443e4fc2638113372bb1f18b05b96 8f4bc542ce5bab8fdc55ce35836160484dffdd1f
git diff 46a41024827414c04571dbcd029e72faef1f0423:edit_builder.py 40151e1708100327f647bdd190adcf4808838a60:edit_builder.py
.\.venv-security\Scripts\python -X utf8 tools/compare_fork_security.py
```

Die beiden letzten `git diff`-Vergleiche müssen leer sein. Erwartete Git-Graph-Merge-Base: `d44dc3ed006cef7cf6895fe4849df1aeacfdb192`; die Links/Rechts-Zählung ist `107 544`. Die spätere, durch Patchvergleich belegte übernommene Entwicklungsspitze `0ffe730ce12443e4fc2638113372bb1f18b05b96` ist davon zu unterscheiden; siehe [Vergleichsbeleg](fork-evidence/comparison.json). Alle Zahlen beziehen sich auf fixierte SHAs. Für den Python-Befehl die separate `.venv-security` nach [SECURITY_TESTING.md](SECURITY_TESTING.md#umgebung-einrichten) einrichten; das Vergleichsskript gehört zum lokalen Hardening-Kandidaten und muss im geprüften Checkout vorhanden sein. Ein Clone eines früheren öffentlichen Commits enthält die noch unveröffentlichten Ergänzungen nicht automatisch.

Die erfolgreiche Wiederholung der Git-Einrichtung in einem neuen öffentlichen Clone sowie die zusätzliche Secret-Prüfung des zuvor ungetrackten Vergleichsskripts sind separat in [DOCUMENTATION_REVIEW.md](DOCUMENTATION_REVIEW.md) dokumentiert. Der genaue Zusatzbefehl steht in [SECURITY_TESTING.md](SECURITY_TESTING.md#zusatzscan-für-das-vergleichsskript).
