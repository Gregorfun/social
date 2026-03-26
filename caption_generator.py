from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path

import requests

from config import AppConfig

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

    def generate_for_image(self, image_path: Path) -> CaptionBundle:
        description = load_image_description(image_path, self.config.descriptions_folder)
        return self._generate_bundle("Bild", description, content_type="image")

    def generate_for_reel(self, image_paths: list[Path]) -> CaptionBundle:
        reel_name = "Reel"
        description_parts: list[str] = []
        for index, image_path in enumerate(image_paths[:4], start=1):
            description = load_image_description(image_path, self.config.descriptions_folder)
            if description and description != "Keine Bildbeschreibung vorhanden.":
                description_parts.append(f"Bild {index}: {description}")
            else:
                description_parts.append(f"Bild {index}")

        description = "\n".join(description_parts) if description_parts else "Mehrere Bilder fuer ein kurzes Social-Media-Reel."
        return self._generate_bundle(reel_name, description, content_type="reel")

    def _build_fallback_topic(self, subject_name: str, description: str) -> str:
        topic = description if description != "Keine Bildbeschreibung vorhanden." else "diesem Motiv"
        compact_lines = [line.strip() for line in topic.splitlines() if line.strip()]
        compact_topic = ", ".join(compact_lines[:3]) if compact_lines else topic.strip()
        compact_topic = compact_topic.replace("  ", " ").strip(" ,")
        if not compact_topic:
            compact_topic = "diesem Motiv"
        return compact_topic[:180] if len(compact_topic) > 180 else compact_topic

    def _generate_bundle(self, subject_name: str, description: str, content_type: str) -> CaptionBundle:
        variants, source = self._generate_variants(subject_name, description, content_type)
        selected = self._choose_variant(variants)
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
        if payload.startswith("```"):
            payload = payload.strip("`")
            payload = payload.replace("json", "", 1).strip()

        data = json.loads(payload)
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
            f"Echt oder KI? 👀\nDu bleibst bei diesem Look sofort haengen.\nWuerdest du bei {topic} genauer hinsehen?\n{self.config.ai_disclosure}",
            f"Irgendetwas an diesem Bild laesst dich nicht los. 🔥\nDu schaust hin und willst direkt mehr wissen.\nWas fuehlst du bei {topic}?\n{self.config.ai_disclosure}",
            f"Zu perfekt, um zufaellig zu sein? 👀\nGenau dieser Moment zieht sofort Aufmerksamkeit.\nIst das fuer dich faszinierend oder unheimlich?\n{self.config.ai_disclosure}",
        ]
        return self._normalize_variants(base_variants)

    def _normalize_variants(self, variants: list[str]) -> list[str]:
        cleaned = []
        disclaimer_lines = [line.strip() for line in self.config.ai_disclosure.splitlines() if line.strip()]

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
            cleaned.append(text)

        if not cleaned:
            cleaned = [self.config.ai_disclosure]

        seed_variants = list(cleaned)
        while len(cleaned) < self.config.caption_variant_count:
            cleaned.append(seed_variants[len(cleaned) % len(seed_variants)])

        return cleaned[: self.config.caption_variant_count]

    def _choose_variant(self, variants: list[str]) -> str:
        if self.config.caption_selection_strategy == "first":
            return variants[0]
        return random.choice(variants)


def load_image_description(image_path: Path, descriptions_folder: Path | None) -> str:
    candidates = [image_path.with_suffix(".txt")]
    if descriptions_folder is not None:
        candidates.append(descriptions_folder / f"{image_path.stem}.txt")

    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip() or "Keine Bildbeschreibung vorhanden."

    return "Keine Bildbeschreibung vorhanden."