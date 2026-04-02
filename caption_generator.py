from __future__ import annotations

import base64
import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

import requests

from config import AppConfig

_EMOJI_RE = re.compile("[\U00002600-\U000027BF\U0001F300-\U0001FAFF]")


def _strip_trailing_emojis(text: str) -> str:
    return _EMOJI_RE.sub("", text).rstrip()


def _ends_with(text: str, *chars: str) -> bool:
    stripped = _strip_trailing_emojis(text)
    return bool(stripped) and stripped[-1] in chars


def extract_caption_features(caption: str) -> dict[str, bool]:
    lines = [ln.strip() for ln in caption.splitlines() if ln.strip()]
    if not lines:
        return {}
    hook = lines[0]
    content_lines = [ln for ln in lines if not ln.startswith("#")]
    content_text = " ".join(content_lines)
    return {
        "starts_with_question": _ends_with(hook, "?"),
        "starts_with_exclamation": _ends_with(hook, "!"),
        "has_emoji_hook": bool(_EMOJI_RE.search(hook)),
        "ends_with_question": bool(content_lines and _ends_with(content_lines[-1], "?")),
        "optimal_length": 60 <= len(content_text) <= 280,
    }


def classify_hook_style(caption: str) -> str:
    lines = [ln.strip() for ln in str(caption or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return "question"
    hook = lines[0]
    lowered = hook.lower()
    if " oder " in lowered:
        return "comparison"
    if _ends_with(hook, "?") or any(token in lowered for token in ("was", "welcher", "würdest", "wuerdest", "echt", "ki")):
        return "question"
    if _ends_with(hook, "!") or any(token in lowered for token in ("scroll", "stark", "nicht", "zu perfekt")):
        return "challenge"
    return "statement"


def classify_cta_style(caption: str) -> str:
    lines = [ln.strip() for ln in str(caption or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return "question"
    cta = lines[-1]
    lowered = cta.lower()
    if any(token in lowered for token in ("speicher", "teilen", "teile", "share", "repost")):
        return "save_share"
    if any(token in lowered for token in ("folg", "mehr solche", "morgen mehr", "mehr davon")):
        return "follow"
    if " oder " in lowered or any(token in lowered for token in ("favorit", "welcher", "1, 2")):
        return "choice"
    return "question"

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CaptionBundle:
    variants: list[str]
    selected: str
    description: str
    source: str
    variant_metadata: list[dict[str, str]]
    selected_metadata: dict[str, str]


class CaptionGenerator:
    IMAGE_HOOKS = {
        "question": [
            "Echt oder KI?",
            "Würdest du hier sofort stoppen?",
            "Was zieht dich hier zuerst an?",
        ],
        "comparison": [
            "Zu perfekt für Zufall?",
            "Mehr Traum oder digitale Täuschung?",
            "Realer Moment oder KI-Magie?",
        ],
        "challenge": [
            "Dieser Look will gesehen werden.",
            "Dieses Bild bleibt nicht unbemerkt.",
            "Hier scrollt man nicht einfach vorbei.",
        ],
        "statement": [
            "Dieser Look setzt sofort ein Zeichen.",
            "Das ist digitale Ästhetik mit Wirkung.",
            "Dieser Moment trägt seine eigene Spannung.",
        ],
    }
    REEL_HOOKS = {
        "question": [
            "Welcher Look gewinnt sofort deine Aufmerksamkeit?",
            "Würdest du hier weiterscrollen oder anhalten?",
            "Welches Bild bleibt dir als erstes im Kopf?",
        ],
        "comparison": [
            "Welcher Slide zieht dich stärker an?",
            "Mehr Fashion-Moment oder Sci-Fi-Vibe?",
            "Welcher Look gewinnt klar gegen den Rest?",
        ],
        "challenge": [
            "Dieses Reel fordert einen zweiten Blick.",
            "Zu stark für einen schnellen Scroll?",
            "Hier hält selbst ein hektischer Feed kurz an.",
        ],
        "statement": [
            "Dieses Reel baut sofort Spannung auf.",
            "Jeder Slide setzt noch einen drauf.",
            "Hier wirkt jeder Look wie ein Statement.",
        ],
    }
    IMAGE_CTAS = {
        "question": [
            "Was fällt dir als erstes auf?",
            "Findest du das faszinierend oder zu perfekt?",
            "Was macht das für dich so stark?",
        ],
        "choice": [
            "Würdest du so ein Bild liken oder skippen?",
            "Speichern oder weiterscrollen?",
            "Welches Detail gewinnt für dich?",
        ],
        "save_share": [
            "Würdest du das speichern oder weitergehen?",
            "Wäre das eher ein Save oder ein Share?",
            "Würdest du das jemandem sofort schicken?",
        ],
        "follow": [
            "Willst du mehr solche Looks sehen?",
            "Folgst du schon für mehr davon?",
            "Soll ich mehr in diesem Stil posten?",
        ],
    }
    REEL_CTAS = {
        "question": [
            "Was wirkt für dich am meisten nach Zukunft?",
            "Willst du mehr solche Reels sehen?",
            "Was bleibt dir davon am meisten im Kopf?",
        ],
        "choice": [
            "Welcher Slide gewinnt für dich: 1, 2, 3 oder 4?",
            "Welcher Look ist dein Favorit?",
            "Welche Szene würdest du nochmal ansehen?",
        ],
        "save_share": [
            "Würdest du das eher liken, speichern oder teilen?",
            "Ist das eher ein Save oder ein Share?",
            "Welcher Slide wäre dein Repost-Moment?",
        ],
        "follow": [
            "Soll ich mehr solcher Reels bauen?",
            "Folgst du schon für den nächsten Look?",
            "Willst du morgen mehr davon sehen?",
        ],
    }

    def __init__(self, config: AppConfig):
        self.config = config

    def generate_for_image(
        self,
        image_path: Path,
        feature_weights: dict[str, float] | None = None,
        experiment_stats: dict[str, dict[str, float] | dict[str, int]] | None = None,
    ) -> CaptionBundle:
        description = load_image_description(image_path, self.config.descriptions_folder)
        if description == "Keine Bildbeschreibung vorhanden." and self.config.ollama.vision_enabled:
            description = self._describe_with_vision(image_path) or description
        return self._generate_bundle(
            image_path.stem,
            description,
            content_type="image",
            feature_weights=feature_weights,
            experiment_stats=experiment_stats,
        )

    def generate_for_reel(
        self,
        image_paths: list[Path],
        feature_weights: dict[str, float] | None = None,
        experiment_stats: dict[str, dict[str, float] | dict[str, int]] | None = None,
    ) -> CaptionBundle:
        reel_name = " ".join(path.stem for path in image_paths[:2]) or "Reel"
        description_parts: list[str] = []
        for index, image_path in enumerate(image_paths[:4], start=1):
            description = load_image_description(image_path, self.config.descriptions_folder)
            if description == "Keine Bildbeschreibung vorhanden." and self.config.ollama.vision_enabled:
                description = self._describe_with_vision(image_path) or description
            if description and description != "Keine Bildbeschreibung vorhanden.":
                description_parts.append(f"Bild {index}: {description}")
            else:
                description_parts.append(f"Bild {index}")

        description = "\n".join(description_parts) if description_parts else "Mehrere Bilder für ein kurzes Social-Media-Reel."
        return self._generate_bundle(
            reel_name,
            description,
            content_type="reel",
            feature_weights=feature_weights,
            experiment_stats=experiment_stats,
        )

    def _describe_with_vision(self, image_path: Path) -> str:
        cfg = self.config.ollama
        if not cfg.vision_model:
            return ""
        try:
            image_b64 = base64.b64encode(image_path.read_bytes()).decode()
            response = requests.post(
                f"{cfg.base_url.rstrip('/')}/api/chat",
                json={
                    "model": cfg.vision_model,
                    "stream": False,
                    "messages": [{
                        "role": "user",
                        "content": (
                            "Beschreibe dieses Bild kurz auf Deutsch in 1-2 Saetzen. "
                            "Fokus auf Personen, Stimmung, Farben und Stil."
                        ),
                        "images": [image_b64],
                    }],
                },
                timeout=cfg.timeout_seconds,
            )
            response.raise_for_status()
            description = ((response.json().get("message") or {}).get("content") or "").strip()
            if description and cfg.vision_cache:
                candidates = [image_path.with_suffix(".txt")]
                if self.config.descriptions_folder is not None:
                    candidates.append(self.config.descriptions_folder / f"{image_path.stem}.txt")
                target = candidates[-1] if self.config.descriptions_folder else candidates[0]
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(description, encoding="utf-8")
                except Exception:
                    pass
            return description
        except Exception as exc:
            log.warning("Ollama-Vision-Beschreibung fehlgeschlagen (%s): %s", image_path.name, exc)
            return ""

    def _build_fallback_topic(self, subject_name: str, description: str) -> str:
        topic = description if description != "Keine Bildbeschreibung vorhanden." else "dieses Motiv"
        compact_lines = [line.strip() for line in topic.splitlines() if line.strip()]
        compact_topic = ", ".join(compact_lines[:3]) if compact_lines else topic.strip()
        compact_topic = compact_topic.replace("  ", " ").strip(" ,")
        if not compact_topic:
            compact_topic = "diesem Motiv"
        return compact_topic[:180] if len(compact_topic) > 180 else compact_topic

    def score_caption(self, caption: str, feature_weights: dict[str, float] | None = None) -> int:
        disclaimer_lines = {ln.strip() for ln in self.config.ai_disclosure.splitlines() if ln.strip()}
        lines = [ln.strip() for ln in caption.splitlines() if ln.strip() and ln.strip() not in disclaimer_lines]
        if not lines:
            return 0
        hook = lines[0]
        content_lines = [ln for ln in lines if not ln.startswith("#")]
        content_text = " ".join(content_lines)

        score = 0
        if _ends_with(hook, "?", "!"):
            score += 25
        if _EMOJI_RE.search(hook):
            score += 15
        if content_lines and _ends_with(content_lines[-1], "?"):
            score += 20
        if 60 <= len(content_text) <= 280:
            score += 20
        if self.config.ai_disclosure.strip() in caption:
            score += 20

        if feature_weights:
            features = extract_caption_features(caption)
            multiplier = 1.0
            for feature, weight in feature_weights.items():
                if features.get(feature) and weight > 1.0:
                    multiplier = max(multiplier, min(weight, 1.5))
            score = int(score * multiplier)

        return min(score, 100)

    def _generate_bundle(
        self,
        subject_name: str,
        description: str,
        content_type: str,
        feature_weights: dict[str, float] | None = None,
        experiment_stats: dict[str, dict[str, float] | dict[str, int]] | None = None,
    ) -> CaptionBundle:
        variants, source = self._generate_variants(subject_name, description, content_type)
        variant_metadata = [self._analyze_variant(variant) for variant in variants]
        selected_index = self._choose_variant_smart(variants, variant_metadata, feature_weights, experiment_stats)
        selected = variants[selected_index]
        selected_metadata = variant_metadata[selected_index]

        if self.config.caption_scoring.enabled:
            cfg = self.config.caption_scoring
            for _ in range(cfg.max_retries):
                if self.score_caption(selected, feature_weights) >= cfg.min_score:
                    break
                new_variants, new_source = self._generate_variants(subject_name, description, content_type)
                if new_variants:
                    new_variant_metadata = [self._analyze_variant(variant) for variant in new_variants]
                    best = max(new_variants, key=lambda value: self.score_caption(value, feature_weights))
                    if self.score_caption(best, feature_weights) > self.score_caption(selected, feature_weights):
                        variants = new_variants
                        variant_metadata = new_variant_metadata
                        source = new_source
                        selected = best
                        selected_index = new_variants.index(best)
                        selected_metadata = new_variant_metadata[selected_index]
            log.debug("Caption-Score: %d (min: %d)", self.score_caption(selected, feature_weights), cfg.min_score)

        return CaptionBundle(
            variants=variants,
            selected=selected,
            description=description,
            source=source,
            variant_metadata=variant_metadata,
            selected_metadata=selected_metadata,
        )

    def _prompt_config(self, content_type: str) -> tuple[str, str]:
        if content_type == "reel":
            return self.config.openai.reel_system_prompt, self.config.openai.reel_user_prompt_template
        return self.config.openai.system_prompt, self.config.openai.user_prompt_template

    def _render_prompt_template(self, template: str, **kwargs: str | int) -> str:
        token_map = {
            "variant_count": "__VARIANT_COUNT__",
            "disclaimer": "__DISCLAIMER__",
            "filename": "__FILENAME__",
            "description": "__DESCRIPTION__",
        }

        safe_template = template
        for key, token in token_map.items():
            safe_template = safe_template.replace(f"{{{key}}}", token)

        safe_template = safe_template.replace("{", "{{").replace("}", "}}")

        for key, token in token_map.items():
            safe_template = safe_template.replace(token, f"{{{key}}}")

        return safe_template.format(**kwargs)

    def _generate_variants(self, subject_name: str, description: str, content_type: str) -> tuple[list[str], str]:
        provider = self.config.caption_provider

        if provider == "ollama":
            variants = self._try_ollama(subject_name, description, content_type)
            if variants:
                return variants, "ollama"
            variants = self._try_openai(subject_name, description, content_type)
            if variants:
                return variants, "openai"
        elif provider == "openai":
            variants = self._try_openai(subject_name, description, content_type)
            if variants:
                return variants, "openai"
            variants = self._try_ollama(subject_name, description, content_type)
            if variants:
                return variants, "ollama"

        return self._fallback_variants(subject_name, description), "fallback"

    def _try_ollama(self, subject_name: str, description: str, content_type: str) -> list[str]:
        if not self.config.ollama.enabled:
            return []

        try:
            return self._generate_with_ollama(subject_name, description, content_type)
        except Exception as exc:
            log.exception("Ollama-Caption-Generierung fehlgeschlagen: %s", exc)
            return []

    def _try_openai(self, subject_name: str, description: str, content_type: str) -> list[str]:
        if not self.config.openai.enabled:
            return []
        if not self.config.openai.api_key:
            log.warning("OPENAI_API_KEY fehlt. OpenAI wird als Caption-Provider übersprungen.")
            return []
        if OpenAI is None:
            log.warning("OpenAI-Paket ist nicht installiert. OpenAI wird übersprungen.")
            return []

        try:
            return self._generate_with_openai(subject_name, description, content_type)
        except Exception as exc:
            log.exception("OpenAI-Caption-Generierung fehlgeschlagen: %s", exc)
            return []

    def _generate_with_ollama(self, subject_name: str, description: str, content_type: str) -> list[str]:
        system_prompt, user_prompt_template = self._prompt_config(content_type)
        prompt = self._render_prompt_template(
            user_prompt_template,
            variant_count=self.config.caption_variant_count,
            disclaimer=self.config.ai_disclosure,
            filename=subject_name,
            description=description,
        )
        response = requests.post(
            f"{self.config.ollama.base_url.rstrip('/')}/api/chat",
            json={
                "model": self.config.ollama.model,
                "stream": False,
                "options": {
                    "temperature": self.config.ollama.temperature,
                },
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=self.config.ollama.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        content = ((payload.get("message") or {}).get("content") or "").strip()
        variants = self._parse_variants(content)
        if not variants:
            raise ValueError("Ollama-Antwort enthielt keine gültigen Varianten.")
        return self._normalize_variants(variants, content_type=content_type, subject_name=subject_name, description=description)

    def _generate_with_openai(self, subject_name: str, description: str, content_type: str) -> list[str]:
        client = OpenAI(api_key=self.config.openai.api_key, timeout=self.config.openai.timeout_seconds)
        system_prompt, user_prompt_template = self._prompt_config(content_type)
        prompt = self._render_prompt_template(
            user_prompt_template,
            variant_count=self.config.caption_variant_count,
            disclaimer=self.config.ai_disclosure,
            filename=subject_name,
            description=description,
        )
        response = client.chat.completions.create(
            model=self.config.openai.model,
            temperature=self.config.openai.temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content or ""
        variants = self._parse_variants(content)
        if not variants:
            raise ValueError("OpenAI-Antwort enthielt keine gültigen Varianten.")
        return self._normalize_variants(variants, content_type=content_type, subject_name=subject_name, description=description)

    def _parse_variants(self, content: str) -> list[str]:
        payload = content.strip()

        code_block = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', payload)
        if code_block:
            payload = code_block.group(1).strip()

        data = None
        parse_candidates = [payload]
        if payload.startswith("{{") and payload.endswith("}}"):
            parse_candidates.append(payload[1:-1].strip())

        json_match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', payload)
        if json_match:
            matched_payload = json_match.group(1).strip()
            parse_candidates.append(matched_payload)
            if matched_payload.startswith("{{") and matched_payload.endswith("}}"):
                parse_candidates.append(matched_payload[1:-1].strip())

        seen_candidates: set[str] = set()
        for candidate in parse_candidates:
            candidate = candidate.strip()
            if not candidate or candidate in seen_candidates:
                continue
            seen_candidates.add(candidate)
            try:
                data = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue

        if data is None:
            return self._parse_variants_from_text(payload)

        if isinstance(data, dict):
            variants = data.get("variants") or data.get("captions") or data.get("items") or data.get("caption") or []
        elif isinstance(data, list):
            variants = data
        else:
            variants = []

        if isinstance(variants, str):
            return [variants.strip()] if variants.strip() else []

        parsed: list[str] = []
        for item in variants:
            if isinstance(item, str) and item.strip():
                parsed.append(item.strip())
                continue
            if isinstance(item, dict):
                for key in ("caption", "text", "content", "value"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        parsed.append(value.strip())
                        break

        return parsed or self._parse_variants_from_text(payload)

    def _parse_variants_from_text(self, content: str) -> list[str]:
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            return []

        bullet_lines = [
            re.sub(r'^(?:[-*]|\d+[.)])\s*', '', line).strip()
            for line in lines
            if re.match(r'^(?:[-*]|\d+[.)])\s+', line)
        ]
        bullet_lines = [line for line in bullet_lines if line]
        if bullet_lines:
            return bullet_lines

        return [content.strip()] if content.strip() else []

    def _fallback_variants(self, subject_name: str, description: str) -> list[str]:
        topic = self._build_fallback_topic(subject_name, description)
        base_variants = [
            f"Echt oder KI? 👀\nDu bleibst bei diesem Look sofort hängen.\nWürdest du bei {topic} genauer hinsehen?\n{self.config.ai_disclosure}",
            f"Irgendetwas an diesem Bild lässt dich nicht los. 🔥\nDu schaust hin und willst direkt mehr wissen.\nWas fühlst du bei {topic}?\n{self.config.ai_disclosure}",
            f"Zu perfekt, um zufällig zu sein? 👀\nGenau dieser Moment zieht sofort Aufmerksamkeit.\nIst das für dich faszinierend oder unheimlich?\n{self.config.ai_disclosure}",
            f"Dieser Look lässt sich nicht ignorieren. ✨\nJedes Detail wirkt durchdacht und einzigartig.\nWas fällt dir bei {topic} als erstes auf?\n{self.config.ai_disclosure}",
            f"Schönheit, die sprachlos macht. 😍\nKI erschafft Welten, die wir uns kaum vorstellen können.\nWürde dich {topic} in echt begeistern?\n{self.config.ai_disclosure}",
            f"Stell dir vor, das wäre real. 😮\nDie KI erschafft täglich neue Gesichter und Welten.\nWas denkst du über {topic}?\n{self.config.ai_disclosure}",
            f"Manchmal ist KI echter als die Realität. 🤯\nJedes Bild erzählt eine Geschichte.\nWelche Geschichte siehst du in {topic}?\n{self.config.ai_disclosure}",
            f"Die Grenze zwischen real und digital verschwimmt. 👁️\nGenau das macht {topic} so faszinierend.\nFolgst du uns schon für mehr solche Momente?\n{self.config.ai_disclosure}",
            f"Diesen Look wirst du nicht so schnell vergessen. 🔥\nKI-Kunst auf einem neuen Level.\nSpeicher diesen Post bevor er dir verloren geht! 🔖\n{self.config.ai_disclosure}",
            f"Perfekt, und doch nicht von dieser Welt. ✨\nWas macht {topic} für dich so besonders?\nSchreib es uns in die Kommentare! 👇\n{self.config.ai_disclosure}",
            f"Das Internet braucht mehr davon. 💫\n{topic} – einfach nicht zu übersehen.\nTeilst du das mit jemandem? 🔄\n{self.config.ai_disclosure}",
            f"KI oder Realität? Du entscheidest. 🧐\nDieser Look fordert einen zweiten Blick.\nWas sagst du zu {topic}? ⬇️\n{self.config.ai_disclosure}",
        ]
        return self._normalize_variants(
            random.sample(base_variants, min(len(base_variants), self.config.caption_variant_count)),
            content_type="image",
            subject_name=subject_name,
            description=description,
        )

    def _build_hashtag_block(self) -> str:
        cfg = self.config.hashtags
        if not cfg.enabled or not cfg.tags:
            return ""
        if cfg.strategy == "all":
            chosen = list(cfg.tags)
        elif cfg.strategy == "fixed":
            chosen = cfg.tags[: cfg.count]
        else:
            count = min(cfg.count, len(cfg.tags))
            chosen = random.sample(cfg.tags, count)
        tags = [t if t.startswith("#") else f"#{t}" for t in chosen]
        return " ".join(tags)

    def _normalize_variants(
        self,
        variants: list[str],
        content_type: str = "image",
        subject_name: str = "",
        description: str = "",
    ) -> list[str]:
        cleaned = []
        disclaimer_lines = [line.strip() for line in self.config.ai_disclosure.splitlines() if line.strip()]
        hashtag_block = self._build_hashtag_block()
        topic = self._build_fallback_topic(subject_name, description)
        hook_styles = list((self.REEL_HOOKS if content_type == "reel" else self.IMAGE_HOOKS).keys())
        cta_styles = list((self.REEL_CTAS if content_type == "reel" else self.IMAGE_CTAS).keys())

        for index, variant in enumerate(variants):
            text = variant.strip()
            if not text:
                continue
            text_lines = [line.rstrip() for line in text.splitlines()]
            filtered_lines = []
            for line in text_lines:
                if line.strip() in disclaimer_lines:
                    continue
                filtered_lines.append(line)
            text = self._optimize_caption_text(
                "\n".join(filtered_lines).strip(),
                content_type,
                topic,
                hook_style=hook_styles[index % len(hook_styles)],
                cta_style=cta_styles[index % len(cta_styles)],
            )
            if not text.endswith(self.config.ai_disclosure):
                text = f"{text}\n{self.config.ai_disclosure}".strip()
            if hashtag_block:
                text = f"{text}\n{hashtag_block}"
            cleaned.append(text)

        if not cleaned:
            cleaned = [self.config.ai_disclosure]

        seed_variants = list(cleaned)
        while len(cleaned) < self.config.caption_variant_count:
            cleaned.append(seed_variants[len(cleaned) % len(seed_variants)])

        return cleaned[: self.config.caption_variant_count]

    def _choose_variant_smart(
        self,
        variants: list[str],
        variant_metadata: list[dict[str, str]],
        feature_weights: dict[str, float] | None = None,
        experiment_stats: dict[str, dict[str, float] | dict[str, int]] | None = None,
    ) -> int:
        if self.config.caption_selection_strategy == "first":
            return 0
        if (
            self.config.caption_experiments.enabled
            and experiment_stats
            and random.random() < self.config.caption_experiments.exploration_rate
        ):
            hook_counts = experiment_stats.get("hook_counts", {})
            cta_counts = experiment_stats.get("cta_counts", {})
            return min(
                range(len(variants)),
                key=lambda idx: (
                    int(hook_counts.get(variant_metadata[idx]["hook_style"], 0)),
                    int(cta_counts.get(variant_metadata[idx]["cta_style"], 0)),
                ),
            )

        scores = []
        for index, variant in enumerate(variants):
            score = self._feature_score(variant, feature_weights or {})
            if self.config.caption_experiments.enabled and experiment_stats:
                score *= float((experiment_stats.get("hook_weights", {}) or {}).get(variant_metadata[index]["hook_style"], 1.0) or 1.0)
                score *= float((experiment_stats.get("cta_weights", {}) or {}).get(variant_metadata[index]["cta_style"], 1.0) or 1.0)
            scores.append(max(score, 0.1))
        total = sum(scores)
        r = random.uniform(0, total)
        for index, score in enumerate(scores):
            r -= score
            if r <= 0:
                return index
        return len(variants) - 1

    def _feature_score(self, caption: str, feature_weights: dict[str, float]) -> float:
        features = extract_caption_features(caption)
        score = 1.0
        for feature, weight in feature_weights.items():
            if features.get(feature):
                score *= weight
        return max(score, 0.01)

    def _optimize_caption_text(
        self,
        text: str,
        content_type: str,
        topic: str,
        hook_style: str | None = None,
        cta_style: str | None = None,
    ) -> str:
        visible_lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        normalized_lines = [self._normalize_sentence(line) for line in visible_lines]
        normalized_lines = [line for line in normalized_lines if line]

        hook = self._select_hook(normalized_lines, content_type, topic, hook_style=hook_style)
        cta = self._select_cta(normalized_lines, content_type, topic, cta_style=cta_style)

        middle_lines = [line for line in normalized_lines if line not in {hook, cta}]
        body_line = middle_lines[0] if middle_lines else self._default_body(content_type, topic)
        optimized_lines = [hook, body_line, cta]
        return "\n".join(line for line in optimized_lines if line).strip()

    def _select_hook(self, visible_lines: list[str], content_type: str, topic: str, hook_style: str | None = None) -> str:
        if visible_lines:
            candidate = visible_lines[0]
            if self._looks_like_hook(candidate) and (hook_style is None or classify_hook_style(candidate) == hook_style):
                return candidate
        return self._default_hook(content_type, topic, hook_style=hook_style)

    def _select_cta(self, visible_lines: list[str], content_type: str, topic: str, cta_style: str | None = None) -> str:
        if len(visible_lines) >= 2:
            candidate = visible_lines[-1]
            if self._looks_like_cta(candidate) and (cta_style is None or classify_cta_style(candidate) == cta_style):
                return candidate
        return self._default_cta(content_type, topic, cta_style=cta_style)

    def _looks_like_hook(self, text: str) -> bool:
        stripped = text.strip()
        if len(stripped.split()) < 3:
            return False
        if len(stripped) > 110:
            return False
        return _ends_with(stripped, "?", "!", ".")

    def _looks_like_cta(self, text: str) -> bool:
        stripped = text.strip()
        if len(stripped.split()) < 3:
            return False
        cta_markers = ("du", "was", "welcher", "würdest", "findest", "willst", "speicher", "teile", "kommentar")
        if not any(marker in stripped.lower() for marker in cta_markers):
            return False
        return _ends_with(stripped, "?", "!")

    def _default_hook(self, content_type: str, topic: str, hook_style: str | None = None) -> str:
        style = hook_style or "question"
        pool_map = self.REEL_HOOKS if content_type == "reel" else self.IMAGE_HOOKS
        pool = pool_map.get(style) or pool_map["question"]
        return self._random_choice(pool)

    def _default_cta(self, content_type: str, topic: str, cta_style: str | None = None) -> str:
        style = cta_style or "question"
        pool_map = self.REEL_CTAS if content_type == "reel" else self.IMAGE_CTAS
        pool = pool_map.get(style) or pool_map["question"]
        return self._random_choice(pool)

    def _analyze_variant(self, caption: str) -> dict[str, str]:
        return {
            "hook_style": classify_hook_style(caption),
            "cta_style": classify_cta_style(caption),
        }

    def _default_body(self, content_type: str, topic: str) -> str:
        if content_type == "reel":
            return f"{topic} ist genau die Art von Look, bei der man nicht einfach weiterscrollt."
        return f"{topic} wirkt sofort präzise, auffällig und nicht ganz von dieser Welt."

    def _normalize_sentence(self, text: str) -> str:
        normalized = re.sub(r"\s+", " ", text).strip(" -–—")
        return normalized

    def _random_choice(self, options: list[str]) -> str:
        if not options:
            return ""
        return random.choice(options)


def load_image_description(image_path: Path, descriptions_folder: Path | None) -> str:
    candidates = [image_path.with_suffix(".txt")]
    if descriptions_folder is not None:
        candidates.append(descriptions_folder / f"{image_path.stem}.txt")

    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip() or "Keine Bildbeschreibung vorhanden."

    return "Keine Bildbeschreibung vorhanden."
