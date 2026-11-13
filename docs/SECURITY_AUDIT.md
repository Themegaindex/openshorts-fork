# Security Audit & Hardening – OpenShorts

**Datum:** 5. November 2026 · **Art:** lokaler, vom Repository-Inhaber beauftragter und KI-unterstützter White-Box-Audit.

## Executive Summary

**Status: implementiert, vollständige Verifikation noch offen.** Acht bestätigte Anwendungsschwachstellen wurden lokal behoben: **0 Critical, 4 High, 3 Medium, 1 Low**. Die abschließende Python-Suite besteht mit **632 bestandenen, 0 fehlgeschlagenen und 8 übersprungenen Tests**. Davon sind **65 bestandene und ein übersprungener Security-Test** neu. Frontend-Lint und Build bestehen. Die ursprünglichen 13 Security-Assertions schlagen am unveränderten Git-Ausgangscode nachweisbar fehl.

Abhängigkeitsscans werden getrennt gezählt: Das ursprüngliche npm-Lockfile meldete 13 betroffene Pakete; das aktualisierte Lockfile meldet null. In der vorhandenen Python-`venv` meldet pip-audit **163 Advisory-Einträge bei 18 von 99 installierten Paketen**. Diese Umgebung wurde aus Kompatibilitätsgründen nicht verändert. Neue Python-Constraints und eine frische Audit-/Testumgebung reduzieren das Risiko für Neuinstallationen; deren Scan ist ohne bekannte Advisories. Das ist **kein** Nachweis, dass die vorhandene Video-/GPU-Laufzeit aktualisiert oder vollständig kompatibel ist.

Docker fehlt lokal. Docker-Builds, Remote-CI sowie die vom Repository vorgesehene externe Claude-/Codex-Reviewkette wurden nicht ausgeführt. Bandit endet weiterhin mit Exitcode 1 wegen zweier Aufrufe zu festen HTTPS-Downloadquellen; diese sind unten bewertet. Es gibt keine Zertifizierung, unabhängige externe Bestätigung oder Zusage vollständiger Sicherheit.

## Scope und Ausgangszustand

Auditiert wurde das aktuell geöffnete Repository **OpenShorts**; „Openshore“ und „Video-Cutter“ aus dem Auftrag werden als Bezeichnungen dieses Projekts verstanden.

- Basis: `98afa9ea5e9a842eb358834fe0cbd251d335573b`.
- Der Working Tree war anfangs **nicht sauber**: `AGENTS.md` war geändert, `.claude/settings.local.json` war ungetrackt. Beides wurde erhalten und gehört nicht zum Hardening-Diff. Die untersuchten Produktquellen entsprachen der Basis.
- Untersucht: die 24 ursprünglichen Python-Module im Projektroot; React-Komponenten und API-/Konfigurationscode; Requirements und npm-Lockfile; Windows-Launcher; Docker-/Compose-Dateien; GitHub-Workflows; relevante Tests und Git-Historie auf Secret-Signaturen. Hinzu kommen zwei neue Sicherheitsmodule.
- Nicht inhaltlich ausgewertet: echte `.env`, Cookies, private Uploads/Ergebnisse, Modellgewichte und Medienbestände. Keine fremden Systeme getestet, keine Veröffentlichungen, Commits, Pushes oder produktiven Änderungen ausgeführt.

| Bereich | Geprüfte Quellen und Grenzen |
| --- | --- |
| API, Persistenz und Auslieferung | `app.py`, Job-/Session-Zustände, Uploads, Downloads, JSON-/HTML-Ausgaben, Locks und Prozessregister |
| Download/Cloud | `youtube_download.py`, `quality_probe.py`, `s3_uploader.py`, HTTP-Aufrufe in `saasshorts.py`, Provider-Folgeantworten |
| Video/AI | `main.py`, `gemini_worker.py`, `editor.py`, `edit_builder.py`, `longform.py`, `render_planning.py`, `clip_selection.py` |
| Medienhilfen | `subtitles.py`, `hooks.py`, `thumbnail.py`, `translate.py`, `video_encoding.py`, `media_audio.py`, `speaker_tracking.py`, `frame_sampler.py`, `whisper_runtime.py`, `advisory_lock.py`, `calibrate_eta.py`, `video_formats.py` |
| Browser und Betrieb | Gesamter Dashboard-Quellbereich, API-Key-Speicherung, Galerie-Sinks, Vite-Proxy, `start.bat`, Docker und CI-Vertrauensgrenzen |

## Methodik und Tools

