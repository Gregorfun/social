Lokale Musikbibliothek fuer automatisch erzeugte Reels.

Verwendung:
- Lege lizenzierte Tracks in diesen Ordner.
- Fuer jeden Track wird eine JSON-Datei mit gleichem Basisnamen erwartet.
- Nur Tracks mit freigegebener Metadatei werden automatisch verwendet.
- Wenn kein freigegebener Track gefunden wird, faellt das System automatisch auf generierte Musik zurueck.

Beispiel:
- summer-loop.mp3
- summer-loop.json

Pflichtfelder in der JSON-Datei:
- title: Anzeigename des Tracks
- license_status: sollte auf approved stehen
- commercial_use: true
- allowed_platforms: Liste z. B. ["facebook", "instagram", "reels"]

Optionale Felder:
- artist
- source_url
- attribution_required
- notes
- moods: Liste wie ["luxury", "summer", "romantic"]
- genres: Liste wie ["house", "pop", "ambient"]
- keywords: freie Stichwoerter fuer Motiv-Matching
- energy: z. B. low, medium, high, upbeat
- priority: Ganzzahl fuer bevorzugte Auswahl bei gleichem Tag-Match

Automatisches Matching:
- Das System analysiert Caption und Bilddateinamen.
- Daraus werden Musik-Tags wie luxury, energetic, dark, summer oder romantic abgeleitet.
- Tracks mit passenden Tags in moods, genres, keywords oder energy werden bevorzugt ausgewaehlt.
- Ohne Treffer greifen die Default-Tags aus der Konfiguration, danach die normale Rotation.
