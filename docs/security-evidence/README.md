# Audit-Belege

Diese Dateien dokumentieren tatsächliche lokale Ausführungen vom 2026-11-05. Sie sind kein unabhängiges Testat. `checks.json` enthält Befehle, Exitcodes und Einordnung des Abschlusslaufs. `manifest.json` enthält SHA-256-Werte der gelieferten Dateien; das Manifest selbst ist ausgenommen und nicht extern signiert.

## Bestandene Abschlussprüfungen

| Datei | Ergebnis |
| --- | --- |
| `final-pytest.txt` | 632 bestanden, 0 fehlgeschlagen, 8 übersprungen |
| `final-security-tests.txt` | 65 bestanden, 0 fehlgeschlagen, 1 übersprungen; Teilmenge der Gesamtsuite |
| `final-frontend-lint.txt`, `final-frontend-build.txt` | Exit 0 |
| `final-npm-audit.json` | 0 betroffene Pakete |
| `final-pip-audit-test-requirements.json` | Aufgelöste schlanke Test-Requirements ohne Advisory-Meldung |
| `final-pip-audit-tool-environment.json` | 71 Pakete der isolierten Audit-Umgebung ohne Advisory-Meldung |
| `final-pip-check.txt` | Keine verletzten Abhängigkeitsanforderungen in der Audit-Umgebung |
| `frontend-token-check.txt` | Synthetisches Backend-Token in keinem Browser-Build-Asset |
| `secret-scan.json` | Keine Signaturkandidaten im angegebenen Scope; keine Secret-Werte ausgegeben |
| `documentation-check.txt`, `diff-check.txt` | Lokale Dokumentationslinks bzw. Diff-Whitespace geprüft |

## Erwartete Baseline-Fehlschläge

`baseline-security-tests.txt` enthält **13 absichtlich fehlschlagende Sicherheitsassertions am alten Git-Code**. Der Wrapper selbst besteht nur bei exakt diesem Ergebnis. `baseline-pytest.txt` zeigt die vorherige bestehende Suite mit 567 bestandenen und sieben übersprungenen Tests. Die frühen Frontend-Lint-/Build-Logs zeigen ebenfalls erfolgreiche Ausführung.

`baseline-npm-audit.json` enthält 13 betroffene Pakete (9 High, 3 Moderate, 1 Low). Das sind Paketmeldungen, keine 13 separat lokal nachgewiesenen Anwendungsexploits.

`intermediate-pytest.txt` ist ein ausdrücklich historisches Zwischenergebnis (580 bestanden, 7 übersprungen), kein Abschlussnachweis.

## Weiterhin meldende Scanner

| Datei | Einordnung |
| --- | --- |
| `baseline-pip-audit-runtime.json`, `final-pip-audit-runtime.json` | Vorgefundene Video-`venv`: 163 Advisory-Einträge in 18 Paketen; diese Runtime wurde nicht aktualisiert |
| `baseline-bandit.json` | 69 rohe Hinweise im ursprünglichen Produktcode |
| `final-bandit.json`, `final-bandit-gate.txt` | 67 rohe Hinweise, davon 2 Medium und 0 High; Gate Exit 1; fachliche Triage im Auditbericht |

Ergänzende `*-command.txt`-Dateien enthalten die Tool-Meldungen zu den JSON-Ausgaben. Hinweise bleiben sichtbar und wurden nicht durch pauschale Check-Unterdrückung entfernt.

## Nicht ausgeführt

Docker-Builds, Container-Smoke-Tests, Remote-CI, externe Claude-/Codex-Gates sowie echte Anbieter-/GPU-Integrationsläufe fehlen. Die Gründe stehen in [SECURITY_AUDIT.md](../SECURITY_AUDIT.md). `review-log.md` protokolliert die stattdessen lokal durchgeführten getrennten Read-only-Reviews und deren Grenzen.

`audit-environment-packages.json` ist die Paketinventur des schlanken Test-/Tool-Venvs und kein Lockfile oder SBOM der vollständigen Video-Laufzeit. Dateien einer bereits vorhandenen Nutzerumgebung wurden nicht ersetzt; echte Medien und Zugangsdaten sind nicht Teil dieser Belege.
