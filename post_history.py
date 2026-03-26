from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


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

    def choose_next_image(
        self,
        state: dict[str, Any],
        images: list[Path],
        selection_mode: str,
        exclude_names: set[str] | None = None,
        prefer_next_image: bool = True,
    ) -> Path | None:
        exclude_names = exclude_names or set()
        registry = state.setdefault("image_registry", {})
        available_images = [
            image
            for image in images
            if not registry.get(image.name, {}).get("posted") and image.name not in exclude_names
        ]
        if not available_images:
            return None

        preferred_name = state.get("next_image") if prefer_next_image else None
        if preferred_name:
            preferred = next((image for image in available_images if image.name == preferred_name), None)
            if preferred is not None:
                return preferred

        if selection_mode == "sequential":
            return available_images[0]

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
    ) -> list[Path]:
        if count <= 0:
            return []

        available_images = list(images)
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

    def update_next_image(self, state: dict[str, Any], images: list[Path], selection_mode: str):
        next_image = self.choose_next_image(state, images, selection_mode, prefer_next_image=True)
        state["next_image"] = next_image.name if next_image else None

    def store_generated_captions(
        self,
        state: dict[str, Any],
        image_name: str,
        variants: list[str],
        selected: str,
        description: str,
    ):
        state.setdefault("captions", {})[image_name] = {
            "variants": variants,
            "selected": selected,
            "description": description,
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
    ):
        state.setdefault("slot_runs", {}).setdefault(day_key, {})[slot] = {
            "status": status,
            "message": message,
            "image_name": image_name,
            "caption": caption,
            "post_id": post_id,
            "time": datetime.now().isoformat(),
        }

    def record_post_success(
        self,
        state: dict[str, Any],
        image: Path,
        slot: str,
        caption: str,
        post_id: str,
        images_after_post: list[Path],
        selection_mode: str,
    ):
        now = datetime.now().isoformat()
        registry = state.setdefault("image_registry", {})
        registry.setdefault(image.name, {})
        registry[image.name].update(
            {
                "posted": True,
                "posted_at": now,
                "caption": caption,
                "slot": slot,
                "post_id": post_id,
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
        )
        self.sync_image_registry(state, images_after_post)
        self.update_next_image(state, images_after_post, selection_mode)