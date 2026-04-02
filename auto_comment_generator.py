from __future__ import annotations

import difflib
import json
import logging
import random
import re
from datetime import datetime
from typing import Any

import requests

from config import AppConfig

log = logging.getLogger(__name__)

_PLURAL_PERSONA_RE = re.compile(r"\b(wir|uns|unser|unsere|unserem|unseren|unsrer|unseres)\b", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#\w+")
_LOW_VALUE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^wow[!. ]*$",
        r"^krass[!. ]*$",
        r"^heftig[!. ]*$",
        r"^sehr schoen[!. ]*$",
        r"^nice[!. ]*$",
        r"^stark[!. ]*$",
    )
]

STYLE_PROFILES = {
    "frech": "leicht frech, charmant, souveraen und ein bisschen neckisch",
    "elegant": "elegant, ruhig, hochwertig und stilvoll",
    "verspielt": "locker, warm, verspielt und nahbar",
    "direkt": "direkt, schnell, pointiert und klar",
}

CONTENT_STYLE_HINTS = {
    "image": "etwas persoenlicher, bildnah und charmant",
    "reel": "kuerzer, direkter, dynamischer und leicht pushend",
}


class AutoCommentGenerator:
    def __init__(self, config: AppConfig):
        self.config = config
        self._cache: list[dict[str, Any]] = []

    def get_comment(self, state: dict[str, Any] | None = None, post_entry: dict[str, Any] | None = None) -> tuple[str, str, dict[str, Any]]:
        cfg = self.config.auto_comment
        content_type = self._resolve_content_type(post_entry)
        style_hint = self._resolve_style_hint(content_type)
        recent_comments = self._recent_comments(state)

        use_ollama = cfg.ollama_enabled and self.config.ollama.enabled and random.random() < max(0.0, min(cfg.ollama_ratio, 1.0))
        if use_ollama:
            selected = self._pop_cached_comment(state, content_type)
            if selected is None:
                generated, filtered_count = self._generate_with_ollama(post_entry, content_type, style_hint, recent_comments, max(1, min(cfg.ollama_cache_size, 12)))
                self._append_to_cache(state, generated)
                if state is not None and generated:
                    self._bump_metric(state, "ollama_generated", len(generated))
                if state is not None and filtered_count:
                    self._bump_metric(state, "ollama_filtered", filtered_count)
                selected = self._pop_cached_comment(state, content_type)
            if selected is not None:
                if state is not None:
                    self._bump_metric(state, "cache_hits")
                    self._bump_metric(state, "ollama_used")
                return selected["text"], "ollama", {
                    "content_type": content_type,
                    "style": style_hint,
                    "cache_used": True,
                }

            if state is not None:
                self._bump_metric(state, "template_fallbacks")

        template = self._choose_template(cfg.templates, recent_comments, post_entry)
        if state is not None:
            self._bump_metric(state, "template_used")
        return template, "template", {
            "content_type": content_type,
            "style": style_hint,
            "cache_used": False,
        }

    def _recent_comments(self, state: dict[str, Any] | None) -> list[str]:
        if state is None:
            return []
        entries = state.get("auto_comment_history", [])[-self.config.auto_comment.repeat_block_count :]
        return [str(entry.get("text") or "").strip() for entry in entries if str(entry.get("text") or "").strip()]

    def _load_cache(self, state: dict[str, Any] | None, content_type: str | None = None) -> list[dict[str, Any]]:
        source_items = self._cache if state is None else state.get("auto_comment_cache", [])
        cached = []
        for item in source_items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            if content_type and str(item.get("content_type") or "").strip() not in {content_type, "any", ""}:
                continue
            cached.append(item)
        self._cache = cached
        return list(cached)

    def _store_cache(self, state: dict[str, Any] | None, comments: list[dict[str, Any]]):
        normalized = comments[: self.config.auto_comment.ollama_cache_size]
        self._cache = list(normalized)
        if state is not None:
            state["auto_comment_cache"] = normalized

    def _append_to_cache(self, state: dict[str, Any] | None, comments: list[dict[str, Any]]):
        combined = self._load_cache(state, None) + comments
        self._store_cache(state, combined)

    def _pop_cached_comment(self, state: dict[str, Any] | None, content_type: str) -> dict[str, Any] | None:
        full_cache = self._load_cache(state, None)
        for index, item in enumerate(full_cache):
            cached_type = str(item.get("content_type") or "").strip()
            if cached_type not in {"", "any", content_type}:
                continue
            selected = item
            del full_cache[index]
            self._store_cache(state, full_cache)
            return selected
        return None

    def _generate_with_ollama(
        self,
        post_entry: dict[str, Any] | None,
        content_type: str,
        style_hint: str,
        recent_comments: list[str],
        count: int,
    ) -> tuple[list[dict[str, Any]], int]:
        prompt = self._build_prompt(post_entry, content_type, style_hint, count)
        try:
            response = requests.post(
                f"{self.config.ollama.base_url.rstrip('/')}/api/chat",
                json={
                    "model": self.config.ollama.model,
                    "stream": False,
                    "options": {
                        "temperature": min(max(self.config.ollama.temperature, 0.4), 1.0),
                    },
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Du schreibst kurze deutschsprachige Auto-Kommentare fuer die eigenen Facebook-Posts einer einzelnen Persona namens Macha-Letiz. "
                                "Die Kommentare sollen menschlich, charmant, leicht frech und engagierend wirken. "
                                "Schreibe 1 bis 2 kurze Saetze, optional mit einem sparsamen Emoji. "
                                "Keine Hashtags, keine langen Texte, keine Nummerierung, kein Werbe-Sprech, keine Anfuehrungszeichen. "
                                "Sprich niemals von wir, uns oder unser, sondern immer aus der Sicht einer einzelnen Person. "
                                "Gib ausschliesslich JSON im Format {\"comments\": [\"...\"]} zurueck."
                            ),
                        },
                        {
                            "role": "user",
                            "content": prompt,
                        },
                    ],
                },
                timeout=self.config.ollama.timeout_seconds,
            )
            response.raise_for_status()
            content = ((response.json().get("message") or {}).get("content") or "").strip()
            raw_comments = self._parse_comments(content)
            filtered_comments, filtered_count = self._filter_comments(raw_comments, recent_comments, post_entry)
            comments = [
                {
                    "text": text,
                    "content_type": content_type,
                    "style": style_hint,
                    "time": datetime.now().isoformat(),
                }
                for text in filtered_comments
            ]
            if comments:
                log.info("Ollama-Auto-Kommentar-Cache gefuellt: %d Eintraege", len(comments))
            return comments, filtered_count
        except Exception as exc:
            log.warning("Ollama-Auto-Kommentar-Generierung fehlgeschlagen: %s", exc)
            return [], 0

    def _build_prompt(self, post_entry: dict[str, Any] | None, content_type: str, style_hint: str, count: int) -> str:
        context_lines: list[str] = []
        if post_entry:
            caption = self._clean_caption(str(post_entry.get("caption") or ""))
            file_name = str(post_entry.get("file") or "").strip()
            slot = str(post_entry.get("slot") or "").strip()
            description = str(post_entry.get("description") or "").strip()
            if file_name:
                context_lines.append(f"Dateiname: {file_name}")
            if slot:
                context_lines.append(f"Posting-Slot: {slot}")
            if caption:
                context_lines.append(f"Post-Caption: {caption}")
            if description:
                context_lines.append(f"Bildbeschreibung: {description}")

        context_block = "\n".join(context_lines) if context_lines else "Kein weiterer Kontext vorhanden."
        persona_style = STYLE_PROFILES.get(self.config.auto_comment.style_profile, STYLE_PROFILES["frech"])
        content_style = CONTENT_STYLE_HINTS.get(content_type, CONTENT_STYLE_HINTS["image"])
        return (
            f"Erstelle {count} verschiedene kurze Auto-Kommentare fuer einen eigenen Facebook-Post.\n"
            "Ziele:\n"
            "- natuerlich und menschlich\n"
            "- 1 bis 2 kurze Saetze\n"
            f"- Persona-Stil: {persona_style}\n"
            f"- Format-Stil: {content_style}\n"
            f"- Feinton: {style_hint}\n"
            "- kommentarfreudig, aber nicht aufdringlich\n"
            "- keine Hashtags\n"
            "- keine Nummerierung\n"
            "- keine Wiederholung der Caption im Wortlaut\n"
            "- keine Formulierungen mit wir, uns oder unser\n"
            "- sparsam Emojis, nicht in jedem Kommentar\n\n"
            f"Kontext:\n{context_block}"
        )

    def _resolve_content_type(self, post_entry: dict[str, Any] | None) -> str:
        content_type = str((post_entry or {}).get("content_type") or "").strip().lower()
        if content_type == "reel":
            return "reel"
        return "image"

    def _resolve_style_hint(self, content_type: str) -> str:
        if content_type == "reel":
            return self.config.auto_comment.reel_style
        return self.config.auto_comment.feed_style

    def _choose_template(self, templates: list[str], recent_comments: list[str], post_entry: dict[str, Any] | None) -> str:
        candidates = [template for template in templates if self._is_acceptable(template, recent_comments, post_entry)[0]]
        if not candidates:
            return random.choice(templates)
        return random.choice(candidates)

    def _clean_caption(self, caption: str) -> str:
        lines = []
        for line in caption.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("Dieses Bild wurde mit KI erstellt"):
                continue
            if stripped.startswith("Aehnlichkeiten mit realen Personen") or stripped.startswith("Ähnlichkeiten mit realen Personen"):
                continue
            lines.append(stripped)
        compact = " ".join(lines).strip()
        return compact[:280]

    def _parse_comments(self, content: str) -> list[str]:
        comments: list[str] = []
        try:
            payload = json.loads(content)
            raw_comments = payload.get("comments") or []
            if isinstance(raw_comments, list):
                comments.extend(str(item).strip() for item in raw_comments)
        except Exception:
            pass

        if not comments:
            for line in content.splitlines():
                stripped = re.sub(r"^[-*\d\.)\s]+", "", line.strip())
                if stripped:
                    comments.append(stripped)

        normalized: list[str] = []
        seen: set[str] = set()
        for comment in comments:
            compact = " ".join(comment.split()).strip().strip('"')
            if len(compact) < 12 or len(compact) > 220:
                continue
            if _PLURAL_PERSONA_RE.search(compact):
                continue
            if compact in seen:
                continue
            seen.add(compact)
            normalized.append(compact)
        return normalized

    def _filter_comments(self, comments: list[str], recent_comments: list[str], post_entry: dict[str, Any] | None) -> tuple[list[str], int]:
        accepted: list[str] = []
        filtered = 0
        local_recent = list(recent_comments)
        for comment in comments:
            ok, _reason = self._is_acceptable(comment, local_recent, post_entry)
            if ok:
                accepted.append(comment)
                local_recent.append(comment)
            else:
                filtered += 1
        return accepted, filtered

    def _is_acceptable(self, comment: str, recent_comments: list[str], post_entry: dict[str, Any] | None) -> tuple[bool, str | None]:
        compact = " ".join(comment.split()).strip()
        if len(compact) < 18 or len(compact) > 220:
            return False, "length"
        if _PLURAL_PERSONA_RE.search(compact):
            return False, "plural"
        if _HASHTAG_RE.search(compact):
            return False, "hashtag"
        if any(pattern.search(compact) for pattern in _LOW_VALUE_PATTERNS):
            return False, "low-value"

        lower_comment = compact.lower()
        caption = self._clean_caption(str((post_entry or {}).get("caption") or "")).lower()
        if caption and lower_comment in caption:
            return False, "caption-repeat"

        for previous in recent_comments[-self.config.auto_comment.repeat_block_count :]:
            similarity = difflib.SequenceMatcher(None, lower_comment, previous.lower()).ratio()
            if similarity >= 0.88:
                return False, "too-similar"
        return True, None

    def _bump_metric(self, state: dict[str, Any], key: str, amount: int = 1):
        metrics = state.setdefault("auto_comment_metrics", {})
        metrics[key] = int(metrics.get(key, 0) or 0) + amount
