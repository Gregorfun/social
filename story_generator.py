from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import AppConfig


@dataclass(slots=True)
class StoryCardResult:
    output_path: Path
    text: str
    theme: str
    variant: str = "default"


class StoryGenerator:
    THEME_COLORS: dict[str, dict[str, tuple[int, int, int]]] = {
        "morning": {
            "top": (255, 214, 153),
            "bottom": (255, 245, 214),
            "accent": (255, 161, 79),
            "accent_secondary": (255, 110, 64),
            "panel": (255, 248, 238),
            "text": (59, 41, 26),
        },
        "day": {
            "top": (166, 224, 255),
            "bottom": (236, 248, 255),
            "accent": (34, 139, 230),
            "accent_secondary": (0, 186, 124),
            "panel": (244, 251, 255),
            "text": (24, 45, 66),
        },
        "evening": {
            "top": (255, 181, 142),
            "bottom": (255, 233, 209),
            "accent": (211, 87, 62),
            "accent_secondary": (245, 144, 77),
            "panel": (255, 245, 238),
            "text": (66, 35, 28),
        },
        "night": {
            "top": (23, 33, 71),
            "bottom": (55, 70, 117),
            "accent": (146, 169, 255),
            "accent_secondary": (110, 220, 204),
            "panel": (31, 41, 78),
            "text": (242, 245, 255),
        },
    }

    def __init__(self, config: AppConfig):
        self.config = config

    def generate_story_card(
        self,
        text: str,
        theme: str,
        *,
        background_image: Path | None = None,
        variant: str = "default",
    ) -> StoryCardResult:
        settings = self.config.stories
        settings.output_folder.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_theme = re.sub(r"[^a-z0-9-]+", "-", theme.lower()).strip("-") or "story"
        safe_variant = re.sub(r"[^a-z0-9-]+", "-", variant.lower()).strip("-") or "default"
        output_path = settings.output_folder / f"story-{safe_theme}-{safe_variant}-{timestamp}.png"

        image = self._build_story_image(text, theme, background_image=background_image, variant=variant)
        image.save(output_path, format="PNG")
        return StoryCardResult(output_path=output_path, text=text, theme=theme, variant=variant)

    def _build_story_image(
        self,
        text: str,
        theme: str,
        *,
        background_image: Path | None = None,
        variant: str = "default",
    ) -> Image.Image:
        settings = self.config.stories
        width = settings.width
        height = settings.height
        palette = self.THEME_COLORS.get(theme, self.THEME_COLORS["day"])

        background = self._build_background(width, height, palette, background_image, variant)

        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay, "RGBA")
        is_prompt = variant == "prompt"
        panel_top = int(height * (0.14 if is_prompt else 0.16))
        panel_bottom = int(height * (0.86 if is_prompt else 0.84))
        panel_left = int(width * 0.08)
        panel_right = int(width * 0.92)
        panel_alpha = 168 if background_image else (212 if theme != "night" else 228)
        if is_prompt:
            panel_alpha = min(panel_alpha + 22, 236)
        panel_fill = palette["panel"] + (panel_alpha,)
        overlay_draw.rounded_rectangle(
            (panel_left, panel_top, panel_right, panel_bottom),
            radius=44,
            fill=panel_fill,
            outline=(255, 255, 255, 90),
            width=2,
        )
        overlay = overlay.filter(ImageFilter.GaussianBlur(radius=0.3))
        background = Image.alpha_composite(background.convert("RGBA"), overlay)

        text_draw = ImageDraw.Draw(background, "RGBA")
        max_text_width = int(width * 0.7)
        start_size = int(height * (0.095 if is_prompt else 0.11))
        body_font = self._fit_text_font(text_draw, text, max_text_width, start_size, 42)
        wrapped_lines = self._wrap_text(text_draw, text, body_font, max_text_width)
        bbox = self._multiline_bbox(text_draw, wrapped_lines, body_font, max_text_width, spacing=18)

        text_x = int((width - (bbox[2] - bbox[0])) / 2)
        text_y = int((height - (bbox[3] - bbox[1])) / 2) - (10 if is_prompt else 24)
        text_draw.multiline_text(
            (text_x, text_y),
            "\n".join(wrapped_lines),
            font=body_font,
            fill=palette["text"] + (248,),
            spacing=18,
            align="center",
            stroke_width=0,
        )

        footer_font = self._load_font(28)
        footer_text = self.config.stories.brand_footer.strip()
        if footer_text:
            footer_bbox = text_draw.textbbox((0, 0), footer_text, font=footer_font)
            footer_x = int((width - (footer_bbox[2] - footer_bbox[0])) / 2)
            footer_y = panel_bottom - 74
            text_draw.text((footer_x, footer_y), footer_text, font=footer_font, fill=palette["text"] + (180,))

        return background.convert("RGB")

    def _build_background(
        self,
        width: int,
        height: int,
        palette: dict[str, tuple[int, int, int]],
        background_image: Path | None,
        variant: str,
    ) -> Image.Image:
        background = Image.new("RGB", (width, height), palette["bottom"])
        draw = ImageDraw.Draw(background, "RGBA")

        for y in range(height):
            ratio = y / max(height - 1, 1)
            color = tuple(
                int((palette["top"][index] * (1 - ratio)) + (palette["bottom"][index] * ratio))
                for index in range(3)
            )
            draw.line((0, y, width, y), fill=color)

        if background_image and background_image.exists():
            try:
                with Image.open(background_image) as source:
                    cover = self._cover_image(source.convert("RGB"), width, height)
                if variant == "prompt":
                    cover = cover.filter(ImageFilter.GaussianBlur(radius=4.0))
                overlay_alpha = 124 if variant == "prompt" else 96
                tinted = Image.new("RGBA", (width, height), palette["top"] + (0,))
                tint_draw = ImageDraw.Draw(tinted, "RGBA")
                tint_draw.rectangle((0, 0, width, height), fill=(6, 10, 20, overlay_alpha))
                cover_rgba = Image.alpha_composite(cover.convert("RGBA"), tinted)
                background = Image.blend(background.convert("RGBA"), cover_rgba, 0.72).convert("RGB")
            except Exception:
                pass

        draw = ImageDraw.Draw(background, "RGBA")
        draw.ellipse((-180, -120, width * 0.78, height * 0.45), fill=palette["accent"] + (72,))
        draw.ellipse((width * 0.22, height * 0.55, width + 160, height + 120), fill=palette["accent_secondary"] + (58,))
        return background

    def _cover_image(self, image: Image.Image, width: int, height: int) -> Image.Image:
        src_w, src_h = image.size
        scale = max(width / max(src_w, 1), height / max(src_h, 1))
        resized = image.resize((max(1, int(src_w * scale)), max(1, int(src_h * scale))), Image.Resampling.LANCZOS)
        left = max(0, (resized.width - width) // 2)
        top = max(0, (resized.height - height) // 2)
        return resized.crop((left, top, left + width, top + height))

    def _wrap_text(self, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
        words = text.split()
        if not words:
            return [text]

        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            bbox = draw.textbbox((0, 0), trial, font=font)
            if (bbox[2] - bbox[0]) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _multiline_bbox(
        self,
        draw: ImageDraw.ImageDraw,
        lines: list[str],
        font: ImageFont.ImageFont,
        max_width: int,
        spacing: int,
    ) -> tuple[int, int, int, int]:
        text = "\n".join(lines)
        return draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing, align="center")

    def _fit_text_font(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        max_width: int,
        start_size: int,
        min_size: int,
    ) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        size = start_size
        while size >= min_size:
            font = self._load_font(size)
            lines = self._wrap_text(draw, text, font, max_width)
            bbox = self._multiline_bbox(draw, lines, font, max_width, spacing=18)
            if (bbox[2] - bbox[0]) <= max_width and len(lines) <= 5:
                return font
            size -= 4
        return self._load_font(min_size)

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = [
            os.getenv("STORIES_FONT_PATH", "").strip(),
            os.getenv("REELS_FONT_PATH", "").strip(),
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
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
