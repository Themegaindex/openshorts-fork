# OpenShorts — Analyse & Roadmap

*Erstellt: 25.07.2026 · Basis: `main` inkl. nicht committeter Änderungen*
*Alle kritischen Befunde wurden mit echten FFmpeg-Renderings überprüft (nicht nur Code-Lektüre).*

> **Stand 25.07.2026 — Phasen 1 bis 3 sind umgesetzt.** Die dort aufgeführten
> Punkte sind erledigt und mit Tests sowie echten Renderings abgesichert; die
> Befundbeschreibungen bleiben als Begründung stehen. Offen sind nur noch
> Phase 4 und die Ideen in Abschnitt 6/10.

---

## Inhalt

1. [Kurze Zusammenfassung des Projekts](#1-kurze-zusammenfassung-des-projekts)
2. [Wichtigste gefundene Probleme](#2-wichtigste-gefundene-probleme)
3. [Mögliche Bugs](#3-mögliche-bugs)
4. [Verbesserungen für Bedienung und Nutzererlebnis](#4-verbesserungen-für-bedienung-und-nutzererlebnis)
5. [Verbesserungen an bestehenden Funktionen](#5-verbesserungen-an-bestehenden-funktionen)
6. [Ideen für sinnvolle neue Funktionen](#6-ideen-für-sinnvolle-neue-funktionen)
7. [Analyse der Untertitel-Presets](#7-analyse-der-untertitel-presets)
8. [Detaillierte Prüfung des neuen Neon-Presets](#8-detaillierte-prüfung-des-neuen-neon-presets)
9. [Priorisierte Empfehlung: Was zuerst?](#9-priorisierte-empfehlung-was-zuerst)
10. [Kleine, mittlere und größere Verbesserungsideen](#10-kleine-mittlere-und-größere-verbesserungsideen)
11. [Anhang: Messmethodik](#11-anhang-messmethodik)

**Legende:** 🔴 kritisch · 🟠 hoch · 🟡 mittel · 🟢 klein
**Status:** ✅ bestätigt (gemessen/gerendert) · ⚠️ Risiko (nicht abschließend geprüft) · 💡 Idee

---

## 1. Kurze Zusammenfassung des Projekts

OpenShorts macht aus langen Videos (YouTube-Link oder eigener Upload) automatisch kurze Hochkant-Clips für TikTok, Reels und Shorts.

Der Ablauf in einfachen Worten:

1. Video herunterladen oder hochladen
2. Sprache in Text umwandeln (Whisper, mit Zeitstempel pro Wort)
3. Szenenwechsel erkennen
4. Google Gemini sucht die 3–15 spannendsten Stellen
5. Diese Stellen werden ausgeschnitten
6. Das Bild wird auf 9:16 umgebaut — je nach Szene mit Zoom auf eine Person, geteiltem Bild für zwei Personen oder unscharfem Hintergrund
7. Danach optional: Untertitel, Text-Hook, KI-Schnitteffekte, Sprachsynchronisation, Veröffentlichung

Technisch: Python/FastAPI im Hintergrund, React im Browser, FFmpeg für alles Video-Bezogene. Rund **19.500 Zeilen** eigener Code.

**Genereller Eindruck:** Das Projekt ist deutlich reifer als übliche Hobby-Projekte. Es gibt eine echte Testsuite (86 Tests laufen grün durch), saubere Job-Wiederherstellung nach Abstürzen, Sperren gegen parallele Konflikte und ein durchdachtes „Ebenen"-System, das verhindert, dass Untertitel doppelt eingebrannt werden. Die Schwächen liegen **nicht** in der Architektur, sondern in konkreten Detailfehlern — und der größte davon steckt ausgerechnet im neuen Neon-Preset.

---

## 2. Wichtigste gefundene Probleme

| # | Problem | Wirkung | Schwere | Status |
|---|---|---|---|---|
| 1 | **Neon Sweep: Untertitel werden am Bildrand abgeschnitten** | Ab Schriftgröße 26 betrifft es die Mehrheit aller Blöcke | 🔴 | ✅ gerendert |
| 2 | **Schriftarten existieren im Docker-Container nicht** | Browser-Vorschau ≠ fertiges Video | 🔴 | ✅ gemessen |
| 3 | **Ton kann komplett verschwinden** | Stummer Clip ohne Warnung im UI | 🟠 | ✅ im Code belegt |
| 4 | **Untertitel lassen sich nie wieder entfernen** | Kein „Zurück" nach dem Einbrennen | 🟠 | ✅ im Code belegt |
| 5 | **Fehler bei „Für alle Clips" werden verschluckt** | 3 von 10 fehlgeschlagen = keine Meldung | 🟠 | ✅ im Code belegt |
| 6 | **Signature-Presets rendern ~6× langsamer** | 27 s statt 4,5 s pro 45-s-Clip | 🟡 | ✅ gemessen |

---

## 3. Mögliche Bugs

### 🔴 ✅ BESTÄTIGT — Neon Sweep: Zeilen werden am Bildrand abgeschnitten

**Beteiligte Stellen:**

```python
# subtitles.py:411
"WrapStyle: 2\n"     # = "brich NIEMALS automatisch um"

# subtitles.py:463
def _balanced_line_indices(block, max_line_chars=16):
```

**Zwei Fehler übereinander:**

**(a) Das Budget von 16 Zeichen ist fest verdrahtet.** Es passt sich weder an die Schriftgröße (Regler 14–40!) noch an die Schriftart an. Bei Schriftgröße 20 ist die Schrift im Video 120 px hoch — da passen ca. 11 Zeichen, nicht 16.

**(b) Es sind nie mehr als 2 Zeilen möglich.** `_balanced_line_indices` liefert nur die Werte `0` und `1`. Gleichzeitig erlaubt der Blockbau bis zu 34 Zeichen (`subtitles.py:708`). Bei einem 30-Zeichen-Block *muss* mindestens eine Zeile über 16 Zeichen kommen:

```
Block ['zu','meint','kompliziert','weil','viel']
→  Zeile 1: "ZU MEINT KOMPLIZIERT"  (20 Zeichen)
   Zeile 2: "WEIL VIEL"             (9 Zeichen)
```

**(c) Kein Sicherheitsnetz.** `WrapStyle: 2` schaltet den automatischen Umbruch von libass ab. Das klassische Preset benutzt `WrapStyle: 0` (`subtitles.py:782`) und wird deshalb im Zweifel gerettet.

**Messergebnis (45-s-Clip, 26 Untertitelblöcke, 1080×1920, echtes Arial Black):**

| Schriftgröße | am Rand abgeschnitten | grenzwertig (<25 px Rand) |
|---|---|---|
| 14 | 0 % | 0 % |
| **20 (Voreinstellung)** | **12 %** | **27 %** |
| 26 | 69 % | — |
| 30 | 85 % | — |
| 40 | ~100 % | — |

**Sichtbarer Beweis** (Schriftgröße 20, echter Render):

```
Frame b_11:   x=   0..1079   → beide Ränder abgeschnitten
              "DASS WIRKLICH WIRD"  ← D links halb abgeschnitten, D rechts weg
              "SO ES WIRD DASS"     ← S des roten aktiven Wortes angeschnitten
```

**Warum das schlimm ist:** Der Zuschauer sieht abgeschnittene Buchstaben am linken und rechten Bildrand — genau der Amateur-Eindruck, den ein Premium-Preset vermeiden soll. Und der Nutzer hat keine Chance, es vorher zu sehen: die Browser-Vorschau bricht automatisch um.

> **Korrektur meiner ersten Schätzung:** Ich hatte zunächst 87 % genannt, basierend auf der Annahme, die Seitenränder (`MarginL/R = 8`) würden die Textbreite auf 973 px begrenzen. Der Render zeigt: libass lässt zentrierten Text bei `WrapStyle: 2` die vollen 1080 px nutzen und schneidet erst dann ab. Bei der Voreinstellung sind es daher **12 %** — der Fehler ist real, aber bei Größe 20 seltener als zunächst berechnet. Ab Größe 26 wird er dominant.

**Wichtigkeit: Sehr hoch.** Erster Fix.

---

### 🔴 ✅ BESTÄTIGT — Die angebotenen Schriftarten gibt es im Container nicht

Die Oberfläche bietet 7 Schriftarten an, die Signature-Presets setzen fest auf „Arial Black", „Beast" auf „Impact". Der Docker-Container installiert aber nur:

```dockerfile
# Dockerfile:32-34
fontconfig \
fonts-dejavu-core \
fonts-liberation2 \
```

**Gemessene Auflösung (Linux/Docker):**

```
Arial          -> DejaVu Sans
Arial Black    -> DejaVu Sans     ← beide Signature-Presets!
Verdana        -> DejaVu Sans     ← 9 von 11 klassischen Presets!
Impact         -> DejaVu Sans     ← "Beast"-Preset!
Helvetica      -> DejaVu Sans
Georgia        -> DejaVu Serif
Courier New    -> DejaVu Sans Mono
```

**Gegenprobe unter Windows** (dort nutzt libass DirectWrite statt fontconfig):

```
[Parsed_ass_0] Using font provider directwrite (with GDI)
[Parsed_ass_0] fontselect: (Arial Black, 700, 0) -> Arial-Black, 0, Arial-Black
```

**Das ist der Kern des Problems:** Auf einem Windows-Rechner funktioniert alles. Im Docker-Container — dem in `CLAUDE.md` dokumentierten Standardweg — ist die Schrift eine andere. Fünf der sieben Auswahloptionen sind dort derselbe Font. Das Preset „Beast", dessen ganzer Charakter auf der schmalen Impact-Optik beruht, unterscheidet sich am Ende nur noch durch GROSSBUCHSTABEN und einen dickeren Rand.

**Fix:** `fonts-liberation` ergänzen (liefert Arial-Metriken) plus einen kondensierten Font als Impact-Ersatz — oder die Auswahl ehrlich auf das reduzieren, was vorhanden ist.

---

### 🟠 ✅ BESTÄTIGT — Clips können stumm werden

```python
# main.py:1572
temp_audio_output = f"{base_name}_temp_audio.aac"
# main.py:1831
'ffmpeg', '-y', '-i', input_video, '-vn', '-acodec', 'copy', temp_audio_output
```

Der Ton wird **unverändert kopiert** in eine `.aac`-Datei. Das klappt nur, wenn er bereits AAC ist. Bei einer hochgeladenen MKV mit AC3-, Opus- oder Vorbis-Ton schlägt es fehl — und dann:

```python
except subprocess.CalledProcessError:
    print("\n   ❌ Audio extraction failed (maybe no audio?). Proceeding without audio.")
    pass
```

Es wird **einfach ohne Ton weitergemacht**. Der Nutzer bekommt fertige Clips ohne jede Warnung und merkt es erst beim Abspielen.

**Fix:** Bei Fehlschlag zu AAC umkodieren (`-c:a aac -b:a 192k`) statt aufzugeben, und die Warnung als sichtbare Meldung ins Job-Log heben.

---

### ⚠️ RISIKO — Verzerrung bei 1:1 und Original-Format

```python
# subtitles.py:409-410
"PlayResX: 162\n"
"PlayResY: 288\n"
```

Feste 9:16-Zeichenfläche. Das Projekt unterstützt aber auch **1:1** und **Original (16:9)**. Bei diesen Formaten passt das Seitenverhältnis nicht — je nach FFmpeg-Version kann die Schrift horizontal verzerrt werden. Der klassische Pfad setzt `PlayResX` gar nicht und hat das Risiko nicht.

**Nicht abschließend geprüft.** Ein Testrender mit „1:1" + Neon Sweep klärt es in einer Minute.

---

### 🟡 ✅ BESTÄTIGT — Farbschleier über den weißen Wörtern

In `_generate_neon_sweep_ass` (`subtitles.py:545–571`):

```python
f"Dialogue: {layer},{start},{end},..."        # weiß:   0,1,2,3
f"Dialogue: {layer + 4},{start},{end},..."    # farbig: 4,5,6,7
```

In ASS liegt eine höhere Ebene über der niedrigeren. Damit liegt der **breite Farb-Nebel** des aktiven Wortes (Weichzeichnung 70 px) **über dem scharfen weißen Kern** der Nachbarwörter.

Im gerenderten Frame sichtbar: das rote aktive Wort wirkt matt und geht im weißen Glühen der Nachbarwörter unter, statt hervorzustechen.

**Fix:** Reihenfolge so umbauen, dass die scharfen Kerne beider Farben ganz oben liegen — z. B. Ebenen 0/1 = Nebel, 2/3 = mittlerer Glanz, 4/5 = scharfe Kerne.

---

### 🟡 ✅ BESTÄTIGT — Weiße Wörter glühen genauso stark wie das aktive

Beide Masken benutzen dieselben vier Glüh-Ebenen mit Weichzeichnung 70 und 28. Im CapCut-Original glüht vor allem das *aktive* Wort. Hier bekommt der gesamte Untertitelblock einen breiten weißen Nebel — im Testrender deutlich zu sehen. Auf hellem Videohintergrund wird das zu Matsch.

**Fix:** Für die weiße Maske nur 2 statt 4 Glüh-Ebenen, oder deren Alpha deutlich reduzieren. Nebeneffekt: halbiert die Renderzeit.

---

### 🟡 ✅ BESTÄTIGT — Farbwechsel: Zeile 1 springt zurück auf Weiß

```python
active_mask = [line_indices[index] == active_line and index <= active_index ...]
```

Sobald Zeile 2 beginnt, wird Zeile 1 **komplett wieder weiß** — ein sichtbarer Sprung mitten im Block. Falls gewollt: okay. Falls nicht: die Zeilenbedingung entfernen, nur `index <= active_index` prüfen.

---

### 🟡 ✅ BESTÄTIGT — Rand-Regler „Keine" erzeugt trotzdem einen Rand

```python
# subtitles.py:750 und 930
outline_width = max(1, int(border_width))
```

Der Regler geht von 0 („None") bis 5. Bei 0 wird trotzdem 1 gesetzt. Ein randloser Untertitel ist unmöglich, obwohl die Oberfläche es verspricht.

---

### 🟡 ✅ BESTÄTIGT — Toter Code im Neon-Modul

```python
# subtitles.py:366
_SIGNATURE_GLOW_LAYERS = (...)   # wird NIRGENDS verwendet
```

Neon Sweep nutzt `_NEON_SWEEP_GLOW_LAYERS`, Rainbow Word hat dieselben Werte nochmal separat inline (`subtitles.py:666–672`). **Drei fast identische Zahlenblöcke, einer davon Leiche.** Wer die Glüh-Werte anpassen will, erwischt mit hoher Wahrscheinlichkeit den falschen.

Weiterer toter Code:
- `hooks.py:322` `add_hook_to_video()`
- `subtitles.py:1039` `burn_subtitles()`
- `Gallery.jsx` + `GalleryCard.jsx` + auskommentierter Endpunkt `app.py:3032` (~350 Zeilen)

---

### 🟡 ✅ BESTÄTIGT — Fehlermeldungen als rohes JSON

```javascript
// ResultCard.jsx:126-129
if (!res.ok) {
    const errText = await res.text();
    throw new Error(errText);       // ← kein JSON.parse!
}
```

Der Nutzer sieht im roten Kasten:

```
{"detail":"No words found for this clip range."}
```

Bei „Auto Edit" und „Dub Voice" ist es korrekt gemacht (`jsonErr.detail`). **Dieselbe Datei, zwei Qualitätsstufen.**

---

### 🟡 ✅ BESTÄTIGT — Geheimnisse landen im Docker-Image

`.dockerignore` schließt `.env` und `*_cookies.txt` **nicht** aus, während der Dockerfile `COPY . .` macht. Damit landen `.env` und die YouTube-Cookies fest in einer Image-Ebene. Solange das Image nie geteilt wird, harmlos — aber eine Falle.

Ebenfalls: `allow_origins=["*"]` (`app.py:1050`) ohne Authentifizierung, und `/videos` gibt das gesamte Ausgabeverzeichnis frei. Für lokalen Betrieb okay, beim Öffnen ins Internet ein Problem.

---

### 🟡 ✅ BESTÄTIGT — Zeilenenden-Chaos im Repository

`git diff --stat` meldet 10.217 geänderte Zeilen — fast alle sind **keine echten Änderungen**, sondern CRLF↔LF-Umschaltungen:

```
app.py    | 7385 ++++++++--------    ← in Wahrheit: 5 echte Zeilen
main.py   | 5146 ++++++++--------
```

Es fehlt eine `.gitattributes`. **Folge:** Code-Reviews sind unbrauchbar, weil echte Änderungen im Rauschen verschwinden.

---

## 4. Verbesserungen für Bedienung und Nutzererlebnis

### 🔴 Untertitel lassen sich nicht mehr entfernen

Es gibt keinen Endpunkt und keinen Knopf, um eine Untertitel- oder Hook-Ebene zu **entfernen**. Der Zustand `subtitle: None` existiert im Code (`_entry_with_clean_source(..., clear_subtitle=True)`) — wird aber ausschließlich intern von der Übersetzung genutzt.

**Für den Nutzer:** Er probiert Neon Sweep, es gefällt nicht → er kann nur ein anderes Preset drüberlegen. Zum sauberen Original kommt er nie zurück.

**Vorschlag:** Ein kleines „Ebenen"-Feld auf jeder Clip-Karte: `[Untertitel ✕] [Hook ✕] [Auto-Edit ✕]`. Die Infrastruktur (saubere Quelle + Ebenen) ist **bereits vollständig gebaut** — es fehlt nur der Knopf.
**Aufwand: klein. Wirkung: sehr groß.**

---

### 🔴 „Untertitel für alle" — blinder Fortschritt, verschluckte Fehler

**(a) Kein sichtbarer Fortschritt.** Der Zähler `1/10, 2/10 …` steht auf dem Knopf *hinter* dem Modal — verdeckt vom dunklen Hintergrund. Sichtbar ist nur ein Spinner mit „Generating…", 10+ Minuten lang. Kein Abbrechen möglich.

**(b) Fehler verschwinden.** `App.jsx:712`:

```javascript
setBulkSubProgress({ running: false, current: total, total, errors });
```

`errors` wird gezählt — und **nirgendwo im UI angezeigt**. Wenn 3 von 10 Clips scheitern, meldet die Oberfläche stillschweigend Erfolg.

**Vorschlag:** Fortschrittsbalken *im* Modal, Abbrechen-Knopf, am Ende „8 von 10 erfolgreich — 2 fehlgeschlagen (anzeigen)".

---

### 🟠 Untertitel-Einstellungen werden nicht gemerkt

Jede Clip-Karte hat ihre **eigene** `SubtitleModal`-Instanz mit eigenem Zustand. Wer für Clip 1 mühsam Farbe, Größe, Position und Preset einstellt und dann Clip 2 öffnet, fängt bei Null an. Auch beim nächsten Job.

**Vorschlag:** Zuletzt benutzten Stil in `localStorage` sichern und als Startwert laden. Optional „Als Standard speichern". **Aufwand: sehr klein.**

---

### 🟠 Hook und Untertitel können sich überlagern

- Hook „oben" landet bei **20 % der Bildhöhe** (`hooks.py:317`)
- Untertitel „oben" landet bei **ca. 9 %** und wächst nach unten (MarginV 25 bzw. 28 bei PlayResY 288)

Bei zweizeiligen Untertiteln in großer Schrift überlappen sich beide. Keine Warnung, keine gemeinsame Vorschau.

**Vorschlag:** Warnhinweis bei gleicher Seite — oder Untertitel automatisch nach unten schieben, wenn oben ein Hook sitzt.

---

### 🟠 Signature-Presets: Regler, die nichts tun (und heimlich das Preset löschen)

Bei aktivem Neon Sweep ignoriert der Generator **komplett**: Textfarbe, Highlight-Farbe, Rand, Hintergrundbox, Effekt, „Inaktive Wörter dimmen", GROSSBUCHSTABEN.

Sie werden aber **weiterhin bedienbar angezeigt**. Und schlimmer: Jeder dieser Regler ruft `markCustom()` auf (`SubtitleModal.jsx:156`), was `preset` still auf `'custom'` zurücksetzt. **Der Nutzer schiebt am Dimm-Regler — und hat unbemerkt das Neon-Preset verloren.**

**Vorschlag:** Bei aktivem Signature-Preset die wirkungslosen Regler ausgrauen mit dem Hinweis „von diesem Preset gesteuert". Bedienbar bleiben nur Position, Größe, Schriftart — genau die drei, die tatsächlich wirken.

---

### 🟡 Kleinere Bedienungspunkte

| Fund | Wirkung |
|---|---|
| **Zwei Presets heißen „Neon"** — klassisches `neon` (#00FF88) und `Neon Sweep` | Verwirrend beim Beschreiben |
| **Gemischte Sprache im UI** — „Subtitles für alle", „Letzte Aktivität", „Der Job hängt wahrscheinlich…" mitten im englischen UI | Wirkt unfertig |
| **Kein Ladefortschritt beim Einbrennen** — nur Spinner, obwohl FFmpeg bei Neon Sweep ~27 s pro 45-s-Clip braucht | Nutzer weiß nicht, ob es hängt |
| **Vorschau-Beispieltexte inkonsistent** — Neon zeigt „UND MEINT / DER ANDERE" (deutsch), Rainbow zeigt „BROWN" (englisch) | Unsauber |
| **Vorschau-Position skaliert nicht** — `top-20`/`bottom-20` sind feste 80 px, unabhängig von der Vorschaugröße | Position stimmt bei kleinem Fenster nicht |
| **Schriftgröße bleibt nach Preset-Wechsel hängen** — Rainbow setzt 29, danach „TikTok" wählen lässt 29 stehen (klassische Presets definieren keine Größe) | TikTok sieht plötzlich riesig aus |
| **Download lädt komplettes Video in den RAM** (`fetch`→`blob`, `ResultCard.jsx:458`) | Bei großen Dateien unnötig langsam |
| **Irreführende Fehlermeldung** bei kaputtem Video im Dub-Pfad: „No words found for this clip range" statt „Video nicht lesbar" | Falsche Fährte |

---

## 5. Verbesserungen an bestehenden Funktionen

### 🟠 Zwischendateien werden nie aufgeräumt

Jeder Untertitel-Durchlauf erzeugt zwei neue Dateien im Job-Ordner:
- `subs_<clip>_<id>.ass`
- `subtitled_<id>_<quelle>.mp4` (kompletter neuer Videoclip!)

Die alten bleiben liegen. Gelöscht wird erst nach **24 Stunden**, wenn der ganze Job weg ist.

**Rechenbeispiel:** 10 Clips × 5 ausprobierte Presets = **50 zusätzliche Videodateien**, mehrere Gigabyte.

**Vorschlag:** Beim Speichern eines neuen Renders die vorherige Version desselben Clips löschen — oder zwei Versionen behalten, dann bekommt man „Rückgängig" gratis dazu.

---

### 🟠 ✅ GEMESSEN — Signature-Presets sind ~6× langsamer

**Renderzeit, 45-s-Clip, 1080×1920, echtes Arial Black:**

| Preset | Renderzeit | ASS-Datei | Zeichenbefehle |
|---|---|---|---|
| klassisch (Glow) | **4,5 s** | 12,5 KB | 143 |
| Neon Sweep | **26,8 s** | 330 KB | 1.144 |
| Rainbow Word | **25,8 s** | 877 KB | 572 |

Neon Sweep erzeugt **8× so viele Zeichenbefehle** wie nötig (4 Glüh-Ebenen × weiße + farbige Maske). Rainbow Word erzeugt einzelne Zeilen mit **4.563 Zeichen** — jeder Buchstabe bekommt bis zu 16 Farbübergänge einzeln.

Bei einem Job mit 10 Clips à 45 s: **4,5 Minuten statt 45 Sekunden.** Ohne Fortschrittsanzeige.

**Vorschlag:** Weiße Maske auf 2 Glüh-Ebenen reduzieren (halbiert die Zeit) und dem Nutzer im UI sagen, dass diese Presets länger brauchen.

---

### 🟡 Whisper-Wortblöcke sind nicht an die Optik gekoppelt

`max_chars` (Zeichen pro Block) und `max_line_chars` (Zeichen pro Zeile) sind zwei unabhängige Konstanten, die nichts voneinander wissen — die Ursache von Problem #1.

**Fix an der Wurzel:** Eine Funktion, die die Zeichenbreite aus der Schriftgröße abschätzt und daraus **beide** Werte berechnet:

```
nutzbare_breite_px = video_breite * 0.90
zeichen_pro_zeile  = nutzbare_breite_px / (schriftgröße_px * breitenfaktor)
max_chars          = 2 * zeichen_pro_zeile
```

Damit funktioniert das Preset bei **jeder** Schriftgröße und in **jedem** Format.

---

### 🟡 Vorschau ist nicht ehrlich

| | Vorschau (Browser) | Render (Video) |
|---|---|---|
| Schrift | echtes Arial Black (Windows/Mac) | DejaVu Sans Bold (Docker) |
| Buchstabenabstand | `-0.035em` (3,5 % schmaler) | `\fscx104` (4 % **breiter**) |
| Zeilenumbruch | automatisch | **nie** (`WrapStyle: 2`) |
| Farbwechsel | alle 4,8 s per CSS-Animation | pro Untertitelblock |

Die Vorschau **kann gar nicht zeigen**, dass Text aus dem Bild läuft — sie bricht ja automatisch um.

**Vorschlag (mittelfristig, sehr wirkungsvoll):** Knopf „Vorschau rendern", der die ersten 3 Sekunden serverseitig mit FFmpeg erzeugt. Dann sieht der Nutzer *exakt*, was er bekommt.

---

## 6. Ideen für sinnvolle neue Funktionen

**💡 Klein & sofort nützlich**

- **Untertitel-Text bearbeiten vor dem Einbrennen.** Whisper macht bei Namen und Fachbegriffen Fehler. Ein einfacher Editor für die erkannten Blöcke wäre vermutlich die meistgenutzte Funktion überhaupt.
- **Ebenen entfernen** (siehe Abschnitt 4) — praktisch schon gebaut.
- **Stil-Voreinstellung merken** über Clips und Jobs hinweg.
- **Untertitel-Datei separat herunterladen** (.srt/.ass) für externe Weiterverarbeitung.

**💡 Mittel**

- **Echte Video-Vorschau** der ersten Sekunden statt CSS-Nachbau.
- **Eigene Presets speichern.** Einmal den Kanal-Stil einstellen und benennen.
- **Untertitel-Sicherheitszone anzeigen** — TikTok/Reels blenden unten UI ein; ein Rahmen in der Vorschau zeigt, wo Text verdeckt wird.
- **Emoji-Untertitel.** Der Hook-Renderer kann bereits farbige Emojis (`hooks.py`), die Untertitel nicht. Bei TikTok Standard.
- **Feineinstellung der Position** — statt nur oben/mitte/unten ein Prozent-Regler.

**💡 Größer**

- **Untertitel als eigenständige Ebene mit Live-Editor.** Timeline mit Blöcken, Text ändern, Zeiten verschieben, dann einmal rendern.
- **Automatische Betonung.** Whisper liefert Lautstärke-Informationen — laute/betonte Wörter automatisch größer oder in Signalfarbe. Genau der Effekt, den erfolgreiche Creator manuell bauen.
- **A/B-Vorschau** — derselbe Clip mit zwei Presets nebeneinander.

---

## 7. Analyse der Untertitel-Presets

### Was es gibt

- **2 Signature-Presets** (eigener Renderer, 4 Glüh-Ebenen): Neon Sweep, Rainbow Word
- **11 klassische Presets**: TikTok, Reels, Shorts Pop, Gold Glow, Neon, Cyber, Karaoke, Minimal, Beast, Boxed, Classic

### Die ehrliche Bewertung: Es sind eigentlich nur 4 Looks

| Preset | Effekt | Schrift | Rand | GROSS | echter Unterschied |
|---|---|---|---|---|---|
| TikTok | keiner | Verdana | 2 | nein | nur Farbe |
| Reels | keiner | Verdana | 2 | nein | **= TikTok, andere Farbe** |
| Karaoke | keiner | Verdana | 2 | nein | **= TikTok, andere Farbe** |
| Minimal | keiner | Verdana | 1 | nein | fast = TikTok |
| Gold Glow | Glow | Verdana | 2 | nein | nur Farbe |
| Neon | Glow | Verdana | 2 | nein | **= Gold, andere Farbe** |
| Cyber | Glow | Verdana | 2 | nein | **= Gold, andere Farbe** |
| Shorts Pop | Bounce | Verdana | 2 | nein | nur Farbe |
| Beast | Bounce | Impact | 3 | **ja** | einziger echter Ausreißer |
| Boxed | Box | Verdana | 2 | nein | eigener Look |
| Classic | — | Verdana | 2 | nein | kein Karaoke |

**Ergebnis:** Von 11 Presets sind **7 reine Farbvarianten** desselben Looks. 9 von 11 nutzen dieselbe Schrift. Nur eines nutzt Großbuchstaben. Und im Docker-Container fällt Impact ohnehin auf DejaVu Sans zurück — damit verliert selbst „Beast" seinen Charakter.

### Was komplett fehlt

- Kein Preset mit **Hintergrundbox** außer „Boxed" (die beliebteste TikTok-Optik: schwarzer Balken hinter weißem Text)
- Kein Preset mit **Schatten statt Rand**
- Kein Preset in **Kleinbuchstaben** (der „Ästhetik"-Look)
- Kein Preset mit **schmaler/kondensierter Schrift**
- Kein Preset mit **wortweisem Einblenden ohne Farbe** (der „Podcast"-Look)
- Alle Presets sind **mittig zentriert** — kein linksbündiger Look

### 💡 Konkrete Vorschläge für neue Presets

| Name | Umsetzung | Warum |
|---|---|---|
| **Bold Box** | Weißer Text, schwarze Box (`bg_opacity: 0.85`), GROSS | Der meistgenutzte TikTok-Stil überhaupt — fehlt |
| **Soft Lower** | Kleinbuchstaben, dünner Rand, weiches Weiß | „Ästhetik"-Trend, passt zu Lifestyle-Content |
| **Podcast** | Kein Farbwechsel, nur Aufhellung des aktiven Wortes (dim 0.5 → 1.0), ohne Effekt | Ruhiger, seriöser Look für Erklärstücke |
| **Highlight Marker** | Aktives Wort mit farbigem Balken hinterlegt (Box-Effekt nur beim aktiven Wort) | Wirkt wie ein Textmarker, sehr lesbar |
| **Duo Tone** | Zwei feste Farben, die pro Block wechseln (wie Neon Sweep, aber ohne Glühen) | Neon-Sweep-Optik ohne die 6× Renderkosten |

---

## 8. Detaillierte Prüfung des neuen Neon-Presets

### ✅ Was korrekt eingebunden ist

| Prüfpunkt | Ergebnis |
|---|---|
| Frontend-Definition | ✅ `SubtitleModal.jsx:15-29`, eigene Karte, Vorschau, Beschreibung |
| Übertragung ans Backend | ✅ `ResultCard.jsx:117` und `App.jsx:697` |
| Backend-Validierung | ✅ `app.py:2062` — `Literal["custom","neon_sweep","rainbow_word"]` |
| Erzwingung des ASS-Pfads | ✅ `app.py:2117` — auch bei `style="classic"` landet man korrekt beim Neon-Renderer |
| Weiterleitung an den Generator | ✅ `subtitles.py:718-721` |
| Speicherung im Ebenen-Zustand | ✅ `app.py:2169` |
| Auch für synchronisierte Videos | ✅ `generate_srt_from_video(..., style="karaoke", **karaoke_opts)` reicht `preset` durch |
| Tests | ✅ `tests/test_subtitles.py:278` prüft Ebenen, Palette, kumulative Maske, Umbruch — **86 Tests grün** |
| CSS-Vorschau | ✅ inkl. `prefers-reduced-motion`-Rücksicht |

**Die Verdrahtung ist tadellos.** Alle Probleme liegen im Render-Verhalten.

### ❌ Was nicht funktioniert

1. **🔴 Zeilen werden abgeschnitten** — visuell bestätigt, siehe Abschnitt 3
2. **🔴 Kein Schutz bei größerer Schrift** — ab Größe 26 sind 69 % der Blöcke betroffen
3. **🟡 Farbschleier über weißem Text** — Ebenenreihenfolge 0–3 unter 4–7
4. **🟡 Weiße Wörter glühen genauso stark wie das aktive** — im Render deutlich sichtbar
5. **🟡 Zeile 1 springt beim Wechsel zu Zeile 2 zurück auf Weiß**
6. **🟡 Nicht wirksame Regler bleiben bedienbar** und werfen das Preset unbemerkt weg
7. **🟠 ~6× langsamer** als der klassische Pfad

### Zusammenspiel von Farben, Effekten, Abständen, Größen

| Aspekt | Bewertung |
|---|---|
| **Farbpalette** (`18F8F4` Türkis / `19FF43` Grün / `FF2038` Rot) | ✅ Gut gewählt, hoher Kontrast, wechselt pro Block — genau der CapCut-Effekt |
| **Glüh-Stufen** (70 / 28 / 3,5 / 0,08 px) | ✅ Sinnvoll abgestuft, im Render überzeugend |
| **Ränder** (0,0 / 0,0 / 0,85 / 0,15) | ✅ Kohärent: erst reine Weichzeichnung, dann Kern mit dünner Kontur |
| **Breitenskalierung** `\fscx104` | ⚠️ Verschärft den Überlauf um 4 % und widerspricht der Vorschau (dort `-0.035em`, also schmaler) |
| **Seitenränder** (MarginL/R 8 bei PlayResX 162 = 4,9 %) | ⚠️ Sinnvoll gewählt, aber wirkungslos, weil `WrapStyle: 2` den Text darüber hinauslaufen lässt |
| **Schriftgröße** 20 → 18 → 120 px im Video | ⚠️ Grenzwertig groß; 14–16 wäre für zweizeilige Untertitel sicherer |
| **Zeitverhalten** (`max_duration` 2,6 s, `max_chars` 34) | ⚠️ Zu großzügig für nur 2 Zeilen. 24 Zeichen würden zum 16er-Budget passen |

### Randfälle — geprüft

| Fall | Verhalten |
|---|---|
| Sehr langes Wort (>11 Zeichen) | ✅ wird gleichmäßig geteilt (`_word_display_segments`) |
| Wort mit Länge 0 | ✅ bekommt eine ASS-Zeiteinheit, verschwindet nicht |
| Überlappende Whisper-Zeitstempel | ✅ geklammert und dedupliziert |
| Geschweifte Klammern im Text | ✅ `_escape_ass_text` neutralisiert sie |
| Ungültige Schriftnamen | ✅ `_sanitize_font_name` filtert Sonderzeichen |
| Leerer Transkript-Bereich | ✅ liefert `False` → HTTP 400 |
| Video-Dauer nicht ermittelbar (Dub-Pfad) | ⚠️ irreführende Meldung „No words found…" |
| Format 1:1 / Original | ⚠️ ungeprüft, mögliche Verzerrung |

### ✅ Rainbow Word ist in Ordnung — Entwarnung

Ich hatte zunächst vermutet, dass lange deutsche Wörter auch bei Rainbow Word überlaufen. **Der Render widerlegt das:**

| Wort | Zeichen | gerenderte Breite | Ergebnis |
|---|---|---|---|
| ANDERE | 6 | 508 px | ok |
| VERSTEHEN | 9 | 747 px | ok |
| JAHRHUNDERT | 11 | 936 px von 1080 | ok (knapp) |
| ZUSAMMENARBEIT | 14 | 628 px (2 Zeilen) | ok |
| WAHRSCHEINLICH | 14 | 640 px (2 Zeilen) | ok |

Die Grenze `max_segment_chars=11` greift genau richtig: 11 Zeichen sind der Worst Case und passen mit 72 px Rand. **Rainbow Word braucht keinen Fix am Umbruch** — nur die Renderzeit und die wirkungslosen Regler betreffen es.

### Fazit zum Neon-Preset

Die **Idee und die Ausführung des Glüh-Effekts sind ausgezeichnet.** Der vierstufige Aufbau mit weichgezeichneten Glyphen-Kopien statt eines dicken Rands ist genau der richtige Ansatz und deutlich besser als das, was die meisten Projekte machen. Die Anbindung ans System ist sauber, getestet und sicher. Im Render sieht die Optik überzeugend aus.

**Aber:** Der Zeilenumbruch macht das Preset ab Schriftgröße 26 unbrauchbar und ist auch bei der Voreinstellung ohne Sicherheitsreserve. Das ist **ein einziger, klar lokalisierter Fehler** — kein Konzeptproblem.

---

## 9. Priorisierte Empfehlung: Was zuerst?

### Phase 1 — sofort (macht das neue Preset benutzbar) — ✅ erledigt

- [x] **Zeilenbudget an Schriftgröße koppeln.** `max_line_chars` aus der Schriftgröße berechnen statt fest 16. Zusätzlich `max_chars` beim Blockbau auf `2 × max_line_chars` begrenzen.
  *→ 1–2 Stunden, behebt den schwerwiegendsten Fehler*
- [x] **Sicherheitsnetz einbauen:** `WrapStyle: 2` → `WrapStyle: 0`. Dann bricht libass im Zweifel selbst um, statt abzuschneiden. Der eigene `\N`-Umbruch funktioniert weiter.
  *→ 1 Zeile*
- [x] **Fonts im Dockerfile ergänzen:** `fonts-liberation` (Arial-Metriken) + kondensierter Font als Impact-Ersatz. Alternativ die Schriftauswahl auf das Vorhandene reduzieren.
  *→ 15 Minuten*
- [x] **`.gitattributes` anlegen** mit `* text=auto eol=lf`
  *→ 2 Minuten, macht alle künftigen Reviews wieder lesbar*

### Phase 2 — diese Woche (Vertrauen & Sicherheit) — ✅ erledigt

- [x] **Ton-Absicherung:** bei Kopier-Fehler nach AAC umkodieren statt stumm weiterzumachen
- [x] **Fehler bei „für alle Clips" anzeigen** + Fortschritt im Modal + Abbrechen-Knopf
- [x] **JSON-Fehlermeldungen parsen** in `handleSubtitle` und `handleHook`
- [x] **`.env` und `*_cookies.txt` in `.dockerignore`** aufnehmen

### Phase 3 — danach (spürbarer Komfort) — ✅ erledigt

- [x] **„Ebene entfernen"-Knöpfe** — größter Nutzen pro Zeile Code im ganzen Projekt
- [x] **Alte Renderings löschen** beim Überschreiben (spart Gigabyte, schenkt „Rückgängig")
- [x] **Untertitel-Stil merken** über Clips und Sitzungen hinweg
- [x] **Nicht wirksame Regler ausgrauen** bei aktivem Signature-Preset
- [x] **Weiße Glüh-Maske auf 2 Ebenen reduzieren** (halbiert die Renderzeit)
- [x] **Ebenenreihenfolge korrigieren** (scharfe Kerne nach oben)

### Phase 4 — mittelfristig (Qualitätssprung) — offen

- [ ] **Echte Server-Vorschau** der ersten 3 Sekunden
- [ ] **Untertitel-Text vor dem Einbrennen editierbar**
- [ ] **Neue Presets** aus Abschnitt 7 (besonders „Bold Box" und „Podcast")
- [ ] **Toten Code entfernen** (Gallery, `add_hook_to_video`, `burn_subtitles`, `_SIGNATURE_GLOW_LAYERS`)
- [ ] **1:1- und Original-Format mit Signature-Presets testen**

---

## 10. Kleine, mittlere und größere Verbesserungsideen

### 🟢 Klein (jeweils unter einer Stunde)

- `.gitattributes` anlegen
- `_SIGNATURE_GLOW_LAYERS` löschen, Rainbow Word auf eine gemeinsame Konstante umstellen
- `outline_width = max(1, ...)` → echtes 0 zulassen
- `JSON.parse` in den zwei Fehlerbehandlungen ergänzen
- Deutsche UI-Texte übersetzen (3 Stellen) oder ganz auf Deutsch umstellen
- Klassisches „Neon"-Preset umbenennen (z. B. „Mint Glow")
- Klassische Presets bekommen eine `fontSize`, damit ein Wechsel die Größe zurücksetzt
- `.env` / Cookies in `.dockerignore`
- Vorschau-Beispieltext vereinheitlichen (beide deutsch oder beide englisch)

### 🟡 Mittel (halber bis ganzer Tag)

- **Zeilenbudget aus Schriftgröße berechnen** ← der wichtigste Punkt
- Fonts im Container korrigieren
- Ton-Umkodierung als Rückfallebene
- Bulk-Fortschritt + Fehlerbericht + Abbrechen
- „Ebene entfernen"-Endpunkt und Knöpfe
- Aufräumen alter Renderings pro Clip
- Untertitel-Stil in `localStorage` merken
- Ebenenreihenfolge im Neon-Renderer korrigieren
- Warnung bei Hook/Untertitel-Kollision
- 3 neue Presets (Bold Box, Podcast, Highlight Marker)

### 🔵 Groß (mehrere Tage, dafür großer Effekt)

- **Echte serverseitige Vorschau** — löst das Vorschau-Ehrlichkeitsproblem komplett
- **Untertitel-Editor** mit Timeline: Text korrigieren, Zeiten verschieben, dann rendern
- **Eigene Presets speichern und benennen**
- **Automatische Betonung** anhand der Lautstärke aus Whisper
- **Zugriffsschutz** (einfacher Token), falls die Instanz je öffentlich erreichbar sein soll
- **Signierte, ablaufende Video-Links** statt eines offenen `/videos`-Verzeichnisses

---

## 11. Anhang: Messmethodik

Damit alle Zahlen nachvollziehbar sind — das habe ich tatsächlich ausgeführt:

**Statische Prüfung**
- Komplette Lektüre von `subtitles.py`, `app.py` (Untertitel-/Ebenen-Teil), `main.py` (Renderpfad), `hooks.py`, `render_planning.py`, `clip_selection.py`, `editor.py`, `edit_builder.py`, `SubtitleModal.jsx`, `ResultCard.jsx`, `App.jsx`
- **86 Tests ausgeführt** (`test_subtitles`, `test_render_planning`, `test_clip_selection`) — alle grün
- Schriftauflösung mit `fc-match` für alle 7 angebotenen Schriften geprüft

**Dynamische Prüfung (echte Renderings)**
- FFmpeg 8.0 (`C:\ffmpeg\bin\ffmpeg.exe`, mit `--enable-libass`), aufgerufen aus WSL
- Testvideo 1080×1920, 45 s, generiert per `lavfi`
- ASS-Dateien mit der **projekteigenen** `generate_ass()` erzeugt, deutsches Testtranskript (143 Wörter, 26 Blöcke)
- Untertitel eingebrannt, dann pro Block ein Einzelbild extrahiert
- Textausdehnung pixelgenau vermessen (hellster Kern bzw. Abweichung vom Hintergrund), links/rechts geprüft auf Abschneiden
- Wiederholt für Schriftgrößen 14 / 20 / 26 / 30 / 40
- Renderzeiten gemessen mit `-f null -`

**Nicht geprüft**
- Ausgabeformate 1:1 und Original mit Signature-Presets (siehe offener Punkt in Phase 4)
- Verhalten im tatsächlichen Docker-Container (nur die Font-Auflösung wurde auf Linux nachgestellt)
- Der komplette Pipeline-Durchlauf mit echtem YouTube-Video

**Korrekturen gegenüber meiner ersten Einschätzung**
- Überlaufquote bei Neon Sweep, Schriftgröße 20: **12 %** statt der zunächst berechneten 87 %. Grund: libass lässt zentrierten Text bei `WrapStyle: 2` die volle Bildbreite nutzen, nicht nur den Bereich innerhalb der Seitenränder. Ab Größe 26 steigt die Quote auf 69 %, bei 30 auf 85 %.
- **Rainbow Word ist unkritisch** — die Wortteilung bei 11 Zeichen greift korrekt, alle getesteten langen deutschen Wörter passen ins Bild.
