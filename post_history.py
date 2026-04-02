from __future__ import annotations

import json
import random
import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import logging
import re

from caption_generator import classify_cta_style, classify_hook_style, extract_caption_features

log = logging.getLogger(__name__)

IGNORED_THEME_PREFIXES = {"alle", "auto"}


def default_state() -> dict[str, Any]:
    return {
        "last_index": -1,
        "last_file": None,
        "next_image": None,
        "cycle_posted": [],
        "posted": [],
        "image_registry": {},
        "captions": {},
        "slot_runs": {},
        "generated_reels": [],
        "generated_stories": [],
        "follower_history": [],
        "comment_response_log": {},
        "auto_comment_history": [],
        "auto_comment_cache": [],
        "auto_comment_metrics": {
            "template_used": 0,
            "ollama_used": 0,
            "ollama_generated": 0,
            "ollama_filtered": 0,
            "cache_hits": 0,
            "template_fallbacks": 0,
        },
        "smart_slot_state": {
            "last_applied_slots": [],
            "last_sources": {},
            "last_updated_at": None,
        },
        "campaign_state": {
            "active_campaign": None,
            "active_theme": None,
            "last_updated_at": None,
        },
        "queue_state": {
            "pinned_next_image": None,
            "last_sort": "campaign",
            "last_updated_at": None,
        },
        "engagement_actions": {
            "recycle_queue": [],
            "alerts": [],
            "followup_comments": [],
        },
        "outreach_assist": {
            "items": [],
        },
        "reel_control": {
            "queue_override": [],
            "caption_override": "",
            "skip_anchors": [],
            "preview_path": None,
            "preview_updated_at": None,
            "planned_source_images": [],
            "planned_anchor_image": None,
            "planned_updated_at": None,
            "planned_caption": "",
            "planned_caption_source": None,
            "planned_caption_updated_at": None,
        },
    }