Die Bewertung folgt Vertrauensgrenzen, Datenfluss und lokalen Gegenbeispielen. Ein Finding setzt einen konkreten Eingabeweg und einen nachprüfbaren gefährlichen Effekt voraus. Varianten derselben Ursache werden zusammengefasst. Schweregrade berücksichtigen den ursprünglich netzwerkweit erreichbaren Dienst; die Low-Einstufung des Launchers nennt ausdrücklich dessen zusätzliche Voraussetzung. Es wurden keine CVSS-Zahlen geschätzt.

Orientierung: [OWASP SSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html), [File Upload](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html) und [OS Command Injection Defense](https://cheatsheetseries.owasp.org/cheatsheets/OS_Command_Injection_Defense_Cheat_Sheet.html); CWE-Kategorien sind pro Finding angegeben. Dies behauptet keine vollständige OWASP-/ASVS-Konformitätsprüfung.

| Werkzeug | Tatsächlich verwendeter Stand / Zweck |
| --- | --- |
| Git und ripgrep | Basisvergleich, vollständige Quellinventur, Sinks und Aufrufer |
| Python / pytest | Python 3.11.6 / pytest 9.2.0, isolierte `.venv-security` |
| HTTPX MockTransport / ASGITransport | Synthetische HTTP-Antworten und API-Clients; kein Netzwerk bei Security-Tests |
| FFmpeg / ffprobe | 8.0, synthetische lokale Video-Integrationstests |
| Node / npm | 24.14.0 / 11.9.0 lokal; CI nutzt Node 20, Vite 8 verlangt mindestens 20.19 oder 22.12 |
| Bandit | 1.9.4; alle Root-Produktmodule, Tests/Tools/venvs ausgeschlossen |
| pip-audit | 2.10.1; vorhandene Runtime-Metadaten, aufgelöste Test-Requirements und frische Tool-Umgebung |
| npm audit | Lockfile einschließlich Entwicklungspaketen |
| `tools/scan_secrets.py` | Sechs dokumentierte Signaturklassen; Fundstellen werden ohne Werte ausgegeben |

Paketinstallation und Advisory-Abfragen kontaktieren öffentliche Registries. Dabei werden Paketmetadaten, keine privaten Videos oder Quellbestände, übertragen. Scannergebnisse sind zeitabhängige Datenbankabgleiche, keine Beweise für die Erreichbarkeit jedes Advisorys im Anwendungscode.

## Bestätigte Anwendungsschwachstellen

Die **Fundstellen beziehen sich auf den Basis-Commit**, damit sie trotz Fixes dauerhaft überprüfbar bleiben (`git show <Basis>:<Datei>`). Die Fix-Spalte nennt die aktuellen Funktionen.

| ID | Schweregrad | CWE | Ursprüngliche Fundstelle | Korrektur |
| --- | --- | --- | --- | --- |
| S01 | High | [CWE-22](https://cwe.mitre.org/data/definitions/22.html), CWE-73 | `app.py:335`, `app.py:3929`, `app.py:4035`, `app.py:4648`; `thumbnail.py:194` | `confined_path`, ID-Validierung, serverseitige Uploadnamen, begrenzte Thumbnail-/Actor-Pfade |
| S02 | High | [CWE-918](https://cwe.mitre.org/data/definitions/918.html) | `saasshorts.py:167`, `saasshorts.py:221`, `saasshorts.py:687`; `app.py:4635` | `public_stream`, öffentliche gepinnte Zieladressen, Weiterleitungsprüfung; YouTube-Host-/Extractor-Grenze |
| S03 | High | CWE-79 | `app.py:4490`, `app.py:4491`, `app.py:4496`, `app.py:4542`, `app.py:4545` | `_html_url`, HTML-Kodierung, `_json_ld` statt JSON-Stringverkettung |
| S04 | High | CWE-306, CWE-942 | `app.py:1523`, `start.bat:10`, `docker-compose.yml:7` | Lokaler Peer oder Token, Host-/Origin-Prüfung, lokale Portbindungen |
| S05 | Medium | CWE-200, CWE-552 | `app.py:1537` | `MediaFiles` statt Veröffentlichung des gesamten Ausgabeordners |
| S06 | Medium | CWE-400, CWE-770 | `app.py:3805`, `app.py:3936`, `app.py:3941`, `app.py:4232`; `saasshorts.py:688` | Begrenzte Streams, Upload-/Bodylimits, sichere temporäre Dateien, Timeouts |
| S07 | Medium | CWE-770 | `app.py:4221`, `app.py:4277`; `saasshorts.py:754` | Mengenvalidierung, geteilte Parallelitätsgrenze, Auftrags-/Anfragelimits |
| S08 | Low, bedingt | CWE-78 | `start.bat:10`, `start.bat:17` | Quotierter `start /D`-Pfad statt Einfügen des Verzeichnisses in `cmd`-Programmtext |

### S01 – Fremde Dateipfade erreichen Lesen und Schreiben

**Ursache:** Ungeprüfte Job-/Session-IDs und übergebene Thumbnail-/Actor-Pfade werden mit Speicherverzeichnissen verbunden. Ein Join erzwingt keine Zugehörigkeit zu einem Verzeichnis. Absolute Windows-Pfade, `..` und alternative Separatoren umgehen diese Annahme.

**Auswirkung:** Der Thumbnail-Publish-Pfad konnte eine außerhalb von `output/thumbnails` liegende lesbare Datei als Upload-Anhang vormerken. Dafür genügte eine eigene Session und ein eigenes Upload-Post-Konto; das fremde Dateisystemziel war nicht auf die eigene hochgeladene Datei beschränkt. Session-Pfade konnten außerdem Ausgaben außerhalb des vorgesehenen Verzeichnisses anlegen. Unter Windows war auch die Status-Hydrierung über Backslash-Traversal erreichbar. Daraus wird keine bereits ausgeführte Secret-Exfiltration behauptet.

**Nachweis:** `test_publish_rejects_thumbnail_path_escape` legt ausschließlich einen eigenen Marker an und verwendet `../../marker.txt`; am Original wird dessen Upload als BackgroundTask akzeptiert. Der Task wird **nicht** ausgeführt. `test_thumbnail_rejects_traversal_session` ersetzt die Bildgenerierung durch einen lokalen Stub. `test_status_rejects_path_job_id` verwendet nur eine künstliche Statusdatei. Ergänzende Tests prüfen absolute Pfade, UNC-/Laufwerksformen, Backslashes, Symlinks und Actor-Pfade.

**Fix:** Pfadkomponenten und IDs prüfen, aufgelöste Speichergrenze kontrollieren, Uploadnamen aus UUIDs bilden, exklusiv erstellen und Publish-/Actor-Ziele auf ihre Medienbereiche begrenzen. Ein lokaler Benutzer mit denselben Schreibrechten bleibt außerhalb dieser Isolationsannahme.

### S02 – SSRF über erste URL und Weiterleitungen

**Ursache:** Website-Scraper und Mediendownloads akzeptieren beliebige HTTP-Ziele und verfolgen Weiterleitungen automatisch. Die frühere Same-Host-Prüfung für zusätzliche Unterseiten schützt weder die erste URL noch deren Redirect-Ziel.

**Auswirkung:** Ein erreichbarer API-Client kann interne HTTP-Dienste aus der Netzwerkperspektive des Backends ansprechen. Der Scraper verarbeitet deren Antwort weiter. Eine tatsächlich erreichbare Cloud-Metadateninstanz oder deren Zugangsdaten wird nicht vorausgesetzt und wurde nicht kontaktiert.

**Nachweis:** `test_scraper_rejects_redirect_to_loopback` simuliert `https://public.example` → `http://127.0.0.1:8123/private`. Am Original werden beide Requests vom MockTransport beobachtet und der Marker verarbeitet; jetzt wird das interne Ziel vor dem zweiten Request verworfen. Tests decken private/link-local/IPv6-Adressen, gemischte DNS-Antworten, Ports, Schemes und SNI/Host-Erhalt bei IP-Pinning ab.

**Fix:** `security.public_stream` prüft jeden Hop, verwirft nicht öffentliche Adressen und verbindet zur zuvor geprüften IP. TLS prüft weiterhin den ursprünglichen Hostnamen. Proxy-Umgebungsvariablen werden bei diesen Downloads nicht übernommen. Das ist eine gezielte Downloadkontrolle und keine Egress-Firewall sämtlicher SDKs.

### S03 – Gespeichertes XSS in Galerie-Seiten

**Ursache:** Frei übermittelbare Script-Metadaten werden in S3-Galerie-Metadaten übernommen. `hashtags` und `language` gelangen ungefiltert in HTML; einige URLs und JSON-LD-Daten werden ebenfalls als Programmtext verkettet. Nur Titel und Beschreibung zu kodieren genügt nicht.

**Auswirkung:** Beim Anzeigen einer manipulierten Galerie kann JavaScript im Anwendungsursprung laufen und unter anderem dort gespeicherte API-Schlüssel lesen. Das Schreiben einer realen S3-Galerie war für den lokalen Nachweis nicht erforderlich.

**Nachweis:** `test_video_page_encodes_untrusted_metadata` ersetzt `list_video_gallery` durch synthetische Metadaten mit einem harmlosen `window.__audit_marker`. Das Original erzeugt zusätzliche HTML-Elemente/Attribute und einen `javascript:`-Link. Der Test prüft den erzeugten HTML-Quelltext; ein realer Browser-Angriff wurde nicht ausgeführt.

**Fix:** Sämtliche relevanten Text-/Attributkontexte kodieren, Link-Schemes begrenzen, IDs für Links URL-kodieren und JSON-LD korrekt serialisieren sowie gegen schließende Script-Tags schützen.

### S04 – Keine Zugriffskontrolle bei Netzwerkexposition

**Ursache:** Ein mit `0.0.0.0` gestartetes Backend und global veröffentlichte Compose-Ports besitzen keine Betreiber-Authentifizierung. Ein beliebiger nicht leerer Anbieter-Key-Header ist kein Zugriffsnachweis für den lokalen Dienst. Wildcard-CORS erweitert zusätzlich die Browser-Angriffsfläche.

**Auswirkung:** Fremde erreichbare Clients können private Zustände abrufen und nicht abgesicherte Vorgänge auslösen. Das Erlangen eines echten Anbieter-Keys ist für mehrere Lese-/Upload-/Abbruch-Endpunkte nicht erforderlich. Kostenpflichtige Integrationen wurden nicht gegen echte Anbieter getestet.

**Nachweis:** ASGI-Requests mit synthetischer fremder Peer-IP bzw. fremdem Origin erreichen am Original eine private künstliche Job-Antwort mit Status 200. Jetzt antwortet die Grenze mit 403. Zusätzliche Tests prüfen fehlende/falsche/korrekte Tokens, lokale Origins, Hostnamen und montierte URL-Präfixe.

**Fix:** Lokal begrenzter Start und Compose-Portbindungen; API/Medien nur für Loopback oder ein geprüftes Betreiber-Token. Ist ein Token konfiguriert, ist es auch lokal erforderlich. Origins/Hosts sind explizit. Bei einem vorgeschalteten lokalen Proxy muss dieser selbst authentifizieren; mehrere Benutzer werden weiterhin nicht getrennt.

### S05 – Private Zustandsdateien über `/videos`

**Ursache:** `StaticFiles(directory=OUTPUT_DIR)` stellt neben Videos auch `job_state.json`, Transkripte und interne Checkpoints bereit.

**Auswirkung:** Bei bekannter Job-ID sind private Metadaten ohne dazugehörige API-Entscheidung abrufbar. Zufällige IDs sind keine explizite Dateifreigabe.

**Nachweis:** Eine künstliche `job_state.json` mit privatem Marker wird über die tatsächliche ASGI-Medienroute angefragt: vorher 200, nachher 404. Es werden keine realen Zustandsdateien gelesen.

**Fix:** Medien-Endungsliste, Sperre versteckter Pfadteile und `nosniff`; die allgemeine API-/Medien-Zugriffsgrenze gilt zusätzlich. Dieser Befund betrifft Dateitypen, S04 dagegen berechtigte Clients.

### S06 – Upload-/Downloadgröße wird zu spät oder nicht begrenzt

**Ursache:** Mehrere Thumbnail-/Actor-Pfade lesen `await file.read()` ohne Grenze. Externe Downloadantworten werden vollständig gepuffert, bevor Text gekürzt oder der Medientyp geprüft wird. Das bestehende 2-GiB-Limit eines anderen Endpunkts schützt diese Wege nicht.

**Auswirkung:** Einzelne übergroße Vorgänge können RAM, temporären Speicher und Datenträger belegen. Der Nachweis verwendet keine tatsächlichen Gigabyte-Dateien oder Prozessabstürze.

**Nachweis:** Ein synthetischer Upload von 1 MiB + 1 Byte wird bei im Test auf 1 MiB abgesenktem Limit am Original akzeptiert; jetzt 413. Chunked-JSON wird vor Verarbeitung begrenzt. Mock-Downloads prüfen deklarierte und tatsächliche Byteüberschreitung, komprimierte Antworten, erfolgreichen begrenzten Transfer und Erhalt einer bereits vorhandenen Zieldatei.

**Fix:** ASGI-Bodylimits vor der Multipart-Verarbeitung, Datei-Streaming und exklusive Dateierstellung; 10-MiB-Bildgrenze; 5-MiB-Websiteantworten, 512-MiB-Mediendownloads; atomarer Austausch erst nach vollständigem Transfer. Weitere Render-/Probe-Timeouts wurden ergänzt. Ein globales Datenträgerkontingent und eine native Medien-Sandbox wurden nicht eingeführt.

### S07 – Unbegrenzte Anzahl erzeugter Actor-Optionen

**Ursache:** `num_options` steuert ohne Bereichsprüfung `ThreadPoolExecutor(max_workers=num_options)` und die Anzahl erzeugter Aufgaben. Der Endpunkt umgeht zudem das gemeinsame Verarbeitungslimit.

**Auswirkung:** Ein einzelner Request kann eine übergroße Anzahl Threads/Futures und kostenpflichtiger Generierungen verursachen. Ein gültiger fal.ai-Key war für die echte Anbieterarbeit notwendig; er ersetzt keine Ressourcenbegrenzung des Servers.

**Nachweis:** Die Original-Pydantic-Anfrage akzeptiert 0, -1 und 1000. Die Regression erwartet Ablehnung vor dem Worker-Aufruf. Es werden keine Threads oder kostenpflichtigen Requests in dieser Größenordnung gestartet.

**Fix:** API und Worker akzeptieren 1–6 Actor-Optionen; Analyse 1–6 Scripts; höchstens 30 Script-Segmente. Actor-Arbeit nutzt das gemeinsame Semaphore. Neue Aufträge und gleichzeitig aktive HTTP-Anfragen werden begrenzt; ein aktiver SaaS-Job kann nicht parallel in denselben Ausgabeordner wiederholt werden. Diese Grenzen gelten je Prozess.

### S08 – Bedingte Command Injection über Checkout-Verzeichnis

**Ursache:** `%~dp0` wird ungequotet in `cmd /k "cd /d … && …"` eingesetzt. Ein Verzeichnisname mit `&` wird zu Shell-Syntax.

**Auswirkung und Einstufung:** Zusätzliche Befehle laufen beim Start mit den Rechten des Betreibers. Das ist **kein** über eine Video-URL erreichbares RCE: Der Angreifer muss den Checkout-/Entpackpfad beeinflussen können und der Betreiber den Launcher starten. Daher Low statt einer pauschalen Critical-Einstufung.

**Nachweis:** `test_launcher_quotes_checkout_path_metacharacters` führt Original- und neue Pfadbehandlung unter Windows aus. Ausschließlich der Dienstaufruf wird durch `echo` ersetzt; `/B /WAIT` verhindert neue Fenster und dauerhafte Prozesse. Der Originaltext gibt `UNSAFE_CHECKOUT_MARKER` aus, die Korrektur nur den vorgesehenen `SAFE_LAUNCH_MARKER`.

**Fix:** Der Arbeitsordner wird als quotierter `start /D`-Parameter übergeben. Der eigentliche `cmd`-Befehl enthält keinen eingefügten Checkout-Pfad.

## Dependency Security – eigener Befund D01

**D01: bekannte verwundbare Paketversionen; High nach den höchsten npm-Advisory-Einstufungen, CWE-1395.** Dies ist ein bestätigter Versions-/Advisory-Befund, keine Behauptung von 163 praktisch ausnutzbaren Anwendungslücken.

| npm-Lockfile | High | Moderate | Low | Betroffene Pakete |
| --- | ---: | ---: | ---: | ---: |
| Vorher | 9 | 3 | 1 | 13 |
| Nachher | 0 | 0 | 0 | 0 |

Vite 4.5.14 und dessen Toolchain wurden auf Vite 8.3.6 / React-Plugin 6.2.1 mit aktualisiertem Lockfile gebracht. Der alte Vite-Stand fällt beispielsweise in die Hersteller-Advisory zu [Windows-Backslash-Dateizugriffen](https://github.com/vitejs/vite/security/advisories/GHSA-93m4-6634-74q7). Die konkreten Voraussetzungen stehen in den Hersteller-Advisories; es wurde kein lokaler Dev-Server aktiv angegriffen. Lint und Produktionsbuild bestehen mit der aktualisierten Toolchain.

Die vorhandene Python-Laufzeit wurde anhand ihrer installierten Metadaten geprüft. Scanner-Ausgabe einschließlich IDs und Fixversionen: [baseline-pip-audit-runtime.json](security-evidence/baseline-pip-audit-runtime.json).

| Paket | Vorgefunden | Advisory-Einträge | Höchste gemeldete Fix-Untergrenze |
| --- | --- | ---: | --- |
| anyio | 4.12.1 | 2 | 4.14.2 |
| brotli | 1.2.0 | 2 | 1.2.1 |
| certifi | 2026.2.25 | 2 | 2026.10.18 |
| cryptography | 46.0.5 | 11 | 50.0.0 |
| filelock | 3.25.2 | 9 | 4.2.1 |
| fsspec | 2026.2.0 | 1 | 2026.6.0 |
| idna | 3.11 | 2 | 3.15 |
| pillow | 12.1.1 | 74 | 13.0.0 |
| pip | 26.0.1 | 8 | 26.2 |
| pyasn1 | 0.6.3 | 10 | 0.6.5 |
| pygments | 2.19.2 | 2 | 2.20.0 |
| python-multipart | 0.0.22 | 10 | 0.0.31 |
| requests | 2.32.5 | 2 | 2.33.0 |
| setuptools | 82.0.1 | 3 | 84.1.0 |
| soupsieve | 2.8.3 | 6 | 2.9.0 |
| starlette | 1.0.0 | 10 | 1.3.1 |
| torch | 2.10.0 | 2 | 2.13.0 |
| urllib3 | 2.6.3 | 7 | 2.8.0 |

`security-constraints.txt` setzt entsprechende Grenzen für die Produktabhängigkeiten; `requirements-security.txt` aktualisiert auch die Audit-Werkzeuge. Die frische Test-Requirements-Auflösung und alle 71 Pakete der Audit-Umgebung melden null bekannte Advisories. Das aktualisiert die bestehende `venv` nicht. Ein vollständiger Resolver-/GPU-/Video-Kompatibilitätsnachweis der schweren Requirements steht aus. D01 ist deshalb **teilweise behoben**, bis die tatsächliche Runtime migriert und erneut geprüft ist. Die Eingabedateien sind weiterhin kein vollständiger Python-Lock mit Hashes.

## Statische Analyse und bewusst nicht als Lücken gezählte Hinweise

Bandit scannt im Endstand 16.626 Codezeilen: **65 Low, 2 Medium, 0 High**, keine Parserfehler und keine unterdrückten Checks. Der Medium-/High-Gate (`-ll -ii`) endet mit Exitcode **1**; dieser Check wird ausdrücklich nicht als grün gemeldet.

| Regeln | Bewertung |
| --- | --- |
| B310, `hooks.py:116`, `main.py:1437` | Feste HTTPS-Quellen für Schrift/Modell. Keine übergebene Benutzer-URL und damit kein nachgewiesener beliebiger URL-Scheme-/Dateizugriff. Prüfsummenbindung und bessere Begrenzung dieser separaten Bootstrap-Downloads bleiben offen. |
| B603/B607/B404 | Nachverfolgte Aufrufe verwenden Argumentlisten. Keine bestätigte Shell-Injection in den Python-Prozessaufrufen. Programme werden über vertrauenswürdige Umgebung/PATH erwartet. |
| B110/B112 | Unterdrückte Fehler einzeln im Datenfluss bewertet; kein zusätzlicher bestätigter Exploit. Generische Fehler dürfen Verfügbarkeit/Diagnose erschweren; Herstellerantworten können weiterhin sensible Inhalte enthalten. |
| B311 | Zufall für Bild-Prompt-/Darstellungsvarianten, keine Tokens oder Passwörter. |
| Ursprüngliches B324 | SHA-1 dient allein einer stabilen Dateinamen-Unterscheidung. `usedforsecurity=False` dokumentiert das; es wurde keine Passwort-/Signaturschwäche erfunden. |

Weitere vorsorgliche Härtungen sind der begrenzte deterministische FFmpeg-Reparaturpfad und die fal.ai-Queue-Origin-Prüfung vor Weitergabe eines Schlüssels. Eine erfolgreiche Prompt-Injection oder ein kompromittierter Anbieter wurde nicht nachgewiesen; diese Änderungen erhöhen daher nicht die Finding-Zahl.

Der Secret-Signaturscan findet **keine Kandidaten** in den geprüften getrackten Quellen, Audit-Dateien und 2.652 historischen Textblobs. Das eigene Tool schließt echte `.env`, Cookies und fremde persönliche ungetrackte Konfiguration aus. Der Umfang und das Ergebnis stehen in [secret-scan.json](security-evidence/secret-scan.json). Unbekannte Secret-Formate oder kopierte Inhalte außerhalb dieses Scopes bleiben möglich.

## Testergebnisse vor und nach den Fixes

| Prüfung | Vorher | Endstand |
| --- | --- | --- |
| Bestehende Python-Suite | 567 bestanden, 0 fehlgeschlagen, 7 übersprungen | Gesamtsuite 632 bestanden, 0 fehlgeschlagen, 8 übersprungen |
| 13 ausgewählte Security-Assertions | 13 erwartete Fehlschläge am Original | Alle 13 bestehen |
| Gesamte neue Security-Suite | Am Original nicht vollständig anwendbar | 65 bestanden, 0 fehlgeschlagen, 1 übersprungen |
| Frontend-Lint / Build | Bestanden | Bestanden |
| npm audit | 13 Pakete mit Meldungen, Exit 1 | 0 Meldungen, Exit 0 |
| pip-audit bestehende Video-venv | 18 Pakete / 163 Einträge, Exit 1 | Unverändert; Migration offen |
| pip-audit frische Test-Requirements / Tool-Umgebung | Keine Aussage über ursprüngliche Runtime | Jeweils 0 Meldungen, Exit 0 |
| Bandit, vollständige Ausgabe | 69 Hinweise | 67 Hinweise; Low-/Medium-Triage siehe oben |
| Bandit Medium-/High-Gate | Nicht als bestandener Ausgangscheck behauptet | Exit 1 wegen 2 B310-Hinweisen |
| Backend-Token in Browser-Assets | Nicht vorhanden als Feature | Synthetisches Token in keinem Build-Asset gefunden |
| Secret-Signaturen | Read-only Analyse ohne Fund | Reproduzierbarer Scan ohne Kandidaten |
| Docker-Builds / Remote-CI | Nicht ausgeführt | Nicht ausgeführt: Docker fehlt; keine Veröffentlichung autorisiert |

Die acht Skips betreffen Calibration-/OpenCV-Umgebung (3), yt-dlp-Importfälle (4) und Windows-Symlink-Rechte (1). Die drei FFmpeg-Integrationstests in `test_longform_ffmpeg.py` wurden mit vorhandenen FFmpeg-Binaries ausgeführt. Es gab keine echten Anbieter-, S3- oder Social-Publishing-Aufrufe.

Frühere Zwischenprüfungen fanden zusätzlich Dummy-URLs außerhalb der nun absichtlich engeren YouTube-Grenze; diese Fixtures wurden zu gültigen YouTube-Beispielen geändert. Keine Assertion wurde entfernt oder abgeschwächt. Beim Windows-Launcher-Test wurde zunächst auch der äußere Testaufruf über den problematischen Pfad geparst; der Test startet jetzt über einen statischen Dateinamen mit getrenntem `cwd`, um ausschließlich die tatsächliche Launcher-Pfadbehandlung zu messen. Danach bestehen die Prüfungen.

## Review und Selbstprüfung

Nach lokalen grünen Funktionsprüfungen wurden drei getrennte Read-only-Reviews durchgeführt: Korrektheit/Medienregressionen, Security/Ressourcen sowie Repository-/Buildkonformität; eines davon mit frischem Kontext. In Runde 1 wurden vier konkrete Fehler der Zwischenfassung validiert und behoben: fehlende Constraints-Datei im Docker-Builder, Abbruch bei fremden Ausgabeordnernamen, Authentifizierungsumgehung bei ASGI-`root_path` und fehlende Weitergabe eines nur in der Root-`.env` konfigurierten Tokens an den lokalen Vite-Prozess. Neue Tests sichern die relevanten Grenzen ab.

Runde 2 meldet keine bestätigten offenen Findings in diesen Reviewbereichen. Der Security-Reviewer führte zudem zehn ASGI-Assertions unter zwei Mount-Präfixen aus. Diese Prüfungen sind lokale KI-Code-Reviews, **keine externe unabhängige Bestätigung**. Sie ersetzen weder Remote-CI noch die nach grüner CI vorgesehene Claude-/Codex-Gatefolge. Der abschließende eigene Diff-Check schließt die vorgefundenen Benutzeränderungen aus.

## Bekannte Restrisiken und manuelle Schritte

1. **Runtime-Abhängigkeiten migrieren:** bestehende `venv` erhalten; neue isolierte Umgebung mit den Constraints aufbauen, vollständige Requirements auflösen/installieren, erneut scannen und reale Video-/GPU-Kompatibilität prüfen. Der jetzige Runtime-Scan bleibt ein offener Befund.
2. **Docker/CI nachholen:** beide Images bauen, Container-Proxy/Token testen und die vorgeschriebene Remote-CI-/Claude-/Codex-Prüfkette nach später ausdrücklich autorisierter Veröffentlichung durchlaufen. Ein statischer Dockerfile-Regressionstest ersetzt keinen Build.
3. **Öffentliche Installation prüfen:** HTTPS, externe Anmeldung, Gateway-Token-Injektion, korrekte Host-/Origin-Listen, Netzwerk-Egress- und Container-Ressourcenlimits. Niemals allein auf die als lokal erscheinende Proxy-IP vertrauen. Kein Mehrbenutzer-/Mandantenschutz vorhanden.
4. **Medien-/Anbieterrisiken:** native Parser sind nicht sandboxed; keine Vollprüfung von FFmpeg/OS-/Image-Layern, Modellen oder echten Cloud-Integrationen. Bootstrap-Downloadhinweise und fehlende Prüfsummenbindung bleiben dokumentiert.
5. **Geheimnisse und Veröffentlichung:** API-Keys bleiben im Browserprofil; öffentliche S3-Galerien sind weiterhin eine echte Veröffentlichungsfunktion. Keine privaten Inhalte ohne passende Betriebskonfiguration verarbeiten. Private Meldestelle im Ziel-Repository einrichten.
6. **Grenzen der Quoten:** einzelne Vorgänge sind begrenzt, aber dauerhafte Benutzer-, Kosten- oder Gesamtplattenquoten fehlen. Die neue Grenze ist je Prozess; mehrere Worker sind keine verteilte Queue.

**Abschließende Einschätzung:** Die nachgewiesenen acht Anwendungslücken sind im lokalen Quellstand korrigiert und durch reproduzierbare Tests abgesichert. Das Projekt ist gegenüber der geprüften Basis deutlich gehärtet. Die vorhandene Runtime und ungeprüfte Deployment-Gates verhindern die Aussage „vollständig verifiziert“ oder „produktionssicher“. D01 und die genannten Nachweise bleiben offen.

Reproduktion: [SECURITY_TESTING.md](SECURITY_TESTING.md). Rohbelege: [security-evidence](security-evidence/README.md). Englische Portfolio-Fassung: [SECURITY_PORTFOLIO.md](SECURITY_PORTFOLIO.md).

## Nachtrag vom 5. November 2026: Fork-Beiträge und aktueller Upstream

Auf zusätzlichen Auftrag wurde der veröffentlichte Fork mit `mutonby/openshorts` bei `894514049a3c6210a7cd65676344f44371b3717a` verglichen. [FORK_CONTRIBUTIONS.md](FORK_CONTRIBUTIONS.md) trennt geerbte Originalfunktionen, 79 Themegaindex-attribuierte Nicht-Merge-Commits, nachgewiesene angepasste Upstream-Übernahmen aus dem ungemergten PR #50 und die unveröffentlichten lokalen Audit-Änderungen. Die ursprüngliche Fassung dieses Berichts ist zusätzlich [unverändert archiviert](fork-evidence/archive/SECURITY_AUDIT.pre-comparison.md).

Die Produktquellen und Security-Tests wurden für diese Dokumentationserweiterung nicht geändert. Die obigen 632/65-Testzahlen bleiben erhaltene Ergebnisse des ursprünglichen lokalen Audit-Laufs, keine neu behauptete Gesamtausführung.

**Zusätzlich dokumentierte offene Grenzen:** Der lokale IP-Helper berücksichtigt NAT64-Adressen mit eingebetteten privaten IPv4-Zielen noch nicht wie der aktuelle Upstream. Lokale Proben bestätigen die unterschiedliche Eingabeentscheidung; eine tatsächlich erreichbare NAT64-Übersetzung oder erfolgreiche interne Verbindung wurde nicht getestet. Der getestete SSRF-Fix darf deshalb nicht als vollständiger Schutz sämtlicher IPv6-Übersetzungsnetze verstanden werden. Auch die Upstream-Verbindungskontrolle innerhalb von yt-dlp/Probes/externen Downloadern ist nicht gleichwertig nachgewiesen.

Weitere belegte Nacharbeit betrifft Credential-URL-Redaktion, die breite Kindprozess-Umgebung und nicht atomare Worker-Metadaten. Eine deterministische Probe zeigt dort einen lesbaren leeren Zwischenzustand, während der geprüfte Upstream-Helfer die bisherige JSON-Datei erhält. Diese neuen Vergleichsbeobachtungen sind weder zusätzliche abgeschlossene Fixes noch behauptete neue hochkritische Remote-Exploits. Details, Bedingungen und reproduzierbare Beobachtungen stehen im [Vergleich](FORK_CONTRIBUTIONS.md) und in [targeted-probes.json](fork-evidence/targeted-probes.json).

Ein neuer Advisory-Abgleich des fixierten aktuellen Upstream-npm-Lockfiles meldet 15 betroffene Pakete; die lokale gehärtete Fassung meldet erneut null. Unterschiedliche Abhängigkeitsbäume und Scannerbefunde erlauben kein pauschales Sicherheitsranking. Die vorgefundene Python-Runtime, Docker-/GPU-/Provider-Verifikation und Remote-CI bleiben wie oben offen. Keine Upstream-Patches, Cloud-Komponenten oder Lizenzen wurden automatisch übernommen; keine Veröffentlichung erfolgte.
