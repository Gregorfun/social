from __future__ import annotations

import json
import math
import os
import subprocess
import textwrap
import unicodedata
import wave
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import imageio.v2 as imageio
import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import AppConfig


@dataclass(slots=True)
class ReelResult:
    output_path: Path
    duration_seconds: int
    frame_count: int
    source_images: list[str]
    audio_source: str
    audio_track: str | None


class ReelGenerator:
    MUSIC_TAG_RULES: dict[str, tuple[str, ...]] = {
        "luxury": ("luxus", "glam", "glamour", "chic", "elegant", "edel", "fashion", "style", "gold", "rich", "beauty", "classy"),
        "romantic": ("liebe", "love", "romance", "romantic", "soft", "dream", "dreamy", "sinnlich", "zart", "rose", "magic", "magisch"),
        "energetic": ("power", "bold", "wild", "party", "dance", "fire", "hot", "crazy", "energie", "stark", "intens", "confident"),
        "dark": ("night", "neon", "shadow", "mystery", "noir", "black", "midnight", "cinematic", "dunkel", "nacht", "urban", "city"),
        "summer": ("beach", "sea", "ocean", "sun", "summer", "holiday", "urlaub", "strand", "meer", "pool", "bikini", "palm", "sunset"),
        "sport": ("gym", "fitness", "workout", "run", "sport", "training", "active", "aktiv", "athletic"),
        "playful": ("cute", "sweet", "fun", "playful", "frech", "happy", "smile", "flirty", "girl-next-door"),
    }

    def __init__(self, config: AppConfig):
        self.config = config

    def generate_reel(self, image_paths: list[Path] | Path, caption: str) -> ReelResult:
        settings = self.config.reels
        settings.output_folder.mkdir(parents=True, exist_ok=True)

        if isinstance(image_paths, Path):
            image_list = [image_paths]
        else:
            image_list = list(image_paths)

        if not image_list:
            raise ValueError("Keine Bilder fuer das Reel uebergeben.")

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_path = settings.output_folder / f"{image_list[0].stem}-reel-{timestamp}.mp4"
        silent_output_path = settings.output_folder / f"{image_list[0].stem}-reel-{timestamp}-silent.mp4"
        content_frame_count = settings.duration_seconds * settings.fps
        outro_frame_count = settings.outro_duration_seconds * settings.fps if settings.outro_enabled else 0
        frame_count = content_frame_count + outro_frame_count
        overlay_lines = self._build_overlay_lines(caption)
        sources = [Image.open(image_path).convert("RGB") for image_path in image_list]
        writer = imageio.get_writer(
            silent_output_path,
            fps=settings.fps,
            codec="libx264",
            quality=8,
            ffmpeg_log_level="error",
            macro_block_size=16,
        )

        try:
            for index in range(content_frame_count):
                progress = index / max(content_frame_count - 1, 1)
                frame = self._build_frame(sources, progress, overlay_lines)
                writer.append_data(np.asarray(frame))

            if settings.outro_enabled:
                for index in range(outro_frame_count):
                    progress = index / max(outro_frame_count - 1, 1)
                    frame = self._build_outro_frame(progress, caption)
                    writer.append_data(np.asarray(frame))
        finally:
            writer.close()

        final_output_path = self._attach_audio_if_enabled(silent_output_path, output_path, frame_count, image_list, caption)

        return ReelResult(
            output_path=final_output_path,
            duration_seconds=math.ceil(frame_count / settings.fps),
            frame_count=frame_count,
            source_images=[image_path.name for image_path in image_list],
            audio_source=self._last_audio_source,
            audio_track=self._last_audio_track,
        )

    _last_audio_source = "generated"
    _last_audio_track: str | None = None

    def _build_frame(self, sources: list[Image.Image], progress: float, overlay_lines: list[str]) -> Image.Image:
        settings = self.config.reels
        width = settings.width
        height = settings.height

        image_count = len(sources)
        if image_count == 1:
            frame = self._render_source_frame(sources[0], progress)
        else:
            timeline = progress * image_count
            segment_index = min(int(timeline), image_count - 1)
            local_progress = min(max(timeline - segment_index, 0.0), 1.0)
            frame = self._render_source_frame(sources[segment_index], local_progress, image_index=segment_index)

            per_image_frames = max((settings.duration_seconds * settings.fps) // image_count, 1)
            transition_ratio = min(settings.transition_frames / per_image_frames, 0.45)
            if segment_index < image_count - 1 and local_progress >= 1 - transition_ratio:
                next_progress = (local_progress - (1 - transition_ratio)) / max(transition_ratio, 1e-6)
                next_frame = self._render_source_frame(sources[segment_index + 1], next_progress, image_index=segment_index + 1)
                frame = self._compose_transition_frame(frame, next_frame, next_progress)

        if settings.text_overlay and overlay_lines:
            self._draw_overlay(frame, overlay_lines)
        return frame

    def _compose_transition_frame(self, current_frame: Image.Image, next_frame: Image.Image, transition_progress: float) -> Image.Image:
        settings = self.config.reels
        width, height = current_frame.size
        alpha = min(max(transition_progress, 0.0), 1.0)

        if settings.transition_style == "fade":
            return Image.blend(current_frame, next_frame, alpha=alpha)

        current_rgba = current_frame.convert("RGBA")
        next_rgba = next_frame.convert("RGBA")
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 255))

        slide_offset = int(width * 0.18 * alpha)
        current_pos = (-slide_offset, 0)
        next_pos = (width - int(width * alpha) - slide_offset, 0)
        current_mask = current_rgba.split()[-1].point(lambda _: int(255 * (1 - (alpha * 0.35))))
        next_mask = next_rgba.split()[-1].point(lambda _: int(255 * alpha))

        canvas.alpha_composite(current_rgba, current_pos)
        faded_current = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        faded_current.paste(current_rgba, current_pos, current_mask)
        canvas = Image.alpha_composite(canvas, faded_current)

        softened_next = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        softened_next.paste(next_rgba, next_pos, next_mask)
        canvas = Image.alpha_composite(canvas, softened_next)

        if settings.transition_style == "hybrid":
            return Image.blend(current_frame, canvas.convert("RGB"), alpha=min(0.8 + alpha * 0.2, 1.0))
        return canvas.convert("RGB")

    def _render_source_frame(self, source: Image.Image, progress: float, image_index: int = 0) -> Image.Image:
        settings = self.config.reels
        width = settings.width
        height = settings.height

        background = source.resize((width, height), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(radius=18))
        zoom = settings.zoom_start + ((settings.zoom_end - settings.zoom_start) * self._ease_in_out(progress))
        scaled_width = max(int(width * zoom), width)
        scaled_height = max(int(height * zoom), height)

        cover = self._resize_cover(source, scaled_width, scaled_height)
        offset_x = max((cover.width - width) // 2, 0)
        offset_y = max((cover.height - height) // 2, 0)

        horizontal_phase = progress + (image_index * 0.17)
        vertical_phase = progress + (image_index * 0.09)
        drift_x = int(math.sin(horizontal_phase * math.pi) * min(44, offset_x)) if offset_x > 0 else 0
        drift_y = int(math.cos(vertical_phase * math.pi / 2) * min(56, offset_y)) if offset_y > 0 else 0

        left = min(max(offset_x - drift_x, 0), max(cover.width - width, 0))
        top = min(max(offset_y - drift_y, 0), max(cover.height - height, 0))
        crop_box = (left, top, left + width, top + height)
        foreground = cover.crop(crop_box)
        return Image.blend(background, foreground, alpha=0.92)

    def _build_outro_frame(self, progress: float, caption: str) -> Image.Image:
        settings = self.config.reels
        width = settings.width
        height = settings.height
        frame = Image.new("RGB", (width, height), (7, 11, 23))
        rgba = frame.convert("RGBA")
        draw = ImageDraw.Draw(rgba, "RGBA")

        glow_alpha = int(120 + (80 * self._ease_in_out(progress)))
        draw.ellipse((-120, -40, width * 0.8, height * 0.55), fill=(94, 234, 212, glow_alpha))
        draw.ellipse((width * 0.25, height * 0.38, width + 120, height + 100), fill=(245, 158, 11, 72))
        draw.rectangle((0, 0, width, height), fill=(7, 11, 23, 170))

        title_font = self._load_font(62)
        subtitle_font = self._load_font(32)
        cta_font = self._load_font(38)

        title = settings.brand_title.strip() or "AI Muse Feed"
        subtitle = settings.brand_subtitle.strip() or "AI-Influencer Reels automatisch erzeugt"
        cta = settings.call_to_action.strip() or "Folgen, speichern, kommentieren"

        title_lines = textwrap.wrap(title, width=18)[:2]
        subtitle_lines = textwrap.wrap(subtitle, width=26)[:2]
        cta_lines = textwrap.wrap(cta, width=24)[:2]

        y = 220
        for line in title_lines:
            draw.text((64, y), line, font=title_font, fill=(255, 255, 255, 245))
            y = draw.textbbox((64, y), line, font=title_font)[3] + 10

        for line in subtitle_lines:
            draw.text((64, y), line, font=subtitle_font, fill=(205, 214, 229, 232))
            y = draw.textbbox((64, y), line, font=subtitle_font)[3] + 8

        y = height - 270
        for line in cta_lines:
            draw.rounded_rectangle((52, y - 14, width - 52, y + 54), radius=24, fill=(15, 23, 42, 170), outline=(94, 234, 212, 120), width=2)
            draw.text((76, y), line, font=cta_font, fill=(255, 255, 255, 245))
            y += 78

        highlight_lines = self._build_overlay_lines(caption)[:2]
        if highlight_lines:
            y = height - 470
            for line in highlight_lines:
                draw.text((64, y), line, font=subtitle_font, fill=(164, 177, 205, 220))
                y = draw.textbbox((64, y), line, font=subtitle_font)[3] + 6

        return rgba.convert("RGB")

    def _draw_overlay(self, frame: Image.Image, overlay_lines: list[str]):
        if frame.mode != "RGBA":
            frame_rgba = frame.convert("RGBA")
        else:
            frame_rgba = frame

        draw = ImageDraw.Draw(frame_rgba, "RGBA")
        width, height = frame.size

        gradient_height = int(height * 0.46)
        overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay, "RGBA")
        for index in range(gradient_height):
            alpha = int(190 * (index / max(gradient_height, 1)))
            y = height - gradient_height + index
            overlay_draw.rectangle((0, y, width, y + 1), fill=(8, 10, 20, alpha))
        frame_rgba.alpha_composite(overlay)

        title_size = max(42, int(width * 0.052))
        body_size = max(30, int(width * 0.036))
        text_x = int(width * 0.06)
        text_max_width = width - (text_x * 2)
        current_y = height - gradient_height + int(height * 0.075)

        for index, line in enumerate(overlay_lines):
            font = self._fit_overlay_font(draw, line, title_size if index == 0 else body_size, text_max_width)
            stroke_width = 3 if index == 0 else 2
            draw.text(
                (text_x, current_y),
                line,
                font=font,
                fill=(255, 255, 255, 244),
                stroke_width=stroke_width,
                stroke_fill=(4, 8, 18, 210),
            )
            bbox = draw.textbbox((text_x, current_y), line, font=font, stroke_width=stroke_width)
            current_y = bbox[3] + (18 if index == 0 else 14)

        if frame.mode != "RGBA":
            frame.paste(frame_rgba.convert("RGB"))

    def _build_overlay_lines(self, caption: str) -> list[str]:
        relevant_lines: list[str] = []
        disclaimer_lines = {line.strip() for line in self.config.ai_disclosure.splitlines() if line.strip()}

        for line in caption.splitlines():
            stripped = self._sanitize_overlay_text(line)
            if not stripped:
                continue
            if stripped in disclaimer_lines:
                continue
            if stripped.startswith("#"):
                continue
            relevant_lines.append(stripped)

        if not relevant_lines:
            relevant_lines = ["AI oder echt?", "Was zieht dich hier sofort an?"]

        max_lines = max(2, int(self.config.reels.hook_text_max_lines or 3))
        segments: list[str] = []
        for line in relevant_lines:
            pieces = [piece.strip(" -–—") for piece in re.split(r"(?<=[.!?])\s+", line) if piece.strip()]
            segments.extend(pieces or [line])

        headline = segments[0] if segments else relevant_lines[0]
        supporting = segments[1:] if len(segments) > 1 else relevant_lines[1:]

        wrapped: list[str] = []
        wrapped.extend(self._wrap_overlay_segment(headline, width=28, limit=min(2, max_lines)))
        remaining = max_lines - len(wrapped)
        for segment in supporting:
            if remaining <= 0:
                break
            lines = self._wrap_overlay_segment(segment, width=32, limit=remaining)
            wrapped.extend(lines)
            remaining = max_lines - len(wrapped)

        return wrapped[:max_lines]

    def _wrap_overlay_segment(self, text: str, width: int, limit: int) -> list[str]:
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized or limit <= 0:
            return []

        wrapped = textwrap.wrap(
            normalized,
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [normalized]
        if len(wrapped) <= limit:
            return wrapped

        clipped = wrapped[:limit]
        clipped[-1] = textwrap.shorten(" ".join(wrapped[limit - 1:]), width=max(width - 1, 8), placeholder="...")
        return clipped

    def _sanitize_overlay_text(self, text: str) -> str:
        cleaned_chars: list[str] = []
        for char in text:
            category = unicodedata.category(char)
            if category in {"So", "Cs"}:
                continue
            cleaned_chars.append(char)

        cleaned = "".join(cleaned_chars)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|•·")
        return cleaned

    def _fit_overlay_font(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        preferred_size: int,
        max_width: int,
    ) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        size = preferred_size
        while size > 26:
            font = self._load_font(size)
            bbox = draw.textbbox((0, 0), text, font=font, stroke_width=2)
            if (bbox[2] - bbox[0]) <= max_width:
                return font
            size -= 2
        return self._load_font(26)

    def _resize_cover(self, image: Image.Image, width: int, height: int) -> Image.Image:
        ratio = max(width / image.width, height / image.height)
        resized = image.resize((int(image.width * ratio), int(image.height * ratio)), Image.Resampling.LANCZOS)
        return resized

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = [
            os.getenv("REELS_FONT_PATH", "").strip(),
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Helvetica.ttc",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if path.exists():
                try:
                    return ImageFont.truetype(str(path), size=size)
                except Exception:
                    continue
        return ImageFont.load_default()

    def _ease_in_out(self, progress: float) -> float:
        return 0.5 - (math.cos(progress * math.pi) / 2)

    def _attach_audio_if_enabled(
        self,
        silent_output_path: Path,
        final_output_path: Path,
        frame_count: int,
        image_paths: list[Path],
        caption: str,
    ) -> Path:
        settings = self.config.reels
        self._last_audio_source = "none"
        self._last_audio_track = None
        if not settings.audio_enabled:
            if final_output_path.exists():
                final_output_path.unlink()
            silent_output_path.replace(final_output_path)
            return final_output_path

        audio_duration = frame_count / max(settings.fps, 1)
        preferred_tags = self._infer_music_tags(image_paths, caption)

        if self.config.music_library.enabled and self.config.music_library.prefer_local_tracks:
            local_track = self._select_music_track(preferred_tags)
            if local_track is not None:
                if self._mux_external_audio(silent_output_path, final_output_path, local_track["path"], audio_duration):
                    self._last_audio_source = "library"
                    self._last_audio_track = local_track["title"]
                    return final_output_path

        with NamedTemporaryFile(suffix=".wav", delete=False) as audio_file:
            audio_path = Path(audio_file.name)

        try:
            self._write_soundtrack(audio_path, audio_duration)
            if self._mux_external_audio(silent_output_path, final_output_path, audio_path, audio_duration, loop_input=False):
                self._last_audio_source = "generated"
                self._last_audio_track = None
                return final_output_path
        except Exception:
            pass
        finally:
            audio_path.unlink(missing_ok=True)

        final_output_path.unlink(missing_ok=True)
        silent_output_path.replace(final_output_path)
        self._last_audio_source = "none"
        self._last_audio_track = None
        return final_output_path

    def _mux_external_audio(
        self,
        silent_output_path: Path,
        final_output_path: Path,
        audio_path: Path,
        audio_duration: float,
        loop_input: bool = True,
    ) -> bool:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        command = [ffmpeg_exe, "-y", "-i", str(silent_output_path)]
        if loop_input:
            command.extend(["-stream_loop", "-1"])
        command.extend([
            "-i",
            str(audio_path),
            "-filter:a",
            f"volume={self.config.reels.audio_volume}",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-t",
            f"{audio_duration:.3f}",
            "-shortest",
            str(final_output_path),
        ])

        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            silent_output_path.unlink(missing_ok=True)
            return True
        except Exception:
            final_output_path.unlink(missing_ok=True)
            return False

    def _select_music_track(self, preferred_tags: set[str]) -> dict[str, Any] | None:
        library = self.config.music_library
        folder = library.folder
        if not folder.exists():
            return None

        candidates = []
        for path in sorted(folder.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() not in set(library.extensions):
                continue
            track = self._load_track_metadata(path)
            if track is not None:
                candidates.append(track)

        if not candidates:
            return None

        effective_tags = {tag.lower() for tag in preferred_tags if tag}
        effective_tags.update(tag.lower() for tag in library.default_tags if tag)

        if library.auto_match_enabled and effective_tags:
            for track in candidates:
                track_tags = set(track.get("tags", []))
                preferred_overlap = len(track_tags.intersection(preferred_tags))
                default_overlap = len(track_tags.intersection(library.default_tags))
                priority = track.get("priority", 0)
                track["score"] = (preferred_overlap * 100) + (default_overlap * 10) + priority
            top_score = max(track.get("score", 0) for track in candidates)
            if top_score > 0:
                candidates = [track for track in candidates if track.get("score", 0) == top_score]

        return candidates[int(datetime.now().timestamp()) % len(candidates)]

    def _load_track_metadata(self, audio_path: Path) -> dict[str, Any] | None:
        library = self.config.music_library
        metadata_path = audio_path.with_suffix(".json")
        metadata: dict[str, Any] = {}

        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                return None
        elif library.require_metadata:
            return None

        status = str(metadata.get("license_status", "")).strip().lower()
        if library.require_metadata and status != library.approved_status:
            return None

        if library.require_commercial_use and metadata and not bool(metadata.get("commercial_use", False)):
            return None

        allowed_platforms = [str(item).lower() for item in metadata.get("allowed_platforms", [])]
        if metadata and library.allowed_platforms:
            if not any(platform in allowed_platforms for platform in library.allowed_platforms):
                return None

        tags = self._extract_metadata_tags(metadata)
        priority_raw = metadata.get("priority", 0)
        try:
            priority = int(priority_raw)
        except Exception:
            priority = 0

        return {
            "path": audio_path,
            "title": str(metadata.get("title") or audio_path.stem),
            "metadata": metadata,
            "tags": tags,
            "priority": priority,
        }

    def _extract_metadata_tags(self, metadata: dict[str, Any]) -> set[str]:
        values: list[str] = []
        for field in ("tags", "moods", "genres", "keywords"):
            field_value = metadata.get(field)
            if isinstance(field_value, str):
                values.extend(part.strip().lower() for part in field_value.split(",") if part.strip())
            elif isinstance(field_value, list):
                values.extend(str(part).strip().lower() for part in field_value if str(part).strip())

        energy = metadata.get("energy")
        if isinstance(energy, str) and energy.strip():
            values.append(energy.strip().lower())

        return {value for value in values if value}

    def _infer_music_tags(self, image_paths: list[Path], caption: str) -> set[str]:
        library = self.config.music_library
        inferred_tags = {tag.lower() for tag in library.default_tags if tag}
        if not library.auto_match_enabled:
            return inferred_tags

        text_parts = [caption]
        text_parts.extend(path.stem for path in image_paths)
        normalized_text = self._normalize_text(" ".join(text_parts))

        for tag, keywords in self.MUSIC_TAG_RULES.items():
            if any(keyword in normalized_text for keyword in keywords):
                inferred_tags.add(tag)

        if "luxury" in inferred_tags:
            inferred_tags.update({"fashion", "glamour"})
        if "summer" in inferred_tags:
            inferred_tags.update({"bright", "tropical"})
        if "dark" in inferred_tags:
            inferred_tags.update({"cinematic", "night"})
        if "sport" in inferred_tags:
            inferred_tags.update({"motivating", "upbeat"})
        if "romantic" in inferred_tags:
            inferred_tags.update({"soft", "dreamy"})
        if "energetic" in inferred_tags:
            inferred_tags.update({"upbeat", "confident"})

        return inferred_tags

    def _normalize_text(self, text: str) -> str:
        lowered = text.lower()
        replacements = {
            "ä": "ae",
            "ö": "oe",
            "ü": "ue",
            "ß": "ss",
        }
        for original, replacement in replacements.items():
            lowered = lowered.replace(original, replacement)
        lowered = re.sub(r"[^a-z0-9\s-]", " ", lowered)
        return " ".join(lowered.split())

    def _write_soundtrack(self, audio_path: Path, duration_seconds: float):
        settings = self.config.reels
        sample_rate = 44100
        sample_count = max(int(sample_rate * duration_seconds), 1)
        timeline = np.linspace(0, duration_seconds, sample_count, endpoint=False)

        pads = [220.0, 261.63, 329.63, 392.0]
        melody = [523.25, 659.25, 587.33, 783.99]
        waveform = np.zeros(sample_count, dtype=np.float32)

        for index, freq in enumerate(pads):
            phase = (index * 0.19) + 1.0
            waveform += 0.18 * np.sin((2 * np.pi * freq * timeline / phase))

        beat_length = sample_rate // 2
        for start in range(0, sample_count, beat_length):
            end = min(start + beat_length, sample_count)
            segment = np.linspace(0, 1, end - start, endpoint=False)
            waveform[start:end] += 0.12 * np.sin(2 * np.pi * melody[(start // beat_length) % len(melody)] * segment)
            waveform[start:end] += 0.08 * np.sin(2 * np.pi * 110.0 * segment)

        envelope = np.minimum(np.linspace(0, 1, sample_count), np.linspace(1, 0, sample_count))
        envelope = np.clip(envelope * 1.8, 0, 1)
        waveform = waveform * envelope * settings.audio_volume
        waveform = np.clip(waveform, -1.0, 1.0)
        pcm = (waveform * 32767).astype(np.int16)

        with wave.open(str(audio_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm.tobytes())