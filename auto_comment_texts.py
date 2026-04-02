from __future__ import annotations

OPENERS = [
    "Was sagst du dazu?",
    "Echt oder KI?",
    "Wie wirkt das auf dich?",
    "Was ist dein erster Gedanke?",
    "Welches Detail springt dir zuerst ins Auge?",
    "Kann man daran einfach vorbeiscrollen?",
    "So ein Look bleibt im Kopf, oder?",
    "Würdest du hier zweimal hinsehen?",
    "Hat dieses Bild sofort etwas bei dir ausgelöst?",
    "Wie nah ist das für dich schon an echter Fotografie?",
    "Was löst dieses Motiv bei dir aus?",
    "Würdest du sagen, das sieht schon erschreckend echt aus?",
    "Welcher Eindruck bleibt bei dir als Erstes hängen?",
    "Ist das eher Kunst, Illusion oder schon fast Realität?",
    "Kann KI für dich schon echte Stimmung erzeugen?",
    "Was macht dieses Bild für dich besonders?",
    "Ist dein erster Gedanke eher wow oder eher skeptisch?",
    "Wie würdest du diesen Look in einem Wort beschreiben?",
    "Hättest du gedacht, dass KI so etwas erzeugen kann?",
    "Welcher Teil dieses Bildes hält deinen Blick fest?",
]

OBSERVATIONS = [
    "Dieses Motiv sieht fast zu perfekt für die Realität aus.",
    "Die Stimmung in diesem Bild fühlt sich gleichzeitig ruhig und stark an.",
    "Hier steckt so viel Atmosphäre in einem einzigen Moment.",
    "Dieser Look wirkt wie aus einem Film, der nie gedreht wurde.",
    "An solchen Bildern merkt man, wie weit KI-Kunst schon ist.",
    "Zwischen Ästhetik und Fantasie liegt hier nur noch ein schmaler Grat.",
    "Die Details machen dieses Bild erst richtig spannend.",
    "Genau solche Motive machen KI-Content gerade so faszinierend.",
    "Hier passt einfach vieles zusammen, ohne laut zu sein.",
    "Dieses Bild hat genau diese Mischung aus Schönheit und Unwirklichkeit.",
    "Je länger man hinschaut, desto mehr kleine Details fallen auf.",
    "Dieses Motiv wirkt wie ein Standbild aus einer anderen Realität.",
    "Hier trifft digitale Perfektion auf eine fast greifbare Stimmung.",
    "Die Bildwirkung ist leise, aber sie bleibt trotzdem hängen.",
    "Gerade die feinen Nuancen machen dieses Motiv so spannend.",
    "Dieses Bild lebt weniger vom Lauten und mehr von seiner Ausstrahlung.",
    "Die Komposition wirkt so stimmig, dass man länger draufschaut.",
    "Hier verschwimmt die Grenze zwischen Inszenierung und Fantasie ziemlich stark.",
    "So ein Motiv zeigt, wie schnell KI visuell glaubwürdig werden kann.",
    "An diesem Bild sieht man, wie viel Wirkung in Details liegen kann.",
    "Es ist genau diese Mischung aus Eleganz und Irrealität, die das Bild trägt.",
    "Das Motiv wirkt fast wie eingefrorene Stimmung in perfektem Licht.",
    "Solche Bilder schaffen es, gleichzeitig vertraut und fremd zu wirken.",
    "Die Ästhetik hier fühlt sich modern an, ohne kalt zu werden.",
    "Dieses Motiv hat etwas, das man nicht nur anschaut, sondern kurz spürt.",
    "Gerade die ruhige Wirkung macht dieses Bild so stark.",
    "Hier steckt viel mehr Tiefe drin, als man im ersten Moment denkt.",
    "Das Bild wirkt so sauber gebaut, dass es fast surreal wird.",
    "Solche Looks zeigen, wie nah KI inzwischen an echter Bildsprache ist.",
    "Die Atmosphäre hier erzählt fast mehr als ein ganzer Text.",
]

CALLS_TO_ACTION = [
    "Schreib deine Meinung in die Kommentare! 👇",
    "Lass gern ein Feedback da! 💬",
    "Erzähl unten, wie du das siehst! ✨",
    "Sag in den Kommentaren, was dir daran auffällt! 👀",
    "Schreib unten deinen ersten Eindruck dazu! 🔥",
    "Sag gern, ob dich das Bild catcht! 💭",
    "Verrat in den Kommentaren, was du darin siehst! 👇",
    "Deine ehrliche Meinung dazu würde mich interessieren! 💬",
]


def _build_templates() -> list[str]:
    templates: list[str] = []
    for opener in OPENERS:
        for observation in OBSERVATIONS:
            for call_to_action in CALLS_TO_ACTION:
                templates.append(f"{opener} {observation} {call_to_action}")
    return templates


DEFAULT_AUTO_COMMENT_TEMPLATES = _build_templates()
