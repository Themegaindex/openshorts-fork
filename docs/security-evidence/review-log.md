# Lokales Review-Protokoll

Datum: 2026-11-05. Drei getrennte KI-Reviewer arbeiteten read-only; einer erhielt einen frischen Kontext ohne die bisherige Audit-Konversation. Keiner hat Projektdateien geändert oder reale Cloud-Dienste aufgerufen. Dies ist keine Bestätigung durch einen externen Auditor.

## Runde 1

| Bereich | Validiertes Finding der Zwischenfassung | Reparatur / Regression |
| --- | --- | --- |
| Korrektheit/Medien | P1: `_recover_jobs_from_disk` wendet neue ID-Prüfung auf unbeteiligte Ausgabeordner an und bricht den Start ab | Nicht passende Verzeichnisnamen vor Zustandszugriff überspringen; `test_recovery_ignores_unrelated_output_directories` |
| Build/Repository; unabhängig doppelt gemeldet | P1: Docker-Builder kopiert die neu referenzierte Constraints-Datei nicht vor `pip install` | Builder-COPY enthält beide Dateien; `test_backend_builder_copies_constraints_before_installation` |
| Security | P1: `scope['path']` enthält ASGI-`root_path`; die Router-Normalisierung umgeht dadurch die neue Präfixprüfung | `get_route_path(scope)` für Zugriff und Limits; `test_mounted_api_keeps_authentication_and_origin_guards` |
| Konfiguration | P2: nur in der Root-`.env` konfiguriertes Backend-Token fehlt im lokalen Vite-Prozess | `loadEnv` mit `OPENSHORTS_`-Präfix serverseitig; synthetisches Token fehlt im Browser-Build |

Die Docker-Meldung wurde vor der Reparatur dedupliziert. Die vier Punkte sind Fehler der Audit-Zwischenfassung; sie werden nicht als zusätzliche ursprüngliche Schwachstellen gezählt.

## Runde 2

- Korrektheitsreview: keine bestätigten neuen Findings. Vier gezielte Assertions zu Wiederherstellung, Docker-Konfiguration und montierten Auth-Grenzen erneut ausgeführt und bestanden.
- Security-Review: keine bestätigten neuen Findings. Zusätzlich zehn netzwerkfreie Assertions mit `/prefix` und `/nested/prefix`: Remote-Ablehnung, Tokenpflicht, Tokenannahme, Origin-Ablehnung und Bodylimit bestehen.
- Frisches Repository-/Buildreview: keine bestätigten neuen Findings; korrigierte Docker-Datei, serverseitiges Env-Laden, Mount-Präfix und Wiederherstellung geprüft.

Der endgültige eigene Abschlusslauf umfasst die anschließend ergänzten Download-, Credential-Origin- und harmlosen Windows-Launcher-Tests. Anwendungslogik wurde nach Runde 2 nicht weiter geändert; nur eine veraltete Helper-Beschreibung präzisiert.

## Offene externe Gates

Die Repository-Regeln sehen für einen PR nach grüner Remote-CI weitere Claude-/Codex- und Security-Reviews vor. Hier gab es keinen Push/PR und keine Remote-CI. Diese Gates bleiben offen. Ein lokaler Reviewer hat keinen tatsächlichen Docker-Build oder echten Provider-/GPU-Lauf bestätigt. Es wurde nicht automatisch gemergt.
