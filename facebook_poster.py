from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests
from PIL import Image

from config import AppConfig

GRAPH_BASE = "https://graph.facebook.com/v19.0"
log = logging.getLogger(__name__)


@dataclass(slots=True)
class FacebookPostResult:
    success: bool
    post_id: str | None = None
    error: str | None = None


class FacebookPoster:
    def __init__(self, config: AppConfig):
        self.config = config

    def _prepare_upload_image(self, image_path: Path) -> tuple[Path, str | None]:
        settings = self.config.watermark
        if not settings.enabled:
            return image_path, None

        if not settings.image_path.exists():
            log.warning("Wasserzeichen-Datei nicht gefunden: %s. Originalbild wird hochgeladen.", settings.image_path)
            return image_path, None

        try:
            with Image.open(image_path) as source_image:
                source_rgba = source_image.convert("RGBA")
                with Image.open(settings.image_path) as watermark_image:
                    watermark_rgba = watermark_image.convert("RGBA")

                target_width = max(1, int(source_rgba.width * max(settings.width_ratio, 0.02)))
                scale = target_width / max(watermark_rgba.width, 1)
                target_height = max(1, int(watermark_rgba.height * scale))
                resample = getattr(Image, "Resampling", Image).LANCZOS
                watermark_rgba = watermark_rgba.resize((target_width, target_height), resample)

                if settings.opacity < 1.0:
                    alpha = watermark_rgba.getchannel("A")
                    alpha = alpha.point(lambda value: int(value * max(0.0, min(settings.opacity, 1.0))))
                    watermark_rgba.putalpha(alpha)

                margin = max(0, settings.margin_px)
                x = margin
                y = margin
                if settings.position in {"top-right", "bottom-right"}:
                    x = max(margin, source_rgba.width - watermark_rgba.width - margin)
                if settings.position in {"bottom-left", "bottom-right"}:
                    y = max(margin, source_rgba.height - watermark_rgba.height - margin)

                composited = source_rgba.copy()
                composited.alpha_composite(watermark_rgba, (x, y))

                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
                temp_path = Path(temp_file.name)
                temp_file.close()
                composited.save(temp_path, format="PNG")
                return temp_path, str(temp_path)
        except Exception as exc:
            log.exception("Wasserzeichen konnte nicht angewendet werden: %s", exc)
            return image_path, None

    def post_photo(self, image_path: Path, caption: str) -> FacebookPostResult:
        if self.config.dry_run:
            return FacebookPostResult(success=True, post_id="dry-run")

        if not self.config.facebook.page_id or not self.config.facebook.access_token:
            return FacebookPostResult(success=False, error="Facebook-Zugangsdaten fehlen.")

        url = f"{GRAPH_BASE}/{self.config.facebook.page_id}/photos"
        upload_path, temp_path = self._prepare_upload_image(image_path)
        try:
            with open(upload_path, "rb") as image_handle:
                files = {"source": (upload_path.name, image_handle, "image/png")} if upload_path.suffix.lower() == ".png" else {"source": (upload_path.name, image_handle)}
                response = requests.post(
                    url,
                    data={
                        "caption": caption,
                        "access_token": self.config.facebook.access_token,
                    },
                    files=files,
                    timeout=60,
                )
            payload = response.json()
        except Exception as exc:
            return FacebookPostResult(success=False, error=str(exc))
        finally:
            if temp_path:
                try:
                    Path(temp_path).unlink(missing_ok=True)
                except Exception:
                    pass

        if response.ok and payload.get("id"):
            return FacebookPostResult(success=True, post_id=str(payload["id"]))

        error_text = payload.get("error") if isinstance(payload, dict) else payload
        return FacebookPostResult(success=False, error=str(error_text))