@dataclass(slots=True)
class PostHistory:
    path: Path

    def load(self) -> dict[str, Any]:
        if self.path.exists():
            with open(self.path, encoding="utf-8") as handle:
                state = json.load(handle)
        else:
            state = default_state()

        normalized = default_state()
        normalized.update(state)
        normalized.setdefault("image_registry", {})
        normalized.setdefault("captions", {})
        normalized.setdefault("slot_runs", {})
        normalized.setdefault("posted", [])
        normalized.setdefault("generated_reels", [])
        normalized.setdefault("generated_stories", [])
        normalized.setdefault("follower_history", [])
        normalized.setdefault("comment_response_log", {})
        normalized.setdefault("auto_comment_history", [])
        normalized.setdefault("auto_comment_cache", [])
        normalized.setdefault(
            "auto_comment_metrics",
            {
                "template_used": 0,
                "ollama_used": 0,
                "ollama_generated": 0,
                "ollama_filtered": 0,
                "cache_hits": 0,
                "template_fallbacks": 0,
            },
        )
        normalized.setdefault(
            "smart_slot_state",
            {
                "last_applied_slots": [],
                "last_sources": {},
                "last_updated_at": None,
            },
        )
        normalized.setdefault(
            "campaign_state",
            {
                "active_campaign": None,
                "active_theme": None,
                "last_updated_at": None,
            },
        )
        normalized.setdefault(
            "queue_state",
            {
                "pinned_next_image": None,
                "last_sort": "campaign",
                "last_updated_at": None,
            },
        )
        normalized.setdefault(
            "engagement_actions",
            {
                "recycle_queue": [],
                "alerts": [],
                "followup_comments": [],
            },
        )
        normalized.setdefault(
            "outreach_assist",
            {
                "items": [],
            },
        )
        normalized.setdefault(
            "reel_control",
            {
                "queue_override": [],
                "caption_override": "",
                "skip_anchors": [],
                "preview_path": None,
                "preview_updated_at": None,
                "planned_source_images": [],
                "planned_anchor_image": None,
                "planned_updated_at": None,
                "planned_caption": "",
                "planned_caption_source": None,
                "planned_caption_updated_at": None,
            },
        )
        return normalized

    def save(self, state: dict[str, Any]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)

    def get_recent_auto_comments(self, state: dict[str, Any], limit: int = 80) -> list[str]:
        return [
            str(entry.get("text") or "").strip()
            for entry in list(state.get("auto_comment_history", []))[-limit:]
            if str(entry.get("text") or "").strip()
        ]

    def get_auto_comment_cache(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in state.get("auto_comment_cache", []):
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                if text:
                    result.append(
                        {
                            "text": text,
                            "content_type": str(item.get("content_type") or "image").strip() or "image",
                            "style": str(item.get("style") or "").strip(),
                            "time": str(item.get("time") or "").strip(),
                        }
                    )
            else:
                text = str(item).strip()
                if text:
                    result.append({"text": text, "content_type": "image", "style": "", "time": ""})
        return result

    def set_auto_comment_cache(self, state: dict[str, Any], comments: list[dict[str, Any]], keep_last: int = 48):
        normalized: list[dict[str, Any]] = []
        for item in comments[:keep_last]:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            normalized.append(
                {
                    "text": text,
                    "content_type": str(item.get("content_type") or "image").strip() or "image",
                    "style": str(item.get("style") or "").strip(),
                    "time": str(item.get("time") or datetime.now().isoformat()).strip(),
                }
            )
        state["auto_comment_cache"] = normalized

    def record_auto_comment_attempt(
        self,
        state: dict[str, Any],
        post_id: str,
        text: str,
        source: str,
        content_type: str,
        status: str,
        filter_reason: str | None = None,
    ):
        history = state.setdefault("auto_comment_history", [])
        history.append(
            {
                "post_id": post_id,
                "text": text,
                "source": source,
                "content_type": content_type,
                "status": status,
                "filter_reason": filter_reason,
                "time": datetime.now().isoformat(),
            }
        )
        if len(history) > 200:
            state["auto_comment_history"] = history[-200:]

    def bump_auto_comment_metric(self, state: dict[str, Any], key: str, amount: int = 1):
        metrics = state.setdefault("auto_comment_metrics", {})
        metrics[key] = int(metrics.get(key, 0) or 0) + amount

    def get_auto_comment_metrics(self, state: dict[str, Any]) -> dict[str, int]:
        metrics = state.setdefault("auto_comment_metrics", {})
        return {
            "template_used": int(metrics.get("template_used", 0) or 0),
            "ollama_used": int(metrics.get("ollama_used", 0) or 0),
            "ollama_generated": int(metrics.get("ollama_generated", 0) or 0),
            "ollama_filtered": int(metrics.get("ollama_filtered", 0) or 0),
            "cache_hits": int(metrics.get("cache_hits", 0) or 0),
            "template_fallbacks": int(metrics.get("template_fallbacks", 0) or 0),
        }

    def prune_generated_reels(self, state: dict[str, Any], output_folder: Path, keep_last: int = 20):
        reels = list(state.get("generated_reels", []))
        if keep_last > 0 and len(reels) > keep_last:
            reels = reels[-keep_last:]
        state["generated_reels"] = reels

        referenced_paths = {
            str(Path(str(item.get("reel_path") or "")).resolve())
            for item in reels
            if str(item.get("reel_path") or "").strip()
        }

        preview_path = str((state.get("reel_control") or {}).get("preview_path") or "").strip()
        if preview_path:
            referenced_paths.add(str(Path(preview_path).resolve()))

        output_folder.mkdir(parents=True, exist_ok=True)
        for reel_file in output_folder.glob("*.mp4"):
            if str(reel_file.resolve()) in referenced_paths:
                continue
            try:
                reel_file.unlink(missing_ok=True)
            except Exception:
                log.warning("Altes Reel konnte nicht geloescht werden: %s", reel_file)

    def prune_generated_stories(self, state: dict[str, Any], output_folder: Path, keep_last: int = 60):
        stories = list(state.get("generated_stories", []))
        if keep_last > 0 and len(stories) > keep_last:
            stories = stories[-keep_last:]
        state["generated_stories"] = stories

        referenced_paths = {
            str(Path(str(item.get("story_path") or "")).resolve())
            for item in stories
            if str(item.get("story_path") or "").strip()
        }

        output_folder.mkdir(parents=True, exist_ok=True)
        for story_file in output_folder.glob("*.png"):
            if str(story_file.resolve()) in referenced_paths:
                continue
            try:
                story_file.unlink(missing_ok=True)
            except Exception:
                log.warning("Alte Story-Karte konnte nicht geloescht werden: %s", story_file)

    def sync_image_registry(self, state: dict[str, Any], images: list[Path]):
        registry = state.setdefault("image_registry", {})
        image_names = {image.name for image in images}
        posted_history = state.setdefault("posted", [])
        posted_lookup: dict[str, dict[str, Any]] = {}
        for entry in posted_history:
            file_name = str(entry.get("file") or "").strip()
            if file_name:
                posted_lookup[file_name] = entry

        for image in images:
            registry.setdefault(
                image.name,
                {
                    "posted": False,
                    "posted_at": None,
                    "caption": None,
                    "slot": None,
                    "post_id": None,
                },
            )
            if image.name in posted_lookup:
                history_entry = posted_lookup[image.name]
                registry[image.name].update(
                    {
                        "posted": True,
                        "posted_at": history_entry.get("time") or registry[image.name].get("posted_at"),
                        "caption": history_entry.get("caption") or registry[image.name].get("caption"),
                        "slot": history_entry.get("slot") or registry[image.name].get("slot"),
                        "post_id": history_entry.get("post_id") or registry[image.name].get("post_id"),
                    }
                )

        state["cycle_posted"] = [
            name for name, meta in registry.items() if meta.get("posted") and name in image_names
        ]

    def was_slot_processed(self, state: dict[str, Any], day_key: str, slot: str) -> bool:
        return slot in state.setdefault("slot_runs", {}).get(day_key, {})

    def count_successful_posts_for_day(self, state: dict[str, Any], day_key: str) -> int:
        day_runs = state.setdefault("slot_runs", {}).get(day_key, {})
        return sum(1 for item in day_runs.values() if item.get("status") == "posted")

    def count_feed_posts_for_day(self, state: dict[str, Any], day_key: str) -> int:
        return sum(
            1
            for entry in state.get("posted", [])
            if str(entry.get("time") or "").startswith(day_key) and entry.get("content_type") != "story_card"
        )

    def count_story_posts_for_day(self, state: dict[str, Any], day_key: str) -> int:
        return sum(
            1
            for entry in state.get("posted", [])
            if str(entry.get("time") or "").startswith(day_key) and entry.get("content_type") == "story_card"
        )

    def choose_story_text(self, state: dict[str, Any], texts: dict[str, list[str]], theme: str) -> str | None:
        theme_candidates = [text.strip() for text in texts.get(theme, []) if text.strip()]
        if not theme_candidates:
            return None

        last_used: dict[str, str] = {}
        for entry in state.get("generated_stories", []):
            text = str(entry.get("text") or "").strip()
            used_at = str(entry.get("time") or "")
            if text and used_at:
                last_used[text] = max(last_used.get(text, ""), used_at)

        unused = [text for text in theme_candidates if text not in last_used]
        if unused:
            return random.choice(unused)

        return min(theme_candidates, key=lambda item: last_used.get(item, "9999-12-31T23:59:59"))

    def choose_next_image(
        self,
        state: dict[str, Any],
        images: list[Path],
        selection_mode: str,
        exclude_names: set[str] | None = None,
        prefer_next_image: bool = True,
        preferred_theme: str | None = None,
        theme_separator: str = "_",
        quality_scores: dict[str, float] | None = None,
        disallowed_names: set[str] | None = None,
    ) -> Path | None:
        exclude_names = exclude_names or set()
        disallowed_names = disallowed_names or set()
        registry = state.setdefault("image_registry", {})
        available_images = [
            image
            for image in images
            if not registry.get(image.name, {}).get("posted") and image.name not in exclude_names and image.name not in disallowed_names
        ]
        if not available_images:
            return None

        if preferred_theme:
            themed_images = [
                image
                for image in available_images
                if self.infer_image_theme(image.name, theme_separator=theme_separator) == preferred_theme
            ]
            available_images = themed_images
            if not available_images:
                return None

        preferred_name = self.get_pinned_next_image(state) if prefer_next_image else None
        if not preferred_name:
            preferred_name = state.get("next_image") if prefer_next_image else None
        if preferred_name:
            preferred = next((image for image in available_images if image.name == preferred_name), None)
            if preferred is not None:
                return preferred

        if selection_mode == "sequential":
            return available_images[0]

        if quality_scores:
            weighted = []
            for image in available_images:
                weighted.append((image, max(float(quality_scores.get(image.name, 1.0) or 1.0), 0.1)))
            total = sum(weight for _, weight in weighted)
            pick = random.uniform(0, total)
            for image, weight in weighted:
                pick -= weight
                if pick <= 0:
                    return image
        return random.choice(available_images)

    def choose_reel_images(
        self,
        state: dict[str, Any],
        images: list[Path],
        selection_mode: str,
        count: int,
        anchor_image: Path | None = None,
    ) -> list[Path]:
        if count <= 0:
            return []

        available_images = list(images)
        if not available_images:
            return []

        selected: list[Path] = []
        seen_names: set[str] = set()

        if anchor_image is not None:
            anchor = next((image for image in available_images if image.name == anchor_image.name), None)
            if anchor is not None:
                selected.append(anchor)
                seen_names.add(anchor.name)

        if not selected:
            first = self.choose_next_image(
                state=state,
                images=images,
                selection_mode=selection_mode,
                prefer_next_image=True,
            )
            if first is not None:
                selected.append(first)
                seen_names.add(first.name)

        remaining_images = [image for image in available_images if image.name not in seen_names]
        if selection_mode == "random":
            remaining_images = list(remaining_images)
            random.shuffle(remaining_images)
        else:
            remaining_images = sorted(remaining_images, key=lambda item: item.name.lower())

        for image in remaining_images:
            if len(selected) >= count:
                break
            selected.append(image)

        return selected[:count]

    def _recent_reel_anchors(self, state: dict[str, Any], limit: int) -> list[str]:
        if limit <= 0:
            return []

        anchors: list[str] = []
        for reel in reversed(state.get("generated_reels", [])):
            source_images = reel.get("source_images") or []
            anchor_name = str(source_images[0]).strip() if source_images else str(reel.get("image_name") or "").strip()
            if anchor_name:
                anchors.append(anchor_name)
            if len(anchors) >= limit:
                break
        return anchors

    def _recent_reel_combinations(self, state: dict[str, Any], limit: int) -> set[tuple[str, ...]]:
        if limit <= 0:
            return set()

        combos: set[tuple[str, ...]] = set()
        for reel in reversed(state.get("generated_reels", [])):
            source_images = [str(name).strip() for name in (reel.get("source_images") or []) if str(name).strip()]
            if not source_images:
                image_name = str(reel.get("image_name") or "").strip()
                if image_name:
                    source_images = [image_name]
            if source_images:
                combos.add(tuple(sorted(dict.fromkeys(source_images))))
            if len(combos) >= limit:
                break
        return combos

    def plan_reel_images(
        self,
        state: dict[str, Any],
        images: list[Path],
        selection_mode: str,
        count: int,
        anchor_image: Path | None = None,
        queue_override: list[str] | None = None,
        skip_anchors: set[str] | None = None,
        anchor_cooldown_reels: int = 0,
        duplicate_window_reels: int = 0,
        prefer_next_anchor: bool = True,
        preferred_theme: str | None = None,
        theme_separator: str = "_",
    ) -> list[Path]:
        if count <= 0:
            return []

        available_images = list(images)
        if not available_images:
            return []

        if preferred_theme:
            themed_images = [
                image
                for image in available_images
                if self.infer_image_theme(image.name, theme_separator=theme_separator) == preferred_theme
            ]
            available_images = themed_images
            if not available_images:
                return []

        available_by_name = {image.name: image for image in available_images}
        queue_override = queue_override or []
        normalized_override = [
            name
            for name in (str(item).strip() for item in queue_override)
            if name and name in available_by_name
        ]
        skip_anchors = {str(name).strip() for name in (skip_anchors or set()) if str(name).strip()}
        anchor_locked = anchor_image is not None and anchor_image.name in available_by_name

        recent_anchors = set(self._recent_reel_anchors(state, anchor_cooldown_reels))
        recent_combinations = self._recent_reel_combinations(state, duplicate_window_reels)

        if selection_mode == "random":
            remaining_anchor_candidates = list(available_images)
            random.shuffle(remaining_anchor_candidates)
        else:
            remaining_anchor_candidates = sorted(available_images, key=lambda item: item.name.lower())

        preferred_anchor_name = state.get("next_image") if prefer_next_anchor else None
        preferred_anchor = None
        if preferred_anchor_name:
            preferred_anchor = available_by_name.get(str(preferred_anchor_name))

        anchor_candidates: list[Path] = []

        def add_anchor_candidate(candidate: Path | None):
            if candidate is None:
                return
            if candidate.name in {item.name for item in anchor_candidates}:
                return
            if candidate.name in skip_anchors:
                return
            anchor_candidates.append(candidate)

        if anchor_locked:
            add_anchor_candidate(available_by_name.get(anchor_image.name))
        else:
            add_anchor_candidate(preferred_anchor)

        for candidate in remaining_anchor_candidates:
            add_anchor_candidate(candidate)

        if not anchor_candidates:
            return []

        if not anchor_locked and recent_anchors:
            cooled_candidates = [candidate for candidate in anchor_candidates if candidate.name not in recent_anchors]
            if cooled_candidates:
                anchor_candidates = cooled_candidates

        def build_candidate(anchor: Path) -> list[Path]:
            selected: list[Path] = [anchor]
            seen_names: set[str] = {anchor.name}

            for name in normalized_override:
                if len(selected) >= count:
                    break
                if name in seen_names:
                    continue
                selected.append(available_by_name[name])
                seen_names.add(name)

            remaining_images = [image for image in available_images if image.name not in seen_names]
            if selection_mode == "random":
                random.shuffle(remaining_images)
            else:
                remaining_images = sorted(remaining_images, key=lambda item: item.name.lower())

            for image in remaining_images:
                if len(selected) >= count:
                    break
                selected.append(image)

            return selected[:count]

        enforce_duplicate_window = duplicate_window_reels > 0 and not normalized_override
        fallback_candidate: list[Path] = []

        for anchor in anchor_candidates:
            attempts = max(1, min(8, len(available_images))) if selection_mode == "random" else 1
            for _ in range(attempts):
                candidate = build_candidate(anchor)
                if not candidate:
                    continue
                if not fallback_candidate:
                    fallback_candidate = candidate
                if not enforce_duplicate_window:
                    return candidate

                combo_key = tuple(sorted(image.name for image in candidate))
                if combo_key not in recent_combinations:
                    return candidate

        return fallback_candidate

    def update_next_image(
        self,
        state: dict[str, Any],
        images: list[Path],
        selection_mode: str,
        preferred_theme: str | None = None,
        theme_separator: str = "_",
        quality_scores: dict[str, float] | None = None,
        disallowed_names: set[str] | None = None,
    ):
        next_image = self.choose_next_image(
            state,
            images,
            selection_mode,
            prefer_next_image=True,
            preferred_theme=preferred_theme,
            theme_separator=theme_separator,
            quality_scores=quality_scores,
            disallowed_names=disallowed_names,
        )
        state["next_image"] = next_image.name if next_image else None

    def infer_image_theme(self, image_name: str, theme_separator: str = "_") -> str:
        stem = Path(str(image_name or "")).stem
        if not stem:
            return ""
        if theme_separator and theme_separator in stem:
            theme = stem.split(theme_separator, maxsplit=1)[0]
        else:
            theme = re.split(r"[-\s]+", stem, maxsplit=1)[0]
        theme = re.sub(r"\d+$", "", theme).strip(" _-").lower()
        if theme in IGNORED_THEME_PREFIXES:
            return ""
        return theme

    def set_pinned_next_image(self, state: dict[str, Any], image_name: str | None):
        queue_state = state.setdefault("queue_state", {})
        queue_state["pinned_next_image"] = image_name or None
        queue_state["last_updated_at"] = datetime.now().isoformat()

    def get_pinned_next_image(self, state: dict[str, Any]) -> str | None:
        return str((state.get("queue_state") or {}).get("pinned_next_image") or "").strip() or None

    def clear_pinned_next_image(self, state: dict[str, Any], image_name: str | None = None):
        pinned = self.get_pinned_next_image(state)
        if image_name is None or pinned == image_name:
            self.set_pinned_next_image(state, None)

    def update_queue_sort(self, state: dict[str, Any], sort_key: str):
        queue_state = state.setdefault("queue_state", {})
        queue_state["last_sort"] = sort_key
        queue_state["last_updated_at"] = datetime.now().isoformat()

    def store_generated_captions(
        self,
        state: dict[str, Any],
        image_name: str,
        variants: list[str],
        selected: str,
        description: str,
        variant_metadata: list[dict[str, Any]] | None = None,
        selected_metadata: dict[str, Any] | None = None,
    ):
        state.setdefault("captions", {})[image_name] = {
            "variants": variants,
            "selected": selected,
            "description": description,
            "variant_metadata": variant_metadata or [],
            "selected_metadata": selected_metadata or {},
            "generated_at": datetime.now().isoformat(),
        }

    def store_generated_reel(
        self,
        state: dict[str, Any],
        image_name: str,
        source_images: list[str],
        reel_path: str,
        duration_seconds: int,
        frame_count: int,
        slot: str,
        caption: str,
        audio_source: str,
        audio_track: str | None,
        simulation_mode: bool,
        publish_status: str,
        publish_message: str,
        published_post_id: str | None = None,
        platform_results: dict[str, Any] | None = None,
        campaign: dict[str, Any] | None = None,
        caption_metadata: dict[str, Any] | None = None,
    ):
        state.setdefault("generated_reels", []).append(
            {
                "image_name": image_name,
                "source_images": source_images,
                "reel_path": reel_path,
                "duration_seconds": duration_seconds,
                "frame_count": frame_count,
                "slot": slot,
                "caption": caption,
                "audio_source": audio_source,
                "audio_track": audio_track,
                "simulation_mode": simulation_mode,
                "publish_status": publish_status,
                "publish_message": publish_message,
                "published_post_id": published_post_id,
                "platform_results": platform_results or {},
                "instagram_post_id": ((platform_results or {}).get("instagram") or {}).get("post_id"),
                "campaign": campaign or {},
                "caption_metadata": caption_metadata or {},
                "time": datetime.now().isoformat(),
            }
        )

    def mark_slot_run(
        self,
        state: dict[str, Any],
        day_key: str,
        slot: str,
        status: str,
        message: str = "",
        image_name: str | None = None,
        caption: str | None = None,
        post_id: str | None = None,
        content_type: str | None = None,
        platform_results: dict[str, Any] | None = None,
    ):
        state.setdefault("slot_runs", {}).setdefault(day_key, {})[slot] = {
            "status": status,
            "message": message,
            "image_name": image_name,
            "caption": caption,
            "post_id": post_id,
            "content_type": content_type,
            "platform_results": platform_results or {},
            "time": datetime.now().isoformat(),
        }

    def record_follower_count(self, state: dict[str, Any], count: int):
        state.setdefault("follower_history", []).append({
            "count": count,
            "time": datetime.now().isoformat(),
        })

    def get_weekly_growth(self, state: dict[str, Any]) -> float | None:
        history = state.get("follower_history", [])
        if len(history) < 2:
            return None
        latest = history[-1]
        cutoff = datetime.now() - timedelta(days=7)
        baseline = next(
            (entry for entry in reversed(history[:-1]) if datetime.fromisoformat(entry["time"]) <= cutoff),
            history[0],
        )
        return float(latest["count"] - baseline["count"])

    def compute_caption_feature_weights(self, state: dict[str, Any]) -> dict[str, float]:
        posts = [entry for entry in self._iter_entries_with_engagement(state) if entry.get("caption")]
        if len(posts) < 3:
            return {}

        # Confidence scaling: weights closer to 1.0 with fewer data points
        confidence = min(len(posts) / 10.0, 1.0)

        feature_names = ["starts_with_question", "starts_with_exclamation", "has_emoji_hook", "ends_with_question", "optimal_length"]
        buckets: dict[str, dict[str, list[int]]] = {f: {"with": [], "without": []} for f in feature_names}

        for entry in posts:
            caption = entry["caption"]
            eng = entry["engagement"]
            eng_score = eng.get("likes", 0) + eng.get("comments", 0) * 2 + eng.get("shares", 0) * 3
            features = extract_caption_features(caption)
            for f in feature_names:
                if features.get(f):
                    buckets[f]["with"].append(eng_score)
                else:
                    buckets[f]["without"].append(eng_score)

        weights: dict[str, float] = {}
        for f, data in buckets.items():
            if data["with"] and data["without"]:
                avg_with = sum(data["with"]) / len(data["with"])
                avg_without = sum(data["without"]) / len(data["without"])
                if avg_without > 0:
                    raw_weight = avg_with / avg_without
                    # Scale toward 1.0 based on confidence (fewer data → closer to neutral)
                    weights[f] = round(1.0 + (raw_weight - 1.0) * confidence, 3)
        return weights

    def compute_caption_experiment_stats(
        self,
        state: dict[str, Any],
        min_data_points: int = 4,
        content_type: str | None = None,
    ) -> dict[str, dict[str, float] | dict[str, int]]:
        posts = [
            entry
            for entry in self._iter_entries_with_engagement(state)
            if entry.get("caption") and (content_type is None or str(entry.get("content_type") or "image") == content_type)
        ]
        if not posts:
            return {
                "hook_weights": {},
                "cta_weights": {},
                "hook_counts": {},
                "cta_counts": {},
            }

        overall_average = sum(self.compute_image_score(entry) for entry in posts) / max(len(posts), 1)
        hook_scores: dict[str, list[int]] = {}
        cta_scores: dict[str, list[int]] = {}
        for entry in posts:
            metadata = entry.get("caption_metadata") or {}
            hook_style = str(metadata.get("hook_style") or classify_hook_style(entry.get("caption", ""))).strip() or "question"
            cta_style = str(metadata.get("cta_style") or classify_cta_style(entry.get("caption", ""))).strip() or "question"
            score = self.compute_image_score(entry)
            hook_scores.setdefault(hook_style, []).append(score)
            cta_scores.setdefault(cta_style, []).append(score)

        def _weights(buckets: dict[str, list[int]]) -> tuple[dict[str, float], dict[str, int]]:
            weights: dict[str, float] = {}
            counts: dict[str, int] = {}
            for style, scores in buckets.items():
                counts[style] = len(scores)
                if not scores or overall_average <= 0:
                    continue
                confidence = min(len(scores) / max(min_data_points, 1), 1.0)
                raw_weight = (sum(scores) / len(scores)) / overall_average
                weights[style] = round(1.0 + (raw_weight - 1.0) * confidence, 3)
            return weights, counts

        hook_weights, hook_counts = _weights(hook_scores)
        cta_weights, cta_counts = _weights(cta_scores)
        return {
            "hook_weights": hook_weights,
            "cta_weights": cta_weights,
            "hook_counts": hook_counts,
            "cta_counts": cta_counts,
        }

    @staticmethod
    def compute_image_score(entry: dict[str, Any]) -> int:
        """Calculates engagement score for a single post entry."""
        eng = entry.get("engagement") or {}
        return eng.get("likes", 0) + eng.get("comments", 0) * 3 + eng.get("shares", 0) * 5

    def _iter_entries_with_engagement(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for entry in state.get("posted", []):
            if entry.get("engagement"):
                item = dict(entry)
                item.setdefault("content_type", "image")
                entries.append(item)

        for entry in state.get("generated_reels", []):
            if entry.get("engagement"):
                item = dict(entry)
                item["post_id"] = item.get("published_post_id") or item.get("post_id")
                item["content_type"] = "reel"
                entries.append(item)

        return entries

    def _extract_visible_caption_lines(self, caption: str) -> list[str]:
        return [
            line.strip()
            for line in str(caption or "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def _caption_lead_key(self, line: str, words: int = 4) -> str:
        normalized = re.sub(r"[^a-z0-9\s]", " ", str(line).lower())
        tokens = [token for token in normalized.split() if token]
        return " ".join(tokens[:words])

    def _caption_display_label(self, line: str, fallback: str) -> str:
        stripped = str(line or "").strip()
        if not stripped:
            return fallback
        return textwrap.shorten(stripped, width=58, placeholder="...")

    def compute_hashtag_performance(self, state: dict[str, Any]) -> dict[str, dict]:
        """Returns per-hashtag engagement stats for posts that have engagement data."""
        hashtag_data: dict[str, list[int]] = {}
        for entry in self._iter_entries_with_engagement(state):
            caption = entry.get("caption", "") or ""
            score = self.compute_image_score(entry)
            for tag in re.findall(r"#\w+", caption):
                hashtag_data.setdefault(tag.lower(), []).append(score)
        return {
            tag: {
                "posts": len(scores),
                "avg_score": round(sum(scores) / len(scores), 1),
                "total_score": sum(scores),
            }
            for tag, scores in hashtag_data.items()
        }

    def compute_weekday_performance(self, state: dict[str, Any]) -> dict[str, float]:
        """Returns average engagement score per weekday+hour slot."""
        WEEKDAY_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
        weekday_data: dict[str, list[int]] = {}
        for entry in self._iter_entries_with_engagement(state):
            time_str = entry.get("time", "")
            if not time_str:
                continue
            try:
                dt = datetime.fromisoformat(time_str)
            except ValueError:
                continue
            key = f"{WEEKDAY_DE[dt.weekday()]}_{dt.strftime('%H:00')}"
            weekday_data.setdefault(key, []).append(self.compute_image_score(entry))
        return {
            key: round(sum(scores) / len(scores), 1)
            for key, scores in weekday_data.items()
        }

    def get_recent_engagement_trend(self, state: dict[str, Any], last_n: int = 5) -> float | None:
        """Returns average engagement score of the last N posts with engagement data."""
        posts_with_data = self._iter_entries_with_engagement(state)
        if not posts_with_data:
            return None
        recent = posts_with_data[-last_n:]
        scores = [self.compute_image_score(e) for e in recent]
        return round(sum(scores) / len(scores), 1)

    def compute_hook_performance(self, state: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for entry in self._iter_entries_with_engagement(state):
            lines = self._extract_visible_caption_lines(entry.get("caption", ""))
            if not lines:
                continue
            hook = lines[0]
            key = self._caption_lead_key(hook)
            if not key:
                continue
            score = self.compute_image_score(entry)
            bucket = buckets.setdefault(key, {"label": self._caption_display_label(hook, key), "scores": [], "posts": 0})
            bucket["scores"].append(score)
            bucket["posts"] += 1

        ranked = []
        for value in buckets.values():
            if not value["scores"]:
                continue
            ranked.append(
                {
                    "label": value["label"],
                    "posts": value["posts"],
                    "avg_score": round(sum(value["scores"]) / len(value["scores"]), 1),
                }
            )
        return sorted(ranked, key=lambda item: (item["avg_score"], item["posts"]), reverse=True)[:limit]

    def compute_cta_performance(self, state: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for entry in self._iter_entries_with_engagement(state):
            lines = self._extract_visible_caption_lines(entry.get("caption", ""))
            if len(lines) < 2:
                continue
            cta_line = lines[-1]
            key = self._caption_lead_key(cta_line)
            if not key:
                continue
            score = self.compute_image_score(entry)
            bucket = buckets.setdefault(key, {"label": self._caption_display_label(cta_line, key), "scores": [], "posts": 0})
            bucket["scores"].append(score)
            bucket["posts"] += 1

        ranked = []
        for value in buckets.values():
            if not value["scores"]:
                continue
            ranked.append(
                {
                    "label": value["label"],
                    "posts": value["posts"],
                    "avg_score": round(sum(value["scores"]) / len(value["scores"]), 1),
                }
            )
        return sorted(ranked, key=lambda item: (item["avg_score"], item["posts"]), reverse=True)[:limit]

    def compute_format_performance(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        buckets: dict[str, list[int]] = {"image": [], "reel": []}
        for entry in self._iter_entries_with_engagement(state):
            content_type = str(entry.get("content_type") or "image").lower()
            if content_type not in buckets:
                buckets[content_type] = []
            buckets[content_type].append(self.compute_image_score(entry))

        labels = {"image": "Bildposts", "reel": "Reels"}
        ranked = []
        for content_type, scores in buckets.items():
            if not scores:
                continue
            ranked.append(
                {
                    "content_type": content_type,
                    "label": labels.get(content_type, content_type.title()),
                    "posts": len(scores),
                    "avg_score": round(sum(scores) / len(scores), 1),
                    "best_score": max(scores),
                }
            )
        return sorted(ranked, key=lambda item: item["avg_score"], reverse=True)

    def compute_caption_style_winners(self, state: dict[str, Any], min_data_points: int = 4) -> dict[str, dict[str, str | float]]:
        result: dict[str, dict[str, str | float]] = {}
        for content_type in ("image", "reel"):
            stats = self.compute_caption_experiment_stats(state, min_data_points=min_data_points, content_type=content_type)
            hook_weights = stats.get("hook_weights", {})
            cta_weights = stats.get("cta_weights", {})
            winner_hook = max(hook_weights.items(), key=lambda item: item[1], default=("", 1.0))
            winner_cta = max(cta_weights.items(), key=lambda item: item[1], default=("", 1.0))
            result[content_type] = {
                "hook_style": winner_hook[0],
                "hook_weight": float(winner_hook[1] or 1.0),
                "cta_style": winner_cta[0],
                "cta_weight": float(winner_cta[1] or 1.0),
            }
        return result

    def compute_top_posts(self, state: dict[str, Any], limit: int = 6) -> list[dict[str, Any]]:
        ranked = []
        for entry in self._iter_entries_with_engagement(state):
            ranked.append(
                {
                    "file": str(entry.get("file") or entry.get("image_name") or Path(str(entry.get("reel_path") or "")).name),
                    "content_type": str(entry.get("content_type") or "image"),
                    "slot": str(entry.get("slot") or ""),
                    "score": self.compute_image_score(entry),
                    "time": str(entry.get("time") or ""),
                }
            )
        return sorted(ranked, key=lambda item: item["score"], reverse=True)[:limit]

    def clear_caption_cache(self, state: dict[str, Any]):
        """Clears all cached captions to force fresh generation in the next cycle."""
        count = len(state.get("captions", {}))
        state["captions"] = {}
        log.info("Caption-Cache geleert: %d Eintraege entfernt.", count)

    def reset_cycle(self, state: dict[str, Any], images: list[Path], selection_mode: str):
        """Resets the posting cycle: marks all images as unposted, clears caption cache."""
        registry = state.setdefault("image_registry", {})
        reset_count = 0
        for name in registry:
            if registry[name].get("posted"):
                registry[name]["posted"] = False
                registry[name]["posted_at"] = None
                reset_count += 1
        state["cycle_posted"] = []
        self.clear_caption_cache(state)
        self.update_next_image(state, images, selection_mode)
        log.info("Posting-Zyklus zurueckgesetzt: %d Bilder freigegeben.", reset_count)

    def compute_best_slots(
        self,
        state: dict[str, Any],
        top_count: int,
        min_data_points: int = 5,
        weekday: int | None = None,
        base_slots: list[str] | None = None,
        exploration_rate: float = 0.0,
    ) -> tuple[list[str], dict[str, str]]:
        slot_data: dict[str, list[int]] = {}
        weekday_slot_data: dict[str, list[int]] = {}
        for entry in self._iter_entries_with_engagement(state):
            slot = entry.get("slot")
            eng = entry.get("engagement")
            if not slot or not eng:
                continue
            score = eng.get("likes", 0) + eng.get("comments", 0) * 2 + eng.get("shares", 0) * 3
            slot_data.setdefault(slot, []).append(score)
            if weekday is not None:
                try:
                    dt = datetime.fromisoformat(str(entry.get("time") or ""))
                except ValueError:
                    continue
                if dt.weekday() == weekday:
                    weekday_slot_data.setdefault(slot, []).append(score)

        threshold = max(2, min_data_points // 3)
        candidate_pool = sorted(set(base_slots or list(slot_data) or []))
        scored_slots: list[tuple[str, float, str]] = []
        for slot in candidate_pool:
            weekday_scores = weekday_slot_data.get(slot, [])
            overall_scores = slot_data.get(slot, [])
            if len(weekday_scores) >= threshold:
                avg_score = sum(weekday_scores) / len(weekday_scores)
                source = "historical-weekday"
            elif len(overall_scores) >= threshold:
                avg_score = sum(overall_scores) / len(overall_scores)
                source = "historical-overall"
            else:
                continue
            scored_slots.append((slot, avg_score, source))

        if not scored_slots or max(len(v) for v in slot_data.values()) < min_data_points:
            return [], {}

        ranked = sorted(scored_slots, key=lambda item: item[1], reverse=True)
        selected = ranked[:top_count]
        selected_slots = [slot for slot, _, _ in selected]
        sources = {slot: source for slot, _, source in selected}

        remaining_slots = [slot for slot in candidate_pool if slot not in selected_slots]
        if exploration_rate > 0 and remaining_slots and top_count > 0:
            exploration_count = min(max(1, round(top_count * exploration_rate)), len(remaining_slots), top_count)
            if exploration_count > 0:
                retained_count = max(0, top_count - exploration_count)
                retained = selected_slots[:retained_count]
                for slot in remaining_slots[:exploration_count]:
                    retained.append(slot)
                    sources[slot] = "exploration"
                selected_slots = retained[:top_count]

        return sorted(selected_slots), sources

    def update_smart_slot_state(self, state: dict[str, Any], slots: list[str], sources: dict[str, str]):
        smart_state = state.setdefault("smart_slot_state", {})
        smart_state["last_applied_slots"] = list(slots)
        smart_state["last_sources"] = dict(sources)
        smart_state["last_updated_at"] = datetime.now().isoformat()

    def update_campaign_state(self, state: dict[str, Any], campaign_name: str | None, theme: str | None):
        campaign_state = state.setdefault("campaign_state", {})
        campaign_state["active_campaign"] = campaign_name
        campaign_state["active_theme"] = theme
        campaign_state["last_updated_at"] = datetime.now().isoformat()

    def compute_campaign_progress(self, state: dict[str, Any], campaign_name: str) -> dict[str, int]:
        progress = {"feed_posts": 0, "stories": 0, "reels": 0}
        for entry in state.get("posted", []):
            campaign = entry.get("campaign") or {}
            if str(campaign.get("campaign_name") or "") != campaign_name:
                continue
            content_type = str(entry.get("content_type") or "image")
            if content_type == "story_card":
                progress["stories"] += 1
            else:
                progress["feed_posts"] += 1
        for reel in state.get("generated_reels", []):
            campaign = reel.get("campaign") or {}
            if str(campaign.get("campaign_name") or "") != campaign_name:
                continue
            if reel.get("published_post_id") or str(reel.get("publish_status") or "") in {"published", "simulated", "created", "manual-published", "manual-simulated"}:
                progress["reels"] += 1
        return progress

    def add_engagement_alert(self, state: dict[str, Any], alert_type: str, post_id: str, message: str, score: int):
        alerts = state.setdefault("engagement_actions", {}).setdefault("alerts", [])
        alerts.append(
            {
                "type": alert_type,
                "post_id": post_id,
                "message": message,
                "score": score,
                "time": datetime.now().isoformat(),
            }
        )
        if len(alerts) > 60:
            state["engagement_actions"]["alerts"] = alerts[-60:]

    def queue_recycle_candidate(self, state: dict[str, Any], post_entry: dict[str, Any], formats: list[str], due_at: datetime):
        queue = state.setdefault("engagement_actions", {}).setdefault("recycle_queue", [])
        post_id = str(post_entry.get("post_id") or "")
        if not post_id:
            return
        if any(str(item.get("post_id") or "") == post_id for item in queue):
            return
        queue.append(
            {
                "post_id": post_id,
                "file": str(post_entry.get("file") or ""),
                "caption": str(post_entry.get("caption") or ""),
                "content_type": str(post_entry.get("content_type") or "image"),
                "formats": [str(fmt).lower() for fmt in formats if str(fmt).strip()],
                "due_at": due_at.isoformat(),
                "status": "queued",
                "time": datetime.now().isoformat(),
            }
        )

    def get_due_recycle_candidates(self, state: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now()
        result = []
        for item in (state.get("engagement_actions", {}) or {}).get("recycle_queue", []):
            if str(item.get("status") or "") != "queued":
                continue
            due_at = str(item.get("due_at") or "")
            if not due_at:
                continue
            try:
                if datetime.fromisoformat(due_at) <= now:
                    result.append(item)
            except ValueError:
                continue
        return result

    def mark_recycle_candidate_used(self, state: dict[str, Any], post_id: str, used_format: str):
        for item in (state.get("engagement_actions", {}) or {}).get("recycle_queue", []):
            if str(item.get("post_id") or "") == post_id:
                item["status"] = "used"
                item["used_format"] = used_format
                item["used_at"] = datetime.now().isoformat()
                break

    def record_followup_comment(self, state: dict[str, Any], post_id: str, text: str, category: str):
        history = state.setdefault("engagement_actions", {}).setdefault("followup_comments", [])
        history.append(
            {
                "post_id": post_id,
                "text": text,
                "category": category,
                "time": datetime.now().isoformat(),
            }
        )
        if len(history) > 120:
            state["engagement_actions"]["followup_comments"] = history[-120:]

    def has_followup_comment(self, state: dict[str, Any], post_id: str) -> bool:
        return any(str(item.get("post_id") or "") == post_id for item in (state.get("engagement_actions", {}) or {}).get("followup_comments", []))

    def mark_auto_commented(self, state: dict[str, Any], post_id: str):
        for entry in state.get("posted", []):
            if entry.get("post_id") == post_id:
                entry["auto_commented"] = True
                entry.pop("auto_comment_blocked", None)
                entry.pop("auto_comment_error", None)
                entry.pop("auto_comment_blocked_at", None)
                break
        for entry in state.get("generated_reels", []):
            if entry.get("published_post_id") == post_id:
                entry["auto_commented"] = True
                entry.pop("auto_comment_blocked", None)
                entry.pop("auto_comment_error", None)
                entry.pop("auto_comment_blocked_at", None)
                break

    def mark_auto_comment_blocked(self, state: dict[str, Any], post_id: str, reason: str):
        for entry in state.get("posted", []):
            if entry.get("post_id") == post_id:
                entry["auto_comment_blocked"] = True
                entry["auto_comment_error"] = reason
                entry["auto_comment_blocked_at"] = datetime.now().isoformat()
                break
        for entry in state.get("generated_reels", []):
            if entry.get("published_post_id") == post_id:
                entry["auto_comment_blocked"] = True
                entry["auto_comment_error"] = reason
                entry["auto_comment_blocked_at"] = datetime.now().isoformat()
                break

    def get_posts_needing_auto_comment(self, state: dict[str, Any], max_age_days: int) -> list[dict]:
        cutoff = datetime.now() - timedelta(days=max_age_days)
        result = []
        seen_post_ids: set[str] = set()
        for entry in state.get("posted", []):
            if entry.get("content_type") == "story_card":
                continue
            post_id = entry.get("post_id", "")
            if not post_id or post_id == "dry-run":
                continue
            if entry.get("auto_commented"):
                continue
            if entry.get("auto_comment_blocked"):
                continue
            posted_at_str = entry.get("time", "")
            if not posted_at_str:
                continue
            try:
                if datetime.fromisoformat(posted_at_str) < cutoff:
                    continue
            except ValueError:
                continue
            result.append(entry)
            seen_post_ids.add(str(post_id))

        for reel in state.get("generated_reels", []):
            post_id = str(reel.get("published_post_id") or "")
            if not post_id or post_id == "dry-run" or post_id in seen_post_ids:
                continue
            if reel.get("auto_commented") or reel.get("auto_comment_blocked"):
                continue
            posted_at_str = str(reel.get("time") or "")
            if not posted_at_str:
                continue
            try:
                if datetime.fromisoformat(posted_at_str) < cutoff:
                    continue
            except ValueError:
                continue
            result.append(
                {
                    "file": reel.get("image_name") or Path(str(reel.get("reel_path") or "")).name,
                    "time": posted_at_str,
                    "slot": reel.get("slot"),
                    "caption": reel.get("caption") or "",
                    "post_id": post_id,
                    "content_type": "reel",
                }
            )
            seen_post_ids.add(post_id)
        return result

    def get_posts_for_comment_response(self, state: dict[str, Any], lookback_days: int) -> list[dict]:
        cutoff = datetime.now() - timedelta(days=lookback_days)
        result = []
        seen_post_ids: set[str] = set()
        for entry in state.get("posted", []):
            if entry.get("content_type") == "story_card":
                continue
            post_id = entry.get("post_id", "")
            if not post_id or post_id == "dry-run":
                continue
            posted_at_str = entry.get("time", "")
            if not posted_at_str:
                continue
            try:
                if datetime.fromisoformat(posted_at_str) < cutoff:
                    continue
            except ValueError:
                continue
            result.append(entry)
            seen_post_ids.add(str(post_id))

        for reel in state.get("generated_reels", []):
            post_id = str(reel.get("published_post_id") or "")
            if not post_id or post_id == "dry-run" or post_id in seen_post_ids:
                continue
            posted_at_str = str(reel.get("time") or "")
            if not posted_at_str:
                continue
            try:
                if datetime.fromisoformat(posted_at_str) < cutoff:
                    continue
            except ValueError:
                continue
            result.append(
                {
                    "file": reel.get("image_name") or Path(str(reel.get("reel_path") or "")).name,
                    "time": posted_at_str,
                    "slot": reel.get("slot"),
                    "caption": reel.get("caption") or "",
                    "post_id": post_id,
                    "content_type": "reel",
                }
            )
            seen_post_ids.add(post_id)
        return result

    def mark_comment_replied(self, state: dict[str, Any], post_id: str, comment_id: str):
        log_entry = state.setdefault("comment_response_log", {}).setdefault(post_id, {
            "replied_comment_ids": [],
            "last_checked_at": None,
        })
        if comment_id not in log_entry["replied_comment_ids"]:
            log_entry["replied_comment_ids"].append(comment_id)
        log_entry["last_checked_at"] = datetime.now().isoformat()

    def get_replied_comment_ids(self, state: dict[str, Any], post_id: str) -> set[str]:
        log_entry = state.get("comment_response_log", {}).get(post_id, {})
        return set(log_entry.get("replied_comment_ids", []))

    def get_posts_needing_engagement_check(self, state: dict, delay_hours: int) -> list[dict]:
        now = datetime.now()
        pending = []
        seen_post_ids: set[str] = set()
        for entry in state.get("posted", []):
            if entry.get("content_type") == "story_card":
                continue
            post_id = entry.get("post_id", "")
            if not post_id or post_id == "dry-run":
                continue
            if entry.get("engagement_checked_at"):
                continue
            posted_at_str = entry.get("time", "")
            if not posted_at_str:
                continue
            try:
                posted_at = datetime.fromisoformat(posted_at_str)
            except ValueError:
                continue
            age_hours = (now - posted_at).total_seconds() / 3600
            if age_hours >= delay_hours:
                pending.append(entry)
                seen_post_ids.add(str(post_id))

        for reel in state.get("generated_reels", []):
            post_id = str(reel.get("published_post_id") or "")
            if not post_id or post_id == "dry-run" or post_id in seen_post_ids:
                continue
            if reel.get("engagement_checked_at"):
                continue
            posted_at_str = str(reel.get("time") or "")
            if not posted_at_str:
                continue
            try:
                posted_at = datetime.fromisoformat(posted_at_str)
            except ValueError:
                continue
            age_hours = (now - posted_at).total_seconds() / 3600
            if age_hours >= delay_hours:
                pending.append(
                    {
                        "file": reel.get("image_name") or Path(str(reel.get("reel_path") or "")).name,
                        "time": posted_at_str,
                        "slot": reel.get("slot"),
                        "caption": reel.get("caption") or "",
                        "post_id": post_id,
                        "content_type": "reel",
                    }
                )
                seen_post_ids.add(post_id)
        return pending

    def store_engagement(self, state: dict, post_id: str, engagement: dict):
        now = datetime.now().isoformat()
        likes = (engagement.get("likes") or {}).get("summary", {}).get("total_count", 0)
        comments = (engagement.get("comments") or {}).get("summary", {}).get("total_count", 0)
        shares = (engagement.get("shares") or {}).get("count", 0)

        for entry in state.get("posted", []):
            if entry.get("post_id") == post_id:
                entry["engagement"] = {"likes": likes, "comments": comments, "shares": shares}
                entry["engagement_checked_at"] = now
                break

        registry = state.get("image_registry", {})
        for name, meta in registry.items():
            if meta.get("post_id") == post_id:
                meta["engagement"] = {"likes": likes, "comments": comments, "shares": shares}
                break

        for reel in state.get("generated_reels", []):
            if reel.get("published_post_id") == post_id:
                reel["engagement"] = {"likes": likes, "comments": comments, "shares": shares}
                reel["engagement_checked_at"] = now
                break

    def record_post_success(
        self,
        state: dict[str, Any],
        image: Path,
        slot: str,
        caption: str,
        post_id: str,
        images_after_post: list[Path],
        selection_mode: str,
        platform_results: dict[str, Any] | None = None,
        campaign: dict[str, Any] | None = None,
        caption_metadata: dict[str, Any] | None = None,
        theme_separator: str = "_",
    ):
        now = datetime.now().isoformat()
        resolved_platform_results = platform_results or {}
        registry = state.setdefault("image_registry", {})
        registry.setdefault(image.name, {})
        registry[image.name].update(
            {
                "posted": True,
                "posted_at": now,
                "caption": caption,
                "slot": slot,
                "post_id": post_id,
                "platform_results": resolved_platform_results,
                "instagram_post_id": (resolved_platform_results.get("instagram") or {}).get("post_id"),
                "campaign": campaign or {},
                "caption_metadata": caption_metadata or {},
            }
        )

        state["last_file"] = image.name
        state["last_index"] = next(
            (index for index, current in enumerate(images_after_post) if current.name == image.name),
            -1,
        )
        state.setdefault("posted", []).append(
            {
                "file": image.name,
                "time": now,
                "slot": slot,
                "caption": caption,
                "post_id": post_id,
                "content_type": "image",
                "platform_results": resolved_platform_results,
                "instagram_post_id": (resolved_platform_results.get("instagram") or {}).get("post_id"),
                "campaign": campaign or {},
                "caption_metadata": caption_metadata or {},
            }
        )
        day_key = now.split("T", maxsplit=1)[0]
        self.mark_slot_run(
            state,
            day_key=day_key,
            slot=slot,
            status="posted",
            message="Posting erfolgreich.",
            image_name=image.name,
            caption=caption,
            post_id=post_id,
            content_type="image",
            platform_results=resolved_platform_results,
        )
        self.sync_image_registry(state, images_after_post)
        self.update_next_image(
            state,
            images_after_post,
            selection_mode,
            preferred_theme=str((campaign or {}).get("theme") or "").strip() or None,
            theme_separator=theme_separator,
        )

    def record_story_success(
        self,
        state: dict[str, Any],
        slot: str,
        text: str,
        story_path: str,
        post_id: str,
        theme: str,
        platform_results: dict[str, Any] | None = None,
    ):
        now = datetime.now().isoformat()
        resolved_platform_results = platform_results or {}
        state.setdefault("generated_stories", []).append(
            {
                "text": text,
                "theme": theme,
                "story_path": story_path,
                "slot": slot,
                "post_id": post_id,
                "platform_results": resolved_platform_results,
                "instagram_post_id": (resolved_platform_results.get("instagram") or {}).get("post_id"),
                "time": now,
            }
        )
        state.setdefault("posted", []).append(
            {
                "file": Path(story_path).name,
                "time": now,
                "slot": slot,
                "caption": text,
                "post_id": post_id,
                "content_type": "story_card",
                "theme": theme,
                "platform_results": resolved_platform_results,
                "instagram_post_id": (resolved_platform_results.get("instagram") or {}).get("post_id"),
            }
        )
        day_key = now.split("T", maxsplit=1)[0]
        self.mark_slot_run(
            state,
            day_key=day_key,
            slot=slot,
            status="posted",
            message="Story-Karte erfolgreich veroefentlicht.",
            image_name=Path(story_path).name,
            caption=text,
            post_id=post_id,
            content_type="story_card",
            platform_results=resolved_platform_results,
        )
