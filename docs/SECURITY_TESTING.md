# Security-Tests reproduzieren

Diese Anleitung bezieht sich auf den Audit vom **2026-11-05**. Befehle werden im Repository-Root in PowerShell ausgeführt. Die Tests verwenden künstliche Dateien, lokale ASGI-Clients und Mock-HTTP-Antworten. Sie starten keine echten AI-, S3-, Upload-Post- oder Social-Publishing-Aufträge.

## Umgebung einrichten

Python 3.11, Git, ripgrep und **Node 24 LTS mit npm** verwenden. Das fixierte Vite 8.3.6 deklariert `^20.19.0 || >=22.12.0`; Node 18 reicht nicht. Siehe [Vite-Kompatibilität](https://vite.dev/guide/) und [Node-Release-Status](https://nodejs.org/en/about/previous-releases). Node 20 ist Stand 2026-11-06 EOL und wird hier trotz technischer Engine-Kompatibilität nicht für neue Installationen empfohlen. Lokal wurde Node 24.14.0 verwendet. Für die synthetischen Videotests müssen `ffmpeg` und `ffprobe` auf PATH liegen. Nicht die vorhandene Video-/GPU-Umgebung überschreiben.

```powershell
py -3.11 -m venv .venv-security
.\.venv-security\Scripts\python -m pip install --upgrade pip
.\.venv-security\Scripts\python -m pip install -r requirements-security.txt
npm --prefix dashboard ci
$env:PYTHON_DOTENV_DISABLED = "1"
$env:AWS_EC2_METADATA_DISABLED = "true"
```

Die Variable deaktiviert das automatische Laden echter `.env`-Werte in den Testprozessen. Die Baseline-Reproduktion entfernt zusätzlich bekannte Zugangsdaten-Variablen aus ihrer Kindprozess-Umgebung. Eigene Tests niemals mit echten Publishing-Aufträgen erweitern. Unter Linux lauten die Interpreterpfade beispielsweise `.venv-security/bin/python`; entsprechend den Pfad anpassen.

## Gesamtsuite und Security-Suite

```powershell
.\.venv-security\Scripts\python -X utf8 -m pytest tests/test_security.py -q -rs
.\.venv-security\Scripts\python -X utf8 -m pytest tests/ -q -rs
npm --prefix dashboard run lint
npm --prefix dashboard run build
```

Im dokumentierten Windows-Testlauf: **65 Security-Tests bestanden, ein Skip**; **632 Tests insgesamt bestanden, acht Skips**, keine Fehler. Erwartet wird, dass alle ausgeführten Assertions bestehen. Die Skip-Anzahl kann mit installierten Videoabhängigkeiten oder Windows-Symlink-Rechten variieren.

Die drei FFmpeg-Integrationstests lassen sich gesondert ausführen:

```powershell
.\.venv-security\Scripts\python -X utf8 -m pytest tests/test_longform_ffmpeg.py -v
```

Diese Tests erzeugen nur synthetische lokale Videos und Töne. Die damaligen Skips waren Calibration-/OpenCV-Abhängigkeiten (3), fehlendes yt-dlp in der schlanken Audit-Umgebung (4) und fehlende Windows-Symlink-Rechte (1). Sie werden nicht als bestandene Tests gezählt. Für Video-/GPU-Kompatibilität ist zusätzlich eine vollständige, separat aufgebaute Laufzeit nötig.

## Vorher-/Nachher-Nachweis am Originalcode

```powershell
.\.venv-security\Scripts\python -X utf8 tools/reproduce_security_baseline.py
```

Das Tool liest ausschließlich die Root-Python-Quellen des Basis-Commits `98afa9ea5e9a842eb358834fe0cbd251d335573b` über `git show`, kopiert sie in ein neues temporäres Verzeichnis und führt dort 13 ausgewählte Assertions aus. Die Arbeitskopie, echte `.env`, Cookies und Medien werden nicht kopiert oder zurückgesetzt. Das temporäre Verzeichnis gehört allein diesem Lauf und wird anschließend entfernt.

**Erwartung:** pytest meldet **13 failed**, ohne Fehler bei der Testeinrichtung und ohne Skips. Der Wrapper gibt nur dann Exitcode **0** zurück, wenn exakt diese 13 Regressionen am alten Code nachgewiesen sind. Ein regulärer Aufruf der finalen Security-Suite muss dagegen erfolgreich sein. Dieser Unterschied verhindert, dass erwartete Baseline-Fehlschläge als endgültige Testerfolge ausgegeben werden.

## Einzelne Finding-Nachweise

| Finding | Gezielter Aufruf hinter `python -X utf8 -m pytest` | Erwartung im Endstand |
| --- | --- | --- |
| S01 Pfade/Dateien | `tests/test_security.py -k 'path or storage or filename or publish or traversal' -v` | Pfadflucht vor Zugriff/Enqueue abgewiesen; ursprüngliche Markerdateien erhalten |
| S02 SSRF | `tests/test_security.py -k 'scraper or dns or destinations or youtube' -v` | Kein Mock-Request an interne Ziele; gemischte DNS-Antworten und Host-Tricks abgewiesen |
| S03 XSS | `tests/test_security.py::test_video_page_encodes_untrusted_metadata -v` | Zusätzliche HTML-Elemente/Attribute und ausführbare Link-Schemes fehlen |
| S04 Zugriff | `tests/test_security.py -k 'api or origin or mounted' -v` | 403/401 bei fehlendem Zugriff, korrekter Token erreicht den Handler; gilt auch mit Mount-Präfix |
| S05 Metadaten | `tests/test_security.py::test_private_metadata_is_not_served -v` | Private künstliche JSON-Datei ergibt 404 |
| S06 Ressourcen | `tests/test_security.py -k 'limit or overflow or download or upload' -v` | Übergrößen abgewiesen, Teil-/Temporärdateien bereinigt, bestehende Zieldateien erhalten |
| S07 Generierungsanzahl | `tests/test_security.py -k 'options or queue or segment or concurrently' -v` | Ungültige Mengen werden vor Arbeit abgewiesen; volle Queue liefert 429 |
| S08 Windows-Launcher | `tests/test_security.py::test_launcher_quotes_checkout_path_metacharacters -v` | Original gibt den zusätzlichen Echo-Marker aus; korrigierter Launcher nur den vorgesehenen Marker |
| Vorsorgliche Filter-/Provider-Härtung | `tests/test_security.py -k 'repair or provider' -v` | Keine freien Modellfilter und keine Key-Weitergabe an fremde Queue-Origin |

Alle URL-/Provider-Nachweise ersetzen HTTPX durch `MockTransport`. ASGI-Peer-IPs sind Testmetadaten, keine ausgehenden Verbindungen zu diesen Adressen. Der XSS-Test prüft den realen Renderer mit synthetischen Metadaten; er startet weder einen Browser-Angriff noch einen S3-Upload. Der Publish-Test legt nur einen BackgroundTask an und führt diesen am Original **nicht** aus.

Der Launcher-Test verwendet den echten alten/neuen Startbefehl aus Git/Arbeitskopie, ersetzt den Backend-Dienst durch `echo` und ergänzt `/B /WAIT`. Der äußere Testaufruf nutzt einen statischen Scriptnamen und ein getrenntes `cwd`; dadurch wird ausschließlich der Launcher geprüft. Unter anderen Betriebssystemen ist dieser Test ein erklärter Skip.

## Bandit: Ergebnisse und Triage getrennt halten

```powershell
$auditSources = @(rg --files -g '*.py' -g '!tests/**' -g '!tools/**' -g '!venv/**' -g '!.venv-security/**')
.\.venv-security\Scripts\python -X utf8 -m bandit $auditSources -f json -o bandit-local.json
.\.venv-security\Scripts\python -X utf8 -m bandit $auditSources -ll -ii
```

Bandit 1.9.4 meldet im dokumentierten Endstand **65 Low, 2 Medium, 0 High**. Beide Befehle enden wegen Hinweisen mit **Exit 1**. Die beiden Medium-B310-Hinweise betreffen `hooks.py:116` und `main.py:1437`, also feste HTTPS-Schrift-/Modelldownloads. Sie sind nicht als beliebige benutzerkontrollierte URL-Sinks nachgewiesen; Prüfsummenbindung und Begrenzung dieser Bootstrap-Aufrufe bleiben offen. Details sämtlicher Regelgruppen stehen im Auditbericht. Keine Check-Unterdrückung wurde ergänzt.

Nicht einfach Exit 1 ignorieren: Neue Hinweise müssen gegen den Datenfluss geprüft werden. Der rohe Scan und die dokumentierte fachliche Bewertung sind unterschiedliche Nachweise.

## Dependency Scanning

```powershell
npm --prefix dashboard audit --json
.\.venv-security\Scripts\python -X utf8 -m pip_audit -r requirements-test.txt --format json
.\.venv-security\Scripts\python -X utf8 -m pip_audit --local --format json
.\.venv-security\Scripts\python -m pip check
```

Beim Audit meldeten das neue npm-Lockfile, die frische Test-Requirements-Auflösung und die Audit-Umgebung **keine bekannten Advisories**, Exit 0. Das bedeutet nur, dass die abgefragte Datenbank für die geprüften Versionen keine Treffer meldet. Spätere Advisories können das Ergebnis verändern. `pip check` prüft Abhängigkeitskonsistenz und ersetzt keinen Advisory-Scan.

Die vorgefundene vollständige Windows-Laufzeit separat und ohne Installation/Änderung prüfen:

```powershell
.\.venv-security\Scripts\python -X utf8 -m pip_audit --path venv/Lib/site-packages --format json
```

Dokumentierter Ausgang: **18 Pakete, 163 Advisory-Einträge, Exit 1**. Paketliste, Versionen und IDs liegen im Rohbericht. Das ist ein **offener Runtime-Befund**. `-c security-constraints.txt` wirkt nur bei einer neuen Installation/Auflösung. Es aktualisiert keine bestehenden Pakete und ist kein vollständiger Lockfile.

Für eine kontrollierte Migration zuerst eine neue, noch nicht verwendete `.venv` anlegen; die vorhandene `venv` behalten. Anschließend vollständige Requirements mit aktualisiertem pip installieren, die neu installierte Umgebung über `--path` scannen und lokale Video-/GPU-Tests ausführen. Diese schwere Migration wurde im Audit nicht vorgenommen; ihre Kompatibilität ist nicht bestätigt. Die Constraints sind nicht als abgeschlossener vollständiger Requirements-Scan auszugeben.

## Secret-Signaturen

```powershell
.\.venv-security\Scripts\python -X utf8 tools/scan_secrets.py --history
```

Erwartung im geprüften Stand: leere `findings`, Exit 0. Das Tool prüft getrackte aktuelle Inhalte, ausdrücklich benannte neue Audit-Dateien und historische Git-Blobs. Es gibt nur Regel, Pfad, Zeile und Revision aus. Echte `.env`, Cookies, Laufzeitmedien und fremde persönliche ungetrackte Konfiguration werden nicht geöffnet. Ein Treffer beendet den Scan mit Exit 1 und muss privat geprüft werden. Kein Treffer beweist keine vollständige Secret-Freiheit.

### Zusatzscan für das Vergleichsskript

`tools/compare_fork_security.py` gehörte vor dem Staging weder zu `git ls-files` noch zur expliziten Liste neuer Dateien des Scanners. Deshalb wurde es zusätzlich mit denselben Regeln geprüft. **Exakter zusätzlicher Befehl**, aus dem Repository-Root in PowerShell; er funktioniert auch nach dem Staging und verändert das Skript nicht:

```powershell
@'
import importlib.util
import json
from pathlib import Path

relative = "tools/compare_fork_security.py"
spec = importlib.util.spec_from_file_location("audit_secret_scan", "tools/scan_secrets.py")
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)
findings, is_text = scanner.scan(Path(relative).read_bytes(), relative, "working-tree")
print(json.dumps({"additional_scanned_files": [relative], "text_file": is_text, "findings": findings}, indent=2))
raise SystemExit(int(bool(findings) or not is_text))
'@ | .\.venv-security\Scripts\python -B -X utf8 -
```

Erwartung: `additional_scanned_files` enthält genau das Vergleichsskript, `text_file` ist `true`, `findings` ist leer, Exit 0. Das importierte Scan-Modul führt seinen `main()`-Scan dabei nicht aus; der reguläre History-Scan oben ist zusätzlich erforderlich. Der Zusatzscan startet das Vergleichsskript nicht und gibt keine Secret-Werte aus. Auf Linux denselben Python-Block beispielsweise über `./.venv-security/bin/python -B -X utf8 - <<'PY'` und die Abschlusszeile `PY` ausführen.

Die erneute Ausführung der hier korrigierten Anleitungen wird getrennt vom Audit-Snapshot unter [DOCUMENTATION_REVIEW.md](DOCUMENTATION_REVIEW.md) nachgewiesen. Frühere Manifeste und Testergebnisse bleiben unverändert erhalten.

## Zugriffskonfiguration und Deployment-Smoke-Tests

Lokal ohne Token bleiben Loopback-Clients mit zugelassenem Host/Origin erlaubt. Für Docker Compose einen individuellen zufälligen Wert für `OPENSHORTS_API_TOKEN` in der privaten Root-`.env` setzen; niemals den Beispielwert aus einem Test übernehmen. Ein gesetztes Token wird auch für lokale API-/Medienzugriffe verlangt. Vite lädt die benötigten Root-Konfigurationsvariablen serverseitig und fügt das Token im Proxy hinzu; niemals als `VITE_*` definieren.

```powershell
py -3.11 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Der Wert aus diesem Befehl ist geheim und gehört nicht in Reports oder Git. Die `.env.example` beschreibt Hosts, Origins und Gateway-Annahmen. Für absichtlich öffentliche Domains müssen `OPENSHORTS_ALLOWED_HOSTS` und exakte `OPENSHORTS_ALLOWED_ORIGINS` passend gesetzt werden; diese Konfiguration ersetzt keine Anmeldung.

Nach verfügbarer Docker-Installation, ausschließlich in einer eigenen Testumgebung:

```powershell
docker build -t openshorts-backend-security .
docker build -t openshorts-dashboard-security ./dashboard
docker compose config --quiet
```

Diese Befehle wurden hier **nicht ausgeführt**, weil Docker fehlt. Der Backend-Build lädt große Bibliotheken/Modelle und erfordert Netzwerk/Speicher. `compose config --quiet` prüft ohne Ausgabe von Geheimnissen; `docker compose config` ohne diese Option könnte das aufgelöste Token anzeigen.

Danach lokal prüfen, dass beide Ports nur an Loopback gebunden sind, Vite den Backend-Dienst erreicht, fehlende/falsche Tokens abgewiesen werden, korrekte Tokens und freigegebene Origins funktionieren sowie JSON-Checkpoints weiter 404 ergeben. Öffentliche Gateways müssen selbst authentifizieren; das Durchreichen über eine lokale Proxy-IP beweist keinen Benutzerzugriff.

Remote-CI, beide Docker-Images und die nach grüner CI vorgeschriebenen externen Reviews sind offene manuelle Schritte. Push/Veröffentlichung sind nicht Teil dieser lokalen Arbeit. Ein Merge darf nicht automatisch erfolgen.

## Belegdateien

Die gespeicherten Ergebnisse stehen unter [security-evidence](security-evidence/README.md). Dort sind erwartete Baseline-Fehlschläge, tatsächlich bestandene Checks, offene Scannerbefunde und nicht ausgeführte Prüfungen getrennt. Paketlisten und SHA-256-Manifeste unterstützen die Nachprüfung; sie ersetzen keine unabhängige Signatur oder erneute Ausführung.

## Ergänzung: gezielter Fork-/Upstream-Vergleich

```powershell
.\.venv-security\Scripts\python -X utf8 tools/compare_fork_security.py
```

Das Tool benötigt die in [FORK_CONTRIBUTIONS.md](FORK_CONTRIBUTIONS.md) fixierten Upstream-Git-Objekte. Es isoliert ausschließlich die geprüften Helper-Definitionen, ersetzt DNS durch einen künstlichen Fehler und verwendet für JSON nur eigene temporäre Dateien. Es startet weder die Upstream-Anwendung noch echte Downloader oder Provider-Aufträge.

Erwartete **Beobachtungen**, keine zusätzlichen bestandenen pytest-Fälle: NAT64-Privatziele werden vom lokalen IP-Helper akzeptiert und von Upstream abgelehnt; der lokale Worker-JSON-Helfer zeigt während des Schreibens einen ungültigen Zwischenzustand; ein synthetisches URL-Passwort bleibt im lokalen Log-Helper erhalten. Interne NAT64-Erreichbarkeit wird nicht geprüft. Beide verglichenen Resolverfehler bleiben in der `ValueError`-Familie; daraus folgt kein neuer Zugriffsbypass.

Die Ausgabe dokumentiert offene Unterschiede und darf nicht als erfolgreicher Abschluss ihrer Behebung ausgegeben werden. Die ursprünglichen Gesamttests wurden in dieser Dokumentationserweiterung nicht neu ausgeführt. Neue Vergleichsbelege, Quellen und deren Integritätsprüfung stehen unter [fork-evidence](fork-evidence/README.md).
