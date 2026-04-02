from __future__ import annotations

import logging
import mimetypes
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from config import AppConfig

GRAPH_BASE = "https://graph.facebook.com/v25.0"
log = logging.getLogger(__name__)


@dataclass(slots=True)
class InstagramPublishResult:
    success: bool
    media_id: str | None = None
    error: str | None = None
    permanent: bool = False
    skipped: bool = False


@dataclass(slots=True)
class InstagramStagedMedia:
    public_url: str
    local_path: Path
    remote_path: str | None = None


class InstagramPoster:
    def __init__(self, config: AppConfig):
        self.config = config
        self._resolved_business_account_id: str | None = None

    def post_image(self, image_path: Path, caption: str) -> InstagramPublishResult:
        if self.config.dry_run:
            return InstagramPublishResult(success=True, media_id="dry-run")
        if not self._image_publishing_enabled():
            return InstagramPublishResult(success=False, skipped=True, error="Instagram-Bildposting ist deaktiviert.")

        ig_user_id = self._resolve_business_account_id()
        if not ig_user_id:
            return InstagramPublishResult(success=False, error="Instagram-Business-Konto konnte nicht aufgeloest werden.", permanent=True)

        staged_media, staging_error = self._stage_public_image(image_path, prefix="ig-post")
        if staged_media is None:
            return InstagramPublishResult(success=False, error=staging_error, permanent=bool(staging_error))

        try:
            return self._post_image_media(
                ig_user_id,
                staged_media,
                caption=caption,
                media_type=None,
                container_error_message="Instagram-Container fuer Bild konnte nicht erstellt werden.",
            )
        finally:
            self._cleanup_staged_file(staged_media)

    def post_story_image(self, image_path: Path) -> InstagramPublishResult:
        if self.config.dry_run:
            return InstagramPublishResult(success=True, media_id="dry-run")
        if not self._story_publishing_enabled():
            return InstagramPublishResult(success=False, skipped=True, error="Instagram-Story-Posting ist deaktiviert.")

        ig_user_id = self._resolve_business_account_id()
        if not ig_user_id:
            return InstagramPublishResult(success=False, error="Instagram-Business-Konto konnte nicht aufgeloest werden.", permanent=True)

        staged_media, staging_error = self._stage_public_image(image_path, prefix="ig-story")
        if staged_media is None:
            return InstagramPublishResult(success=False, error=staging_error, permanent=bool(staging_error))

        try:
            return self._post_image_media(
                ig_user_id,
                staged_media,
                caption=None,
                media_type="STORIES",
                container_error_message="Instagram-Container fuer Story konnte nicht erstellt werden.",
            )
        finally:
            self._cleanup_staged_file(staged_media)

    def post_reel(self, video_path: Path, caption: str) -> InstagramPublishResult:
        if self.config.dry_run:
            return InstagramPublishResult(success=True, media_id="dry-run")
        if not self._reel_publishing_enabled():
            return InstagramPublishResult(success=False, skipped=True, error="Instagram-Reel-Posting ist deaktiviert.")
        if not video_path.exists():
            return InstagramPublishResult(success=False, error=f"Reel-Datei nicht gefunden: {video_path}", permanent=True)

        ig_user_id = self._resolve_business_account_id()
        if not ig_user_id:
            return InstagramPublishResult(success=False, error="Instagram-Business-Konto konnte nicht aufgeloest werden.", permanent=True)

        response = self._graph_post(
            f"/{ig_user_id}/media",
            {
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "share_to_feed": str(bool(self.config.instagram.share_reels_to_feed)).lower(),
            },
            timeout=60,
        )
        if not response.ok:
            return self._build_error_result(response)

        payload = response.json() or {}
        container_id = str(payload.get("id") or "").strip()
        upload_url = str(payload.get("uri") or "").strip()
        if not container_id or not upload_url:
            return InstagramPublishResult(success=False, error=f"Instagram-Reel-Uploadstart unvollstaendig: {payload}")

        uploaded, upload_error = self._upload_video(upload_url, video_path)
        if not uploaded:
            return InstagramPublishResult(success=False, error=upload_error)

        ready, ready_error = self._wait_for_container(container_id)
        if not ready:
            return InstagramPublishResult(success=False, error=ready_error)

        return self._publish_container(ig_user_id, container_id)

    def _resolve_business_account_id(self) -> str | None:
        configured = str(self.config.instagram.business_account_id or "").strip()
        if configured:
            return configured
        if self._resolved_business_account_id:
            return self._resolved_business_account_id
        if not self.config.facebook.page_id or not self._access_token:
            return None

        try:
            response = requests.get(
                f"{GRAPH_BASE}/{self.config.facebook.page_id}",
                params={
                    "fields": "instagram_business_account{id,username}",
                    "access_token": self._access_token,
                },
                timeout=30,
            )
        except Exception as exc:
            log.warning("Instagram-Business-Konto konnte nicht abgefragt werden: %s", exc)
            return None

        if not response.ok:
            message, _, _ = self._parse_graph_error(response)
            log.warning("Instagram-Business-Konto konnte nicht abgefragt werden: %s", message)
            return None

        account = (response.json() or {}).get("instagram_business_account") or {}
        resolved_id = str(account.get("id") or "").strip()
        if resolved_id:
            self._resolved_business_account_id = resolved_id
            username = str(account.get("username") or "").strip()
            if username:
                log.info("Instagram-Business-Konto erkannt: %s (%s)", username, resolved_id)
        return self._resolved_business_account_id

    @property
    def _access_token(self) -> str:
        return str(self.config.instagram.access_token or "").strip()

    def _image_publishing_enabled(self) -> bool:
        cfg = self.config.instagram
        return bool(cfg.enabled and cfg.publish_posts)

    def _story_publishing_enabled(self) -> bool:
        cfg = self.config.instagram
        return bool(cfg.enabled and cfg.publish_stories)

    def _reel_publishing_enabled(self) -> bool:
        cfg = self.config.instagram
        return bool(cfg.enabled and cfg.publish_reels)

    def fetch_account_overview(self) -> dict[str, Any]:
        overview: dict[str, Any] = {
            "enabled": bool(self.config.instagram.enabled),
            "business_account_id": str(self.config.instagram.business_account_id or "").strip(),
            "username": str(self.config.instagram.username or "").strip(),
            "followers_count": None,
            "media_count": None,
            "error": None,
        }
        if not self.config.instagram.enabled:
            overview["error"] = "Instagram ist deaktiviert."
            return overview

        ig_user_id = self._resolve_business_account_id()
        if not ig_user_id:
            overview["error"] = "Instagram-Business-Konto konnte nicht aufgeloest werden."
            return overview

        overview["business_account_id"] = ig_user_id
        response = self._graph_get(f"/{ig_user_id}", {"fields": "id,username,followers_count,media_count"}, timeout=30)
        if not response.ok:
            message, _, _ = self._parse_graph_error(response)
            overview["error"] = message
            return overview

        payload = response.json() or {}
        overview["username"] = str(payload.get("username") or overview["username"] or "").strip()
        overview["followers_count"] = payload.get("followers_count")
        overview["media_count"] = payload.get("media_count")
        return overview

    def fetch_media_snapshot(self, media_id: str) -> dict[str, Any]:
        resolved_media_id = str(media_id or "").strip()
        if not resolved_media_id:
            return {"id": "", "error": "Leere Media-ID."}

        response = self._graph_get(
            f"/{resolved_media_id}",
            {
                "fields": "id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url,media_url,like_count,comments_count",
            },
            timeout=30,
        )
        if not response.ok:
            message, _, _ = self._parse_graph_error(response)
            return {"id": resolved_media_id, "error": message}

        payload = response.json() or {}
        snapshot: dict[str, Any] = {
            "id": resolved_media_id,
            "caption": str(payload.get("caption") or "").strip(),
            "media_type": str(payload.get("media_type") or "").strip(),
            "media_product_type": str(payload.get("media_product_type") or "").strip(),
            "permalink": str(payload.get("permalink") or "").strip(),
            "timestamp": str(payload.get("timestamp") or "").strip(),
            "thumbnail_url": str(payload.get("thumbnail_url") or "").strip(),
            "media_url": str(payload.get("media_url") or "").strip(),
            "like_count": int(payload.get("like_count") or 0),
            "comments_count": int(payload.get("comments_count") or 0),
            "insights": {},
            "error": None,
        }

        media_kind = str(snapshot["media_product_type"] or snapshot["media_type"]).upper()
        metrics = ["reach", "saved", "total_interactions"]
        if media_kind == "REELS" or snapshot["media_type"] == "VIDEO":
            metrics.append("plays")
        else:
            metrics.append("impressions")

        insights_response = self._graph_get(
            f"/{resolved_media_id}/insights",
            {"metric": ",".join(metrics)},
            timeout=30,
        )
        if insights_response.ok:
            insight_payload = insights_response.json() or {}
            snapshot["insights"] = {
                str(item.get("name") or "").strip(): item.get("values", [{}])[0].get("value")
                for item in insight_payload.get("data", [])
                if str(item.get("name") or "").strip()
            }

        return snapshot

    def _graph_post(self, path: str, data: dict[str, str], timeout: int) -> requests.Response:
        payload = dict(data)
        payload["access_token"] = self._access_token
        return requests.post(f"{GRAPH_BASE}{path}", data=payload, timeout=timeout)

    def _graph_get(self, path: str, params: dict[str, str], timeout: int) -> requests.Response:
        payload = dict(params)
        payload["access_token"] = self._access_token
        return requests.get(f"{GRAPH_BASE}{path}", params=payload, timeout=timeout)

    def _post_image_media(
        self,
        ig_user_id: str,
        staged_media: InstagramStagedMedia,
        *,
        caption: str | None,
        media_type: str | None,
        container_error_message: str,
    ) -> InstagramPublishResult:
        response = self._create_image_container(ig_user_id, staged_media.public_url, caption=caption, media_type=media_type)
        if not response.ok and self._is_media_download_failure(response):
            fallback_url, fallback_error = self._upload_external_fallback(staged_media.local_path)
            if fallback_url:
                log.warning(
                    "Instagram-Medienabruf ueber %s fehlgeschlagen; versuche externen Fallback ueber %s",
                    staged_media.public_url,
                    fallback_url,
                )
                response = self._create_image_container(ig_user_id, fallback_url, caption=caption, media_type=media_type)
            elif fallback_error:
                log.warning("Instagram-URL-Fallback konnte nicht hochgeladen werden: %s", fallback_error)

        if not response.ok:
            return self._build_error_result(response)

        container_id = str((response.json() or {}).get("id") or "").strip()
        if not container_id:
            return InstagramPublishResult(success=False, error=container_error_message)

        ready, ready_error = self._wait_for_container(container_id)
        if not ready:
            return InstagramPublishResult(success=False, error=ready_error)

        return self._publish_container(ig_user_id, container_id)

    def _create_image_container(
        self,
        ig_user_id: str,
        image_url: str,
        *,
        caption: str | None,
        media_type: str | None,
    ) -> requests.Response:
        payload = {"image_url": image_url}
        if caption:
            payload["caption"] = caption
        if media_type:
            payload["media_type"] = media_type
        return self._graph_post(f"/{ig_user_id}/media", payload, timeout=60)

    def _publish_container(self, ig_user_id: str, container_id: str) -> InstagramPublishResult:
        response = self._graph_post(
            f"/{ig_user_id}/media_publish",
            {"creation_id": container_id},
            timeout=60,
        )
        if not response.ok:
            return self._build_error_result(response)

        media_id = str((response.json() or {}).get("id") or "").strip()
        if not media_id:
            return InstagramPublishResult(success=False, error="Instagram-Medium wurde ohne Media-ID bestaetigt.")
        return InstagramPublishResult(success=True, media_id=media_id)

    def _wait_for_container(self, container_id: str) -> tuple[bool, str | None]:
        timeout_seconds = max(int(self.config.instagram.container_check_timeout_seconds), 5)
        interval_seconds = max(float(self.config.instagram.container_check_interval_seconds), 1.0)
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            try:
                response = requests.get(
                    f"{GRAPH_BASE}/{container_id}",
                    params={
                        "fields": "status_code,status",
                        "access_token": self._access_token,
                    },
                    timeout=30,
                )
            except Exception as exc:
                return False, str(exc)

            if not response.ok:
                message, _, _ = self._parse_graph_error(response)
                return False, message

            payload = response.json() or {}
            status_code = str(payload.get("status_code") or payload.get("status") or "").upper().strip()
            if status_code in {"FINISHED", "PUBLISHED"}:
                return True, None
            if status_code in {"ERROR", "EXPIRED"}:
                return False, f"Instagram-Container Status: {status_code}"
            time.sleep(interval_seconds)

        return False, "Instagram-Container wurde nicht rechtzeitig bereitgestellt."

    def _upload_video(self, upload_url: str, video_path: Path) -> tuple[bool, str | None]:
        headers = {
            "Authorization": f"OAuth {self._access_token}",
            "offset": "0",
            "file_size": str(video_path.stat().st_size),
        }
        try:
            with open(video_path, "rb") as handle:
                response = requests.post(upload_url, headers=headers, data=handle, timeout=600)
        except Exception as exc:
            return False, str(exc)

        if response.ok:
            return True, None

        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        return False, str(payload)

    def _stage_public_image(self, image_path: Path, prefix: str) -> tuple[InstagramStagedMedia | None, str | None]:
        public_base_url = str(self.config.instagram.public_base_url or "").strip().rstrip("/")
        if not public_base_url:
            return None, "Instagram-Bild-/Story-Posting braucht instagram.public_base_url."
        public_path_prefix = "/" + str(self.config.instagram.public_path_prefix or "/public-media").strip().strip("/")
        if not image_path.exists():
            return None, f"Datei nicht gefunden: {image_path}"

        staging_folder = self.config.instagram.staging_folder
        staging_folder.mkdir(parents=True, exist_ok=True)
        target_name = f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:10]}.jpg"
        target_path = staging_folder / target_name

        try:
            self._prepare_image(image_path, target_path)
        except Exception as exc:
            return None, f"Instagram-Bild konnte nicht vorbereitet werden: {exc}"

        self._prune_staged_files(staging_folder)
        public_url = f"{public_base_url}{public_path_prefix}/{target_name}"
        remote_path: str | None = None
        if self._remote_staging_enabled():
            remote_path, upload_error = self._upload_to_remote_staging(target_path)
            if upload_error:
                self._cleanup_staged_file(InstagramStagedMedia(public_url=public_url, local_path=target_path, remote_path=remote_path))
                return None, upload_error

        staged_media = InstagramStagedMedia(public_url=public_url, local_path=target_path, remote_path=remote_path)
        self._log_staged_file(staged_media)
        return staged_media, None

    def _prepare_image(self, source_path: Path, target_path: Path):
        watermark_cfg = self.config.watermark
        with Image.open(source_path) as source_image:
            canvas = source_image.convert("RGBA")
            if watermark_cfg.enabled and watermark_cfg.image_path.exists():
                with Image.open(watermark_cfg.image_path) as watermark_image:
                    watermark = watermark_image.convert("RGBA")
                target_width = max(1, int(canvas.width * max(watermark_cfg.width_ratio, 0.02)))
                scale = target_width / max(watermark.width, 1)
                target_height = max(1, int(watermark.height * scale))
                resample = getattr(Image, "Resampling", Image).LANCZOS
                watermark = watermark.resize((target_width, target_height), resample)

                if watermark_cfg.opacity < 1.0:
                    alpha = watermark.getchannel("A")
                    alpha = alpha.point(lambda value: int(value * max(0.0, min(watermark_cfg.opacity, 1.0))))
                    watermark.putalpha(alpha)

                margin = max(0, watermark_cfg.margin_px)
                x = margin
                y = margin
                if watermark_cfg.position in {"top-right", "bottom-right"}:
                    x = max(margin, canvas.width - watermark.width - margin)
                if watermark_cfg.position in {"bottom-left", "bottom-right"}:
                    y = max(margin, canvas.height - watermark.height - margin)
                canvas.alpha_composite(watermark, (x, y))

            background = Image.new("RGB", canvas.size, (255, 255, 255))
            background.paste(canvas, mask=canvas.getchannel("A"))
            background.save(target_path, format="JPEG", quality=95, optimize=True)

    def _prune_staged_files(self, staging_folder: Path):
        self._prune_expired_staged_files(staging_folder)
        keep_files = max(int(self.config.instagram.keep_files), 10)
        files = sorted(
            [item for item in staging_folder.iterdir() if item.is_file()],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale_file in files[keep_files:]:
            try:
                stale_file.unlink(missing_ok=True)
            except Exception:
                log.warning("Instagram-Staging-Datei konnte nicht geloescht werden: %s", stale_file)

    def _prune_expired_staged_files(self, staging_folder: Path):
        if not self.config.instagram.auto_cleanup_enabled:
            return

        ttl_seconds = max(int(self.config.instagram.cleanup_ttl_seconds), 60)
        cutoff = time.time() - ttl_seconds
        for staged_file in staging_folder.iterdir():
            if not staged_file.is_file():
                continue
            try:
                if staged_file.stat().st_mtime <= cutoff:
                    staged_file.unlink(missing_ok=True)
            except Exception:
                log.warning("Abgelaufene Instagram-Staging-Datei konnte nicht geloescht werden: %s", staged_file)

    def _cleanup_staged_file(self, staged_media: InstagramStagedMedia | None):
        if not self.config.instagram.auto_cleanup_enabled or staged_media is None:
            return

        try:
            if staged_media.remote_path:
                self._delete_remote_staged_file(staged_media.remote_path)

            existed_before_cleanup = staged_media.local_path.exists()
            staged_media.local_path.unlink(missing_ok=True)
            log.info(
                "Instagram-Staging-Datei bereinigt: path=%s remote_path=%s existed_before_cleanup=%s exists_after_cleanup=%s",
                staged_media.local_path.resolve(),
                staged_media.remote_path,
                existed_before_cleanup,
                staged_media.local_path.exists(),
            )
        except Exception:
            log.warning("Instagram-Staging-Datei konnte nach Upload nicht geloescht werden: %s", staged_media.local_path)

    def _log_staged_file(self, staged_media: InstagramStagedMedia):
        try:
            resolved_path = staged_media.local_path.resolve()
            exists = staged_media.local_path.exists()
            details = {
                "path": str(resolved_path),
                "name": staged_media.local_path.name,
                "public_url": staged_media.public_url,
                "remote_path": staged_media.remote_path,
                "exists": exists,
            }
            if exists:
                stats = staged_media.local_path.stat()
                details.update(
                    {
                        "size_bytes": stats.st_size,
                        "mode": stat.filemode(stats.st_mode),
                        "mtime": int(stats.st_mtime),
                    }
                )
            log.info("Instagram-Staging-Datei erstellt: %s", details)
        except Exception as exc:
            log.warning("Instagram-Staging-Datei konnte nicht protokolliert werden: %s", exc)

    def _remote_staging_enabled(self) -> bool:
        cfg = self.config.instagram
        return bool(cfg.remote_staging_enabled and cfg.remote_host and cfg.remote_user and cfg.remote_path)

    def _upload_to_remote_staging(self, local_path: Path) -> tuple[str | None, str | None]:
        cfg = self.config.instagram
        remote_dir = str(cfg.remote_path).rstrip("/")
        remote_path = f"{remote_dir}/{local_path.name}"
        ssh_options = [
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]

        mkdir_cmd = [
            "ssh",
            *ssh_options,
            "-p",
            str(cfg.remote_ssh_port),
            f"{cfg.remote_user}@{cfg.remote_host}",
            f"mkdir -p {remote_dir}",
        ]
        mkdir_result = subprocess.run(mkdir_cmd, capture_output=True, text=True, timeout=60, check=False)
        if mkdir_result.returncode != 0:
            return None, (mkdir_result.stderr or mkdir_result.stdout or "Remote-Staging-Ordner konnte nicht erstellt werden.").strip()

        if cfg.remote_upload_method != "scp":
            return None, f"Nicht unterstuetzte Upload-Methode fuer Remote-Staging: {cfg.remote_upload_method}"

        copy_cmd = [
            "scp",
            *ssh_options,
            "-P",
            str(cfg.remote_ssh_port),
            str(local_path),
            f"{cfg.remote_user}@{cfg.remote_host}:{remote_path}",
        ]
        copy_result = subprocess.run(copy_cmd, capture_output=True, text=True, timeout=120, check=False)
        if copy_result.returncode != 0:
            return remote_path, (copy_result.stderr or copy_result.stdout or "Remote-Staging-Upload fehlgeschlagen.").strip()

        return remote_path, None

    def _delete_remote_staged_file(self, remote_path: str):
        cfg = self.config.instagram
        ssh_options = [
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]
        delete_cmd = [
            "ssh",
            *ssh_options,
            "-p",
            str(cfg.remote_ssh_port),
            f"{cfg.remote_user}@{cfg.remote_host}",
            f"rm -f {remote_path}",
        ]
        result = subprocess.run(delete_cmd, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            log.warning("Remote-Instagram-Staging-Datei konnte nicht geloescht werden: %s", (result.stderr or result.stdout or remote_path).strip())

    def _upload_external_fallback(self, local_path: Path) -> tuple[str | None, str | None]:
        cfg = self.config.instagram
        if not cfg.external_url_fallback_enabled:
            return None, None

        provider = str(cfg.external_url_fallback_provider or "").strip().lower()
        if provider != "litterbox":
            return None, f"Nicht unterstuetzter Instagram-URL-Fallback-Provider: {provider}"

        mime_type = mimetypes.guess_type(local_path.name)[0] or "image/jpeg"
        try:
            with open(local_path, "rb") as handle:
                response = requests.post(
                    "https://litterbox.catbox.moe/resources/internals/api.php",
                    data={
                        "reqtype": "fileupload",
                        "time": cfg.external_url_fallback_expiry,
                    },
                    files={
                        "fileToUpload": (local_path.name, handle, mime_type),
                    },
                    timeout=120,
                )
        except Exception as exc:
            return None, str(exc)

        if not response.ok:
            return None, (response.text or f"HTTP {response.status_code}").strip()

        fallback_url = response.text.strip()
        if not fallback_url.startswith("http"):
            return None, f"Ungueltige Fallback-URL erhalten: {fallback_url}"
        return fallback_url, None

    def _is_media_download_failure(self, response: requests.Response) -> bool:
        try:
            payload = response.json() or {}
        except ValueError:
            return False

        error = payload.get("error") or {}
        return error.get("code") == 9004 and error.get("error_subcode") == 2207052

    def _build_error_result(self, response: requests.Response) -> InstagramPublishResult:
        message, permanent, _ = self._parse_graph_error(response)
        return InstagramPublishResult(success=False, error=message, permanent=permanent)

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
        auth_error = code == 190
        if auth_error and (subcode == 463 or "Session has expired" in message):
            normalized_message = "Instagram-Zugriffstoken abgelaufen. Bitte ein neues Token fuer instagram.access_token hinterlegen."
        elif auth_error:
            normalized_message = "Instagram-Zugriffstoken ungueltig. Bitte instagram.access_token pruefen oder erneuern."
        elif code == 9004 and subcode == 2207052:
            normalized_message = (
                "Instagram kann die Bild-/Story-URL derzeit nicht als gueltige Mediendatei abrufen. "
                "Die URL ist oeffentlich erreichbar, erfuellt aber aktuell nicht Metas Abrufanforderungen. "
                "Pruefe den externen /ig-tmp-Host, TLS/Weiterleitungen und die direkte Auslieferung fuer Meta-Downloader."
            )

        permanent = auth_error or response.status_code in {401, 403}
        return normalized_message, permanent, ()