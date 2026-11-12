# Dokumentationskorrektur und Veröffentlichungsprüfung

Stand: **2026-11-06**. Diese Nachprüfung ergänzt die Audit-/Fork-Snapshots vom 2026-11-05. Sie verändert keine bestehenden Security-Fixes, Tests, Scanner oder historischen Belege. Die bisherigen Reports und Manifeste behalten ihren damaligen Geltungsbereich; heutige README-/Anleitungskorrekturen werden separat erfasst.

## Korrekturen

1. Alle aktuellen README-Clone-Befehle verwenden `https://github.com/Themegaindex/openshorts-fork.git`. **TheMegaindex** ist die sichtbare Fork-Zuordnung; die ursprünglichen OpenShorts-/MIT-Hinweise bleiben erhalten.
2. [README.md](../README.md) ist die vollständige englische Standardfassung; [README.de.md](../README.de.md) enthält die deutsche Anleitung. [README.en.md](../README.en.md) erhält bestehende Links als englischer Einstieg. Installation, Start, Nutzung, Konfiguration und Sicherheitsgrenzen sind getrennt beschrieben.
3. Das gesperrte und lokal installierte Vite ist **8.3.6**, Engine `^20.19.0 || >=22.12.0`. Für neue Installationen wird **Node 24 LTS** empfohlen, lokal vorhanden: **24.14.0**. Offizielle Quellen: [Vite](https://vite.dev/guide/), [Node.js](https://nodejs.org/en/about/previous-releases), jeweils am 2026-11-06 geprüft. Bestehende Node-20-CI-/Container-Konfigurationen bleiben unverändert; ihre EOL-Basis ist offen dokumentiert.
4. [FORK_CONTRIBUTIONS.md](FORK_CONTRIBUTIONS.md#9-reproduktion) beschreibt Clone, `git remote add upstream`, Kontrolle vorhandener Remotes und Fetch des exakten Audit-SHAs. Git-Graph-Merge-Base und spätere patchgleiche übernommene Entwicklungsspitze sind ausdrücklich getrennt.
5. [SECURITY_TESTING.md](SECURITY_TESTING.md#zusatzscan-für-das-vergleichsskript) enthält den vollständigen zusätzlichen Secret-Scan für `tools/compare_fork_security.py`. Der reguläre History-Scan ersetzt diesen vor dem Staging nicht. Beide verwenden dieselben begrenzten Signaturregeln; ein leeres Ergebnis beweist keine vollständige Secret-Freiheit.

## Verifikation und Belege

Die ausgeführten Befehle, Exitcodes und Ergebnisse dieser Nachprüfung stehen unter [documentation-evidence](documentation-evidence/README.md). Sie sind von den ursprünglichen Testläufen und Vergleichsbelegen getrennt. Dort sind auch nicht ausgeführte Prüfungen aufgeführt. Neue oder alte Ergebnisse werden nicht als Remote-CI, unabhängige Zertifizierung oder vollständiger Deployment-Nachweis ausgegeben.

Die Node-/Vite-Anforderung wird gegen Lockfile, installierte Paketmetadaten und offizielle Dokumentation geprüft. Relative Links einschließlich Anker werden auf den geänderten Anleitungsseiten kontrolliert. Der Secret-Scan meldet ausschließlich Pfad, Regel und Fundstelle, keine Werte. Private `.env`, Cookies, lokale Agent-Konfiguration und Nutzermedien gehören nicht zum Veröffentlichungskandidaten.

Für `docs/security-evidence/` und `docs/fork-evidence/` verhindert eine gezielte `.gitattributes`-Ausnahme die automatische Zeilenenden-Konvertierung beim Staging. Die historischen Rohbelege behalten dadurch ihre Bytes. Die alten Manifeste beziehen sich auf den damaligen Arbeitskopie-Stand; später bearbeitete Dokumente und Git-normalisierte Quelldateien können davon abweichen. Die neue Integritätsprüfung unterscheidet diese Fälle ausdrücklich, statt alte Manifeste nachträglich umzuschreiben.

Die erste Indexprüfung erkannte konvertierte Belegdateien; nach explizitem erneuten Staging mit den neuen Attributen stimmen alle 47 historischen Rohbelege bytegenau mit der Arbeitskopie überein. Der normale Whitespace-Check wertete erhaltene CRLF zunächst als nachgestellte Leerzeichen. Die auf diese Belegpfade begrenzte `cr-at-eol`-Regel erkennt deren originale Zeilenenden an; Prüfungen auf tatsächliche nachgestellte Leerzeichen, Leerzeilen am Dateiende und Leerzeichen vor Tabs bleiben aktiv.

Der vollständige `git diff --cached --check` meldet weiterhin **Exit 2** für vier Formatstellen in drei historischen Rohlogs: zwei leere Abschlusszeilen und zwei Leerzeichen in der ursprünglichen pytest-Fehlerausgabe. Diese Logs bleiben als Belege unverändert. Der separat protokollierte Check ohne historische Rohbelegverzeichnisse prüft den übrigen Kandidaten. Die Rohlog-Hinweise sind keine fehlgeschlagenen Anwendungs- oder Reproduktionstests.

### Tatsächlich erneut ausgeführt

| Prüfung | Ergebnis |
| --- | --- |
| Öffentlicher frischer Clone, neues Upstream-Remote, exakter SHA-Fetch | PASS; ohne Checkout/Merge in der ursprünglichen Arbeitskopie. |
| Historische Merge-Base, Divergenz, Log/Stat und zwei leere Gleichheits-Diffs | PASS; `d44dc3e…`, `107 544`, beide Vergleichs-Diffs leer. |
| Frische Python-3.11-Testumgebung mit `requirements-security.txt` | PASS; nur im eigenen temporären Kandidaten-Clone installiert. |
| Gesamtsuite im exportierten Kandidaten | **632 bestanden, 8 übersprungen, 0 fehlgeschlagen**. |
| Security-Subset im selben Kandidaten | **65 bestanden, 1 übersprungen, 0 fehlgeschlagen**; Teilmenge der Gesamtsuite, nicht addieren. |
| Isoliertes Fork-Vergleichsskript | PASS als reproduzierte Beobachtung; bekannte Vergleichslücken bleiben offen. |
| Frisches `npm ci`, Frontend-Lint und Build | PASS unter Node 24.14.0 mit Vite 8.3.6. |
| PowerShell-Parser für die Anleitungsblöcke | PASS; 20 Blöcke, keine Syntaxfehler. Parsing ersetzt keine Ausführung der ausdrücklich offenen ML-/Docker-Befehle. |
| Relative Links und Anker, offizielle Node-/Vite-Quellen | Siehe aktuellen maschinenlesbaren Link-/Quellennachweis. |
| Regulärer Secret-History-Scan und dokumentierter Zusatzscan | Leere Findings; Signaturprüfung mit den beschriebenen Grenzen. |
| Schutz vorhandener Security-Quellen, Tests und historischer Rohbelege | 177 vorgefundene Dateien verglichen; nur fünf ausdrücklich dokumentierte Bestandsdateien geändert, 172 unverändert. |

Zum Kandidatentest wurde der vorbereitete Git-Index über einen eigenen frischen öffentlichen Clone exportiert. Der öffentliche Basis-Commit enthält die lokalen Hardening-Ergänzungen noch nicht; ein reiner Clone allein wäre deshalb kein Test dieses Kandidaten. Private/ungetrackte Betreiberdateien wurden nicht übernommen. Die nachfolgenden Anpassungen betreffen ausschließlich Dokumentation, Belegmetadaten und Attribute; die geprüften Anwendungsquellen und Tests bleiben unverändert.

## Offene Verifikation und Veröffentlichung

Nicht abgeschlossen: vollständige neue Video-/ML-Installation und Runtime-Migration, echte GPU-/Provider-Verarbeitung, Docker-Builds/-Betrieb, macOS-/Linux-Laufzeitprüfung und Remote-CI/externe Review-Gates. Eine statisch korrekte Installationsanleitung ist kein Nachweis, dass alle Plattform-/GPU-Abhängigkeiten erfolgreich installiert wurden. Die im [Audit](SECURITY_AUDIT.md) und [Fork-Vergleich](FORK_CONTRIBUTIONS.md) beschriebenen Restrisiken bleiben bestehen.

Commit und Push benötigen die ausdrückliche Freigabe des Eigentümers. Ein vorbereiteter Git-Index ist weder ein Commit noch eine Veröffentlichung. Audit-Fixes, Regressionstests, Reports und Belege sollen gemeinsam versioniert werden, damit die Portfolio-Aussagen am veröffentlichten Stand nachvollziehbar sind. Persönliche Agent-Dateien und Laufzeitdaten sind ausgeschlossen. Das vollständige Dateiverzeichnis des konkreten Kandidaten wird lokal unter `.git/portfolio-commit-preview.md` zur Freigabe bereitgestellt; es ist kein öffentliches Audit-Artefakt.
