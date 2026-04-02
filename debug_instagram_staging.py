from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests

from config import load_settings
from instagram_poster import InstagramPoster


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Erzeugt eine Instagram-Staging-Datei fuer Debug-Zwecke.")
    parser.add_argument(
        "image",
        nargs="?",
        default="/opt/social/images/Maerchen_20260326-131240_001.png",
        help="Absoluter oder relativer Pfad zum Testbild.",
    )
    parser.add_argument(
        "--prefix",
        default="debug-ig-stage",
        help="Praefix fuer den erzeugten Dateinamen.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Bereinigt die erzeugte Datei nach dem Debug-Lauf wieder.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_settings()
    image_path = Path(args.image).expanduser()
    if not image_path.is_absolute():
        image_path = (Path.cwd() / image_path).resolve()

    poster = InstagramPoster(config)
    staged_media, stage_error = poster._stage_public_image(image_path, prefix=args.prefix)
    payload = {
        "input_image": str(image_path),
        "staging_folder": str(config.instagram.staging_folder.resolve()),
        "public_url": staged_media.public_url if staged_media else None,
        "staged_path": str(staged_media.local_path.resolve()) if staged_media else None,
        "remote_path": staged_media.remote_path if staged_media else None,
        "stage_error": stage_error,
    }

    if staged_media is not None and staged_media.local_path.exists():
        stats = staged_media.local_path.stat()
        payload["exists_after_stage"] = True
        payload["size_bytes"] = stats.st_size
        payload["mode_octal"] = oct(stats.st_mode & 0o777)
        payload["filename"] = staged_media.local_path.name
        try:
            response = requests.get(staged_media.public_url or "", timeout=30)
            payload["http_status"] = response.status_code
            payload["http_content_type"] = response.headers.get("content-type")
        except Exception as exc:
            payload["http_error"] = str(exc)
    else:
        payload["exists_after_stage"] = False

    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.cleanup and staged_media is not None:
        poster._cleanup_staged_file(staged_media)
        print(json.dumps({
            "cleanup_requested": True,
            "exists_after_cleanup": staged_media.local_path.exists(),
            "staged_path": str(staged_media.local_path.resolve()),
            "remote_path": staged_media.remote_path,
        }, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())