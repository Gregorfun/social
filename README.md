# Social Auto-Poster

Ein automatisiertes Social-Media-System fuer visuelle KI-Accounts mit Fokus auf Facebook, Instagram, Reels, Stories und Kampagnenlogik.

## Was das System kann

- automatische Feed-Posts fuer Facebook und Instagram
- Reel-Erzeugung aus Bildserien
- Story-Karten mit Text- und Bild-Hintergruenden
- Kampagnen- und Themenmodus mit Tages-Overrides
- Querbeet- und Thementage pro Wochentag
- lernende Smart Slots fuer bessere Posting-Zeiten
- Caption-A/B-Tests mit Hook- und CTA-Auswertung
- Duplicate- und Qualitaetspruefung fuer Bilder
- Engagement-Tracking mit Alerts, Recycle-Queue und Follow-up-Logik
- Dashboard fuer Queue, Analytics, Kampagnen und manuelle Eingriffe
- Comment-Assist fuer manuelle Outreach-Kommentare auf fremden Posts

## Projektstruktur

- `main.py`: zentrale Runtime und Posting-Logik
- `dashboard.py`: lokales Web-Dashboard
- `config.py`: Laden und Normalisieren der Konfiguration
- `config.example.json`: Beispielkonfiguration
- `facebook_poster.py`: Facebook-Uploads
- `instagram_poster.py`: Instagram-Uploads
- `caption_generator.py`: Captions, Hooks und A/B-Auswahl
- `reel_generator.py`: Reel-Erzeugung
- `story_generator.py`: Story-Karten mit Bild- oder Textlayout
- `post_history.py`: State, Historie, Auswertung und Lernlogik
- `images/`: Eingangsordner fuer Bildmaterial
- `generated_reels/`: lokal erzeugte Reels
- `generated_stories/`: lokal erzeugte Story-Karten
- `public_media/`: oeffentlich bereitgestellte Dateien fuer Instagram-Staging/Fallback

## Voraussetzungen

- Python 3.11+ empfohlen
- Zugriff auf Facebook- und Instagram-APIs
- optional:
  - Ollama fuer lokale Caption-/Kommentar-Generierung
  - OpenAI fuer Caption-Erzeugung

Abhaengigkeiten:

```bash
pip install -r requirements.txt
```

## Einrichtung

1. Beispielkonfiguration kopieren:

```bash
cp config.example.json config.json
```

2. `.env` anlegen und Tokens/IDs hinterlegen.

3. In `config.json` die wichtigsten Bereiche pruefen:

- `facebook`
- `instagram`
- `posting_slots`
- `stories`
- `reels`
- `campaigns`
- `smart_slots`
- `caption_experiments`
- `content_quality`

4. Bilder in `images/` ablegen.

## Starten

Poster starten:

```bash
python main.py
```

Dashboard starten:

```bash
python dashboard.py
```

Standardmaessig laeuft das Dashboard lokal auf:

```text
http://localhost:5000
```

## Typischer Ablauf

1. Das System liest `config.json` und `state.json`.
2. Es berechnet aktive Kampagne, Tagesmodus und bevorzugte Slots.
3. Es waehlt ein Bild anhand von Thema, Qualitaet, Duplikaten und Queue-Status.
4. Es erzeugt Captions und optional Reels oder Story-Karten.
5. Es postet an Facebook und Instagram.
6. Es speichert Historie, Engagement-Daten und Lernwerte in `state.json`.

## Kampagnen und Themen

Das System unterstuetzt:

- feste Kampagnen mit Themenrotation
- `weekday_modes` fuer `theme` oder `mix`
- `daily_theme_overrides` fuer Tages-Themen wie `astronautin`

Beispiel:

```json
"campaigns": {
  "enabled": true,
  "weekday_modes": {
    "monday": "theme",
    "tuesday": "mix"
  },
  "daily_theme_overrides": {
    "2026-04-02": "astronautin"
  }
}
```

## Stories

Stories laufen als kleine Tagessequenz:

- Story 1: starkes Bild + Hook
- Story 2: Bild + Frage
- Story 3: bildgestuetzte Interaktions-Story

Wenn ein aktives Thema gesetzt ist, wird bevorzugt ein passendes Themenbild als Story-Hintergrund verwendet.

## Dashboard

Das Dashboard bietet unter anderem:

- Status der Poster-Runtime
- Queue mit Filter, Sortierung und Pin-Funktion
- Kampagnenumschaltung
- Reel-Vorschau und manuelle Reel-Aktionen
- Story- und Campaign-Kontext
- Caption- und Engagement-Analytics
- Comment-Assist fuer manuelle Outreach-Kommentare

## Wichtige Dateien

- `config.json`: Laufzeitkonfiguration
- `.env`: Tokens und geheime Zugangsdaten
- `state.json`: Historie, Queue, Lernwerte und Analytics
- `poster.log`: Laufzeitprotokoll

## Hinweise

- `config.json`, `.env`, `state.json`, `images/`, `generated_reels/` und `generated_stories/` sollten nicht ins Repo.
- Instagram-Uploads koennen oeffentliche Media-URLs oder das lokale Staging in `public_media/` benoetigen.
- Wenn Ollama lokal nicht erreichbar ist, faellt das System auf Templates oder andere Caption-Wege zurueck.

## Entwicklung

Schneller Syntax-Check:

```bash
python -m py_compile main.py dashboard.py post_history.py story_generator.py config.py caption_generator.py
```

Git-Status pruefen:

```bash
git status
```

## Lizenz / Nutzung

Die technische Infrastruktur ist fuer private oder projektinterne Nutzung vorbereitet. API- und Plattformregeln fuer Facebook, Instagram und spaeter TikTok muessen separat beachtet werden.
