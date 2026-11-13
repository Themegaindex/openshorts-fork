# Fork-Vergleich: Belege und Grenzen

Stand: **2026-11-05**. Bewegliche GitHub-Referenzen wurden gelesen; der Vergleich selbst verwendet vollständige, fixierte Commit-IDs. Es wurden keine fremden Anwendungen getestet, keine Upstream-Anwendung gestartet und keine Änderungen veröffentlicht.

| Datei | Inhalt / tatsächlich ausgeführte Prüfung |
| --- | --- |
| `comparison.json` | Git-Graph, Commit-Autoren ohne E-Mail-Werte, Dateideltas, Einführungscommits der 15 Root-Module, Integrität aller 69 bisherigen Audit-Dateien vor den Dokumentationsänderungen |
| `github-provenance.json` | Per GitHub-Connector geprüfte Branch-/PR-Daten, geschlossener ungemergter PR #50, explizite Upstream-Übernahmen, identische Ausgangsbasis und Builder-Datei, unveränderter Lizenzblob |
| `upstream-security-commits.json` | Elf geprüfte sicherheitsrelevante Upstream-Commits mit SHA, Datum, öffentlichen Autorennamen und betroffenen Dateien; die Auswahl ist kein vollständiger Upstream-Audit |
| `targeted-probes.json` | Tatsächliche ungefährliche Helper-Beobachtungen zu IP-/NAT64-Policy, Fehlernormalisierung, JSON-Atomarität und synthetischen Credential-URLs |
| `upstream-npm-audit.json` | Reiner Advisory-Abgleich des Upstream-Lockfiles bei `8945140`: 15 betroffene Pakete, Exit 1; keine Pakete installiert |
| `upstream-npm-audit-summary.json` | Befehl, Exitcode und Scope dieses neuen Abgleichs |
| `local-npm-audit.json` | Erneuter reiner Advisory-Abgleich des gehärteten lokalen Lockfiles: null Treffer |
| `validation.json` | Abschließende Dokumentationslinks, Git-Commit-/Tree-/Blob-Belege, unveränderte Produkt-/Testdateien und original erhaltene Audit-Belege |
| `secret-scan.json` | Erneuter Signaturscan nach der Dokumentationserweiterung; gibt nur Fundstellen, keine Werte aus |
| `manifest.json` | SHA-256 des neuen Vergleichsstands einschließlich der bewusst ergänzten Dokumente; kein extern signiertes Testat |

Die älteren Test-/Scannerergebnisse stehen unverändert unter [security-evidence](../security-evidence/README.md). In dieser Erweiterung wurden die vollständige Python-Suite, Docker, GPU-/Providerintegration und Remote-CI **nicht erneut ausgeführt**. Die bisherigen 632 bestandenen Tests sind als damalige lokale Ausführung erhalten, nicht als neue Aussage über Upstream.

## Originaltexte erhalten

- [Audit vor der Ergänzung](archive/SECURITY_AUDIT.pre-comparison.md)
- [Erste Portfolio-Zusammenfassung](archive/SECURITY_PORTFOLIO.pre-comparison.md)

Diese Kopien stimmen mit den im ursprünglichen [Audit-Manifest](../security-evidence/manifest.json) gespeicherten Hashes der damaligen Dokumente überein. Das alte Manifest bleibt unverändert. Die heutigen Fassungen von Audit, Testanleitung, Portfolio und README wurden bewusst ergänzt bzw. neu gerahmt; ihre aktuellen Hashes gehören zum neuen Manifest. Ein alter Dokumenthash ist deshalb kein Versprechen, dass eine später ergänzte Dokumentfassung bytegleich geblieben ist.

Relative Links in den unveränderten Archivtexten beziehen sich auf ihren ursprünglichen Ort unter `docs/`. Für aktuelle Navigation die heutigen Berichte bzw. die Links dieses Verzeichnisses verwenden. Die abschließende Linkprüfung behandelt Archivtexte deshalb als historische Originaltexte mit dokumentiertem Ursprungsort.

## Reproduktion

Git-Vergleiche und der fixierte Quellenstand sind in [FORK_CONTRIBUTIONS.md](../FORK_CONTRIBUTIONS.md) dokumentiert. Die lokalen Beobachtungen lassen sich aus der Projektwurzel ausführen:

```powershell
.\.venv-security\Scripts\python -X utf8 tools/compare_fork_security.py
```

Die Ausgabe zeigt unter anderem **offene** Unterschiede. Exit 0 bedeutet, dass der Vergleich ausgeführt wurde; es bedeutet nicht, dass NAT64, Worker-Metadaten oder Log-Redaktion behoben sind. Die erste interne Probe hatte wegen einer fehlenden Typannotation im isolierten Ausführungskontext abgebrochen. Der dokumentierte erfolgreiche Lauf enthält diesen Harness-Fix (`Optional` im isolierten Namespace); es wurde keine Anwendung geändert und kein Ergebnis erfunden.

Für den Upstream-npm-Abgleich werden ausschließlich `dashboard/package.json` und `dashboard/package-lock.json` aus dem fixierten Git-Snapshot in ein neues temporäres Verzeichnis kopiert. Darin läuft `npm audit --package-lock-only --json --ignore-scripts`. Der Ablauf ist mit Python 3.11 und vorhandenem npm unter Windows reproduzierbar:

```python
import pathlib, subprocess, tempfile
repo = pathlib.Path.cwd()
sha = "894514049a3c6210a7cd65676344f44371b3717a"
with tempfile.TemporaryDirectory(prefix="openshorts-upstream-deps-") as directory:
    target = pathlib.Path(directory)
    for name in ("package.json", "package-lock.json"):
        data = subprocess.check_output(
            ["git", "show", f"{sha}:dashboard/{name}"], cwd=repo)
        (target / name).write_bytes(data)
    result = subprocess.run(
        ["npm.cmd", "audit", "--package-lock-only", "--json", "--ignore-scripts"],
        cwd=target, capture_output=True, timeout=90)
    print(result.stdout.decode("utf-8"))
    print("Exit:", result.returncode)
```

Ein Advisory-Abgleich kontaktiert öffentliche Paketdienste mit Paketmetadaten. Er führt keinen Exploit gegen Upstream aus. Datenbankergebnisse können sich nach dem dokumentierten Datum ändern. Abweichende Paketbäume und Trefferzahlen sind kein umfassendes Sicherheitsranking. Unter anderen Betriebssystemen ist der npm-Programmname anzupassen.
