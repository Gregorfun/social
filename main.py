from __future__ import annotations

import ctypes
import logging
import os
from datetime import datetime
from pathlib import Path

from caption_generator import CaptionGenerator
from config import AppConfig, LOCK_FILE, load_settings, setup_logging
from facebook_poster import FacebookPoster
from post_history import PostHistory
from reel_generator import ReelGenerator
from scheduler import DailySlotScheduler

if os.name != "nt":
    import fcntl

log = logging.getLogger(__name__)

_lock_handle = None
_mutex_handle = None


class AutoPostingService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.history = PostHistory(config.history_file)
        self.caption_generator = CaptionGenerator(config)
        self.facebook_poster = FacebookPoster(config)
        self.reel_generator = ReelGenerator(config)

    def _simulate_reel_publish(self, reel_path: Path, slot: str, source_images: list[str]) -> tuple[str, str]:
        if self.config.reels.simulation_mode:
            return (
                "simulated",
                f"Reel-Testlauf fuer Slot {slot}: {reel_path.name} mit {len(source_images)} Bildern simuliert, kein externer Upload.",
            )
        return ("created", "Reel erzeugt und lokal gespeichert. Kein externer Upload konfiguriert.")

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

    def _build_reel_caption(self, state: dict, control: dict, reel_images: list[Path], fallback_caption: str) -> tuple[str, str]:
        caption_override = str(control.get("caption_override") or "").strip()
        if caption_override:
            return caption_override, "manual"

        planned_names = [image.name for image in reel_images]
        cached_names = [str(name) for name in control.get("planned_source_images", []) if str(name)]
        cached_caption = str(control.get("planned_caption") or "").strip()
        if planned_names and cached_names == planned_names and cached_caption:
            return cached_caption, str(control.get("planned_caption_source") or "cached")

        try:
            bundle = self.caption_generator.generate_for_reel(reel_images)
            control["planned_caption"] = bundle.selected
            control["planned_caption_source"] = bundle.source
            control["planned_caption_updated_at"] = datetime.now().isoformat()
            return bundle.selected, bundle.source
        except Exception as exc:
            log.exception("Reel-Caption-Generierung fehlgeschlagen: %s", exc)
            return fallback_caption, "fallback"

    def _build_reel_plan(self, state: dict, images: list[Path], anchor_image: Path) -> list[Path]:
        control = self._get_reel_control(state)
        return self.history.plan_reel_images(
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

        if self.history.was_slot_processed(state, day_key, slot):
            log.info("Slot %s wurde heute bereits verarbeitet und wird uebersprungen.", slot)
            return

        if self.history.count_successful_posts_for_day(state, day_key) >= self.config.max_posts_per_day:
            log.info("Maximale Anzahl an Posts fuer heute erreicht. Slot %s wird uebersprungen.", slot)
            self.history.mark_slot_run(state, day_key, slot, status="skipped", message="Maximale Tagesanzahl erreicht.")
            self.history.save(state)
            return

        images = self._list_available_images()
        self.history.sync_image_registry(state, images)

        if not images:
            log.warning("Keine Bilder verfuegbar. Slot %s wird uebersprungen.", slot)
            self.history.update_next_image(state, images, self.config.selection_mode)
            self.history.mark_slot_run(state, day_key, slot, status="skipped", message="Keine Bilder verfuegbar.")
            self.history.save(state)
            return

        image = self.history.choose_next_image(state, images, self.config.selection_mode)
        if image is None:
            log.info("Keine unverwendeten Bilder mehr verfuegbar. Slot %s wird uebersprungen.", slot)
            self.history.update_next_image(state, images, self.config.selection_mode)
            self.history.mark_slot_run(state, day_key, slot, status="skipped", message="Keine unverwendeten Bilder verfuegbar.")
            self.history.save(state)
            return

        log.info("Gewaehlter Slot: %s", slot)
        log.info("Gewaehltes Bild: %s", image.name)

        caption_bundle = self.caption_generator.generate_for_image(image)
        self.history.store_generated_captions(
            state,
            image_name=image.name,
            variants=caption_bundle.variants,
            selected=caption_bundle.selected,
            description=caption_bundle.description,
        )
        log.info("Caption-Quelle: %s", caption_bundle.source)
        log.info("Generierter Text:\n%s", caption_bundle.selected)

        if self.config.reels.enabled:
            try:
                control = self._get_reel_control(state)
                reel_images = self._build_reel_plan(state, images, image)
                if reel_images:
                    reel_caption, reel_caption_source = self._build_reel_caption(state, control, reel_images, caption_bundle.selected)
                    reel_result = self.reel_generator.generate_reel(reel_images, reel_caption)
                    reel_publish_status, reel_publish_message = self._simulate_reel_publish(
                        reel_result.output_path,
                        slot,
                        reel_result.source_images,
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
                    )
                    log.info(
                        "Reel erzeugt: %s | Quellen: %s | Audio: %s%s | Status: %s",
                        reel_result.output_path,
                        ", ".join(reel_result.source_images),
                        reel_result.audio_source,
                        f" ({reel_result.audio_track})" if reel_result.audio_track else "",
                        reel_publish_status,
                    )
                    log.info("Reel-Caption-Quelle: %s", reel_caption_source)
                    log.info("Reel-Text:\n%s", reel_caption)
                    log.info(reel_publish_message)
                else:
                    log.info("Naechstes Reel fuer Slot %s wurde manuell uebersprungen.", slot)
                self._consume_reel_control(state, image.name)
            except Exception as exc:
                log.exception("Reel-Erzeugung fehlgeschlagen: %s", exc)

        result = self.facebook_poster.post_photo(image, caption_bundle.selected)
        if not result.success:
            log.error("Facebook-Posting fehlgeschlagen: %s", result.error)
            self.history.mark_slot_run(
                state,
                day_key,
                slot,
                status="failed",
                message=result.error or "Facebook-Posting fehlgeschlagen.",
                image_name=image.name,
                caption=caption_bundle.selected,
            )
            self.history.update_next_image(state, images, self.config.selection_mode)
            self.history.save(state)
            return

        log.info("Facebook-Posting erfolgreich: %s (Post-ID: %s)", image.name, result.post_id)

        images_after_post = list(images)
        if self.config.delete_after_post and not self.config.dry_run:
            moved_path = self._move_to_sent_folder(image)
            images_after_post = [current for current in images if current.name != image.name]
            log.info("Bild verschoben nach 'versendet': %s", moved_path.name)

        self.history.record_post_success(
            state,
            image=image,
            slot=slot,
            caption=caption_bundle.selected,
            post_id=result.post_id or "",
            images_after_post=images_after_post,
            selection_mode=self.config.selection_mode,
        )
        self.history.save(state)

    def prepare_runtime_state(self):
        state = self.history.load()
        images = self._list_available_images()
        self.history.sync_image_registry(state, images)
        self.history.update_next_image(state, images, self.config.selection_mode)
        self.history.save(state)

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

    log.info("=" * 60)
    log.info("AI-Influencer Auto-Poster gestartet")
    log.info("  Ordner        : %s", config.images_folder)
    log.info("  Slots         : %s", ", ".join(config.posting_slots))
    log.info("  Max/Tag       : %s", config.max_posts_per_day)
    log.info("  Auswahl       : %s", config.selection_mode)
    log.info("  Caption       : %s", config.caption_provider)
    log.info("  Ollama Modell : %s", config.ollama.model)
    log.info("  OpenAI Fallback: %s", config.openai.enabled)
    log.info("  Reels aktiv   : %s", config.reels.enabled)
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
        posting_slots=config.posting_slots,
        callback=service.process_slot,
        poll_interval_seconds=config.poll_interval_seconds,
    )
    scheduler.start()


if __name__ == "__main__":
    main()