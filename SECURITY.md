# Security Policy

OpenShorts ist eine selbst betriebene Videoanwendung für einen vertrauenswürdigen Betreiber. Die Anwendung ist keine Plattform mit getrennten Benutzerkonten oder Mandanten. Der lokale Audit vom **5. November 2026** ist in [SECURITY_AUDIT.md](docs/SECURITY_AUDIT.md) dokumentiert.

## Unterstützter Stand

| Stand | Sicherheitsstatus |
| --- | --- |
| Aktueller Quellstand mit den dokumentierten Hardening-Änderungen | Gegenstand dieses Audits; keine zugesicherte Wartungsdauer |
| Basis `98afa9ea5e9a842eb358834fe0cbd251d335573b` und ältere Stände | Enthalten die im Bericht nachgewiesenen Probleme; für diese Probleme aktualisieren |
| Andere Forks, ältere Installationen und abweichende Abhängigkeiten | Nicht durch diesen Audit abgedeckt |

Es gibt keine für diesen Bericht verifizierte Release- oder LTS-Zusage. Eine sichere Quelländerung aktualisiert vorhandene Python-Umgebungen nicht automatisch.

## Schwachstellen verantwortungsvoll melden

Falls im jeweiligen GitHub-Repository aktiviert, unter **Security → Report a vulnerability** eine private Meldung anlegen. Ist diese Funktion nicht verfügbar, muss der Betreiber zunächst einen privaten Kontaktweg bereitstellen. Dieser Bericht erfindet keine Kontaktadresse und behauptet keine bereits aktivierte GitHub-Funktion.

Eine Meldung sollte betroffenen Commit, Komponente, Voraussetzungen, Auswirkungen und einen ungefährlichen lokalen Reproduktionsfall enthalten. Keine Zugangsdaten, Cookies, Kundenvideos oder personenbezogenen Daten mitsenden. Zugangsdaten im Verdachtsfall über den jeweiligen Anbieter sperren bzw. erneuern.

Exploitdetails nicht als öffentliche Issue einstellen, solange keine koordinierte Veröffentlichung vereinbart wurde. Keine fremden Installationen, Cloud-Metadatendienste oder kostenpflichtigen APIs für Nachweise angreifen. Es wird keine garantierte Antwortfrist und kein Bug-Bounty-Programm zugesagt.

## Sicherheitsprinzipien

- Netzwerkzugriffe, Uploads, Dateinamen, Modellantworten und Galerie-Metadaten sind nicht vertrauenswürdig.
- API und lokale Medien sind standardmäßig nur für lokale Clients erreichbar. Bei gesetztem `OPENSHORTS_API_TOKEN` wird dieser auch lokal verlangt. Das Token ist ein Betreiber-Zugang, keine Benutzerverwaltung.
- Ein öffentlicher Betrieb benötigt einen authentifizierenden HTTPS-Gateway vor API **und** Medien. Der Gateway darf das Backend-Token erst nach erfolgreicher Anmeldung hinzufügen. Ein lokaler Reverse Proxy ist kein automatischer Authentifizierungsnachweis.
- Hosts und Browser-Ursprünge werden ausdrücklich zugelassen; Wildcards sind kein Ersatz für Authentifizierung. Vite bleibt ein lokaler Entwicklungsserver.
- Dateizugriffe verwenden serverseitige Namen und begrenzte Speicherbereiche. Die Medienrouten liefern keine JSON-Zustände oder Transkripte aus.
- Website- und Mediendownloads prüfen Adressen und Weiterleitungen und begrenzen die übertragenen Daten. Die YouTube-Funktion akzeptiert ausschließlich YouTube-URLs.
- Prozesse verwenden Argumentlisten; Modellantworten erzeugen keine freien FFmpeg-Reparaturfilter. Mengen, Uploads und parallele Arbeit sind begrenzt.
- `.env`, Cookies, erzeugte Medien und persönliche Konfiguration gehören nicht in Git oder Images. Browsergespeicherte API-Schlüssel bleiben sensible Daten.
- Die öffentlichen S3-Galerien sind eine bestehende Veröffentlichungsfunktion. Private Inhalte dort nicht verarbeiten, solange diese Funktion aktiv ist.

## Reproduktion und Grenzen

[Threat Model](docs/THREAT_MODEL.md), [Testanleitung](docs/SECURITY_TESTING.md) und [Portfolio-Zusammenfassung](docs/SECURITY_PORTFOLIO.md) beschreiben Schutzgrenzen und überprüfbare Nachweise. Dieses Repository behauptet weder vollständige Sicherheit noch eine Zertifizierung oder eine externe Audit-Bestätigung.
