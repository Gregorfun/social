from __future__ import annotations

import ctypes
import logging
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image

from auto_comment_generator import AutoCommentGenerator
from caption_generator import CaptionGenerator
from config import AppConfig, LOCK_FILE, load_settings, setup_logging
from facebook_poster import FacebookPoster
from instagram_poster import InstagramPoster
from post_history import PostHistory
from reel_generator import ReelGenerator
from scheduler import DailySlotScheduler
from story_generator import StoryGenerator

if os.name != "nt":
    import fcntl

log = logging.getLogger(__name__)

_lock_handle = None
_mutex_handle = None


class AutoPostingService:
    WEEKDAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    def __init__(self, config: AppConfig):
        self.config = config
        self.history = PostHistory(config.history_file)
        self.caption_generator = CaptionGenerator(config)
        self.auto_comment_generator = AutoCommentGenerator(config)
        self.facebook_poster = FacebookPoster(config)
        self.instagram_poster = InstagramPoster(config)
        self.reel_generator = ReelGenerator(config)
        self.story_generator = StoryGenerator(config)
        self._comment_api_unavailable_reason: str | None = None

    def _get_post_entry(self, state: dict[str, Any] | None, post_id: str) -> dict[str, Any] | None:
        if state is None or not post_id:
            return None
        for entry in reversed(state.get("posted", [])):
            if entry.get("post_id") == post_id:
                enriched = dict(entry)
                file_name = str(enriched.get("file") or "").strip()
                if file_name:
                    enriched["description"] = str(((state.get("captions") or {}).get(file_name) or {}).get("description") or "")
                return enriched
        for reel in reversed(state.get("generated_reels", [])):
            if reel.get("published_post_id") == post_id:
                return {
                    "file": reel.get("image_name") or Path(str(reel.get("reel_path") or "")).name,
                    "slot": reel.get("slot"),
                    "caption": reel.get("caption") or "",
                    "post_id": post_id,
                    "content_type": "reel",
                    "description": " ".join(str(item) for item in (reel.get("source_images") or []) if str(item).strip()),
                }
        return None

    def _story_theme_for_slot(self, slot: str, now: datetime) -> str:
        try:
            hour = int(slot.split(":", maxsplit=1)[0])
        except (ValueError, IndexError):
            hour = now.hour

        if 5 <= hour <= 10:
            return "morning"
        if 11 <= hour <= 16:
            return "day"
        if 17 <= hour <= 20:
            return "evening"
        return "night"

    def _humanize_story_theme(self, theme: str) -> str:
        cleaned = str(theme or "").strip().replace("-", " ").replace("_", " ")
        return cleaned.title() if cleaned else "Visual Story"

    def _story_sequence_variant(self, state: dict[str, Any], day_key: str) -> str:
        sequence = ["hook", "question", "prompt"]
        index = self.history.count_story_posts_for_day(state, day_key)
        return sequence[min(index, len(sequence) - 1)]

    def _pick_story_background_image(self, images: list[Path], preferred_theme: str | None) -> Path | None:
        theme_separator = self.config.campaigns.theme_separator
        if preferred_theme:
            themed = [
                image for image in images
                if self.history.infer_image_theme(image.name, theme_separator) == preferred_theme
            ]
            if themed:
                return random.choice(themed)

        valid = [
            image for image in images
            if self.history.infer_image_theme(image.name, theme_separator)
        ]
        if valid:
            return random.choice(valid)
        return random.choice(images) if images else None

    def _build_story_sequence_text(self, theme: str, base_text: str, variant: str) -> str:
        theme_label = self._humanize_story_theme(theme)
        base = str(base_text or "").strip()
        if variant == "hook":
            return f"{theme_label} heute.\nZu stark zum Wegklicken?"
        if variant == "question":
            return f"{theme_label} oder nur Fantasie?\n{base or 'Wuerdest du hier laenger hinschauen?'}"
        if variant == "prompt":
            return f"{base or 'Welcher Look sollte als Naechstes kommen?'}\nMehr {theme_label} oder lieber etwas ganz anderes?"
        return base or f"{theme_label} fuer deine Story."

    def _content_mode_for_day(self, now: datetime) -> str:
        cfg = self.config.campaigns
        weekday_key = self.WEEKDAY_KEYS[now.weekday()]
        mode = str((cfg.weekday_modes or {}).get(weekday_key, "theme")).strip().lower()
        return mode if mode in {"theme", "mix"} else "theme"

    def _get_campaign_blueprint(self, state: dict[str, Any], now: datetime) -> dict[str, Any]:
        cfg = self.config.campaigns
        if not cfg.enabled:
            return {}

        target_campaign = None
        override_name = str(((state.get("campaign_state") or {}).get("campaign_override")) or "").strip()
        if not override_name and cfg.active_campaign_name:
            target_campaign = next(
                (campaign for campaign in cfg.campaigns if campaign.name == cfg.active_campaign_name),
                None,
            )
        if override_name:
            target_campaign = next((campaign for campaign in cfg.campaigns if campaign.name == override_name), None)
        if target_campaign is None:
            for campaign in cfg.campaigns:
                if campaign.start_date and now.date().isoformat() < campaign.start_date:
                    continue
                if campaign.end_date and now.date().isoformat() > campaign.end_date:
                    continue
                target_campaign = campaign
                break
        if target_campaign is None and cfg.auto_rotate and cfg.campaigns:
            target_campaign = cfg.campaigns[now.toordinal() % len(cfg.campaigns)]
        if target_campaign is None:
            return {}

        themes = [theme.strip().lower() for theme in target_campaign.themes if theme.strip()]
        if not themes:
            return {}

        reference_date = target_campaign.start_date or now.date().isoformat()
        try:
            start_dt = datetime.fromisoformat(reference_date)
        except ValueError:
            start_dt = now
        days_active = max(0, (now.date() - start_dt.date()).days)
        theme_index = (days_active // max(1, target_campaign.days_per_theme)) % len(themes)
        return {
            "campaign_name": target_campaign.name,
            "theme": themes[theme_index],
            "preferred_slots": list(target_campaign.preferred_slots),
            "days_per_theme": target_campaign.days_per_theme,
            "target_feed_posts": target_campaign.target_feed_posts,
            "target_stories": target_campaign.target_stories,
            "target_reels": target_campaign.target_reels,
        }

    def _resolve_campaign_context(self, state: dict[str, Any], images: list[Path], now: datetime) -> dict[str, Any]:
        override_theme = str((self.config.campaigns.daily_theme_overrides or {}).get(now.date().isoformat()) or "").strip().lower()
        if override_theme:
            self.history.update_campaign_state(state, "Tages-Thema", override_theme)
            return {
                "campaign_name": "Tages-Thema",
                "theme": override_theme,
                "preferred_slots": [],
                "days_per_theme": 1,
                "mode": "theme",
            }

        content_mode = self._content_mode_for_day(now)
        if content_mode == "mix":
            self.history.update_campaign_state(state, "Querbeet", None)
            return {
                "campaign_name": "Querbeet",
                "theme": None,
                "preferred_slots": [],
                "days_per_theme": 1,
                "mode": "mix",
            }

        blueprint = self._get_campaign_blueprint(state, now)
        if blueprint:
            blueprint["mode"] = "theme"
            self.history.update_campaign_state(state, blueprint.get("campaign_name"), blueprint.get("theme"))
            return blueprint

        cfg = self.config.campaigns
        if not cfg.enabled or not cfg.fallback_to_detected_themes:
            self.history.update_campaign_state(state, None, None)
            return {}

        detected_themes = sorted(
            {
                self.history.infer_image_theme(image.name, theme_separator=cfg.theme_separator)
                for image in images
                if self.history.infer_image_theme(image.name, theme_separator=cfg.theme_separator)
            }
        )
        if not detected_themes:
            self.history.update_campaign_state(state, None, None)
            return {}

        theme_index = (now.toordinal() // max(1, cfg.default_days_per_theme)) % len(detected_themes)
        theme = detected_themes[theme_index]
        context = {
            "campaign_name": "Auto-Serie",
            "theme": theme,
            "preferred_slots": [],
            "days_per_theme": cfg.default_days_per_theme,
            "mode": "theme",
        }
        self.history.update_campaign_state(state, context["campaign_name"], context["theme"])
        return context

    def _campaign_theme_exclusions(self, images: list[Path], mode: str = "theme") -> set[str]:
        if not self.config.campaigns.enabled:
            return set()
        if mode != "theme":
            return set()
        excluded: set[str] = set()
        for image in images:
            theme = self.history.infer_image_theme(image.name, self.config.campaigns.theme_separator)
            if not theme:
                excluded.add(image.name)
        return excluded

    def _choose_feed_image(
        self,
        state: dict[str, Any],
        images: list[Path],
        preferred_theme: str | None,
        quality_scores: dict[str, float],
        disallowed_names: set[str],
        content_mode: str = "theme",
    ) -> tuple[Path | None, str]:
        image = self.history.choose_next_image(
            state,
            images,
            self.config.selection_mode,
            preferred_theme=preferred_theme if content_mode == "theme" else None,
            theme_separator=self.config.campaigns.theme_separator,
            quality_scores=quality_scores,
            disallowed_names=disallowed_names,
        )
        if image is not None:
            return image, "preferred_theme" if preferred_theme and content_mode == "theme" else "mix" if content_mode == "mix" else "default"

        fallback_disallowed = set(disallowed_names) | self._campaign_theme_exclusions(images, mode=content_mode)
        image = self.history.choose_next_image(
            state,
            images,
            self.config.selection_mode,
            preferred_theme=None,
            theme_separator=self.config.campaigns.theme_separator,
            quality_scores=quality_scores,
            disallowed_names=fallback_disallowed,
        )
        if image is not None:
            return image, "theme_fallback"
        return None, "none"

    def _update_next_image_with_fallback(
        self,
        state: dict[str, Any],
        images: list[Path],
        preferred_theme: str | None,
        quality_scores: dict[str, float],
        disallowed_names: set[str],
        content_mode: str = "theme",
    ):
        image, _ = self._choose_feed_image(
            state,
            images,
            preferred_theme,
            quality_scores,
            disallowed_names,
            content_mode=content_mode,
        )
        state["next_image"] = image.name if image is not None else None

    def _hamming_distance(self, left: str, right: str) -> int:
        if not left or not right or len(left) != len(right):
            return 999
        return sum(1 for a, b in zip(left, right) if a != b)

    def _average_hash(self, image_path: Path) -> tuple[str, int, int]:
        with Image.open(image_path) as image:
            grayscale = image.convert("L")
            width, height = grayscale.size
            reduced = grayscale.resize((8, 8), Image.Resampling.LANCZOS)
            pixels = list(reduced.getdata())
        avg = sum(pixels) / max(len(pixels), 1)
        bits = "".join("1" if pixel >= avg else "0" for pixel in pixels)
        return bits, width, height

    def _image_quality_score(self, image_path: Path, width: int, height: int, preferred_theme: str | None) -> int:
        score = 35
        score += min(int(min(width, height) / 40), 25)
        aspect_ratio = width / max(height, 1)
        if 0.45 <= aspect_ratio <= 1.1:
            score += 18
        elif 0.35 <= aspect_ratio <= 1.3:
            score += 10
        size_mb = image_path.stat().st_size / (1024 * 1024)
        if 0.15 <= size_mb <= self.config.image_validation.max_file_size_mb:
            score += min(int(size_mb * 3), 12)
        theme = self.history.infer_image_theme(image_path.name, self.config.campaigns.theme_separator)
        if preferred_theme and theme == preferred_theme:
            score += 8
        if width < self.config.image_validation.min_width or height < self.config.image_validation.min_height:
            score -= 30
        return max(0, min(score, 100))

    def _prepare_image_inventory(
        self,
        state: dict[str, Any],
        images: list[Path],
        preferred_theme: str | None,
    ) -> tuple[dict[str, float], set[str], list[dict[str, Any]]]:
        registry = state.setdefault("image_registry", {})
        quality_scores: dict[str, float] = {}
        disallowed_names: set[str] = set()
        diagnostics: list[dict[str, Any]] = []
        cfg = self.config.content_quality
        whitelist = set(cfg.theme_whitelist)
        blacklist = set(cfg.theme_blacklist)
        current_names = {image.name for image in images}
        recent_hashes: list[tuple[str, str]] = [
            (name, str(meta.get("hash") or ""))
            for name, meta in registry.items()
            if name not in current_names and str(meta.get("hash") or "").strip()
        ]

        for image in images:
            meta = registry.setdefault(image.name, {})
            theme = self.history.infer_image_theme(image.name, self.config.campaigns.theme_separator)
            meta["theme"] = theme

            if whitelist and theme not in whitelist:
                disallowed_names.add(image.name)
                diagnostics.append({"file": image.name, "reason": "theme_not_whitelisted", "theme": theme})
                continue
            if blacklist and theme in blacklist:
                disallowed_names.add(image.name)
                diagnostics.append({"file": image.name, "reason": "theme_blacklisted", "theme": theme})
                continue

            hash_bits = str(meta.get("hash") or "")
            width = int(meta.get("width") or 0)
            height = int(meta.get("height") or 0)
            try:
                if not hash_bits or width <= 0 or height <= 0:
                    hash_bits, width, height = self._average_hash(image)
                    meta["hash"] = hash_bits
                    meta["width"] = width
                    meta["height"] = height
            except Exception as exc:
                log.warning("Bildanalyse fehlgeschlagen fuer %s: %s", image.name, exc)
                disallowed_names.add(image.name)
                diagnostics.append({"file": image.name, "reason": "analysis_failed"})
                continue

            quality_score = self._image_quality_score(image, width, height, preferred_theme)
            meta["quality_score"] = quality_score
            quality_scores[image.name] = max(quality_score / 100.0, 0.1)

            if cfg.enabled and quality_score < cfg.min_score:
                disallowed_names.add(image.name)
                diagnostics.append({"file": image.name, "reason": "quality_below_threshold", "score": quality_score})
                continue

            if cfg.enabled and cfg.skip_similar_images:
                duplicate_of = ""
                for other_name, other_hash in recent_hashes:
                    if self._hamming_distance(hash_bits, other_hash) <= cfg.duplicate_hamming_threshold:
                        duplicate_of = other_name
                        break
                if duplicate_of:
                    disallowed_names.add(image.name)
                    meta["duplicate_of"] = duplicate_of
                    diagnostics.append({"file": image.name, "reason": "similar_image", "duplicate_of": duplicate_of})
                    continue

            recent_hashes.append((image.name, hash_bits))
            meta.pop("duplicate_of", None)

        state["content_quality"] = {
            "last_scan_at": datetime.now().isoformat(),
            "diagnostics": diagnostics[-120:],
        }
        return quality_scores, disallowed_names, diagnostics

    def _recycle_story_text(self, post_entry: dict[str, Any]) -> str:
        caption = str(post_entry.get("caption") or "").strip()
        lines = [line.strip() for line in caption.splitlines() if line.strip() and not line.strip().startswith("#")]
        lead = lines[0] if lines else "Dieses Motiv verdient noch einen Blick."
        return f"Noch nicht gesehen?\n{lead[:110]}"

    def _consume_due_recycle_story(self, state: dict[str, Any], day_key: str, slot: str) -> bool:
        candidates = [
            item for item in self.history.get_due_recycle_candidates(state)
            if "story" in [str(fmt).lower() for fmt in item.get("formats", [])]
        ]
        if not candidates or not self.config.stories.enabled:
            return False
        item = candidates[0]
        text = self._recycle_story_text(item)
        theme = self._story_theme_for_slot(slot, datetime.now())
        card_result = self.story_generator.generate_story_card(text, theme)
        post_id, platform_results, published = self._publish_story_targets(card_result.output_path)
        if not published:
            return False
        self.history.record_story_success(
            state,
            slot=slot,
            text=text,
            story_path=str(card_result.output_path),
            post_id=post_id,
            theme=theme,
            platform_results=platform_results,
        )
        self.history.mark_recycle_candidate_used(state, str(item.get("post_id") or ""), "story")
        self.history.prune_generated_stories(state, self.config.stories.output_folder, keep_last=60)
        self.history.save(state)
        log.info("Recycle-Story fuer Post %s ausgefuehrt.", item.get("post_id"))
        return True

    def _due_recycle_reel_anchor(self, state: dict[str, Any], images: list[Path]) -> Path | None:
        candidates = [
            item for item in self.history.get_due_recycle_candidates(state)
            if "reel" in [str(fmt).lower() for fmt in item.get("formats", [])]
        ]
        if not candidates:
            return None
        available = {image.name: image for image in images}
        for item in candidates:
            file_name = str(item.get("file") or "").strip()
            if file_name in available:
                item["recycle_selected_at"] = datetime.now().isoformat()
                return available[file_name]
        return None

    def _experiment_stats_for_content(
        self,
        state: dict[str, Any],
        content_type: str,
        low_engagement_mode: bool,
    ) -> dict[str, dict[str, float] | dict[str, int]]:
        stats = self.history.compute_caption_experiment_stats(
            state,
            self.config.caption_experiments.min_data_points,
            content_type=content_type,
        )
        if not low_engagement_mode:
            return stats

        boosted = {key: dict(value) for key, value in stats.items() if isinstance(value, dict)}
        for bucket_key in ("hook_weights", "cta_weights"):
            bucket = boosted.get(bucket_key, {})
            if not bucket:
                continue
            winner = max(bucket.items(), key=lambda item: item[1], default=None)
            if winner:
                bucket[winner[0]] = round(float(winner[1]) * 1.12, 3)
        return boosted

    def _maybe_process_engagement_actions(self, state: dict[str, Any], entry: dict[str, Any], score: int, average_score: float):
        post_id = str(entry.get("post_id") or "")
        if not post_id or post_id == "dry-run":
            return
        cfg = self.config.engagement
        low_threshold = cfg.low_engagement_threshold
        high_threshold = cfg.high_engagement_threshold
        unusual_cutoff = average_score * cfg.unusual_spike_multiplier if average_score > 0 else 0

        if score <= low_threshold:
            self.history.add_engagement_alert(state, "low", post_id, f"Post unter Erwartung mit Score {score}.", score)
            if cfg.recycle_low_performers:
                self.history.queue_recycle_candidate(
                    state,
                    entry,
                    cfg.recycle_formats,
                    datetime.now() + timedelta(hours=cfg.recycle_after_hours),
                )
        elif score >= max(high_threshold, int(unusual_cutoff) if unusual_cutoff else high_threshold):
            self.history.add_engagement_alert(state, "high", post_id, f"Post performt stark mit Score {score}.", score)

        if not cfg.followup_comments_enabled or self.history.has_followup_comment(state, post_id):
            return

        templates = cfg.low_followup_templates if score <= low_threshold else cfg.high_followup_templates if score >= high_threshold else []
        if not templates:
            return
        text = random.choice(templates)
        result = self.facebook_poster.post_comment(post_id, text)
        if result.success:
            self.history.record_followup_comment(state, post_id, text, "low" if score <= low_threshold else "high")
            log.info("Engagement-Follow-up-Kommentar gepostet fuer %s", post_id)

    def get_runtime_slots(self, post_slots: list[str] | None = None) -> list[str]:
        runtime_slots = list(post_slots or self.config.posting_slots)
        if self.config.stories.enabled:
            runtime_slots.extend(self.config.stories.eligible_slots)
        return sorted(set(runtime_slots))

    def get_feed_slots(self, post_slots: list[str] | None = None) -> list[str]:
        feed_slots = list(post_slots or self.config.posting_slots)
        if not self.config.stories.enabled:
            return sorted(set(feed_slots))

        story_slots = set(self.config.stories.eligible_slots)
        filtered_slots = [slot for slot in feed_slots if slot not in story_slots]
        removed_slots = sorted(set(feed_slots) - set(filtered_slots))
        if removed_slots:
            log.warning("Diese Slots sind fuer Stories reserviert und werden aus Bild-/Reel-Slots entfernt: %s", ", ".join(removed_slots))
        return sorted(set(filtered_slots))

    def _try_story_card(self, state: dict, day_key: str, slot: str) -> bool:
        settings = self.config.stories
        if not settings.enabled:
            return False
        if slot not in settings.eligible_slots:
            return False
        if self.history.count_story_posts_for_day(state, day_key) >= settings.max_per_day:
            return False
        if settings.chance_per_slot < 1.0 and random.random() > max(settings.chance_per_slot, 0.0):
            return False

        now = datetime.now()
        time_theme = self._story_theme_for_slot(slot, now)
        images = self._list_available_images()
        campaign_context = self._resolve_campaign_context(state, images, now)
        story_theme = str(campaign_context.get("theme") or "").strip() or time_theme
        variant = self._story_sequence_variant(state, day_key)
        base_text = self.history.choose_story_text(state, settings.texts, time_theme)
        text = self._build_story_sequence_text(story_theme, base_text or "", variant)
        if not text:
            return False

        card_result = self.story_generator.generate_story_card(
            text,
            story_theme,
            background_image=self._pick_story_background_image(images, str(campaign_context.get("theme") or "").strip() or None),
            variant=variant,
        )
        post_id, platform_results, published = self._publish_story_targets(card_result.output_path)
        if not published:
            if settings.publish_to_facebook or self.config.instagram.publish_stories:
                log.warning("Story-Karte konnte auf keiner Plattform veroefentlicht werden.")
                return False
            log.info("Story-Karte erzeugt, aber nicht veroefentlicht: %s", card_result.output_path)

        self.history.record_story_success(
            state,
            slot=slot,
            text=text,
            story_path=str(card_result.output_path),
            post_id=post_id,
            theme=story_theme,
            platform_results=platform_results,
        )
        self.history.prune_generated_stories(state, settings.output_folder, keep_last=60)
        self.history.save(state)
        log.info("Story-Karte aktiv fuer Slot %s: %s (%s)", slot, card_result.output_path.name, variant)
        log.info("Story-Text:\n%s", text)
        return True

    def _process_story_only_slot(self, state: dict, day_key: str, slot: str):
        if self.history.count_successful_posts_for_day(state, day_key) >= self.config.max_posts_per_day:
            log.info("Maximale Anzahl an Posts fuer heute erreicht. Story-Slot %s wird uebersprungen.", slot)
            self.history.mark_slot_run(
                state,
                day_key,
                slot,
                status="skipped",
                message="Maximale Tagesanzahl erreicht.",
                content_type="story_card",
            )
            self.history.save(state)
            return

        if self._consume_due_recycle_story(state, day_key, slot):
            return

        if self._try_story_card(state, day_key, slot):
            return

        log.info("Kein Story-Post fuer Story-Slot %s ausgefuehrt.", slot)
        self.history.mark_slot_run(
            state,
            day_key,
            slot,
            status="skipped",
            message="Kein Story-Post fuer diesen Story-Slot ausgefuehrt.",
            content_type="story_card",
        )
        self.history.save(state)

    def _disable_comment_features(self, state: dict | None, post_ids: list[str], reason: str | None):
        resolved_reason = reason or "Facebook-Kommentare sind fuer dieses Token derzeit nicht verfuegbar."
        if self._comment_api_unavailable_reason == resolved_reason:
            return

        self._comment_api_unavailable_reason = resolved_reason
        log.warning("Facebook-Kommentare deaktiviert: %s", resolved_reason)

        if state is None:
            return

        changed = False
        for post_id in post_ids:
            if not post_id:
                continue
            self.history.mark_auto_comment_blocked(state, post_id, resolved_reason)
            changed = True
        if changed:
            self.history.save(state)

    def _simulate_reel_publish(self, reel_path: Path, slot: str, source_images: list[str]) -> tuple[str, str]:
        if self.config.reels.simulation_mode:
            return (
                "simulated",
                f"Reel-Testlauf fuer Slot {slot}: {reel_path.name} mit {len(source_images)} Bildern simuliert, kein externer Upload.",
            )
        return ("created", "Reel erzeugt und lokal gespeichert. Kein externer Upload konfiguriert.")

    def _platform_result(
        self,
        *,
        attempted: bool,
        success: bool,
        post_id: str | None = None,
        message: str = "",
        skipped: bool = False,
    ) -> dict[str, Any]:
        return {
            "attempted": attempted,
            "success": success,
            "post_id": post_id or "",
            "message": message,
            "skipped": skipped,
        }

    def _publish_story_targets(self, story_path: Path) -> tuple[str, dict[str, Any], bool]:
        platform_results = {
            "facebook": self._platform_result(attempted=False, success=False, skipped=True, message="Facebook-Story-Upload deaktiviert."),
            "instagram": self._platform_result(attempted=False, success=False, skipped=True, message="Instagram-Story-Upload deaktiviert."),
        }

        if self.config.stories.publish_to_facebook:
            result = self.facebook_poster.post_story_photo(story_path)
            platform_results["facebook"] = self._platform_result(
                attempted=True,
                success=result.success,
                post_id=result.post_id,
                message=result.error or ("Facebook-Story erfolgreich veroefentlicht." if result.success else "Facebook-Story fehlgeschlagen."),
            )
            if result.success:
                log.info("Facebook-Story erfolgreich veroefentlicht (Post-ID: %s)", result.post_id)
            else:
                log.warning("Facebook-Story fehlgeschlagen: %s", result.error)

        if self.config.instagram.enabled and self.config.instagram.publish_stories:
            result = self.instagram_poster.post_story_image(story_path)
            platform_results["instagram"] = self._platform_result(
                attempted=not result.skipped,
                success=result.success,
                post_id=result.media_id,
                skipped=result.skipped,
                message=result.error or ("Instagram-Story erfolgreich veroefentlicht." if result.success else "Instagram-Story uebersprungen." if result.skipped else "Instagram-Story fehlgeschlagen."),
            )
            if result.success:
                log.info("Instagram-Story erfolgreich veroefentlicht (Media-ID: %s)", result.media_id)
            elif not result.skipped:
                log.warning("Instagram-Story fehlgeschlagen: %s", result.error)

        primary_post_id = (
            platform_results["facebook"].get("post_id")
            or platform_results["instagram"].get("post_id")
            or ""
        )
        published = bool(platform_results["facebook"].get("success") or platform_results["instagram"].get("success"))
        local_only = not platform_results["facebook"].get("attempted") and not platform_results["instagram"].get("attempted")
        return primary_post_id, platform_results, (published or local_only)

    def _publish_feed_targets(self, image_path: Path, caption: str) -> tuple[bool, str | None, dict[str, Any], str]:
        platform_results = {
            "facebook": self._platform_result(attempted=True, success=False, message="Facebook-Posting noch nicht ausgefuehrt."),
            "instagram": self._platform_result(attempted=False, success=False, skipped=True, message="Instagram-Posting deaktiviert."),
        }

        fb_result = self.facebook_poster.post_photo(image_path, caption)
        platform_results["facebook"] = self._platform_result(
            attempted=True,
            success=fb_result.success,
            post_id=fb_result.post_id,
            message=fb_result.error or ("Facebook-Posting erfolgreich." if fb_result.success else "Facebook-Posting fehlgeschlagen."),
        )
        if not fb_result.success:
            return False, fb_result.error, platform_results, ""

        if self.config.instagram.enabled and self.config.instagram.publish_posts:
            ig_result = self.instagram_poster.post_image(image_path, caption)
            platform_results["instagram"] = self._platform_result(
                attempted=not ig_result.skipped,
                success=ig_result.success,
                post_id=ig_result.media_id,
                skipped=ig_result.skipped,
                message=ig_result.error or ("Instagram-Posting erfolgreich." if ig_result.success else "Instagram-Posting uebersprungen." if ig_result.skipped else "Instagram-Posting fehlgeschlagen."),
            )
            if ig_result.success:
                log.info("Instagram-Posting erfolgreich: Media-ID %s", ig_result.media_id)
            elif not ig_result.skipped:
                log.warning("Instagram-Posting fehlgeschlagen: %s", ig_result.error)

        return True, None, platform_results, fb_result.post_id or ""

    def _should_move_image_after_feed_publish(self, platform_results: dict[str, Any]) -> bool:
        instagram_enabled_for_posts = bool(self.config.instagram.enabled and self.config.instagram.publish_posts)
        if not instagram_enabled_for_posts:
            return True

        instagram_result = platform_results.get("instagram") or {}
        if instagram_result.get("skipped"):
            return True

        return bool(instagram_result.get("success"))

    def _publish_reel(self, reel_path: Path, slot: str, source_images: list[str], caption: str) -> tuple[str, str, str | None, dict[str, Any]]:
        platform_results = {
            "facebook": self._platform_result(attempted=False, success=False, skipped=True, message="Facebook-Reel-Upload deaktiviert."),
            "instagram": self._platform_result(attempted=False, success=False, skipped=True, message="Instagram-Reel-Upload deaktiviert."),
        }

        if self.config.reels.simulation_mode:
            return (
                "simulated",
                f"Reel-Testlauf fuer Slot {slot}: {reel_path.name} mit {len(source_images)} Bildern simuliert, kein externer Upload.",
                None,
                platform_results,
            )

        messages: list[str] = []
        facebook_post_id: str | None = None

        if self.config.platform == "facebook" and self.config.reels.publish_to_facebook:
            result = self.facebook_poster.post_reel(reel_path, caption)
            platform_results["facebook"] = self._platform_result(
                attempted=True,
                success=result.success,
                post_id=result.reel_id,
                message=result.error or ("Facebook-Reel erfolgreich veroefentlicht." if result.success else "Facebook-Reel fehlgeschlagen."),
            )
            if result.success:
                facebook_post_id = result.reel_id
                messages.append(f"Facebook-Reel veroeffentlicht (Reel-ID: {result.reel_id}).")
            else:
                messages.append(f"Facebook-Reel-Upload fehlgeschlagen: {result.error or 'Unbekannter Fehler'}")

        if self.config.instagram.enabled and self.config.instagram.publish_reels:
            result = self.instagram_poster.post_reel(reel_path, caption)
            platform_results["instagram"] = self._platform_result(
                attempted=not result.skipped,
                success=result.success,
                post_id=result.media_id,
                skipped=result.skipped,
                message=result.error or ("Instagram-Reel erfolgreich veroefentlicht." if result.success else "Instagram-Reel uebersprungen." if result.skipped else "Instagram-Reel fehlgeschlagen."),
            )
            if result.success:
                messages.append(f"Instagram-Reel veroeffentlicht (Media-ID: {result.media_id}).")
            elif not result.skipped:
                messages.append(f"Instagram-Reel-Upload fehlgeschlagen: {result.error or 'Unbekannter Fehler'}")

        if platform_results["facebook"].get("success") or platform_results["instagram"].get("success"):
            return ("published", " ".join(messages) or "Reel veroeffentlicht.", facebook_post_id, platform_results)
        if platform_results["facebook"].get("attempted") or platform_results["instagram"].get("attempted"):
            return ("failed", " ".join(messages) or "Reel-Upload fehlgeschlagen.", facebook_post_id, platform_results)
        return ("created", "Reel erzeugt und lokal gespeichert. Kein externer Upload konfiguriert.", facebook_post_id, platform_results)

    def _get_reel_control(self, state: dict) -> dict:
        control = state.setdefault("reel_control", {})
        control.setdefault("queue_override", [])
        control.setdefault("caption_override", "")
        control.setdefault("skip_anchors", [])
        control.setdefault("preview_path", None)
        control.setdefault("preview_updated_at", None)
        control.setdefault("planned_source_images", [])
        control.setdefault("planned_anchor_image", None)
        control.setdefault("planned_updated_at", None)
        control.setdefault("planned_caption", "")
        control.setdefault("planned_caption_source", None)
        control.setdefault("planned_caption_updated_at", None)
        return control

    def _clear_reel_plan(self, control: dict):
        control["planned_source_images"] = []
        control["planned_anchor_image"] = None
        control["planned_updated_at"] = None
        control["planned_caption"] = ""
        control["planned_caption_source"] = None
        control["planned_caption_updated_at"] = None

    def _build_reel_caption(
        self,
        state: dict,
        control: dict,
        reel_images: list[Path],
        fallback_caption: str,
        feature_weights: dict | None = None,
        experiment_stats: dict[str, dict[str, float] | dict[str, int]] | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        caption_override = str(control.get("caption_override") or "").strip()
        if caption_override:
            return caption_override, "manual", {"hook_style": "manual", "cta_style": "manual"}

        planned_names = [image.name for image in reel_images]
        cached_names = [str(name) for name in control.get("planned_source_images", []) if str(name)]
        cached_caption = str(control.get("planned_caption") or "").strip()
        if planned_names and cached_names == planned_names and cached_caption:
            return cached_caption, str(control.get("planned_caption_source") or "cached"), {
                "hook_style": "cached",
                "cta_style": "cached",
            }

        try:
            bundle = self.caption_generator.generate_for_reel(
                reel_images,
                feature_weights=feature_weights,
                experiment_stats=experiment_stats,
            )
            control["planned_caption"] = bundle.selected
            control["planned_caption_source"] = bundle.source
            control["planned_caption_updated_at"] = datetime.now().isoformat()
            return bundle.selected, bundle.source, bundle.selected_metadata
        except Exception as exc:
            log.exception("Reel-Caption-Generierung fehlgeschlagen: %s", exc)
            return fallback_caption, "fallback", {"hook_style": "fallback", "cta_style": "fallback"}

    def _build_reel_plan(self, state: dict, images: list[Path], anchor_image: Path, preferred_theme: str | None = None, content_mode: str = "theme") -> list[Path]:
        control = self._get_reel_control(state)
        reel_images = self.history.plan_reel_images(
            state=state,
            images=images,
            selection_mode=self.config.selection_mode,
            count=self.config.reels.images_per_reel,
            anchor_image=anchor_image,
            queue_override=[str(name) for name in control.get("queue_override", [])],
            skip_anchors={str(name) for name in control.get("skip_anchors", [])},
            anchor_cooldown_reels=self.config.reels.anchor_cooldown_reels,
            duplicate_window_reels=self.config.reels.duplicate_window_reels,
            prefer_next_anchor=False,
            preferred_theme=preferred_theme if content_mode == "theme" else None,
            theme_separator=self.config.campaigns.theme_separator,
        )
        if reel_images or not self.config.campaigns.enabled or content_mode != "theme":
            return reel_images

        filtered_images = [
            image
            for image in images
            if self.history.infer_image_theme(image.name, self.config.campaigns.theme_separator)
        ]
        return self.history.plan_reel_images(
            state=state,
            images=filtered_images,
            selection_mode=self.config.selection_mode,
            count=self.config.reels.images_per_reel,
            anchor_image=anchor_image if anchor_image in filtered_images else None,
            queue_override=[str(name) for name in control.get("queue_override", [])],
            skip_anchors={str(name) for name in control.get("skip_anchors", [])},
            anchor_cooldown_reels=self.config.reels.anchor_cooldown_reels,
            duplicate_window_reels=self.config.reels.duplicate_window_reels,
            prefer_next_anchor=False,
            preferred_theme=None,
            theme_separator=self.config.campaigns.theme_separator,
        )

    def _consume_reel_control(self, state: dict, anchor_image_name: str):
        control = self._get_reel_control(state)
        control["skip_anchors"] = [name for name in control.get("skip_anchors", []) if name != anchor_image_name]
        self._clear_reel_plan(control)
        override_names = [str(name) for name in control.get("queue_override", [])]
        if anchor_image_name in override_names or not override_names:
            control["queue_override"] = []
            control["caption_override"] = ""
            preview_path = control.get("preview_path")
            if preview_path:
                try:
                    Path(preview_path).unlink(missing_ok=True)
                except Exception:
                    pass
            control["preview_path"] = None
            control["preview_updated_at"] = None

    def process_slot(self, slot: str):
        now = datetime.now()
        day_key = now.date().isoformat()
        state = self.history.load()
        post_slots = set(self.config.posting_slots)
        story_slots = set(self.config.stories.eligible_slots if self.config.stories.enabled else [])
        is_post_slot = slot in post_slots
        is_story_slot = slot in story_slots

        if self.history.was_slot_processed(state, day_key, slot):
            log.info("Slot %s wurde heute bereits verarbeitet und wird uebersprungen.", slot)
            return

        if is_story_slot and not is_post_slot:
            self._process_story_only_slot(state, day_key, slot)
            return

        if not is_post_slot:
            log.info("Slot %s ist weder als Bild-/Reel-Slot noch als Story-Slot aktiv und wird uebersprungen.", slot)
            self.history.mark_slot_run(state, day_key, slot, status="skipped", message="Slot nicht aktiv.")
            self.history.save(state)
            return

        if self.history.count_feed_posts_for_day(state, day_key) >= self.config.max_posts_per_day:
            log.info("Maximale Anzahl an Posts fuer heute erreicht. Slot %s wird uebersprungen.", slot)
            self.history.mark_slot_run(state, day_key, slot, status="skipped", message="Maximale Tagesanzahl erreicht.")
            self.history.save(state)
            return

        images = self._list_available_images()
        self.history.sync_image_registry(state, images)
        campaign_context = self._resolve_campaign_context(state, images, now)
        quality_scores, disallowed_names, diagnostics = self._prepare_image_inventory(
            state,
            images,
            str(campaign_context.get("theme") or "").strip() or None,
        )
        feature_weights = self.history.compute_caption_feature_weights(state)
        trend = self.history.get_recent_engagement_trend(state, self.config.engagement.low_engagement_last_n) if self.config.engagement.enabled else None
        low_engagement_mode = bool(
            self.config.engagement.enabled
            and trend is not None
            and trend < self.config.engagement.low_engagement_threshold
        )
        experiment_stats = self._experiment_stats_for_content(state, "image", low_engagement_mode)
        if feature_weights:
            log.info("Caption-Lerngewichte aktiv: %s", {k: f"{v:.2f}" for k, v in feature_weights.items()})
        if self.config.caption_experiments.enabled:
            log.info(
                "Caption-A/B-Test aktiv: Hooks %s | CTAs %s",
                experiment_stats.get("hook_weights", {}),
                experiment_stats.get("cta_weights", {}),
            )
        if diagnostics:
            log.info("Content-Qualitaet aktiv: %d Bilder gefiltert.", len(diagnostics))
        if campaign_context:
            log.info(
                "Aktive Kampagne: %s | Thema: %s | Modus: %s",
                campaign_context.get("campaign_name"),
                campaign_context.get("theme"),
                campaign_context.get("mode", "theme"),
            )

        if self.config.engagement.enabled:
            if trend is not None:
                threshold = self.config.engagement.low_engagement_threshold
                if trend < threshold:
                    log.warning(
                        "Niedriges Engagement erkannt: Durchschnitt %.1f (letzte %d Posts, Schwellenwert: %.1f).",
                        trend, self.config.engagement.low_engagement_last_n, threshold,
                    )
                else:
                    log.info("Engagement-Trend: Durchschnitt %.1f (letzte %d Posts mit Daten).", trend, self.config.engagement.low_engagement_last_n)

        if not images:
            log.warning("Keine Bilder verfuegbar. Slot %s wird uebersprungen.", slot)
            self._update_next_image_with_fallback(
                state,
                images,
                str(campaign_context.get("theme") or "").strip() or None,
                quality_scores,
                disallowed_names,
                content_mode=str(campaign_context.get("mode") or "theme"),
            )
            self.history.mark_slot_run(state, day_key, slot, status="skipped", message="Keine Bilder verfuegbar.")
            self.history.save(state)
            return

        pinned_name = self.history.get_pinned_next_image(state)
        if pinned_name:
            pinned_image = next((candidate for candidate in images if candidate.name == pinned_name and candidate.name not in disallowed_names), None)
        else:
            pinned_image = None
        image, image_selection_mode = self._choose_feed_image(
            state,
            images,
            str(campaign_context.get("theme") or "").strip() or None,
            quality_scores,
            disallowed_names,
            content_mode=str(campaign_context.get("mode") or "theme"),
        )
        if pinned_image is not None:
            image = pinned_image
            image_selection_mode = "pinned"
        if image is None:
            if self.config.loop and images:
                log.info("Alle Bilder wurden gepostet – Posting-Zyklus wird zurueckgesetzt.")
                self.history.reset_cycle(state, images, self.config.selection_mode)
                image, image_selection_mode = self._choose_feed_image(
                    state,
                    images,
                    str(campaign_context.get("theme") or "").strip() or None,
                    quality_scores,
                    disallowed_names,
                    content_mode=str(campaign_context.get("mode") or "theme"),
                )

        if image is None:
            log.info("Keine unverwendeten Bilder mehr verfuegbar. Slot %s wird uebersprungen.", slot)
            self._update_next_image_with_fallback(
                state,
                images,
                str(campaign_context.get("theme") or "").strip() or None,
                quality_scores,
                disallowed_names,
                content_mode=str(campaign_context.get("mode") or "theme"),
            )
            self.history.mark_slot_run(state, day_key, slot, status="skipped", message="Keine unverwendeten Bilder verfuegbar.")
            self.history.save(state)
            return

        log.info("Gewaehlter Slot: %s", slot)
        log.info("Gewaehltes Bild: %s", image.name)
        if image_selection_mode == "theme_fallback":
            log.info(
                "Kein passendes Bild mehr fuer Thema '%s'. Fallback auf anderes Themenbild: %s",
                campaign_context.get("theme"),
                image.name,
            )

        caption_bundle = self.caption_generator.generate_for_image(
            image,
            feature_weights=feature_weights,
            experiment_stats=experiment_stats,
        )
        self.history.store_generated_captions(
            state,
            image_name=image.name,
            variants=caption_bundle.variants,
            selected=caption_bundle.selected,
            description=caption_bundle.description,
            variant_metadata=caption_bundle.variant_metadata,
            selected_metadata=caption_bundle.selected_metadata,
        )
        log.info("Caption-Quelle: %s", caption_bundle.source)
        log.info("Caption-Stil: %s", caption_bundle.selected_metadata)
        log.info("Generierter Text:\n%s", caption_bundle.selected)

        if self.config.reels.enabled:
            try:
                control = self._get_reel_control(state)
                recycle_anchor = self._due_recycle_reel_anchor(state, images)
                anchor_image = recycle_anchor or image
                reel_images = self._build_reel_plan(
                    state,
                    images,
                    anchor_image,
                    preferred_theme=campaign_context.get("theme"),
                    content_mode=str(campaign_context.get("mode") or "theme"),
                )
                if reel_images:
                    reel_experiment_stats = self._experiment_stats_for_content(state, "reel", low_engagement_mode)
                    reel_caption, reel_caption_source, reel_caption_metadata = self._build_reel_caption(
                        state,
                        control,
                        reel_images,
                        caption_bundle.selected,
                        feature_weights=feature_weights,
                        experiment_stats=reel_experiment_stats,
                    )
                    reel_result = self.reel_generator.generate_reel(reel_images, reel_caption)
                    reel_publish_status, reel_publish_message, reel_post_id, reel_platform_results = self._publish_reel(
                        reel_result.output_path,
                        slot,
                        reel_result.source_images,
                        reel_caption,
                    )
                    self.history.store_generated_reel(
                        state,
                        image_name=image.name,
                        source_images=reel_result.source_images,
                        reel_path=str(reel_result.output_path),
                        duration_seconds=reel_result.duration_seconds,
                        frame_count=reel_result.frame_count,
                        slot=slot,
                        caption=reel_caption,
                        audio_source=reel_result.audio_source,
                        audio_track=reel_result.audio_track,
                        simulation_mode=self.config.reels.simulation_mode,
                        publish_status=reel_publish_status,
                        publish_message=reel_publish_message,
                        published_post_id=reel_post_id,
                        platform_results=reel_platform_results,
                        campaign=campaign_context,
                        caption_metadata=reel_caption_metadata,
                    )
                    if recycle_anchor is not None:
                        original_post_id = next(
                            (
                                str(item.get("post_id") or "")
                                for item in self.history.get_due_recycle_candidates(state)
                                if str(item.get("file") or "") == recycle_anchor.name and "reel" in [str(fmt).lower() for fmt in item.get("formats", [])]
                            ),
                            "",
                        )
                        if original_post_id:
                            self.history.mark_recycle_candidate_used(state, original_post_id, "reel")
                    self.history.prune_generated_reels(state, self.config.reels.output_folder, keep_last=20)
                    self.history.save(state)
                    log.info(
                        "Reel erzeugt: %s | Quellen: %s | Audio: %s%s | Status: %s",
                        reel_result.output_path,
                        ", ".join(reel_result.source_images),
                        reel_result.audio_source,
                        f" ({reel_result.audio_track})" if reel_result.audio_track else "",
                        reel_publish_status,
                    )
                    log.info("Reel-Caption-Quelle: %s", reel_caption_source)
                    log.info("Reel-Caption-Stil: %s", reel_caption_metadata)
                    log.info("Reel-Text:\n%s", reel_caption)
                    log.info(reel_publish_message)
                    if reel_post_id and reel_post_id != "dry-run":
                        self._post_auto_comment(
                            reel_post_id,
                            state=state,
                            post_entry={
                                "file": image.name,
                                "slot": slot,
                                "caption": reel_caption,
                                "post_id": reel_post_id,
                                "content_type": "reel",
                                "description": " ".join(reel_result.source_images),
                            },
                            delay_override=0,
                        )
                else:
                    log.info("Naechstes Reel fuer Slot %s wurde manuell uebersprungen.", slot)
                self._consume_reel_control(state, image.name)
            except Exception as exc:
                log.exception("Reel-Erzeugung fehlgeschlagen: %s", exc)

        publish_success, publish_error, platform_results, primary_post_id = self._publish_feed_targets(image, caption_bundle.selected)
        if not publish_success:
            log.error("Facebook-Posting fehlgeschlagen: %s", publish_error)
            self.history.mark_slot_run(
                state,
                day_key,
                slot,
                status="failed",
                message=publish_error or "Facebook-Posting fehlgeschlagen.",
                image_name=image.name,
                caption=caption_bundle.selected,
                platform_results=platform_results,
            )
            self._update_next_image_with_fallback(
                state,
                images,
                str(campaign_context.get("theme") or "").strip() or None,
                quality_scores,
                disallowed_names,
            )
            self.history.save(state)
            return

        log.info("Facebook-Posting erfolgreich: %s (Post-ID: %s)", image.name, primary_post_id)

        images_after_post = list(images)
        if self.config.delete_after_post and not self.config.dry_run and self._should_move_image_after_feed_publish(platform_results):
            moved_path = self._move_to_sent_folder(image)
            images_after_post = [current for current in images if current.name != image.name]
            log.info("Bild verschoben nach 'versendet': %s", moved_path.name)
        elif self.config.delete_after_post and not self.config.dry_run and self.config.instagram.enabled and self.config.instagram.publish_posts:
            log.warning(
                "Bild bleibt vorerst im Quellordner, weil Instagram-Bildposting nicht erfolgreich war: %s",
                image.name,
            )

        self._track_follower_count(state)
        self.history.record_post_success(
            state,
            image=image,
            slot=slot,
            caption=caption_bundle.selected,
            post_id=primary_post_id,
            images_after_post=images_after_post,
            selection_mode=self.config.selection_mode,
            platform_results=platform_results,
            campaign=campaign_context,
            caption_metadata=caption_bundle.selected_metadata,
            theme_separator=self.config.campaigns.theme_separator,
        )
        self.history.clear_pinned_next_image(state, image.name)
        self.history.save(state)

        if primary_post_id and primary_post_id != "dry-run":
            self._post_auto_comment(primary_post_id, state=state)

    def prepare_runtime_state(self):
        state = self.history.load()
        images = self._list_available_images()
        self.history.sync_image_registry(state, images)
        campaign_context = self._resolve_campaign_context(state, images, datetime.now())
        quality_scores, disallowed_names, _ = self._prepare_image_inventory(
            state,
            images,
            str(campaign_context.get("theme") or "").strip() or None,
        )
        self._update_next_image_with_fallback(
            state,
            images,
            str(campaign_context.get("theme") or "").strip() or None,
            quality_scores,
            disallowed_names,
            content_mode=str(campaign_context.get("mode") or "theme"),
        )
        self.history.save(state)

    def check_pending_engagement(self):
        if not self.config.engagement.enabled:
            return
        state = self.history.load()
        pending = self.history.get_posts_needing_engagement_check(state, self.config.engagement.delay_hours)
        if not pending:
            return
        log.info("Pruefe Engagement fuer %d ausstehende Posts ...", len(pending))
        changed = False
        for entry in pending:
            post_id = entry["post_id"]
            engagement = self.facebook_poster.fetch_engagement(post_id)
            if engagement:
                self.history.store_engagement(state, post_id, engagement)
                likes = (engagement.get("likes") or {}).get("summary", {}).get("total_count", 0)
                comments = (engagement.get("comments") or {}).get("summary", {}).get("total_count", 0)
                shares = (engagement.get("shares") or {}).get("count", 0)
                score = likes + comments * 3 + shares * 5
                baseline = self.history.get_recent_engagement_trend(state, self.config.engagement.low_engagement_last_n) or 0.0
                self._maybe_process_engagement_actions(state, entry, score, baseline)
                log.info("Engagement Post %s: %d Likes, %d Kommentare, %d Shares", post_id, likes, comments, shares)
                changed = True
        if changed:
            self.history.save(state)

    def _post_auto_comment(
        self,
        post_id: str,
        state: dict | None = None,
        post_entry: dict[str, Any] | None = None,
        delay_override: int | None = None,
    ):
        cfg = self.config.auto_comment
        if not cfg.enabled or ((not cfg.templates) and (not cfg.ollama_enabled)) or self._comment_api_unavailable_reason:
            return
        delay_seconds = cfg.delay_seconds if delay_override is None else max(delay_override, 0)
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        if post_entry is None:
            post_entry = self._get_post_entry(state, post_id)
        text, source, meta = self.auto_comment_generator.get_comment(state=state, post_entry=post_entry)
        result = self.facebook_poster.post_comment(post_id, text)
        if result.success:
            log.info("Auto-Kommentar gepostet unter Post %s (Quelle: %s)", post_id, source)
            if state is not None:
                self.history.record_auto_comment_attempt(
                    state,
                    post_id=post_id,
                    text=text,
                    source=source,
                    content_type=str(meta.get("content_type") or "image"),
                    status="posted",
                )
                self.history.mark_auto_commented(state, post_id)
                self.history.save(state)
        elif result.permanent:
            if state is not None:
                self.history.record_auto_comment_attempt(
                    state,
                    post_id=post_id,
                    text=text,
                    source=source,
                    content_type=str(meta.get("content_type") or "image"),
                    status="blocked",
                    filter_reason=result.error,
                )
            self._disable_comment_features(state, [post_id], result.error)
        else:
            if state is not None:
                self.history.record_auto_comment_attempt(
                    state,
                    post_id=post_id,
                    text=text,
                    source=source,
                    content_type=str(meta.get("content_type") or "image"),
                    status="failed",
                    filter_reason=result.error,
                )
                self.history.save(state)
            log.warning("Auto-Kommentar fehlgeschlagen fuer Post %s: %s", post_id, result.error or "Unbekannter Fehler")

    def check_and_comment_old_posts(self):
        cfg = self.config.auto_comment
        if not cfg.enabled or not cfg.retroactive or ((not cfg.templates) and (not cfg.ollama_enabled)) or self._comment_api_unavailable_reason:
            return
        state = self.history.load()
        pending = self.history.get_posts_needing_auto_comment(state, cfg.retroactive_max_age_days)
        if not pending:
            return
        log.info("Retroaktive Kommentierung: %d Posts ohne Auto-Kommentar gefunden.", len(pending))
        for index, entry in enumerate(pending):
            post_id = entry["post_id"]
            if not entry.get("description"):
                file_name = str(entry.get("file") or "").strip()
                if file_name:
                    entry["description"] = str(((state.get("captions") or {}).get(file_name) or {}).get("description") or "")
            text, source, meta = self.auto_comment_generator.get_comment(state=state, post_entry=entry)
            result = self.facebook_poster.post_comment(post_id, text)
            if result.success:
                log.info("Retroaktiver Kommentar gepostet unter Post %s (%s, Quelle: %s)", post_id, entry.get("file", ""), source)
                self.history.record_auto_comment_attempt(
                    state,
                    post_id=post_id,
                    text=text,
                    source=source,
                    content_type=str(meta.get("content_type") or entry.get("content_type") or "image"),
                    status="posted",
                )
                self.history.mark_auto_commented(state, post_id)
                self.history.save(state)
            elif result.permanent:
                self.history.record_auto_comment_attempt(
                    state,
                    post_id=post_id,
                    text=text,
                    source=source,
                    content_type=str(meta.get("content_type") or entry.get("content_type") or "image"),
                    status="blocked",
                    filter_reason=result.error,
                )
                remaining_post_ids = [
                    str(candidate.get("post_id") or "")
                    for candidate in pending[index:]
                ]
                self._disable_comment_features(state, remaining_post_ids, result.error)
                return
            else:
                self.history.record_auto_comment_attempt(
                    state,
                    post_id=post_id,
                    text=text,
                    source=source,
                    content_type=str(meta.get("content_type") or entry.get("content_type") or "image"),
                    status="failed",
                    filter_reason=result.error,
                )
                self.history.save(state)
                log.warning("Retroaktiver Kommentar fehlgeschlagen fuer Post %s: %s", post_id, result.error or "Unbekannter Fehler")

    def _track_follower_count(self, state: dict):
        if not self.config.follower_tracking.enabled:
            return
        count = self.facebook_poster.fetch_follower_count()
        if count is not None:
            self.history.record_follower_count(state, count)
            growth = self.history.get_weekly_growth(state)
            if growth is not None:
                log.info("Follower: %d (Wachstum letzte 7 Tage: %+.0f)", count, growth)
            else:
                log.info("Follower: %d", count)

    def check_and_respond_to_comments(self):
        if not self.config.comment_response.enabled or self._comment_api_unavailable_reason:
            return
        cfg = self.config.comment_response
        state = self.history.load()
        posts = self.history.get_posts_for_comment_response(state, cfg.lookback_days)
        if not posts:
            return
        log.info("Pruefe Kommentare auf %d Posts ...", len(posts))
        changed = False
        for entry in posts:
            post_id = entry["post_id"]
            replied_ids = self.history.get_replied_comment_ids(state, post_id)
            comments = self.facebook_poster.fetch_unanswered_comments(
                post_id, replied_ids, cfg.max_responses_per_post
            )
            for comment in comments:
                comment_id = comment["id"]
                text = random.choice(cfg.templates)
                result = self.facebook_poster.reply_to_comment(comment_id, text)
                if result.success:
                    self.history.mark_comment_replied(state, post_id, comment_id)
                    log.info("Auf Kommentar %s geantwortet", comment_id)
                    changed = True
                elif result.permanent:
                    self._disable_comment_features(None, [], result.error)
                    return
                else:
                    log.warning("Kommentar-Antwort fehlgeschlagen fuer Kommentar %s: %s", comment_id, result.error or "Unbekannter Fehler")
        if changed:
            self.history.save(state)

    def apply_smart_slots(self) -> list[str]:
        if not self.config.smart_slots.enabled:
            return self.config.posting_slots
        cfg = self.config.smart_slots
        state = self.history.load()
        now = datetime.now()
        campaign_blueprint = self._get_campaign_blueprint(state, now)
        preferred_slots = [slot for slot in campaign_blueprint.get("preferred_slots", []) if slot]
        base_slots = preferred_slots + [slot for slot in self.config.posting_slots if slot not in preferred_slots]

        if cfg.prefer_historical:
            historical_slots, slot_sources = self.history.compute_best_slots(
                state,
                cfg.top_slots_count,
                cfg.min_data_points,
                weekday=now.weekday(),
                base_slots=base_slots,
                exploration_rate=cfg.exploration_rate,
            )
            if historical_slots:
                self.history.update_smart_slot_state(state, historical_slots, slot_sources)
                self.history.save(state)
                log.info("Historisch optimale Posting-Slots: %s", ", ".join(f"{slot} ({slot_sources.get(slot, 'historical')})" for slot in historical_slots))
                return historical_slots

        log.info("Lade beste Posting-Zeiten von Facebook Insights ...")
        api_slots = self.facebook_poster.fetch_best_posting_slots(cfg.top_slots_count)
        if api_slots:
            merged_slots = preferred_slots + [slot for slot in api_slots if slot not in preferred_slots]
            chosen_slots = sorted(merged_slots[: cfg.top_slots_count])
            self.history.update_smart_slot_state(
                state,
                chosen_slots,
                {slot: ("campaign-preferred" if slot in preferred_slots else "facebook-insights") for slot in chosen_slots},
            )
            self.history.save(state)
            log.info("API-optimale Posting-Slots ermittelt: %s", ", ".join(api_slots))
            return chosen_slots

        log.warning("Slot-Optimierung fehlgeschlagen, verwende konfigurierte Slots.")
        return self.config.posting_slots

    def _list_available_images(self) -> list[Path]:
        if not self.config.images_folder.exists():
            log.error("Bilderordner nicht gefunden: %s", self.config.images_folder)
            return []

        extensions = set(self.config.supported_extensions)
        return sorted(
            [
                image
                for image in self.config.images_folder.iterdir()
                if image.is_file() and image.suffix.lower() in extensions
            ],
            key=lambda item: item.name.lower(),
        )

    def _move_to_sent_folder(self, image: Path) -> Path:
        sent_folder = self.config.images_folder / "versendet"
        sent_folder.mkdir(exist_ok=True)
        destination = sent_folder / image.name
        counter = 1
        while destination.exists():
            destination = sent_folder / f"{image.stem}-{counter}{image.suffix}"
            counter += 1
        image.replace(destination)
        return destination


def acquire_single_instance_lock() -> bool:
    global _lock_handle, _mutex_handle

    if os.name == "nt":
        _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\SocialPosterSingleton")
        if not _mutex_handle:
            raise OSError("CreateMutexW fehlgeschlagen")
        if ctypes.windll.kernel32.GetLastError() == 183:
            return False
        return True

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    _lock_handle = open(LOCK_FILE, "a+", encoding="utf-8")

    try:
        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False

    _lock_handle.seek(0)
    _lock_handle.truncate()
    _lock_handle.write(str(os.getpid()))
    _lock_handle.flush()
    return True


def main():
    config = load_settings()
    setup_logging(config.log_file)

    if not acquire_single_instance_lock():
        log.warning("poster.py laeuft bereits. Zweite Instanz wird beendet.")
        return

    service = AutoPostingService(config)
    service.prepare_runtime_state()
    service.check_pending_engagement()
    service.check_and_respond_to_comments()
    service.check_and_comment_old_posts()
    active_slots = service.apply_smart_slots()
    feed_slots = service.get_feed_slots(active_slots)
    service.config.posting_slots = feed_slots
    runtime_slots = service.get_runtime_slots(feed_slots)

    log.info("=" * 60)
    log.info("AI-Influencer Auto-Poster gestartet")
    log.info("  Ordner        : %s", config.images_folder)
    log.info("  Post-Slots    : %s", ", ".join(feed_slots))
    log.info("  Story-Slots   : %s", ", ".join(config.stories.eligible_slots) if config.stories.enabled else "deaktiviert")
    log.info("  Scheduler     : %s", ", ".join(runtime_slots))
    log.info("  Max/Tag       : %s", config.max_posts_per_day)
    log.info("  Auswahl       : %s", config.selection_mode)
    log.info("  Caption       : %s", config.caption_provider)
    log.info("  Ollama Modell : %s", config.ollama.model)
    log.info("  Vision-Modell : %s", config.ollama.vision_model if config.ollama.vision_enabled else "deaktiviert")
    log.info("  Vision-Cache  : %s", config.ollama.vision_cache)
    log.info("  OpenAI Fallback: %s", config.openai.enabled)
    log.info("  Caption-Score : %s (min %d, max %dx Retry)", config.caption_scoring.enabled, config.caption_scoring.min_score, config.caption_scoring.max_retries)
    log.info("  Auto-Kommentar: %s (Delay %ds)", config.auto_comment.enabled, config.auto_comment.delay_seconds)
    log.info("  Follower-Track: %s", config.follower_tracking.enabled)
    log.info("  Comment-Reply : %s (%d Tage Lookback)", config.comment_response.enabled, config.comment_response.lookback_days)
    log.info("  Hashtags      : %s", config.hashtags.enabled)
    log.info("  Retry         : %s (max %dx, %.0fs Delay)", config.retry.enabled, config.retry.max_attempts, config.retry.delay_seconds)
    log.info("  Validierung   : %s (min %dx%d, max %.0fMB)", config.image_validation.enabled, config.image_validation.min_width, config.image_validation.min_height, config.image_validation.max_file_size_mb)
    log.info("  Engagement    : %s (nach %dh)", config.engagement.enabled, config.engagement.delay_hours)
    log.info("  Smart Slots   : %s (Exploration %.0f%%)", config.smart_slots.enabled, config.smart_slots.exploration_rate * 100)
    log.info("  Kampagnen     : %s", config.campaigns.enabled)
    log.info("  Caption-A/B   : %s (Exploration %.0f%%)", config.caption_experiments.enabled, config.caption_experiments.exploration_rate * 100)
    log.info("  Reels aktiv   : %s", config.reels.enabled)
    log.info("  Reel-Upload FB: %s", config.reels.publish_to_facebook)
    log.info("  Instagram     : %s", config.instagram.enabled)
    log.info("  IG-Posts       : %s", config.instagram.publish_posts)
    log.info("  IG-Reels       : %s", config.instagram.publish_reels)
    log.info("  IG-Stories     : %s", config.instagram.publish_stories)
    log.info("  IG-Media-URL   : %s", config.instagram.public_base_url or "nicht gesetzt")
    log.info("  Reel-Ausgabe  : %s", config.reels.output_folder)
    log.info("  Reel-Simulation: %s", config.reels.simulation_mode)
    log.info("  Reel-Bilder   : %s", config.reels.images_per_reel)
    log.info("  Reel-Transition: %s", config.reels.transition_style)
    log.info("  Reel-Audio    : %s", config.reels.audio_enabled)
    log.info("  Reel-Outro    : %s", config.reels.outro_enabled)
    log.info("  Musikbibliothek: %s", config.music_library.folder)
    log.info("  Lokale Tracks : %s", config.music_library.prefer_local_tracks)
    log.info("  Dry-Run       : %s", config.dry_run)
    log.info("=" * 60)

    scheduler = DailySlotScheduler(
        posting_slots=runtime_slots,
        callback=service.process_slot,
        poll_interval_seconds=config.poll_interval_seconds,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
