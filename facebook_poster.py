from __future__ import annotations

import http.client
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

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
    permanent: bool = False


@dataclass(slots=True)
class FacebookCommentResult:
    success: bool
    error: str | None = None
    permanent: bool = False
    missing_permissions: tuple[str, ...] = ()


@dataclass(slots=True)
class FacebookReelResult:
    success: bool
    reel_id: str | None = None
    error: str | None = None
    permanent: bool = False
    missing_permissions: tuple[str, ...] = ()


class FacebookPoster:
    def __init__(self, config: AppConfig):
        self.config = config

    def _parse_graph_error(self, response: requests.Response) -> tuple[str, bool, tuple[str, ...]]:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text

        message = str(payload)
        code = None
        subcode = None
        error_type = None
        if isinstance(payload, dict):
            error = payload.get("error") or {}
            message = str(error.get("message") or payload)
            code = error.get("code")
            subcode = error.get("error_subcode")
            error_type = error.get("type")

        normalized_message = message
        auth_error = code == 190 or error_type == "OAuthException"
        if auth_error and (subcode == 463 or "Session has expired" in message):
            normalized_message = "Facebook-Zugriffstoken abgelaufen. Bitte ein neues Seiten-Token in config.json hinterlegen."
        elif auth_error:
            normalized_message = "Facebook-Zugriffstoken ungueltig. Bitte das Seiten-Token in config.json pruefen oder erneuern."

        missing_permissions = tuple(
            permission
            for permission in (
                "pages_manage_engagement",
                "pages_read_engagement",
                "pages_read_user_content",
                "pages_manage_posts",
                "pages_show_list",
            )
            if permission in message
        )
        permanent = bool(missing_permissions) or auth_error or (response.status_code in {401, 403} and code == 200)
        return normalized_message, permanent, missing_permissions

    def _post_video_bytes(self, upload_url: str, video_path: Path) -> tuple[bool, str | None]:
        headers = {
            "Authorization": f"OAuth {self.config.facebook.access_token}",
            "Content-Type": "application/octet-stream",
            "Transfer-Encoding": "chunked",
            "offset": "0",
        }

        parsed = urlparse(upload_url)

        def iter_chunks(chunk_size: int = 1024 * 1024):
            with open(video_path, "rb") as video_handle:
                while True:
                    chunk = video_handle.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk

        try:
            connection = http.client.HTTPSConnection(parsed.netloc, timeout=300)
            connection.request("POST", parsed.path, body=iter_chunks(), headers=headers, encode_chunked=True)
            response = connection.getresponse()
            payload = response.read().decode("utf-8", errors="replace")
            connection.close()
        except Exception as exc:
            return False, str(exc)

        if 200 <= response.status < 300:
            return True, None

        return False, payload

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

    def _validate_image(self, image_path: Path) -> str | None:
        cfg = self.config.image_validation
        if not cfg.enabled:
            return None
        file_size_mb = image_path.stat().st_size / (1024 * 1024)
        if file_size_mb > cfg.max_file_size_mb:
            return f"Datei zu gross: {file_size_mb:.1f} MB (max {cfg.max_file_size_mb} MB)"
        try:
            with Image.open(image_path) as img:
                width, height = img.size
            if width < cfg.min_width or height < cfg.min_height:
                return f"Aufloesung zu klein: {width}x{height} (min {cfg.min_width}x{cfg.min_height})"
        except Exception as exc:
            return f"Bild konnte nicht geoeffnet werden: {exc}"
        return None

    def post_photo(self, image_path: Path, caption: str) -> FacebookPostResult:
        if self.config.dry_run:
            return FacebookPostResult(success=True, post_id="dry-run")

        if not self.config.facebook.page_id or not self.config.facebook.access_token:
            return FacebookPostResult(success=False, error="Facebook-Zugangsdaten fehlen.", permanent=True)

        validation_error = self._validate_image(image_path)
        if validation_error:
            return FacebookPostResult(success=False, error=f"Bildvalidierung fehlgeschlagen: {validation_error}", permanent=True)

        retry = self.config.retry
        max_attempts = retry.max_attempts if retry.enabled else 1
        last_error: str = "Unbekannter Fehler"

        for attempt in range(max_attempts):
            if attempt > 0:
                log.warning("Facebook-Post-Retry %d/%d nach %.0fs Wartezeit ...", attempt + 1, max_attempts, retry.delay_seconds)
                time.sleep(retry.delay_seconds)

            result = self._post_photo_once(image_path, caption, published=True)
            if result.success:
                return result
            last_error = result.error or last_error
            if result.permanent:
                return result
            log.warning("Facebook-Post-Versuch %d fehlgeschlagen: %s", attempt + 1, last_error)

        return FacebookPostResult(success=False, error=last_error)

    def post_story_photo(self, image_path: Path) -> FacebookPostResult:
        if self.config.dry_run:
            return FacebookPostResult(success=True, post_id="dry-run")

        if not self.config.facebook.page_id or not self.config.facebook.access_token:
            return FacebookPostResult(success=False, error="Facebook-Zugangsdaten fehlen.", permanent=True)

        validation_error = self._validate_image(image_path)
        if validation_error:
            return FacebookPostResult(success=False, error=f"Bildvalidierung fehlgeschlagen: {validation_error}", permanent=True)

        retry = self.config.retry
        max_attempts = retry.max_attempts if retry.enabled else 1
        last_error: str = "Unbekannter Fehler"

        for attempt in range(max_attempts):
            if attempt > 0:
                log.warning("Facebook-Story-Retry %d/%d nach %.0fs Wartezeit ...", attempt + 1, max_attempts, retry.delay_seconds)
                time.sleep(retry.delay_seconds)

            result = self._post_story_photo_once(image_path)
            if result.success:
                return result
            last_error = result.error or last_error
            if result.permanent:
                return result
            log.warning("Facebook-Story-Versuch %d fehlgeschlagen: %s", attempt + 1, last_error)

        return FacebookPostResult(success=False, error=last_error)

    def _post_story_photo_once(self, image_path: Path) -> FacebookPostResult:
        upload_result = self._post_photo_once(image_path, caption="", published=False)
        if not upload_result.success:
            return upload_result

        try:
            response = requests.post(
                f"{GRAPH_BASE}/{self.config.facebook.page_id}/photo_stories",
                data={
                    "photo_id": upload_result.post_id,
                    "access_token": self.config.facebook.access_token,
                },
                timeout=60,
            )
        except Exception as exc:
            return FacebookPostResult(success=False, error=str(exc))

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.ok and payload.get("success"):
            story_post_id = str(payload.get("post_id") or upload_result.post_id or "")
            return FacebookPostResult(success=True, post_id=story_post_id)

        message, permanent, _ = self._parse_graph_error(response)
        return FacebookPostResult(success=False, error=message, permanent=permanent)

    def _post_photo_once(self, image_path: Path, caption: str, published: bool) -> FacebookPostResult:
        url = f"{GRAPH_BASE}/{self.config.facebook.page_id}/photos"
        upload_path, temp_path = self._prepare_upload_image(image_path)
        try:
            with open(upload_path, "rb") as image_handle:
                files = {"source": (upload_path.name, image_handle, "image/png")} if upload_path.suffix.lower() == ".png" else {"source": (upload_path.name, image_handle)}
                response = requests.post(
                    url,
                    data={
                        "caption": caption,
                        "published": str(bool(published)).lower(),
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

        message, permanent, _ = self._parse_graph_error(response)
        return FacebookPostResult(success=False, error=message, permanent=permanent)

    def post_reel(self, video_path: Path, caption: str) -> FacebookReelResult:
        if self.config.dry_run:
            return FacebookReelResult(success=True, reel_id="dry-run")

        if not self.config.facebook.page_id or not self.config.facebook.access_token:
            return FacebookReelResult(success=False, error="Facebook-Zugangsdaten fehlen.", permanent=True)

        if not video_path.exists():
            return FacebookReelResult(success=False, error=f"Reel-Datei nicht gefunden: {video_path}", permanent=True)

        retry = self.config.retry
        max_attempts = retry.max_attempts if retry.enabled else 1
        last_error: str = "Unbekannter Fehler"

        for attempt in range(max_attempts):
            if attempt > 0:
                log.warning("Facebook-Reel-Retry %d/%d nach %.0fs Wartezeit ...", attempt + 1, max_attempts, retry.delay_seconds)
                time.sleep(retry.delay_seconds)

            result = self._post_reel_once(video_path, caption)
            if result.success:
                return result
            last_error = result.error or last_error
            if result.permanent:
                return result
            log.warning("Facebook-Reel-Versuch %d fehlgeschlagen: %s", attempt + 1, last_error)

        return FacebookReelResult(success=False, error=last_error)

    def _post_reel_once(self, video_path: Path, caption: str) -> FacebookReelResult:
        try:
            start_response = requests.post(
                f"{GRAPH_BASE}/{self.config.facebook.page_id}/video_reels",
                data={
                    "upload_phase": "start",
                    "access_token": self.config.facebook.access_token,
                },
                timeout=60,
            )
        except Exception as exc:
            return FacebookReelResult(success=False, error=str(exc))

        if not start_response.ok:
            message, permanent, missing_permissions = self._parse_graph_error(start_response)
            return FacebookReelResult(
                success=False,
                error=message,
                permanent=permanent,
                missing_permissions=missing_permissions,
            )

        try:
            start_payload = start_response.json()
        except ValueError:
            return FacebookReelResult(success=False, error="Facebook-Reel-Start lieferte keine gueltige JSON-Antwort.")

        reel_id = str(start_payload.get("video_id") or start_payload.get("id") or "").strip() or None
        upload_url = str(start_payload.get("upload_url") or "").strip()
        if not reel_id or not upload_url:
            return FacebookReelResult(success=False, error=f"Facebook-Reel-Startantwort unvollstaendig: {start_payload}")

        uploaded, upload_error = self._post_video_bytes(upload_url, video_path)
        if not uploaded:
            return FacebookReelResult(success=False, reel_id=reel_id, error=upload_error)

        try:
            finish_response = requests.post(
                f"{GRAPH_BASE}/{self.config.facebook.page_id}/video_reels",
                data={
                    "upload_phase": "finish",
                    "video_id": reel_id,
                    "video_state": "PUBLISHED",
                    "description": caption,
                    "access_token": self.config.facebook.access_token,
                },
                timeout=60,
            )
        except Exception as exc:
            return FacebookReelResult(success=False, reel_id=reel_id, error=str(exc))

        if not finish_response.ok:
            message, permanent, missing_permissions = self._parse_graph_error(finish_response)
            return FacebookReelResult(
                success=False,
                reel_id=reel_id,
                error=message,
                permanent=permanent,
                missing_permissions=missing_permissions,
            )

        try:
            finish_payload = finish_response.json()
        except ValueError:
            finish_payload = {}

        published_id = str(finish_payload.get("post_id") or finish_payload.get("video_id") or reel_id)
        return FacebookReelResult(success=True, reel_id=published_id)

    def fetch_engagement(self, post_id: str) -> dict:
        if not self.config.facebook.access_token:
            return {}
        try:
            response = requests.get(
                f"{GRAPH_BASE}/{post_id}",
                params={
                    "fields": "likes.summary(true),comments.summary(true),shares",
                    "access_token": self.config.facebook.access_token,
                },
                timeout=30,
            )
            if response.ok:
                return response.json()
        except Exception as exc:
            log.warning("Engagement-Abruf fehlgeschlagen fuer Post %s: %s", post_id, exc)
        return {}

    def post_comment(self, post_id: str, text: str) -> FacebookCommentResult:
        if not self.config.facebook.access_token:
            return FacebookCommentResult(success=False, error="Facebook-Zugangsdaten fehlen.", permanent=True)
        try:
            response = requests.post(
                f"{GRAPH_BASE}/{post_id}/comments",
                data={"message": text, "access_token": self.config.facebook.access_token},
                timeout=30,
            )
            if response.ok:
                return FacebookCommentResult(success=True)

            message, permanent, missing_permissions = self._parse_graph_error(response)
            return FacebookCommentResult(
                success=False,
                error=message,
                permanent=permanent,
                missing_permissions=missing_permissions,
            )
        except Exception as exc:
            log.warning("Kommentar-Post fehlgeschlagen fuer Post %s: %s", post_id, exc)
            return FacebookCommentResult(success=False, error=str(exc))

    def fetch_follower_count(self) -> int | None:
        if not self.config.facebook.page_id or not self.config.facebook.access_token:
            return None
        try:
            response = requests.get(
                f"{GRAPH_BASE}/{self.config.facebook.page_id}",
                params={"fields": "fan_count", "access_token": self.config.facebook.access_token},
                timeout=30,
            )
            if response.ok:
                return response.json().get("fan_count")
        except Exception as exc:
            log.warning("Follower-Anzahl-Abruf fehlgeschlagen: %s", exc)
        return None

    def fetch_unanswered_comments(self, post_id: str, replied_ids: set[str], max_count: int = 3) -> list[dict]:
        if not self.config.facebook.access_token:
            return []
        try:
            response = requests.get(
                f"{GRAPH_BASE}/{post_id}/comments",
                params={
                    "fields": "id,from,message",
                    "access_token": self.config.facebook.access_token,
                    "limit": 25,
                },
                timeout=30,
            )
            if not response.ok:
                message, _, _ = self._parse_graph_error(response)
                log.warning("Kommentar-Abruf fehlgeschlagen fuer Post %s: %s", post_id, message)
                return []
            comments = response.json().get("data", [])
            page_id = self.config.facebook.page_id
            unanswered = [
                c for c in comments
                if c.get("id") not in replied_ids
                and str((c.get("from") or {}).get("id", "")) != str(page_id)
            ]
            return unanswered[:max_count]
        except Exception as exc:
            log.warning("Kommentar-Abruf fehlgeschlagen fuer Post %s: %s", post_id, exc)
        return []

    def reply_to_comment(self, comment_id: str, text: str) -> FacebookCommentResult:
        if not self.config.facebook.access_token:
            return FacebookCommentResult(success=False, error="Facebook-Zugangsdaten fehlen.", permanent=True)
        try:
            response = requests.post(
                f"{GRAPH_BASE}/{comment_id}/comments",
                data={"message": text, "access_token": self.config.facebook.access_token},
                timeout=30,
            )
            if response.ok:
                return FacebookCommentResult(success=True)

            message, permanent, missing_permissions = self._parse_graph_error(response)
            return FacebookCommentResult(
                success=False,
                error=message,
                permanent=permanent,
                missing_permissions=missing_permissions,
            )
        except Exception as exc:
            log.warning("Kommentar-Antwort fehlgeschlagen fuer Kommentar %s: %s", comment_id, exc)
            return FacebookCommentResult(success=False, error=str(exc))

    def fetch_best_posting_slots(self, top_count: int = 4) -> list[str]:
        if not self.config.facebook.page_id or not self.config.facebook.access_token:
            return []
        try:
            response = requests.get(
                f"{GRAPH_BASE}/{self.config.facebook.page_id}/insights/page_fans_online_per_day",
                params={"access_token": self.config.facebook.access_token},
                timeout=30,
            )
            if not response.ok:
                return []
            data = response.json().get("data", [])
            hourly: dict[int, int] = {}
            for entry in data:
                for hour_str, count in (entry.get("value") or {}).items():
                    hour = int(hour_str)
                    hourly[hour] = hourly.get(hour, 0) + count
            if not hourly:
                return []
            sorted_hours = sorted(hourly, key=lambda h: hourly[h], reverse=True)
            chosen = sorted(sorted_hours[:top_count])
            return [f"{h:02d}:00" for h in chosen]
        except Exception as exc:
            log.warning("Beste-Posting-Zeit-Abruf fehlgeschlagen: %s", exc)
            return []