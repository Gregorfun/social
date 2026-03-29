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


class CaptionGenerator:
    def __init__(self, config: AppConfig):
        self.config = config

    def generate_for_image(self, image_path: Path, feature_weights: dict[str, float] | None = None) -> CaptionBundle:
        description = load_image_description(image_path, self.config.descriptions_folder)
        if description == "Keine Bildbeschreibung vorhanden." and self.config.ollama.vision_enabled:
            description = self._describe_with_vision(image_path) or description
        return self._generate_bundle("Bild", description, content_type="image", feature_weights=feature_weights)

    def generate_for_reel(self, image_paths: list[Path], feature_weights: dict[str, float] | None = None) -> CaptionBundle:
        reel_name = "Reel"
        description_parts: list[str] = []
        for index, image_path in enumerate(image_paths[:4], start=1):
            description = load_image_description(image_path, self.config.descriptions_folder)
            if description == "Keine Bildbeschreibung vorhanden." and self.config.ollama.vision_enabled:
                description = self._describe_with_vision(image_path) or description
            if description and description != "Keine Bildbeschreibung vorhanden.":
                description_parts.append(f"Bild {index}: {description}")
            else:
                description_parts.append(f"Bild {index}")

        description = "\n".join(description_parts) if description_parts else "Mehrere Bilder fuer ein kurzes Social-Media-Reel."
        return self._generate_bundle(reel_name, description, content_type="reel", feature_weights=feature_weights)

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
        topic = description if description != "Keine Bildbeschreibung vorhanden." else "diesem Motiv"
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

    def _generate_bundle(self, subject_name: str, description: str, content_type: str, feature_weights: dict[str, float] | None = None) -> CaptionBundle:
        variants, source = self._generate_variants(subject_name, description, content_type)
        selected = self._choose_variant_smart(variants, feature_weights)

        if self.config.caption_scoring.enabled:
            cfg = self.config.caption_scoring
            for _ in range(cfg.max_retries):
                if self.score_caption(selected, feature_weights) >= cfg.min_score:
                    break
                new_variants, new_source = self._generate_variants(subject_name, description, content_type)
                if new_variants:
                    all_candidates = variants + new_variants
                    best = max(all_candidates, key=lambda v: self.score_caption(v, feature_weights))
                    if self.score_caption(best, feature_weights) > self.score_caption(selected, feature_weights):
                        variants = new_variants
                        source = new_source
                        selected = best
            log.debug("Caption-Score: %d (min: %d)", self.score_caption(selected, feature_weights), cfg.min_score)

        return CaptionBundle(variants=variants, selected=selected, description=description, source=source)

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
            log.warning("OPENAI_API_KEY fehlt. OpenAI wird als Caption-Provider uebersprungen.")
            return []
        if OpenAI is None:
            log.warning("OpenAI-Paket ist nicht installiert. OpenAI wird uebersprungen.")
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
            raise ValueError("Ollama-Antwort enthielt keine gueltigen Varianten.")
        return self._normalize_variants(variants)

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
            raise ValueError("OpenAI-Antwort enthielt keine gueltigen Varianten.")
        return self._normalize_variants(variants)

    def _parse_variants(self, content: str) -> list[str]:
        payload = content.strip()

        code_block = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', payload)
        if code_block:
            payload = code_block.group(1).strip()

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            json_match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', payload)
            if not json_match:
                return []
            try:
                data = json.loads(json_match.group(1))
            except json.JSONDecodeError:
                return []
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

        return parsed

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
        return self._normalize_variants(random.sample(base_variants, min(len(base_variants), self.config.caption_variant_count)))

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

    def _normalize_variants(self, variants: list[str]) -> list[str]:
        cleaned = []
        disclaimer_lines = [line.strip() for line in self.config.ai_disclosure.splitlines() if line.strip()]
        hashtag_block = self._build_hashtag_block()

        for variant in variants:
            text = variant.strip()
            if not text:
                continue
            text_lines = [line.rstrip() for line in text.splitlines()]
            filtered_lines = []
            for line in text_lines:
                if line.strip() in disclaimer_lines:
                    continue
                filtered_lines.append(line)
            text = "\n".join(filtered_lines).strip()
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

    def _choose_variant_smart(self, variants: list[str], feature_weights: dict[str, float] | None = None) -> str:
        if self.config.caption_selection_strategy == "first":
            return variants[0]
        if not feature_weights:
            return random.choice(variants)
        scores = [max(self._feature_score(v, feature_weights), 0.1) for v in variants]
        total = sum(scores)
        r = random.uniform(0, total)
        for variant, score in zip(variants, scores):
            r -= score
            if r <= 0:
                return variant
        return variants[-1]

    def _feature_score(self, caption: str, feature_weights: dict[str, float]) -> float:
        features = extract_caption_features(caption)
        score = 1.0
        for feature, weight in feature_weights.items():
            if features.get(feature):
                score *= weight
        return max(score, 0.01)


def load_image_description(image_path: Path, descriptions_folder: Path | None) -> str:
    candidates = [image_path.with_suffix(".txt")]
    if descriptions_folder is not None:
        candidates.append(descriptions_folder / f"{image_path.stem}.txt")

    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip() or "Keine Bildbeschreibung vorhanden."

    return "Keine Bildbeschreibung vorhanden."