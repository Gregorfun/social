"""
Dashboard – Social Media Auto-Poster
=====================================
Startet einen lokalen Webserver (http://localhost:5000) mit Echtzeit-Übersicht:
  - Letztes & nächstes Bild
  - Post-Verlauf
  - Live-Log
  - Poster starten / stoppen

Start: python dashboard.py
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import base64
import mimetypes
import random
from pathlib import Path
from datetime import datetime, timedelta, time
import time as time_module

from flask import Flask, jsonify, render_template_string, send_file, abort, request
from dotenv import load_dotenv
from caption_generator import CaptionGenerator
from post_history import PostHistory

CONFIG_FILE = Path(__file__).parent / "config.json"
STATE_FILE  = Path(__file__).parent / "state.json"
LOG_FILE    = Path(__file__).parent / "poster.log"

load_dotenv(Path(__file__).parent / ".env")

app = Flask(__name__)

_poster_proc: subprocess.Popen | None = None
_poster_lock = threading.Lock()
_instagram_monitor_cache: dict[str, object] = {"timestamp": 0.0, "payload": None}
_IGNORED_THEME_PREFIXES = {"alle", "auto"}


def _dashboard_auth_enabled() -> bool:
  return bool(os.getenv("DASHBOARD_USERNAME") and os.getenv("DASHBOARD_PASSWORD"))


def _poster_systemd_service() -> str:
  return os.getenv("POSTER_SYSTEMD_SERVICE", "").strip()


def _run_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    ["systemctl", *args],
    capture_output=True,
    text=True,
    check=False,
  )


@app.before_request
def require_basic_auth():
  if request.path.startswith("/public-media/") or request.path.startswith("/ig-tmp/"):
    return None

  if not _dashboard_auth_enabled():
    return None

  auth = request.authorization
  username = os.getenv("DASHBOARD_USERNAME", "")
  password = os.getenv("DASHBOARD_PASSWORD", "")
  if auth and auth.username == username and auth.password == password:
    return None

  return app.response_class(
    "Authentifizierung erforderlich.",
    401,
    {"WWW-Authenticate": 'Basic realm="Social Dashboard"'},
  )


def default_state() -> dict:
  return {
    "last_index": -1,
    "last_file": None,
    "next_image": None,
    "cycle_posted": [],
    "posted": [],
    "image_registry": {},
    "slot_runs": {},
    "generated_reels": [],
    "auto_comment_history": [],
    "auto_comment_cache": [],
    "auto_comment_metrics": {},
    "campaign_state": {},
    "queue_state": {},
    "content_quality": {},
    "engagement_actions": {},
    "outreach_assist": {"items": []},
  }


# --------------------------------------------------------------------------- #
# Hilfsfunktionen
# --------------------------------------------------------------------------- #
def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: Path, payload: dict):
  with open(path, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, ensure_ascii=False)


def prune_generated_reels(state: dict, cfg: dict, keep_last: int = 20):
  reel_cfg = cfg.get("reels") or {}
  output_folder = Path(str(reel_cfg.get("output_folder") or (Path(__file__).parent / "generated_reels")))
  PostHistory(STATE_FILE).prune_generated_reels(state, output_folder, keep_last=keep_last)


def load_state() -> dict:
  state = load_json(STATE_FILE, default_state())
  for key, value in default_state().items():
    state.setdefault(key, value)
  return state


def build_auto_comment_stats(state: dict) -> dict:
  metrics = state.get("auto_comment_metrics", {}) or {}
  history = list(reversed(state.get("auto_comment_history", [])))[:8]
  cache = state.get("auto_comment_cache", []) or []
  return {
    "template_used": int(metrics.get("template_used", 0) or 0),
    "ollama_used": int(metrics.get("ollama_used", 0) or 0),
    "ollama_generated": int(metrics.get("ollama_generated", 0) or 0),
    "ollama_filtered": int(metrics.get("ollama_filtered", 0) or 0),
    "cache_hits": int(metrics.get("cache_hits", 0) or 0),
    "template_fallbacks": int(metrics.get("template_fallbacks", 0) or 0),
    "cache_size": len(cache),
    "recent": history,
  }


def resolve_dashboard_campaign_context(cfg: dict, state: dict, now: datetime | None = None) -> dict:
  now = now or datetime.now()
  campaign_cfg = cfg.get("campaigns") or {}
  campaign_state = state.get("campaign_state") or {}
  override_theme = str(((campaign_cfg.get("daily_theme_overrides") or {}).get(now.date().isoformat())) or "").strip().lower()
  if override_theme:
    return {
      "active_campaign": "Tages-Thema",
      "active_theme": override_theme,
      "mode": "theme",
      "override_campaign": str(campaign_state.get("campaign_override") or "").strip(),
    }

  weekday_key = now.strftime("%A").lower()
  weekday_mode = str(((campaign_cfg.get("weekday_modes") or {}).get(weekday_key, "theme")) or "theme").strip().lower()
  if weekday_mode not in {"theme", "mix"}:
    weekday_mode = "theme"

  override_campaign = str(campaign_state.get("campaign_override") or "").strip()
  configured_campaigns = [item for item in (campaign_cfg.get("campaigns") or []) if isinstance(item, dict)]
  target_campaign = None
  if override_campaign:
    target_campaign = next((item for item in configured_campaigns if str(item.get("name") or "").strip() == override_campaign), None)
  if target_campaign is None:
    active_name = str(campaign_cfg.get("active_campaign_name") or "").strip()
    if active_name:
      target_campaign = next((item for item in configured_campaigns if str(item.get("name") or "").strip() == active_name), None)
  if target_campaign is None:
    for item in configured_campaigns:
      start_date = str(item.get("start_date") or "").strip()
      end_date = str(item.get("end_date") or "").strip()
      if start_date and now.date().isoformat() < start_date:
        continue
      if end_date and now.date().isoformat() > end_date:
        continue
      target_campaign = item
      break
  if target_campaign is None and bool(campaign_cfg.get("auto_rotate", False)) and configured_campaigns:
    target_campaign = configured_campaigns[now.toordinal() % len(configured_campaigns)]

  active_campaign = str(campaign_state.get("active_campaign") or "").strip()
  active_theme = str(campaign_state.get("active_theme") or "").strip().lower()
  if weekday_mode == "mix":
    return {
      "active_campaign": "Querbeet",
      "active_theme": "",
      "mode": "mix",
      "override_campaign": override_campaign,
    }

  if target_campaign:
    themes = [str(theme).strip().lower() for theme in target_campaign.get("themes", []) if str(theme).strip()]
    days_per_theme = max(1, int(target_campaign.get("days_per_theme", campaign_cfg.get("default_days_per_theme", 2)) or 2))
    if themes:
      active_campaign = str(target_campaign.get("name") or "").strip() or active_campaign
      active_theme = themes[((now.date().toordinal()) // days_per_theme) % len(themes)]

  return {
    "active_campaign": active_campaign,
    "active_theme": active_theme,
    "mode": "theme",
    "override_campaign": override_campaign,
  }


def build_outreach_assist_overview(state: dict) -> dict:
  items = list(reversed(((state.get("outreach_assist") or {}).get("items") or [])))[:12]
  used = sum(1 for item in items if str(item.get("status") or "") == "used")
  pending = sum(1 for item in items if str(item.get("status") or "pending") == "pending")
  skipped = sum(1 for item in items if str(item.get("status") or "") == "skipped")
  return {
    "used": used,
    "pending": pending,
    "skipped": skipped,
    "items": items,
  }


_OUTREACH_STOPWORDS = {
  "und", "oder", "aber", "eine", "einer", "einem", "einen", "der", "die", "das", "den", "dem",
  "mit", "von", "für", "fuer", "zum", "zur", "ist", "sind", "war", "noch", "schon", "hier",
  "auch", "wirklich", "dieses", "dieser", "diese", "post", "bild", "video", "reel", "look",
  "sehr", "mehr", "weniger", "einfach", "gerade", "fast", "bald", "echt", "oder", "ki", "ai",
}


def _extract_outreach_keywords(text: str, limit: int = 3) -> list[str]:
  words = re.findall(r"[a-zA-ZäöüÄÖÜß0-9_]+", (text or "").lower())
  seen: list[str] = []
  for word in words:
    if len(word) < 4 or word in _OUTREACH_STOPWORDS:
      continue
    if word not in seen:
      seen.append(word)
    if len(seen) >= limit:
      break
  return seen


def _build_outreach_suggestions(platform: str, creator_handle: str, post_caption: str, theme: str, note: str) -> list[str]:
  context = " / ".join(part for part in [theme.strip(), *(_extract_outreach_keywords(post_caption)[:2])] if part).strip(" /")
  focus = context or "Bildstimmung"
  closers = [
    "Das bleibt direkt haengen.",
    "So etwas stoppt sofort den Scroll.",
    "Die Wirkung ist richtig stark.",
    "Das wirkt ungewoehnlich sauber gebaut.",
    "Der Look bleibt direkt im Kopf.",
  ]
  lead_ins = [
    f"Die {focus} hier ist richtig stark.",
    f"Gerade die {focus} macht den Post spannend.",
    f"Der {focus}-Vibe funktioniert hier sofort.",
    f"Bei {focus} passt die Stimmung komplett.",
    f"Das Detailgefuehl bei {focus} ist echt stark.",
  ]
  specifics = [
    "Vor allem Licht und Ausstrahlung passen zusammen.",
    "Gerade die ruhige Wirkung macht es spannend.",
    "Die Komposition fuehlt sich sehr stimmig an.",
    "Vor allem die Details ziehen den Blick an.",
    "Genau so etwas bleibt beim zweiten Hinschauen haengen.",
  ]
  if creator_handle.strip():
    specifics.append(f"Passt stark zum Stil von {creator_handle.strip()}.")
  if note.strip():
    specifics.append(f"Vor allem {note.strip()[:60]} wirkt hier gut eingebaut.")

  combinations: list[str] = []
  for idx in range(5):
    lead = lead_ins[idx % len(lead_ins)]
    detail = specifics[idx % len(specifics)]
    close = closers[idx % len(closers)]
    combinations.append(f"{lead} {detail} {close}")

  random.seed(f"{platform}|{creator_handle}|{post_caption}|{theme}|{note}")
  random.shuffle(combinations)
  selected: list[str] = []
  for comment in combinations:
    if comment not in selected:
      selected.append(comment)
    if len(selected) >= 3:
      break
  return selected


def build_campaign_overview(cfg: dict, state: dict) -> dict:
  campaign_cfg = cfg.get("campaigns") or {}
  campaign_state = state.get("campaign_state") or {}
  resolved_context = resolve_dashboard_campaign_context(cfg, state)
  active_campaign = str(resolved_context.get("active_campaign") or "").strip()
  active_theme = str(resolved_context.get("active_theme") or "").strip()
  override_campaign = str(resolved_context.get("override_campaign") or "").strip()
  current_mode = str(resolved_context.get("mode") or "theme").strip()

  theme_counts: dict[str, int] = {}
  for entry in state.get("posted", []):
    campaign = entry.get("campaign") or {}
    theme = str(campaign.get("theme") or "").strip()
    if theme:
      theme_counts[theme] = theme_counts.get(theme, 0) + 1

  ph = PostHistory(STATE_FILE)
  configured_campaigns = []
  for item in campaign_cfg.get("campaigns", []):
    if not isinstance(item, dict):
      continue
    name = str(item.get("name") or "").strip()
    progress = ph.compute_campaign_progress(state, name) if name else {"feed_posts": 0, "stories": 0, "reels": 0}
    configured_campaigns.append({
      "name": name,
      "themes": [str(theme).strip() for theme in item.get("themes", []) if str(theme).strip()],
      "preferred_slots": [str(slot).strip() for slot in item.get("preferred_slots", []) if str(slot).strip()],
      "targets": {
        "feed_posts": int(item.get("target_feed_posts", 0) or 0),
        "stories": int(item.get("target_stories", 0) or 0),
        "reels": int(item.get("target_reels", 0) or 0),
      },
      "progress": progress,
    })

  theme_calendar: list[dict] = []
  if configured_campaigns:
    active_definition = next((item for item in configured_campaigns if item["name"] == (override_campaign or active_campaign)), configured_campaigns[0])
    themes = active_definition.get("themes", []) or [active_theme]
    days_per_theme = max(1, int(next((item.get("days_per_theme", 2) for item in campaign_cfg.get("campaigns", []) if str(item.get("name") or "") == active_definition["name"]), campaign_cfg.get("default_days_per_theme", 2)) or 2))
    for offset in range(7):
      day = datetime.now().date() + timedelta(days=offset)
      override_theme = str(((campaign_cfg.get("daily_theme_overrides") or {}).get(day.isoformat())) or "").strip().lower()
      weekday_key = day.strftime("%A").lower()
      weekday_mode = str(((campaign_cfg.get("weekday_modes") or {}).get(weekday_key, "theme")) or "theme").strip().lower()
      if override_theme:
        theme_calendar.append({"date": day.isoformat(), "theme": override_theme, "mode": "theme", "source": "override"})
        continue
      if weekday_mode == "mix":
        theme_calendar.append({"date": day.isoformat(), "theme": "Querbeet", "mode": "mix", "source": "weekday_mode"})
        continue
      theme = themes[((day.toordinal()) // days_per_theme) % max(len(themes), 1)] if themes else active_theme
      theme_calendar.append({"date": day.isoformat(), "theme": theme, "mode": "theme", "source": "campaign"})

  return {
    "enabled": bool(campaign_cfg.get("enabled", False)),
    "active_campaign": active_campaign,
    "override_campaign": override_campaign,
    "active_theme": active_theme,
    "current_mode": current_mode,
    "last_updated_at": campaign_state.get("last_updated_at"),
    "fallback_to_detected_themes": bool(campaign_cfg.get("fallback_to_detected_themes", True)),
    "configured_campaigns": configured_campaigns,
    "theme_post_counts": theme_counts,
    "theme_calendar": theme_calendar,
  }


def build_smart_slot_overview(cfg: dict, state: dict) -> dict:
  smart_cfg = cfg.get("smart_slots") or {}
  smart_state = state.get("smart_slot_state") or {}
  return {
    "enabled": bool(smart_cfg.get("enabled", False)),
    "exploration_rate": float(smart_cfg.get("exploration_rate", 0.0) or 0.0),
    "last_applied_slots": list(smart_state.get("last_applied_slots", []) or []),
    "last_sources": dict(smart_state.get("last_sources", {}) or {}),
    "last_updated_at": smart_state.get("last_updated_at"),
  }


def build_content_quality_overview(state: dict) -> dict:
  quality_state = state.get("content_quality") or {}
  registry = state.get("image_registry") or {}
  diagnostics = list(quality_state.get("diagnostics", []) or [])
  duplicates = [
    {"file": name, "duplicate_of": meta.get("duplicate_of"), "quality_score": meta.get("quality_score")}
    for name, meta in registry.items()
    if meta.get("duplicate_of")
  ]
  lowest_quality = sorted(
    [
      {"file": name, "quality_score": int(meta.get("quality_score", 0) or 0), "theme": meta.get("theme", "")}
      for name, meta in registry.items()
      if "quality_score" in meta
    ],
    key=lambda item: item["quality_score"],
  )[:8]
  return {
    "last_scan_at": quality_state.get("last_scan_at"),
    "diagnostics": diagnostics[-20:],
    "duplicates": duplicates[:20],
    "lowest_quality": lowest_quality,
  }


def build_engagement_actions_overview(state: dict) -> dict:
  actions = state.get("engagement_actions") or {}
  return {
    "alerts": list(reversed(actions.get("alerts", []) or []))[:12],
    "recycle_queue": list(reversed(actions.get("recycle_queue", []) or []))[:12],
    "followup_comments": list(reversed(actions.get("followup_comments", []) or []))[:12],
  }


def infer_dashboard_image_theme(filename: str, separator: str = "_") -> str:
  stem = Path(str(filename or "")).stem
  if not stem:
    return ""
  if separator and separator in stem:
    theme = stem.split(separator, maxsplit=1)[0]
  else:
    theme = re.split(r"[-\s]+", stem, maxsplit=1)[0]
  theme = re.sub(r"\d+$", "", theme).strip(" _-").lower()
  if theme in _IGNORED_THEME_PREFIXES:
    return ""
  return theme


def _instagram_recent_items_from_state(state: dict, limit: int = 12) -> list[dict]:
  items: list[dict] = []

  for entry in state.get("posted", []):
    media_id = str(entry.get("instagram_post_id") or "").strip()
    if not media_id:
      continue
    items.append({
      "media_id": media_id,
      "content_type": "image",
      "file": str(entry.get("file") or "").strip(),
      "caption": str(entry.get("caption") or "").strip(),
      "time": str(entry.get("time") or "").strip(),
      "slot": str(entry.get("slot") or "").strip(),
      "platform_message": str(((entry.get("platform_results") or {}).get("instagram") or {}).get("message") or "").strip(),
    })

  for entry in state.get("generated_reels", []):
    media_id = str(entry.get("instagram_post_id") or "").strip()
    if not media_id:
      continue
    items.append({
      "media_id": media_id,
      "content_type": "reel",
      "file": str(entry.get("image_name") or Path(str(entry.get("reel_path") or "")).name),
      "caption": str(entry.get("caption") or "").strip(),
      "time": str(entry.get("time") or "").strip(),
      "slot": str(entry.get("slot") or "").strip(),
      "platform_message": str(((entry.get("platform_results") or {}).get("instagram") or {}).get("message") or "").strip(),
    })

  for entry in state.get("generated_stories", []):
    media_id = str(entry.get("instagram_post_id") or "").strip()
    if not media_id:
      continue
    items.append({
      "media_id": media_id,
      "content_type": "story",
      "file": Path(str(entry.get("story_path") or "story")).name,
      "caption": str(entry.get("text") or "").strip(),
      "time": str(entry.get("time") or "").strip(),
      "slot": str(entry.get("slot") or "").strip(),
      "platform_message": str(((entry.get("platform_results") or {}).get("instagram") or {}).get("message") or "").strip(),
    })

  items.sort(key=lambda item: item.get("time") or "", reverse=True)
  return items[:limit]


def build_instagram_monitor_payload(force_refresh: bool = False) -> dict:
  now = time_module.time()
  cached_payload = _instagram_monitor_cache.get("payload")
  cached_at = float(_instagram_monitor_cache.get("timestamp") or 0.0)
  if not force_refresh and cached_payload is not None and (now - cached_at) < 60:
    return cached_payload  # type: ignore[return-value]

  from config import load_settings
  from instagram_poster import InstagramPoster

  settings = load_settings()
  state = load_state()
  history_items = _instagram_recent_items_from_state(state)

  payload = {
    "enabled": bool(settings.instagram.enabled),
    "publish_posts": bool(settings.instagram.publish_posts),
    "publish_reels": bool(settings.instagram.publish_reels),
    "publish_stories": bool(settings.instagram.publish_stories),
    "username": str(settings.instagram.username or "").strip(),
    "business_account_id": str(settings.instagram.business_account_id or "").strip(),
    "public_base_url": str(settings.instagram.public_base_url or "").strip(),
    "public_path_prefix": str(settings.instagram.public_path_prefix or "").strip(),
    "remote_staging_enabled": bool(settings.instagram.remote_staging_enabled),
    "remote_target": f"{settings.instagram.remote_user}@{settings.instagram.remote_host}:{settings.instagram.remote_path}" if settings.instagram.remote_staging_enabled else "",
    "profile": {},
    "recent_media": [],
    "totals": {
      "tracked_media": len(history_items),
      "images": sum(1 for item in history_items if item.get("content_type") == "image"),
      "reels": sum(1 for item in history_items if item.get("content_type") == "reel"),
      "stories": sum(1 for item in history_items if item.get("content_type") == "story"),
    },
    "last_updated": datetime.now().isoformat(),
  }

  if settings.instagram.enabled:
    poster = InstagramPoster(settings)
    payload["profile"] = poster.fetch_account_overview()
    recent_media = []
    for item in history_items[:8]:
      snapshot = poster.fetch_media_snapshot(str(item.get("media_id") or ""))
      recent_media.append({**item, **snapshot})
    payload["recent_media"] = recent_media
  else:
    payload["profile"] = {"enabled": False, "error": "Instagram ist deaktiviert."}

  _instagram_monitor_cache["timestamp"] = now
  _instagram_monitor_cache["payload"] = payload
  return payload


def get_recent_reels(state: dict, limit: int = 12) -> list[dict]:
  reels = state.get("generated_reels", [])
  return list(reversed(reels))[:limit]


def get_last_reel(state: dict) -> dict | None:
  reels = state.get("generated_reels", [])
  if not reels:
    return None
  return reels[-1]


def get_reel_control(state: dict) -> dict:
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


def clear_reel_preview(control: dict):
  preview_path = control.get("preview_path")
  if preview_path:
    try:
      Path(preview_path).unlink(missing_ok=True)
    except Exception:
      pass
  control["preview_path"] = None
  control["preview_updated_at"] = None


def clear_reel_plan(control: dict):
  control["planned_source_images"] = []
  control["planned_anchor_image"] = None
  control["planned_updated_at"] = None
  control["planned_caption"] = ""
  control["planned_caption_source"] = None
  control["planned_caption_updated_at"] = None


def _saved_reel_plan_matches_last_reel(state: dict, control: dict) -> bool:
  last_reel = get_last_reel(state) or {}
  planned_images = [str(name) for name in control.get("planned_source_images", []) if str(name)]
  if not planned_images:
    return False

  last_sources = [str(name) for name in last_reel.get("source_images", []) if str(name)]
  if last_sources and planned_images == last_sources:
    return True

  planned_anchor = str(control.get("planned_anchor_image") or "").strip()
  last_anchor = str(last_reel.get("image_name") or "").strip()
  return bool(planned_anchor and last_anchor and planned_anchor == last_anchor)


def _preview_matches_saved_plan(control: dict) -> bool:
  preview_path = str(control.get("preview_path") or "").strip()
  planned_anchor = str(control.get("planned_anchor_image") or "").strip()
  if not preview_path or not planned_anchor:
    return False

  preview_file = Path(preview_path)
  if not preview_file.exists():
    return False

  anchor_stem = Path(planned_anchor).stem
  if not preview_file.stem.startswith(f"{anchor_stem}-reel-"):
    return False

  preview_updated_at = str(control.get("preview_updated_at") or "").strip()
  planned_updated_at = str(control.get("planned_updated_at") or "").strip()
  if planned_updated_at and preview_updated_at and preview_updated_at < planned_updated_at:
    return False

  return True


def regenerate_reel_caption(cfg: dict, state: dict, images: list[Path]) -> dict:
  control = get_reel_control(state)
  control["caption_override"] = ""
  control["planned_caption"] = ""
  control["planned_caption_source"] = None
  control["planned_caption_updated_at"] = None
  return build_next_reel_plan(cfg, state, images)


def _is_saved_reel_plan_valid(control: dict, available_by_name: dict[str, Path], desired_count: int, skip_anchors: set[str]) -> bool:
  planned_source_images = [str(name) for name in control.get("planned_source_images", []) if str(name)]
  planned_anchor = str(control.get("planned_anchor_image") or "").strip()
  if not planned_source_images or not planned_anchor:
    return False
  if planned_anchor != planned_source_images[0]:
    return False
  if planned_anchor in skip_anchors:
    return False
  if len(planned_source_images) != min(len(planned_source_images), desired_count):
    return False
  if len(planned_source_images) > desired_count:
    return False
  if len(set(planned_source_images)) != len(planned_source_images):
    return False
  return all(name in available_by_name for name in planned_source_images)


def _resolve_reel_caption(control: dict, planned_images: list[Path], fallback_caption: str) -> tuple[str, str | None, str | None]:
  caption_override = str(control.get("caption_override") or "").strip()
  if caption_override:
    return caption_override, "manual", control.get("planned_caption_updated_at")

  planned_names = [image.name for image in planned_images]
  cached_names = [str(name) for name in control.get("planned_source_images", []) if str(name)]
  cached_caption = str(control.get("planned_caption") or "").strip()
  if planned_names and planned_names == cached_names and cached_caption:
    return cached_caption, str(control.get("planned_caption_source") or "cached"), control.get("planned_caption_updated_at")

  try:
    from config import load_settings

    bundle = CaptionGenerator(load_settings()).generate_for_reel(planned_images)
    control["planned_caption"] = bundle.selected
    control["planned_caption_source"] = bundle.source
    control["planned_caption_updated_at"] = datetime.now().isoformat()
    return bundle.selected, bundle.source, control.get("planned_caption_updated_at")
  except Exception:
    control["planned_caption"] = fallback_caption
    control["planned_caption_source"] = "fallback"
    control["planned_caption_updated_at"] = datetime.now().isoformat()
    return fallback_caption, "fallback", control.get("planned_caption_updated_at")


def build_next_reel_plan(cfg: dict, state: dict, images: list[Path]) -> dict:
  desired_count = int(((cfg.get("reels") or {}).get("images_per_reel", 4)) or 4)
  control = get_reel_control(state)
  available = list(images)
  skip_anchors = {str(name) for name in control.get("skip_anchors", [])}
  available_by_name = {image.name: image for image in available}
  reels_cfg = cfg.get("reels") or {}
  history = PostHistory(STATE_FILE)
  if _is_saved_reel_plan_valid(control, available_by_name, desired_count, skip_anchors) and not _saved_reel_plan_matches_last_reel(state, control):
    planned_images = [available_by_name[name] for name in control.get("planned_source_images", [])]
  else:
    clear_reel_preview(control)
    clear_reel_plan(control)
    planned_images = history.plan_reel_images(
      state=state,
      images=available,
      selection_mode=str(cfg.get("selection_mode", "random") or "random").lower(),
      count=desired_count,
      queue_override=[str(name) for name in control.get("queue_override", [])],
      skip_anchors=skip_anchors,
      anchor_cooldown_reels=int(reels_cfg.get("anchor_cooldown_reels", 3) or 0),
      duplicate_window_reels=int(reels_cfg.get("duplicate_window_reels", 12) or 0),
      prefer_next_anchor=True,
    )
    control["planned_source_images"] = [image.name for image in planned_images]
    control["planned_anchor_image"] = planned_images[0].name if planned_images else None
    control["planned_updated_at"] = datetime.now().isoformat() if planned_images else None

  if control.get("preview_path") and not _preview_matches_saved_plan(control):
    clear_reel_preview(control)

  if not planned_images:
    clear_reel_plan(control)
    return {
      "anchor_image": None,
      "source_images": [],
      "image_count": 0,
      "caption": str(control.get("caption_override") or "").strip(),
      "caption_source": control.get("planned_caption_source"),
      "caption_updated_at": control.get("planned_caption_updated_at"),
      "preview_path": control.get("preview_path"),
      "preview_updated_at": control.get("preview_updated_at"),
      "used_override": bool(control.get("queue_override")),
      "skipped_anchors": list(control.get("skip_anchors", [])),
      "available_images": [image.name for image in available],
      "anchor_cooldown_reels": int(reels_cfg.get("anchor_cooldown_reels", 3) or 0),
      "duplicate_window_reels": int(reels_cfg.get("duplicate_window_reels", 12) or 0),
      "planned_updated_at": control.get("planned_updated_at"),
    }

  anchor_name = planned_images[0].name
  selected_names = [image.name for image in planned_images if image.name in available_by_name]

  captions = state.get("captions", {})
  stored_caption = ((captions.get(anchor_name) or {}).get("selected") or "").strip()
  fallback_caption = stored_caption or ((cfg.get("caption_template") or "").strip() or (cfg.get("ai_disclosure") or "").strip())
  caption, caption_source, caption_updated_at = _resolve_reel_caption(control, planned_images, fallback_caption)

  return {
    "anchor_image": anchor_name,
    "source_images": selected_names[:desired_count],
    "image_count": min(len(selected_names), desired_count),
    "caption": caption,
    "caption_source": caption_source,
    "caption_updated_at": caption_updated_at,
    "preview_path": control.get("preview_path"),
    "preview_updated_at": control.get("preview_updated_at"),
    "used_override": bool(control.get("queue_override")),
    "skipped_anchors": list(control.get("skip_anchors", [])),
    "available_images": [image.name for image in available if image.name != anchor_name],
    "anchor_cooldown_reels": int(reels_cfg.get("anchor_cooldown_reels", 3) or 0),
    "duplicate_window_reels": int(reels_cfg.get("duplicate_window_reels", 12) or 0),
    "planned_updated_at": control.get("planned_updated_at"),
  }


def get_next_reel_images(cfg: dict, state: dict, images: list[Path]) -> list[dict]:
  next_image = state.get("next_image")
  desired_count = int(((cfg.get("reels") or {}).get("images_per_reel", 4)) or 4)

  available = list(images)
  if not available:
    return []

  selected: list[Path] = []
  seen_names: set[str] = set()

  if next_image:
    preferred = next((image for image in available if image.name == next_image), None)
    if preferred is not None:
      selected.append(preferred)
      seen_names.add(preferred.name)

  for image in available:
    if len(selected) >= desired_count:
      break
    if image.name in seen_names:
      continue
    selected.append(image)
    seen_names.add(image.name)

  result = []
  for index, image in enumerate(selected, start=1):
    result.append({
      "name": image.name,
      "role": "anchor" if index == 1 else "support",
      "position": index,
    })
  return result


def build_reel_status(cfg: dict, state: dict, is_running: bool) -> dict:
  reel_cfg = cfg.get("reels") or {}
  images = get_images(
    cfg.get("images_folder", ""),
    cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]),
  )
  next_reel_plan = build_next_reel_plan(cfg, state, images)
  last_reel = get_last_reel(state)
  return {
    "enabled": reel_cfg.get("enabled", False),
    "simulation_mode": reel_cfg.get("simulation_mode", True),
    "publish_to_facebook": reel_cfg.get("publish_to_facebook", False),
    "output_folder": reel_cfg.get("output_folder", ""),
    "images_per_reel": reel_cfg.get("images_per_reel", 4),
    "duration_seconds": reel_cfg.get("duration_seconds", 10),
    "fps": reel_cfg.get("fps", 24),
    "generated_count": len(state.get("generated_reels", [])),
    "running": is_running,
    "next_slot": compute_next_slot_label(cfg, state, is_running),
    "last_reel": last_reel,
    "next_reel": next_reel_plan,
  }


def get_images(folder: str, extensions: list) -> list[Path]:
    p = Path(folder)
    if not p.exists():
        return []
    exts = {e.lower() for e in extensions}
    return sorted([f for f in p.iterdir() if f.suffix.lower() in exts], key=lambda x: x.name)


def get_cycle_posted(state: dict, images: list[Path]) -> list[str]:
    available_names = {image.name for image in images}
    return [name for name in state.get("cycle_posted", []) if name in available_names]


def get_posted_names(state: dict) -> set[str]:
  posted_names = {
    name
    for name, meta in state.get("image_registry", {}).items()
    if meta.get("posted")
  }
  posted_names.update(
    str(entry.get("file") or "").strip()
    for entry in state.get("posted", [])
    if str(entry.get("file") or "").strip()
  )
  return posted_names


def get_posting_slots(cfg: dict) -> list[str]:
  slots = cfg.get("posting_slots") or []
  return [slot for slot in slots if isinstance(slot, str) and ":" in slot]


def compute_next_slot_label(cfg: dict, state: dict, is_running: bool) -> str | None:
  posting_slots = get_posting_slots(cfg)
  if not posting_slots or not is_running:
    return None

  slot_runs = state.get("slot_runs", {})
  now = datetime.now()

  for day_offset in range(2):
    current_day = now.date() + timedelta(days=day_offset)
    day_key = current_day.isoformat()
    day_runs = slot_runs.get(day_key, {})
    for slot in posting_slots:
      hour_text, minute_text = slot.split(":", maxsplit=1)
      candidate = datetime.combine(current_day, time(hour=int(hour_text), minute=int(minute_text)))
      if candidate <= now:
        continue
      if slot in day_runs:
        continue
      return candidate.strftime("%d.%m.%Y %H:%M:%S")

  return None


def build_schedule_overview(cfg: dict, state: dict, is_running: bool) -> dict:
  posting_slots = get_posting_slots(cfg)
  now = datetime.now()
  day_key = now.date().isoformat()
  day_runs = state.get("slot_runs", {}).get(day_key, {})
  next_slot_label = compute_next_slot_label(cfg, state, is_running)
  entries = []

  for slot in posting_slots:
    hour_text, minute_text = slot.split(":", maxsplit=1)
    candidate = datetime.combine(now.date(), time(hour=int(hour_text), minute=int(minute_text)))
    run = day_runs.get(slot)
    status = "pending"
    message = "Geplant"
    if run:
      status = str(run.get("status") or "pending")
      message = str(run.get("message") or "")
    elif next_slot_label and candidate.strftime("%d.%m.%Y %H:%M:%S") == next_slot_label:
      status = "next"
      message = "Als Nächstes geplant"
    elif candidate < now:
      status = "open"
      message = "Noch nicht verarbeitet"

    entries.append({
      "slot": slot,
      "status": status,
      "message": message,
      "is_next": status == "next",
    })

  return {
    "day": now.strftime("%d.%m.%Y"),
    "entries": entries,
    "next_slot": next_slot_label,
  }


def choose_dashboard_next_image(
  images: list[Path],
  state: dict,
  cfg: dict,
  exclude_names: set[str] | None = None,
  prefer_state_next: bool = True,
) -> str | None:
  exclude_names = exclude_names or set()
  selection_mode = cfg.get("selection_mode", "random").lower()
  posted_names = get_posted_names(state)
  filtered_images = [
    image for image in images if image.name not in exclude_names and image.name not in posted_names
  ]

  if not filtered_images:
    return None

  if selection_mode == "sequential":
    return filtered_images[0].name

  candidates = filtered_images

  preferred_name = state.get("next_image") if prefer_state_next else None
  preferred_image = next((image for image in candidates if image.name == preferred_name), None)
  if preferred_image is not None:
    return preferred_image.name

  return candidates[0].name


def build_image_path(filename: str) -> Path:
  cfg = load_json(CONFIG_FILE, {})
  folder = Path(cfg.get("images_folder", "")).resolve()
  target = (folder / filename).resolve()
  if not str(target).startswith(str(folder)):
    abort(403)
  return target


def build_image_path_for_reel_source(filename: str) -> Path:
  cfg = load_json(CONFIG_FILE, {})
  folder = Path(cfg.get("images_folder", "")).resolve()
  candidates = [
    (folder / filename).resolve(),
    (folder / "versendet" / filename).resolve(),
    (folder / "entfernt" / filename).resolve(),
  ]

  for target in candidates:
    if not str(target).startswith(str(folder)):
      continue
    if target.exists():
      return target

  abort(404)


def refresh_next_image_after_change(state: dict, cfg: dict, images: list[Path], removed_name: str | None = None):
  exclude_names = {removed_name} if removed_name else set()
  next_image = state.get("next_image")
  available_names = {image.name for image in images}
  if next_image and next_image in available_names and next_image not in exclude_names:
    return
  state["next_image"] = choose_dashboard_next_image(
    images=images,
    state=state,
    cfg=cfg,
    exclude_names=exclude_names,
    prefer_state_next=False,
  )


def _music_library_settings(cfg: dict) -> dict:
  return cfg.get("music_library") or {}


def _normalize_music_tag_values(value) -> list[str]:
  if isinstance(value, str):
    items = value.split(",")
  elif isinstance(value, list):
    items = value
  else:
    items = []
  result = []
  for item in items:
    text = str(item).strip().lower()
    if text:
      result.append(text)
  return result


def inspect_music_library(cfg: dict) -> dict:
  library = _music_library_settings(cfg)
  folder = Path(library.get("folder", Path(__file__).parent / "music"))
  allowed_platforms = {platform.lower() for platform in library.get("allowed_platforms", ["facebook", "instagram", "reels"])}
  extensions = {ext.lower() for ext in library.get("extensions", [".mp3", ".wav", ".m4a", ".aac"])}
  approved_status = str(library.get("approved_status", "approved")).lower()
  require_metadata = bool(library.get("require_metadata", True))
  require_commercial_use = bool(library.get("require_commercial_use", True))
  default_tags = _normalize_music_tag_values(library.get("default_tags", ["modern", "social"]))

  tracks: list[dict] = []
  summary = {
    "enabled": bool(library.get("enabled", True)),
    "folder": str(folder),
    "prefer_local_tracks": bool(library.get("prefer_local_tracks", True)),
    "auto_match_enabled": bool(library.get("auto_match_enabled", True)),
    "default_tags": default_tags,
    "total": 0,
    "eligible": 0,
    "blocked": 0,
    "missing_metadata": 0,
  }

  if not folder.exists():
    return {"summary": summary, "tracks": tracks}

  for path in sorted(folder.iterdir(), key=lambda item: item.name.lower()):
    if not path.is_file() or path.suffix.lower() not in extensions:
      continue

    summary["total"] += 1
    metadata_path = path.with_suffix(".json")
    metadata = {}
    status = "eligible"
    reason = "Track kann verwendet werden."

    if metadata_path.exists():
      try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
      except Exception:
        status = "blocked"
        reason = "Metadatei ist kein gueltiges JSON."
        metadata = {}
    elif require_metadata:
      status = "missing_metadata"
      reason = "Metadatei fehlt."

    license_status = str(metadata.get("license_status", "")).strip().lower() if metadata else ""
    commercial_use = bool(metadata.get("commercial_use", False)) if metadata else False
    track_platforms = _normalize_music_tag_values(metadata.get("allowed_platforms", [])) if metadata else []
    tags = sorted(set(
      _normalize_music_tag_values(metadata.get("moods", [])) +
      _normalize_music_tag_values(metadata.get("genres", [])) +
      _normalize_music_tag_values(metadata.get("keywords", [])) +
      _normalize_music_tag_values(metadata.get("tags", [])) +
      _normalize_music_tag_values(metadata.get("energy", []))
    ))

    if status == "eligible" and require_metadata and license_status != approved_status:
      status = "blocked"
      reason = f"license_status ist {license_status or 'leer'} statt {approved_status}."

    if status == "eligible" and require_commercial_use and metadata and not commercial_use:
      status = "blocked"
      reason = "commercial_use ist nicht erlaubt."

    if status == "eligible" and metadata and allowed_platforms and not set(track_platforms).intersection(allowed_platforms):
      status = "blocked"
      reason = "allowed_platforms passt nicht zu den Zielplattformen."

    if status == "eligible":
      summary["eligible"] += 1
    elif status == "missing_metadata":
      summary["missing_metadata"] += 1
      summary["blocked"] += 1
    else:
      summary["blocked"] += 1

    tracks.append({
      "file": path.name,
      "title": metadata.get("title") or path.stem,
      "artist": metadata.get("artist") or "",
      "status": status,
      "reason": reason,
      "license_status": license_status or None,
      "commercial_use": commercial_use,
      "allowed_platforms": track_platforms,
      "tags": tags,
      "energy": metadata.get("energy") or None,
      "priority": metadata.get("priority") or 0,
      "metadata_file": metadata_path.name if metadata_path.exists() else None,
      "source_url": metadata.get("source_url") or "",
      "attribution_required": bool(metadata.get("attribution_required", False)) if metadata else False,
    })

  return {"summary": summary, "tracks": tracks}


def list_poster_processes() -> list[dict]:
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.CommandLine -match 'poster\\.py' } | "
                    "Select-Object ProcessId, ParentProcessId, CommandLine | "
                    "ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        output = result.stdout.strip()
        if not output:
            return []
        data = json.loads(output)
        return data if isinstance(data, list) else [data]

    result = subprocess.run(
        ["pgrep", "-af", "poster.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    processes = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        processes.append({
            "ProcessId": int(parts[0]),
            "ParentProcessId": None,
            "CommandLine": parts[1] if len(parts) > 1 else "",
        })
    return processes


def get_root_poster_process_ids(processes: list[dict]) -> list[int]:
    process_ids = {int(process["ProcessId"]) for process in processes}
    root_ids = []
    for process in processes:
        process_id = int(process["ProcessId"])
        parent_id = process.get("ParentProcessId")
        if parent_id is not None and int(parent_id) in process_ids:
            continue
        root_ids.append(process_id)
    return root_ids


def poster_running() -> bool:
  global _poster_proc
  service_name = _poster_systemd_service()
  if service_name:
    result = _run_systemctl("is-active", service_name)
    if result.returncode == 0:
      return True
  with _poster_lock:
    managed_running = _poster_proc is not None and _poster_proc.poll() is None
  return managed_running or bool(list_poster_processes())


# --------------------------------------------------------------------------- #
# API-Routen
# --------------------------------------------------------------------------- #
@app.route("/api/status")
def api_status():
    cfg   = load_json(CONFIG_FILE, {})
    state = load_state()
    is_running = poster_running()

    folder     = cfg.get("images_folder", "")
    extensions = cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"])
    images     = get_images(folder, extensions)
    total      = len(images)
    cycle_posted = get_posted_names(state)

    last_image = state.get("last_file")
    if not last_image:
        last_index = state.get("last_index", -1)
        last_image = images[last_index].name if 0 <= last_index < total else None

    next_image = state.get("next_image")
    if next_image and (next_image in cycle_posted or not any(image.name == next_image for image in images)):
        next_image = None

    next_post_time = compute_next_slot_label(cfg, state, is_running)
    posting_slots = get_posting_slots(cfg)
    posted_list = state.get("posted", [])

    return jsonify({
        "running":         is_running,
        "dry_run":         cfg.get("dry_run", True),
        "platform":        cfg.get("platform", "facebook"),
        "selection_mode":  cfg.get("selection_mode", "random"),
        "posting_slots":   posting_slots,
        "max_posts_per_day": cfg.get("max_posts_per_day", len(posting_slots) or 0),
        "total_images":    total,
        "posted_count":    len(posted_list),
        "last_image":      last_image,
        "next_image":      next_image,
        "next_post_time":  next_post_time,
        "loop":            cfg.get("loop", True),
        "images_folder":   folder,
        "auto_comment":    build_auto_comment_stats(state),
        "campaign":        build_campaign_overview(cfg, state),
        "smart_slots":     build_smart_slot_overview(cfg, state),
        "queue_state":     state.get("queue_state", {}),
    })


@app.route("/api/history")
def api_history():
    state = load_state()
    history = list(reversed(state.get("posted", [])))[:50]
    return jsonify(history)


@app.route("/api/analytics")
def api_analytics():
    state = load_state()
    ph = PostHistory(STATE_FILE)

    trend = ph.get_recent_engagement_trend(state, last_n=5)
    weights = ph.compute_caption_feature_weights(state)
    hashtags_raw = ph.compute_hashtag_performance(state)
    weekday_raw = ph.compute_weekday_performance(state)
    hook_data = ph.compute_hook_performance(state)
    cta_data = ph.compute_cta_performance(state)
    format_data = ph.compute_format_performance(state)
    top_posts = ph.compute_top_posts(state)
    experiment_stats = ph.compute_caption_experiment_stats(
        state,
        int(((cfg := load_json(CONFIG_FILE, {})).get("caption_experiments") or {}).get("min_data_points", 4)),
    )
    image_experiment_stats = ph.compute_caption_experiment_stats(
        state,
        int(((cfg.get("caption_experiments") or {}).get("min_data_points", 4))),
        content_type="image",
    )
    reel_experiment_stats = ph.compute_caption_experiment_stats(
        state,
        int(((cfg.get("caption_experiments") or {}).get("min_data_points", 4))),
        content_type="reel",
    )
    style_winners = ph.compute_caption_style_winners(
        state,
        int(((cfg.get("caption_experiments") or {}).get("min_data_points", 4))),
    )
    top_hashtags = sorted(
        [{"tag": t, **d} for t, d in hashtags_raw.items()],
        key=lambda x: x["avg_score"],
        reverse=True,
    )[:15]

    posts_with_data = sum(1 for e in state.get("posted", []) if e.get("engagement")) + sum(1 for e in state.get("generated_reels", []) if e.get("engagement"))
    threshold = (cfg.get("engagement") or {}).get("low_engagement_threshold", 5)

    return jsonify({
        "engagement_trend": trend,
        "engagement_threshold": threshold,
        "caption_weights": weights,
        "hashtag_performance": top_hashtags,
        "weekday_performance": weekday_raw,
        "hook_performance": hook_data,
        "cta_performance": cta_data,
        "format_performance": format_data,
        "top_posts": top_posts,
        "posts_with_engagement": posts_with_data,
        "total_posts": len(state.get("posted", [])) + len(state.get("generated_reels", [])),
        "auto_comment": build_auto_comment_stats(state),
        "campaign": build_campaign_overview(cfg, state),
        "smart_slots": build_smart_slot_overview(cfg, state),
        "content_quality": build_content_quality_overview(state),
        "engagement_actions": build_engagement_actions_overview(state),
        "caption_experiments": {
            "enabled": bool((cfg.get("caption_experiments") or {}).get("enabled", False)),
            "exploration_rate": float((cfg.get("caption_experiments") or {}).get("exploration_rate", 0.0) or 0.0),
            "hook_weights": experiment_stats.get("hook_weights", {}),
            "cta_weights": experiment_stats.get("cta_weights", {}),
            "hook_counts": experiment_stats.get("hook_counts", {}),
            "cta_counts": experiment_stats.get("cta_counts", {}),
            "image": image_experiment_stats,
            "reel": reel_experiment_stats,
            "winners": style_winners,
        },
    })


@app.route("/api/outreach-assist")
def api_outreach_assist():
  state = load_state()
  return jsonify(build_outreach_assist_overview(state))


@app.route("/api/outreach-assist", methods=["POST"])
def api_outreach_assist_create():
  state = load_state()
  payload = request.get_json(silent=True) or {}
  creator_handle = str(payload.get("creator_handle") or "").strip()
  post_caption = str(payload.get("post_caption") or "").strip()
  if not creator_handle and not post_caption:
    return jsonify({"ok": False, "error": "Bitte Handle oder Post-Text angeben."}), 400

  platform = str(payload.get("platform") or "instagram").strip().lower()
  theme = str(payload.get("theme") or "").strip()
  post_url = str(payload.get("post_url") or "").strip()
  note = str(payload.get("note") or "").strip()
  suggestions = _build_outreach_suggestions(platform, creator_handle, post_caption, theme, note)

  outreach_state = state.setdefault("outreach_assist", {"items": []})
  items = outreach_state.setdefault("items", [])
  item_id = f"outreach-{int(time_module.time() * 1000)}"
  items.append({
    "id": item_id,
    "platform": platform,
    "creator_handle": creator_handle,
    "post_caption": post_caption,
    "theme": theme,
    "post_url": post_url,
    "note": note,
    "status": "pending",
    "selected_comment": "",
    "suggestions": suggestions,
    "created_at": datetime.now().isoformat(),
    "updated_at": datetime.now().isoformat(),
  })
  outreach_state["items"] = items[-60:]
  save_json(STATE_FILE, state)
  return jsonify({"ok": True, "item_id": item_id, "suggestions": suggestions})


@app.route("/api/outreach-assist/regenerate", methods=["POST"])
def api_outreach_assist_regenerate():
  state = load_state()
  payload = request.get_json(silent=True) or {}
  item_id = str(payload.get("id") or "").strip()
  items = ((state.get("outreach_assist") or {}).get("items") or [])
  for item in items:
    if str(item.get("id") or "") != item_id:
      continue
    item["suggestions"] = _build_outreach_suggestions(
      str(item.get("platform") or "instagram"),
      str(item.get("creator_handle") or ""),
      str(item.get("post_caption") or ""),
      str(item.get("theme") or ""),
      str(item.get("note") or ""),
    )
    item["updated_at"] = datetime.now().isoformat()
    save_json(STATE_FILE, state)
    return jsonify({"ok": True, "suggestions": item["suggestions"]})
  return jsonify({"ok": False, "error": "Eintrag nicht gefunden."}), 404


@app.route("/api/outreach-assist/mark", methods=["POST"])
def api_outreach_assist_mark():
  state = load_state()
  payload = request.get_json(silent=True) or {}
  item_id = str(payload.get("id") or "").strip()
  status = str(payload.get("status") or "pending").strip().lower()
  selected_comment = str(payload.get("selected_comment") or "").strip()
  if status not in {"pending", "used", "skipped"}:
    return jsonify({"ok": False, "error": "Ungueltiger Status."}), 400

  items = ((state.get("outreach_assist") or {}).get("items") or [])
  for item in items:
    if str(item.get("id") or "") != item_id:
      continue
    item["status"] = status
    item["selected_comment"] = selected_comment
    item["updated_at"] = datetime.now().isoformat()
    save_json(STATE_FILE, state)
    return jsonify({"ok": True})
  return jsonify({"ok": False, "error": "Eintrag nicht gefunden."}), 404


@app.route("/api/instagram/monitor")
def api_instagram_monitor():
    force_refresh = str(request.args.get("refresh") or "").strip().lower() in {"1", "true", "yes"}
    return jsonify(build_instagram_monitor_payload(force_refresh=force_refresh))


@app.route("/api/state/clear-caption-cache", methods=["POST"])
def api_clear_caption_cache():
    state = load_state()
    count = len(state.get("captions", {}))
    state["captions"] = {}
    save_json(STATE_FILE, state)
    return jsonify({"ok": True, "cleared": count, "msg": f"{count} Caption(s) aus dem Cache entfernt."})


@app.route("/api/reels")
def api_reels():
    state = load_state()
    return jsonify(get_recent_reels(state))


@app.route("/api/reels/status")
def api_reels_status():
  cfg = load_json(CONFIG_FILE, {})
  state = load_state()
  payload = build_reel_status(cfg, state, poster_running())
  save_json(STATE_FILE, state)
  return jsonify(payload)


@app.route("/api/reels/queue")
def api_reels_queue():
  cfg = load_json(CONFIG_FILE, {})
  state = load_state()
  images = get_images(
    cfg.get("images_folder", ""),
    cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]),
  )
  plan = build_next_reel_plan(cfg, state, images)
  save_json(STATE_FILE, state)
  return jsonify([
    {
      "name": name,
      "role": "anchor" if index == 0 else "support",
      "position": index + 1,
      "status": "next" if index == 0 else "included",
    }
    for index, name in enumerate(plan.get("source_images", []))
  ])


@app.route("/api/reels/plan")
def api_reels_plan():
  cfg = load_json(CONFIG_FILE, {})
  state = load_state()
  images = get_images(
    cfg.get("images_folder", ""),
    cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]),
  )
  payload = build_next_reel_plan(cfg, state, images)
  save_json(STATE_FILE, state)
  return jsonify(payload)


@app.route("/api/reels/preview", methods=["POST"])
def api_reels_preview():
  cfg = load_json(CONFIG_FILE, {})
  state = load_state()
  images = get_images(
    cfg.get("images_folder", ""),
    cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]),
  )
  plan = build_next_reel_plan(cfg, state, images)
  if not plan.get("source_images"):
    return jsonify({"ok": False, "msg": "Kein nächstes Reel zum Vorschauen verfügbar."}), 400

  from config import load_settings
  from facebook_poster import FacebookPoster
  from reel_generator import ReelGenerator

  control = get_reel_control(state)
  clear_reel_preview(control)

  settings = load_settings()
  settings.reels.output_folder = settings.reels.output_folder / "previews"
  source_paths = [build_image_path_for_reel_source(name) for name in plan.get("source_images", [])]
  result = ReelGenerator(settings).generate_reel(source_paths, plan.get("caption") or settings.ai_disclosure)
  control["preview_path"] = str(result.output_path)
  control["preview_updated_at"] = datetime.now().isoformat()
  save_json(STATE_FILE, state)
  plan["preview_path"] = str(result.output_path)
  plan["preview_updated_at"] = control["preview_updated_at"]
  return jsonify({"ok": True, "plan": plan})


@app.route("/api/reels/regenerate-caption", methods=["POST"])
def api_reels_regenerate_caption():
  cfg = load_json(CONFIG_FILE, {})
  state = load_state()
  images = get_images(
    cfg.get("images_folder", ""),
    cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]),
  )
  plan = regenerate_reel_caption(cfg, state, images)
  save_json(STATE_FILE, state)
  return jsonify({"ok": True, "plan": plan})


@app.route("/api/reels/queue/update", methods=["POST"])
def api_reels_queue_update():
  payload = request.get_json(silent=True) or {}
  cfg = load_json(CONFIG_FILE, {})
  state = load_state()
  images = get_images(
    cfg.get("images_folder", ""),
    cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]),
  )
  plan = build_next_reel_plan(cfg, state, images)
  anchor_name = plan.get("anchor_image")
  if not anchor_name:
    return jsonify({"ok": False, "msg": "Kein nächstes Reel verfügbar."}), 400

  requested = payload.get("source_images") or []
  desired_count = int(((cfg.get("reels") or {}).get("images_per_reel", 4)) or 4)
  available_names = set(plan.get("available_images", [])) | {anchor_name}
  normalized: list[str] = []
  for item in requested:
    name = str(item).strip()
    if not name or name not in available_names or name in normalized:
      continue
    normalized.append(name)

  if anchor_name in normalized:
    normalized = [name for name in normalized if name != anchor_name]
  normalized = [anchor_name] + normalized[: max(desired_count - 1, 0)]

  control = get_reel_control(state)
  clear_reel_plan(control)
  control["queue_override"] = normalized
  if "caption" in payload:
    control["caption_override"] = str(payload.get("caption") or "").strip()
  clear_reel_preview(control)
  save_json(STATE_FILE, state)
  updated_plan = build_next_reel_plan(cfg, state, images)
  return jsonify({"ok": True, "plan": updated_plan})


@app.route("/api/reels/queue/remove-image", methods=["POST"])
def api_reels_queue_remove_image():
  payload = request.get_json(silent=True) or {}
  filename = str(payload.get("filename") or "").strip()
  cfg = load_json(CONFIG_FILE, {})
  state = load_state()
  images = get_images(
    cfg.get("images_folder", ""),
    cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]),
  )
  plan = build_next_reel_plan(cfg, state, images)
  if filename == plan.get("anchor_image"):
    return jsonify({"ok": False, "msg": "Das Startbild kannst du nicht entfernen. Nutze Überspringen für das nächste Reel."}), 400

  updated_names = [name for name in plan.get("source_images", []) if name != filename]
  control = get_reel_control(state)
  clear_reel_plan(control)
  control["queue_override"] = updated_names
  clear_reel_preview(control)
  save_json(STATE_FILE, state)
  return jsonify({"ok": True, "plan": build_next_reel_plan(cfg, state, images)})


@app.route("/api/reels/queue/move", methods=["POST"])
def api_reels_queue_move():
  payload = request.get_json(silent=True) or {}
  filename = str(payload.get("filename") or "").strip()
  direction = str(payload.get("direction") or "").strip().lower()
  cfg = load_json(CONFIG_FILE, {})
  state = load_state()
  images = get_images(
    cfg.get("images_folder", ""),
    cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]),
  )
  plan = build_next_reel_plan(cfg, state, images)
  names = list(plan.get("source_images", []))
  if not filename or filename not in names:
    return jsonify({"ok": False, "msg": "Reel-Bild nicht gefunden."}), 404
  if filename == plan.get("anchor_image"):
    return jsonify({"ok": False, "msg": "Das Startbild bleibt an Position 1."}), 400

  index = names.index(filename)
  target_index = index - 1 if direction == "up" else index + 1
  if target_index <= 0 or target_index >= len(names):
    return jsonify({"ok": False, "msg": "Verschieben nicht möglich."}), 400
  names[index], names[target_index] = names[target_index], names[index]

  control = get_reel_control(state)
  clear_reel_plan(control)
  control["queue_override"] = names
  clear_reel_preview(control)
  save_json(STATE_FILE, state)
  return jsonify({"ok": True, "plan": build_next_reel_plan(cfg, state, images)})


@app.route("/api/reels/skip-next", methods=["POST"])
def api_reels_skip_next():
  cfg = load_json(CONFIG_FILE, {})
  state = load_state()
  images = get_images(
    cfg.get("images_folder", ""),
    cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]),
  )
  plan = build_next_reel_plan(cfg, state, images)
  anchor_name = plan.get("anchor_image")
  if not anchor_name:
    return jsonify({"ok": False, "msg": "Kein nächstes Reel verfügbar."}), 400

  control = get_reel_control(state)
  skip_anchors = [str(name) for name in control.get("skip_anchors", []) if str(name) != anchor_name]
  skip_anchors.append(anchor_name)
  control["skip_anchors"] = skip_anchors
  clear_reel_plan(control)
  control["queue_override"] = []
  control["caption_override"] = ""
  clear_reel_preview(control)
  save_json(STATE_FILE, state)
  return jsonify({"ok": True, "plan": build_next_reel_plan(cfg, state, images)})


@app.route("/api/reels/reset-next", methods=["POST"])
def api_reels_reset_next():
  cfg = load_json(CONFIG_FILE, {})
  state = load_state()
  images = get_images(
    cfg.get("images_folder", ""),
    cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]),
  )
  control = get_reel_control(state)
  clear_reel_plan(control)
  control["queue_override"] = []
  control["caption_override"] = ""
  clear_reel_preview(control)
  save_json(STATE_FILE, state)
  return jsonify({"ok": True, "plan": build_next_reel_plan(cfg, state, images)})


@app.route("/api/reels/delete", methods=["POST"])
def api_reels_delete():
  payload = request.get_json(silent=True) or {}
  reel_path = str(payload.get("reel_path") or "").strip()
  if not reel_path:
    return jsonify({"ok": False, "msg": "Kein Reel-Pfad angegeben."}), 400

  state = load_state()
  reels = state.get("generated_reels", [])
  updated_reels = [item for item in reels if str(item.get("reel_path") or "") != reel_path]
  if len(updated_reels) == len(reels):
    return jsonify({"ok": False, "msg": "Reel nicht gefunden."}), 404

  try:
    Path(reel_path).unlink(missing_ok=True)
  except Exception:
    pass
  state["generated_reels"] = updated_reels
  prune_generated_reels(state, load_json(CONFIG_FILE, {}), keep_last=20)
  save_json(STATE_FILE, state)
  return jsonify({"ok": True})


@app.route("/api/music-library")
def api_music_library():
  cfg = load_json(CONFIG_FILE, {})
  return jsonify(inspect_music_library(cfg))


@app.route("/api/schedule")
def api_schedule():
  cfg = load_json(CONFIG_FILE, {})
  state = load_state()
  return jsonify(build_schedule_overview(cfg, state, poster_running()))


@app.route("/api/reel-file")
def api_reel_file():
    path_text = request.args.get("path", "").strip()
    if not path_text:
        abort(400)

    target = Path(path_text).resolve()
    base_dir = Path(__file__).parent.resolve()
    if not str(target).startswith(str(base_dir)):
        abort(403)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_file(target, mimetype="video/mp4")


@app.route("/api/reels/generate-now", methods=["POST"])
def api_reels_generate_now():
  cfg = load_json(CONFIG_FILE, {})
  state = load_state()
  images = get_images(
    cfg.get("images_folder", ""),
    cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]),
  )
  plan = build_next_reel_plan(cfg, state, images)
  if not plan.get("source_images"):
    return jsonify({"ok": False, "msg": "Kein nächstes Reel zum Generieren verfügbar."}), 400

  from config import load_settings
  from facebook_poster import FacebookPoster
  from reel_generator import ReelGenerator

  settings = load_settings()
  source_paths = [build_image_path_for_reel_source(name) for name in plan.get("source_images", [])]
  result = ReelGenerator(settings).generate_reel(source_paths, plan.get("caption") or settings.ai_disclosure)
  publish_status = "manual-simulated" if settings.reels.simulation_mode else "manual"
  publish_message = "Reel manuell im Dashboard erzeugt, kein externer Upload."
  published_post_id = None
  if not settings.reels.simulation_mode and settings.reels.publish_to_facebook and settings.platform == "facebook":
    publish_result = FacebookPoster(settings).post_reel(result.output_path, plan.get("caption") or settings.ai_disclosure)
    if publish_result.success:
      publish_status = "manual-published"
      publish_message = f"Facebook-Reel veroeffentlicht (Reel-ID: {publish_result.reel_id})."
      published_post_id = publish_result.reel_id
    else:
      publish_status = "manual-failed"
      publish_message = f"Facebook-Reel-Upload fehlgeschlagen: {publish_result.error or 'Unbekannter Fehler'}"
      published_post_id = publish_result.reel_id
  elif not settings.reels.simulation_mode:
    publish_message = "Reel manuell im Dashboard erzeugt, Facebook-Reel-Upload ist deaktiviert."
  state.setdefault("generated_reels", []).append(
    {
      "image_name": plan.get("anchor_image"),
      "source_images": result.source_images,
      "reel_path": str(result.output_path),
      "duration_seconds": result.duration_seconds,
      "frame_count": result.frame_count,
      "slot": "manual",
      "caption": plan.get("caption") or settings.ai_disclosure,
      "audio_source": result.audio_source,
      "audio_track": result.audio_track,
      "simulation_mode": settings.reels.simulation_mode,
      "publish_status": publish_status,
      "publish_message": publish_message,
      "published_post_id": published_post_id,
      "time": datetime.now().isoformat(),
    }
  )
  control = get_reel_control(state)
  clear_reel_preview(control)
  clear_reel_plan(control)
  prune_generated_reels(state, cfg, keep_last=20)
  save_json(STATE_FILE, state)
  return jsonify({
    "ok": True,
    "reel_path": str(result.output_path),
    "publish_status": publish_status,
    "msg": publish_message,
    "published_post_id": published_post_id,
  })


@app.route("/api/log")
def api_log():
    if not LOG_FILE.exists():
        return jsonify({"lines": []})
    with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return jsonify({"lines": [l.rstrip() for l in lines[-100:]]})


@app.route("/api/images")
def api_images():
    cfg   = load_json(CONFIG_FILE, {})
    state = load_state()
    folder     = cfg.get("images_folder", "")
    extensions = cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"])
    images     = get_images(folder, extensions)
    cycle_posted = get_posted_names(state)
    next_image = state.get("next_image")
    resolved_context = resolve_dashboard_campaign_context(cfg, state)
    active_theme = str(resolved_context.get("active_theme") or "").strip().lower()
    theme_separator = str(((cfg.get("campaigns") or {}).get("theme_separator")) or "_").strip() or "_"
    pinned_next_image = str(((state.get("queue_state") or {}).get("pinned_next_image")) or "").strip()

    result = []
    for i, img in enumerate(images):
        theme = infer_dashboard_image_theme(img.name, separator=theme_separator)
        meta = (state.get("image_registry") or {}).get(img.name, {}) or {}
        result.append({
            "name":   img.name,
            "index":  i,
            "status": "posted"  if img.name in cycle_posted else
                      "next"    if img.name == next_image else
                      "pending",
            "theme": theme,
            "matches_active_theme": bool(active_theme and theme == active_theme),
            "quality_score": int(meta.get("quality_score", 0) or 0),
            "duplicate_of": str(meta.get("duplicate_of") or "").strip(),
            "pinned": img.name == pinned_next_image,
            "posted_at": str(meta.get("posted_at") or ""),
        })
    return jsonify(result)


@app.route("/api/images/pin-next", methods=["POST"])
def api_pin_next_image():
    payload = request.get_json(silent=True) or {}
    filename = str(payload.get("filename") or "").strip()
    state = load_state()
    cfg = load_json(CONFIG_FILE, {})
    images = get_images(
      cfg.get("images_folder", ""),
      cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]),
    )
    available_names = {image.name for image in images}
    history = PostHistory(STATE_FILE)

    if not filename:
      history.clear_pinned_next_image(state)
      save_json(STATE_FILE, state)
      return jsonify({"ok": True, "msg": "Pin entfernt.", "pinned_next_image": None})

    if filename not in available_names:
      return jsonify({"ok": False, "msg": "Bild nicht gefunden."}), 404

    history.set_pinned_next_image(state, filename)
    state["next_image"] = filename
    save_json(STATE_FILE, state)
    return jsonify({"ok": True, "msg": f"{filename} wurde als nächstes Bild gepinnt.", "pinned_next_image": filename})


@app.route("/api/campaign/activate", methods=["POST"])
def api_activate_campaign():
    payload = request.get_json(silent=True) or {}
    campaign_name = str(payload.get("campaign_name") or "").strip()
    cfg = load_json(CONFIG_FILE, {})
    state = load_state()
    valid_names = {
      str(item.get("name") or "").strip()
      for item in ((cfg.get("campaigns") or {}).get("campaigns", []) or [])
      if isinstance(item, dict)
    }
    if campaign_name and campaign_name not in valid_names:
      return jsonify({"ok": False, "msg": "Kampagne nicht gefunden."}), 404

    state.setdefault("campaign_state", {})["campaign_override"] = campaign_name or None
    if campaign_name:
      state["campaign_state"]["active_campaign"] = campaign_name
      campaign_items = [item for item in ((cfg.get("campaigns") or {}).get("campaigns", []) or []) if str(item.get("name") or "") == campaign_name]
      if campaign_items:
        themes = [str(theme).strip() for theme in campaign_items[0].get("themes", []) if str(theme).strip()]
        days_per_theme = max(1, int(campaign_items[0].get("days_per_theme", (cfg.get("campaigns") or {}).get("default_days_per_theme", 2)) or 2))
        if themes:
          state["campaign_state"]["active_theme"] = themes[((datetime.now().date().toordinal()) // days_per_theme) % len(themes)]
    else:
      state["campaign_state"]["active_campaign"] = None
      state["campaign_state"]["active_theme"] = None
    state["campaign_state"]["last_updated_at"] = datetime.now().isoformat()
    save_json(STATE_FILE, state)
    return jsonify({
      "ok": True,
      "msg": f"Kampagnen-Override gesetzt: {campaign_name}" if campaign_name else "Kampagnen-Override entfernt.",
      "campaign": build_campaign_overview(cfg, state),
    })


@app.route("/api/images/remove", methods=["POST"])
def api_remove_image():
    payload = request.get_json(silent=True) or {}
    filename = payload.get("filename", "").strip()
    if not filename:
        return jsonify({"ok": False, "msg": "Kein Dateiname angegeben."}), 400

    cfg = load_json(CONFIG_FILE, {})
    state = load_state()
    target = build_image_path(filename)
    if not target.exists():
        return jsonify({"ok": False, "msg": "Bild nicht gefunden."}), 404

    removed_folder = target.parent / "entfernt"
    removed_folder.mkdir(exist_ok=True)
    destination = removed_folder / target.name
    suffix = 1
    while destination.exists():
        destination = removed_folder / f"{target.stem}-{suffix}{target.suffix}"
        suffix += 1

    target.replace(destination)

    state["cycle_posted"] = [name for name in state.get("cycle_posted", []) if name != filename]
    if state.get("next_image") == filename:
        state["next_image"] = None
    PostHistory(STATE_FILE).clear_pinned_next_image(state, filename)

    images = get_images(cfg.get("images_folder", ""), cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]))
    refresh_next_image_after_change(state, cfg, images, removed_name=filename)
    save_json(STATE_FILE, state)

    return jsonify({
        "ok": True,
        "msg": f"{filename} wurde nach entfernt verschoben.",
        "next_image": state.get("next_image"),
    })


@app.route("/api/images/skip-next", methods=["POST"])
def api_skip_next_image():
    cfg = load_json(CONFIG_FILE, {})
    state = load_state()
    images = get_images(cfg.get("images_folder", ""), cfg.get("supported_extensions", [".jpg", ".jpeg", ".png", ".gif", ".webp"]))

    current_next = state.get("next_image")
    if not current_next:
        state["next_image"] = choose_dashboard_next_image(images, state, cfg, prefer_state_next=False)
        save_json(STATE_FILE, state)
        if not state.get("next_image"):
            return jsonify({"ok": False, "msg": "Kein nächstes Bild verfügbar."}), 400
        current_next = state["next_image"]

    next_choice = choose_dashboard_next_image(
        images=images,
        state=state,
        cfg=cfg,
        exclude_names={current_next},
        prefer_state_next=False,
    )

    if not next_choice:
        return jsonify({"ok": False, "msg": "Kein alternatives Bild zum Überspringen verfügbar."}), 400

    state["next_image"] = next_choice
    PostHistory(STATE_FILE).clear_pinned_next_image(state, current_next)
    save_json(STATE_FILE, state)
    return jsonify({
        "ok": True,
        "msg": f"{current_next} wird übersprungen. Als Nächstes kommt {next_choice}.",
        "next_image": next_choice,
    })


@app.route("/api/thumbnail/<path:filename>")
def api_thumbnail(filename):
    cfg = load_json(CONFIG_FILE, {})
    folder = Path(cfg.get("images_folder", "")).resolve()
    candidates = [
        (folder / filename).resolve(),
        (folder / "versendet" / filename).resolve(),
        (folder / "entfernt" / filename).resolve(),
    ]
    for target in candidates:
        if not str(target).startswith(str(folder)):
            abort(403)
        if target.exists():
            return send_file(target)
    abort(404)


@app.route("/api/source-thumbnail/<path:filename>")
def api_source_thumbnail(filename):
  return send_file(build_image_path_for_reel_source(filename))


@app.route("/public-media/<path:filename>")
@app.route("/ig-tmp/<path:filename>")
def public_media(filename):
  from config import load_settings

  settings = load_settings()
  staging_folder = settings.instagram.staging_folder.resolve()
  target = (staging_folder / filename).resolve()
  if not str(target).startswith(str(staging_folder)):
    abort(403)
  if not target.exists() or not target.is_file():
    abort(404)
  response = send_file(target)
  response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
  response.headers["Cache-Control"] = "private, max-age=300"
  return response


@app.route("/api/poster/start", methods=["POST"])
def api_poster_start():
  global _poster_proc
  if poster_running():
    return jsonify({"ok": False, "msg": "Poster läuft bereits."})

  service_name = _poster_systemd_service()
  if service_name:
    result = _run_systemctl("start", service_name)
    if result.returncode != 0:
      message = (result.stderr or result.stdout or "systemd-Start fehlgeschlagen.").strip()
      return jsonify({"ok": False, "msg": message}), 500
    return jsonify({"ok": True, "msg": "Poster über systemd gestartet."})

  with _poster_lock:
    _poster_proc = subprocess.Popen(
      [sys.executable, str(Path(__file__).parent / "poster.py")],
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
    )
  return jsonify({"ok": True, "msg": "Poster gestartet."})


@app.route("/api/poster/stop", methods=["POST"])
def api_poster_stop():
    global _poster_proc
    service_name = _poster_systemd_service()
    if service_name:
        result = _run_systemctl("stop", service_name)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "systemd-Stopp fehlgeschlagen.").strip()
            return jsonify({"ok": False, "msg": message}), 500
        with _poster_lock:
            _poster_proc = None
        return jsonify({"ok": True, "msg": "Poster über systemd gestoppt."})

    processes = list_poster_processes()
    if not processes:
        return jsonify({"ok": False, "msg": "Poster lief nicht."})

    root_process_ids = get_root_poster_process_ids(processes)

    if os.name == "nt":
        for process_id in root_process_ids:
            subprocess.run(
                ["taskkill", "/PID", str(process_id), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
    else:
        for process_id in root_process_ids:
            os.kill(process_id, signal.SIGTERM)

    with _poster_lock:
        _poster_proc = None
    return jsonify({"ok": True, "msg": "Poster gestoppt."})

@app.route("/api/poster/post-now", methods=["POST"])
def api_poster_post_now():
  from config import load_settings
  from main import AutoPostingService

  settings = load_settings()
  manual_slot = f"manual-{datetime.now().strftime('%H%M%S')}"

  with _poster_lock:
    service = AutoPostingService(settings)
    service.process_slot(manual_slot)

  state = PostHistory(settings.history_file).load()
  day_key = datetime.now().date().isoformat()
  slot_run = state.get("slot_runs", {}).get(day_key, {}).get(manual_slot, {})
  status = slot_run.get("status") or "unknown"
  message = slot_run.get("message") or "Sofort-Posting wurde verarbeitet."
  return jsonify({
      "ok": status == "posted",
      "status": status,
      "msg": message,
      "image_name": slot_run.get("image_name"),
      "post_id": slot_run.get("post_id"),
    })


# --------------------------------------------------------------------------- #
# Dashboard HTML
# --------------------------------------------------------------------------- #
HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Auto-Poster Dashboard</title>
<style>
  :root {
    --bg:      #0f1117;
    --card:    #1a1d27;
    --border:  #2a2d3e;
    --accent:  #4f8ef7;
    --green:   #22c55e;
    --red:     #ef4444;
    --yellow:  #f59e0b;
    --text:    #e2e8f0;
    --muted:   #64748b;
    --radius:  12px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; min-height: 100vh; }

  header {
    background: var(--card);
    border-bottom: 1px solid var(--border);
    padding: 16px 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky; top: 0; z-index: 100;
  }
  header h1 { font-size: 1.2rem; font-weight: 700; letter-spacing: .5px; }
  header h1 span { color: var(--accent); }

  .status-dot {
    width: 10px; height: 10px; border-radius: 50%;
    display: inline-block; margin-right: 8px;
    background: var(--muted);
    box-shadow: 0 0 0 0 transparent;
    transition: background .3s;
  }
  .status-dot.running { background: var(--green); animation: pulse 1.5s infinite; }
  .status-dot.stopped { background: var(--red); }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(34,197,94,.6); }
    70%  { box-shadow: 0 0 0 8px rgba(34,197,94,0); }
    100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); }
  }

  .btn {
    padding: 8px 20px; border-radius: 8px; border: none;
    cursor: pointer; font-size: .875rem; font-weight: 600;
    transition: opacity .15s, transform .1s;
  }
  .btn:hover { opacity: .85; }
  .btn:active { transform: scale(.97); }
  .btn-start { background: var(--green); color: #fff; }
  .btn-stop  { background: var(--red);   color: #fff; }
  .btn-refresh { background: var(--border); color: var(--text); }

  main { padding: 24px 28px; display: grid; gap: 20px; }

  /* KPI row */
  .kpi-row { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 14px; }
  .kpi {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 18px 20px;
  }
  .kpi-label { font-size: .72rem; color: var(--muted); text-transform: uppercase; letter-spacing: .8px; margin-bottom: 8px; }
  .kpi-value { font-size: 1.7rem; font-weight: 700; }
  .kpi-value.accent { color: var(--accent); }
  .kpi-value.green  { color: var(--green); }
  .kpi-value.yellow { color: var(--yellow); }

  /* Two-col layout */
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 800px) { .two-col { grid-template-columns: 1fr; } }

  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); overflow: hidden;
  }
  .card-header {
    padding: 14px 20px; border-bottom: 1px solid var(--border);
    font-size: .85rem; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: .6px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .card-body { padding: 20px; }

  /* Image previews */
  .preview-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .preview-box { border-radius: 8px; overflow: hidden; position: relative; background: #111; aspect-ratio: 1; }
  .preview-box img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .preview-label {
    position: absolute; bottom: 0; left: 0; right: 0;
    background: rgba(0,0,0,.65); backdrop-filter: blur(4px);
    font-size: .7rem; font-weight: 700; text-align: center; padding: 6px;
    letter-spacing: .4px;
  }
  .preview-label.last { color: var(--muted); }
  .preview-label.next { color: var(--accent); }
  .preview-name { font-size: .78rem; color: var(--muted); text-align: center; margin-top: 6px; word-break: break-all; }
  .no-image { display: flex; align-items: center; justify-content: center; height: 100%; color: var(--muted); font-size: .8rem; }

  /* History table */
  .history-table { width: 100%; border-collapse: collapse; font-size: .82rem; }
  .history-table th { color: var(--muted); font-weight: 600; text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }
  .history-table td { padding: 8px 10px; border-bottom: 1px solid rgba(255,255,255,.04); }
  .history-table tr:last-child td { border-bottom: none; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 99px;
    font-size: .68rem; font-weight: 700; letter-spacing: .3px;
  }
  .badge-posted  { background: rgba(34,197,94,.15);  color: var(--green); }
  .badge-next    { background: rgba(79,142,247,.15);  color: var(--accent); }
  .badge-pending { background: rgba(100,116,139,.12); color: var(--muted); }
  .badge-dry     { background: rgba(245,158,11,.15);  color: var(--yellow); }

  /* Image queue */
  .queue { max-height: 320px; overflow-y: auto; }
  .queue::-webkit-scrollbar { width: 6px; }
  .queue::-webkit-scrollbar-track { background: transparent; }
  .queue::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  .queue-item {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 10px; border-radius: 8px; margin-bottom: 4px;
    background: rgba(255,255,255,.02);
  }
  .queue-item:hover { background: rgba(255,255,255,.05); }
  .queue-item.campaign-match {
    background: rgba(34,197,94,.06);
    box-shadow: inset 3px 0 0 rgba(34,197,94,.65);
  }
  .queue-thumb {
    width: 40px; height: 40px; border-radius: 6px; object-fit: cover; flex-shrink: 0; background: #111;
    cursor: zoom-in;
  }
  .queue-info { flex: 1; min-width: 0; }
  .queue-name { font-size: .8rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .queue-submeta { font-size: .7rem; color: var(--muted); margin-top: 3px; }
  .queue-idx  { font-size: .7rem; color: var(--muted); }
  .queue-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; }
  .badge-theme { background: rgba(34,197,94,.15); color: #86efac; }
  .btn-icon.active-filter {
    border-color: rgba(34,197,94,.45);
    color: #86efac;
    background: rgba(34,197,94,.08);
  }
  .toolbar-select {
    background: rgba(255,255,255,.04); border: 1px solid var(--border); color: var(--text);
    border-radius: 8px; padding: 6px 10px; font-size: .75rem;
  }
  .toolbar-note { font-size: .72rem; color: var(--muted); }

  .btn-icon {
    background: transparent; border: 1px solid var(--border); color: var(--text);
    border-radius: 8px; padding: 6px 10px; font-size: .75rem; cursor: pointer;
  }
  .btn-icon:hover { border-color: var(--accent); color: #fff; }
  .btn-icon.danger:hover { border-color: var(--red); }
  .btn-icon.warn:hover { border-color: var(--yellow); }

  .preview-box.clickable { cursor: zoom-in; }

  .toolbar-actions { display: flex; align-items: center; gap: 8px; }

  .modal {
    position: fixed; inset: 0; background: rgba(2,6,23,.82); backdrop-filter: blur(8px);
    display: none; align-items: center; justify-content: center; padding: 24px; z-index: 300;
  }
  .modal.open { display: flex; }
  .modal-card {
    width: min(1100px, 100%); max-height: calc(100vh - 48px); overflow: hidden;
    background: #0b1220; border: 1px solid rgba(148,163,184,.18); border-radius: 18px;
    box-shadow: 0 24px 80px rgba(0,0,0,.45);
  }
  .modal-head {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 16px 18px; border-bottom: 1px solid var(--border);
  }
  .modal-title { font-size: .95rem; font-weight: 700; word-break: break-all; }
  .modal-body {
    display: grid; grid-template-columns: minmax(0, 1fr) 260px; gap: 0;
  }
  .modal-image-wrap {
    background: radial-gradient(circle at top, rgba(79,142,247,.16), transparent 45%), #050816;
    display: flex; align-items: center; justify-content: center; min-height: 60vh; padding: 18px;
  }
  .modal-image {
    max-width: 100%; max-height: calc(100vh - 180px); border-radius: 14px; object-fit: contain;
    box-shadow: 0 14px 48px rgba(0,0,0,.35);
  }
  .modal-side {
    border-left: 1px solid var(--border); padding: 18px; display: grid; gap: 14px; align-content: start;
    background: rgba(15,23,42,.72);
  }
  .modal-meta { font-size: .8rem; color: var(--muted); word-break: break-all; }
  .modal-actions { display: grid; gap: 10px; }
  .btn-block { width: 100%; }

  @media (max-width: 900px) {
    .modal-body { grid-template-columns: 1fr; }
    .modal-side { border-left: none; border-top: 1px solid var(--border); }
  }

  /* Log */
  .log-box {
    background: #0a0c14; border-radius: 8px;
    padding: 12px 14px; font-family: 'Cascadia Code', 'Consolas', monospace;
    font-size: .72rem; max-height: 260px; overflow-y: auto;
    color: #94a3b8; line-height: 1.7;
  }
  .log-box::-webkit-scrollbar { width: 6px; }
  .log-box::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  .log-line-info    { color: #94a3b8; }
  .log-line-warning { color: var(--yellow); }
  .log-line-error   { color: var(--red); }
  .log-line-dryrun  { color: #a78bfa; }

  .reel-list { display: grid; gap: 10px; }
  .reel-item {
    display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px;
    padding: 12px; border-radius: 10px; background: rgba(255,255,255,.03);
  }
  .reel-title { font-size: .82rem; font-weight: 700; word-break: break-all; }
  .reel-meta { font-size: .74rem; color: var(--muted); margin-top: 4px; }
  .reel-actions { display: flex; align-items: center; gap: 8px; }
  .reel-preview-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .reel-preview-box {
    border-radius: 10px; overflow: hidden; position: relative; background: #0a0c14;
    min-height: 0; border: 1px solid rgba(255,255,255,.05);
  }
  .reel-preview-box video, .reel-preview-box img {
    width: 100%; height: min(32vh, 360px); object-fit: contain; display: block; background: #050816;
  }
  .reel-preview-stack {
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; padding: 12px;
  }
  .reel-preview-tile {
    position: relative; border-radius: 10px; overflow: hidden; border: 1px solid rgba(255,255,255,.08); background: #111;
  }
  .reel-preview-tile img {
    width: 100%; height: min(18vh, 148px); object-fit: cover; display: block; background: #111;
  }
  .reel-preview-tile span {
    position: absolute; left: 8px; right: 8px; bottom: 8px; padding: 5px 8px; border-radius: 999px;
    background: rgba(5,8,22,.78); color: #f8fafc; font-size: .68rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .reel-preview-tile strong {
    position: absolute; top: 8px; left: 8px; padding: 4px 8px; border-radius: 999px; background: rgba(94,234,212,.18);
    color: var(--accent); font-size: .64rem; letter-spacing: .06em; text-transform: uppercase;
  }
  .reel-summary { padding: 12px; }
  .reel-summary-title { font-size: .8rem; font-weight: 700; }
  .reel-summary-meta { font-size: .73rem; color: var(--muted); margin-top: 6px; line-height: 1.5; }
  .reel-summary-actions { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
  @media (max-width: 800px) { .reel-preview-grid { grid-template-columns: 1fr; } }

  .next-time { font-size: .8rem; color: var(--muted); margin-top: 4px; }
  .tag-dryrun {
    background: rgba(245,158,11,.15); color: var(--yellow);
    border: 1px solid rgba(245,158,11,.3);
    padding: 4px 10px; border-radius: 6px; font-size: .75rem; font-weight: 600;
  }
  .schedule-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
  .schedule-item {
    border: 1px solid var(--border); border-radius: 12px; padding: 12px; background: rgba(255,255,255,.03);
  }
  .schedule-time { font-size: .95rem; font-weight: 700; }
  .schedule-meta { font-size: .74rem; color: var(--muted); margin-top: 6px; line-height: 1.5; }
  .schedule-item.next { border-color: rgba(79,142,247,.55); box-shadow: inset 0 0 0 1px rgba(79,142,247,.35); }
  .schedule-item.posted { border-color: rgba(34,197,94,.4); }
  .schedule-item.failed { border-color: rgba(239,68,68,.4); }
  .schedule-item.skipped, .schedule-item.open { border-color: rgba(245,158,11,.35); }
  .busy-indicator {
    display: inline-flex; align-items: center; gap: 8px; font-size: .78rem; color: var(--yellow);
  }
  .busy-indicator::before {
    content: ''; width: 10px; height: 10px; border-radius: 50%;
    background: var(--yellow); box-shadow: 0 0 0 0 rgba(245,158,11,.5); animation: pulse 1.2s infinite;
  }

  /* Analytics */
  .analytics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
  @media (max-width: 800px) { .analytics-grid { grid-template-columns: 1fr; } }
  .analytics-section-title { font-size: .78rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .07em; margin-bottom: 10px; }
  .analytics-bar-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .analytics-bar-label { font-size: .78rem; min-width: 130px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .analytics-bar-track { flex: 1; height: 8px; background: rgba(255,255,255,.07); border-radius: 99px; overflow: hidden; }
  .analytics-bar-fill { height: 100%; border-radius: 99px; background: var(--accent); transition: width .4s; }
  .analytics-bar-fill.green { background: var(--green); }
  .analytics-bar-fill.yellow { background: var(--yellow); }
  .analytics-bar-fill.red { background: var(--red); }
  .analytics-bar-val { font-size: .75rem; color: var(--muted); min-width: 36px; text-align: right; }
  .analytics-hashtag-table { width: 100%; border-collapse: collapse; font-size: .78rem; }
  .analytics-hashtag-table th { color: var(--muted); font-weight: 600; padding: 4px 6px; text-align: left; border-bottom: 1px solid var(--border); }
  .analytics-hashtag-table td { padding: 5px 6px; border-bottom: 1px solid rgba(255,255,255,.04); }
  .analytics-engagement-alert {
    padding: 10px 14px; border-radius: 8px; font-size: .82rem; margin-bottom: 14px;
    border: 1px solid rgba(239,68,68,.4); background: rgba(239,68,68,.08); color: #fca5a5;
  }
  .analytics-engagement-ok {
    padding: 10px 14px; border-radius: 8px; font-size: .82rem; margin-bottom: 14px;
    border: 1px solid rgba(34,197,94,.3); background: rgba(34,197,94,.06); color: #86efac;
  }
  .analytics-pill-row { display: flex; flex-wrap: wrap; gap: 10px; }
  .analytics-pill {
    flex: 1 1 140px; min-width: 140px; padding: 10px 12px; border-radius: 10px;
    border: 1px solid var(--border); background: rgba(255,255,255,.03);
  }
  .analytics-pill strong { display: block; font-size: .95rem; color: var(--text); }
  .analytics-pill span { display: block; margin-top: 4px; font-size: .76rem; color: var(--muted); }
  .analytics-no-data { font-size: .8rem; color: var(--muted); padding: 8px 0; }
  .analytics-meta-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
  @media (max-width: 800px) { .analytics-meta-grid { grid-template-columns: 1fr; } }
  .analytics-meta-card {
    border: 1px solid var(--border); border-radius: 12px; padding: 14px;
    background: rgba(255,255,255,.03);
  }
  .analytics-meta-line { font-size: .8rem; color: var(--muted); line-height: 1.6; }
  .analytics-meta-line strong { color: var(--text); }
  .analytics-source-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
  .analytics-source-pill {
    padding: 6px 10px; border-radius: 999px; border: 1px solid var(--border);
    background: rgba(255,255,255,.04); font-size: .74rem; color: var(--text);
  }
  .outreach-form {
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px;
  }
  .outreach-field { display: grid; gap: 6px; }
  .outreach-field.full { grid-column: 1 / -1; }
  .outreach-label { font-size: .75rem; color: var(--muted); }
  .outreach-input, .outreach-textarea, .outreach-select {
    background: rgba(255,255,255,.04); border: 1px solid var(--border); color: var(--text);
    border-radius: 10px; padding: 10px 12px; font-size: .82rem; width: 100%;
  }
  .outreach-textarea { min-height: 88px; resize: vertical; }
  .outreach-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .outreach-list { display: grid; gap: 12px; }
  .outreach-item {
    border: 1px solid var(--border); border-radius: 14px; padding: 14px; background: rgba(255,255,255,.03);
  }
  .outreach-head, .outreach-meta, .outreach-item-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .outreach-head { justify-content: space-between; margin-bottom: 8px; }
  .outreach-title { font-size: .88rem; font-weight: 700; }
  .outreach-meta { font-size: .74rem; color: var(--muted); margin-bottom: 10px; }
  .outreach-notes { font-size: .78rem; color: var(--muted); margin: 8px 0; line-height: 1.5; }
  .outreach-suggestions { display: grid; gap: 8px; margin-top: 10px; }
  .outreach-suggestion {
    border: 1px solid rgba(148,163,184,.18); border-radius: 10px; padding: 10px 12px; background: rgba(15,23,42,.65);
  }
  .outreach-suggestion-text { font-size: .82rem; line-height: 1.5; }
  .outreach-item-actions { margin-top: 10px; }
  .badge-used { background: rgba(34,197,94,.15); color: #86efac; }
  .badge-skipped { background: rgba(245,158,11,.15); color: #fcd34d; }
  @media (max-width: 800px) {
    .outreach-form { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<header>
  <h1>Auto-Poster <span>Dashboard</span></h1>
  <div style="display:flex;align-items:center;gap:12px;">
    <span id="dry-badge"></span>
    <span><span class="status-dot" id="dot"></span><span id="status-text">Lädt…</span></span>
    <button class="btn btn-refresh" onclick="window.open('/instagram', '_blank', 'noopener')">Instagram</button>
    <button class="btn btn-refresh" onclick="window.open('/reels', '_blank', 'noopener')">Reels</button>
    <button class="btn btn-refresh" onclick="window.open('/music', '_blank', 'noopener')">Musik</button>
    <button class="btn btn-start"    id="btn-start"   onclick="posterAction('start')">Starten</button>
    <button class="btn btn-stop"     id="btn-stop"    onclick="posterAction('stop')">Stoppen</button>
    <button class="btn btn-refresh"  onclick="refresh()">↻</button>
  </div>
</header>

<main>

  <!-- KPIs -->
  <div class="kpi-row">
    <div class="kpi">
      <div class="kpi-label">Bilder gesamt</div>
      <div class="kpi-value accent" id="kpi-total">–</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Bereits gepostet</div>
      <div class="kpi-value green" id="kpi-posted">–</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Slots</div>
      <div class="kpi-value" id="kpi-interval">–</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Plattform</div>
      <div class="kpi-value" id="kpi-platform">–</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Kommentar-Cache</div>
      <div class="kpi-value" id="kpi-comment-cache">–</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Ollama-Kommentare</div>
      <div class="kpi-value" id="kpi-comment-ollama">–</div>
    </div>
  </div>

  <!-- Letztes / Nächstes Bild -->
  <div class="card">
    <div class="card-header">
      Vorschau
      <div class="toolbar-actions">
        <button class="btn-icon" id="post-now-top" onclick="postNowImage()">Bild jetzt posten</button>
        <button class="btn-icon warn" id="skip-next-top" onclick="skipNextImage()">Nächstes überspringen</button>
        <span id="next-time" class="next-time"></span>
      </div>
    </div>
    <div class="card-body">
      <div class="preview-grid">
        <div>
          <div class="preview-box" id="box-last">
            <div class="no-image">Noch kein Post</div>
          </div>
          <div class="preview-name" id="name-last"></div>
        </div>
        <div>
          <div class="preview-box" id="box-next">
            <div class="no-image">Kein Bild</div>
          </div>
          <div class="preview-name" id="name-next"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="two-col">

    <!-- Warteschlange -->
    <div class="card">
      <div class="card-header">Bilderwarteschlange
        <div class="toolbar-actions">
          <span class="toolbar-note" id="queue-counter">0 von 0 sichtbar</span>
          <select class="toolbar-select" id="queue-sort-select" onchange="changeQueueSort(this.value)">
            <option value="campaign">Sort: Kampagne</option>
            <option value="theme">Sort: Thema</option>
            <option value="unposted">Sort: Ungepostet zuerst</option>
            <option value="recent">Sort: Zuletzt genutzt</option>
          </select>
          <button class="btn-icon" id="queue-theme-filter-btn" onclick="toggleQueueThemeFilter()">Nur aktives Thema</button>
          <button class="btn-icon" id="queue-unposted-filter-btn" onclick="toggleQueueUnpostedFilter()">Nur ungepostete</button>
        </div>
      </div>
      <div class="card-body" style="padding:12px;">
        <div class="queue" id="queue-list">Lädt…</div>
      </div>
    </div>

    <!-- Verlauf -->
    <div class="card">
      <div class="card-header">Post-Verlauf</div>
      <div class="card-body" style="padding:0;">
        <table class="history-table" id="history-table">
          <thead><tr><th>#</th><th>Datei</th><th>Zeit</th><th>Status</th></tr></thead>
          <tbody id="history-body"><tr><td colspan="4" style="padding:16px;color:var(--muted)">Lädt…</td></tr></tbody>
        </table>
      </div>
    </div>

  </div>

  <div class="card">
    <div class="card-header">Zeitplan heute</div>
    <div class="card-body">
      <div class="schedule-grid" id="schedule-list">Lädt…</div>
    </div>
  </div>

  <!-- Log -->
  <div class="card">
    <div class="card-header">Live-Log</div>
    <div class="card-body" style="padding:12px;">
      <div class="log-box" id="log-box">Lädt…</div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">Reel-Vorschau
      <div class="toolbar-actions">
        <span id="dashboard-reel-status"></span>
        <button class="btn-icon warn" id="dashboard-generate-reel" onclick="generateNowReel()">Reel jetzt posten</button>
      </div>
    </div>
    <div class="card-body">
      <div class="reel-preview-grid">
        <div class="reel-preview-box" id="last-reel-box">
          <div class="no-image">Noch kein Reel simuliert</div>
        </div>
        <div class="reel-preview-box" id="next-reel-box">
          <div class="no-image">Kein nächstes Reel geplant</div>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">Reel-Verlauf</div>
    <div class="card-body" style="padding:12px;">
      <div class="reel-list" id="reel-list">Lädt…</div>
    </div>
  </div>

  <!-- Analytics -->
  <div class="card">
    <div class="card-header">
      Analytics &amp; Performance
      <div class="toolbar-actions">
        <button class="btn-icon" onclick="clearCaptionCache()">Caption-Cache leeren</button>
      </div>
    </div>
    <div class="card-body" style="padding:16px;">
      <div id="analytics-engagement-status"></div>
      <div class="analytics-meta-grid">
        <div>
          <div class="analytics-section-title">Aktive Kampagne</div>
          <div id="analytics-campaign"><div class="analytics-no-data">Noch keine Kampagnen-Daten</div></div>
        </div>
        <div>
          <div class="analytics-section-title">Aktive Smart Slots</div>
          <div id="analytics-smart-slots"><div class="analytics-no-data">Noch keine Slot-Daten</div></div>
        </div>
      </div>
      <div class="analytics-grid">
        <div>
          <div class="analytics-section-title">Engagement-Alerts</div>
          <div id="analytics-alerts"><div class="analytics-no-data">Noch keine Alerts</div></div>
        </div>
        <div>
          <div class="analytics-section-title">Recycle-Queue</div>
          <div id="analytics-recycle"><div class="analytics-no-data">Noch keine Recycle-Kandidaten</div></div>
        </div>
      </div>
      <div class="analytics-grid">
        <div>
          <div class="analytics-section-title">Top-Hashtags nach Engagement</div>
          <div id="analytics-hashtags"><div class="analytics-no-data">Noch keine Engagement-Daten</div></div>
        </div>
        <div>
          <div class="analytics-section-title">Beste Posting-Zeiten</div>
          <div id="analytics-weekday"><div class="analytics-no-data">Noch keine Daten</div></div>
        </div>
      </div>
      <div class="analytics-grid">
        <div>
          <div class="analytics-section-title">Starke Hook-Starts</div>
          <div id="analytics-hooks"><div class="analytics-no-data">Noch keine Hook-Daten</div></div>
        </div>
        <div>
          <div class="analytics-section-title">Starke CTA-Abschlüsse</div>
          <div id="analytics-ctas"><div class="analytics-no-data">Noch keine CTA-Daten</div></div>
        </div>
      </div>
      <div class="analytics-grid">
        <div>
          <div class="analytics-section-title">Format-Performance</div>
          <div id="analytics-formats"><div class="analytics-no-data">Noch keine Format-Daten</div></div>
        </div>
        <div>
          <div class="analytics-section-title">Top-Performer</div>
          <div id="analytics-top-posts"><div class="analytics-no-data">Noch keine Top-Posts</div></div>
        </div>
      </div>
      <div>
        <div class="analytics-section-title">Auto-Kommentar-Engine</div>
        <div id="analytics-auto-comments"><div class="analytics-no-data">Noch keine Daten zur Kommentar-Engine</div></div>
      </div>
      <div>
        <div class="analytics-section-title">Caption-Lerngewichte <span id="analytics-weights-data-hint" style="font-weight:400;text-transform:none;letter-spacing:0;"></span></div>
        <div id="analytics-weights"><div class="analytics-no-data">Noch keine Engagement-Daten (mind. 3 Posts)</div></div>
      </div>
      <div style="margin-top:18px;">
        <div class="analytics-section-title">Caption A/B Tests</div>
        <div id="analytics-caption-experiments"><div class="analytics-no-data">Noch keine A/B-Test-Daten</div></div>
      </div>
      <div style="margin-top:18px;">
        <div class="analytics-section-title">Content-Qualität & Duplicates</div>
        <div id="analytics-content-quality"><div class="analytics-no-data">Noch keine Qualitätsdaten</div></div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">Comment Assist</div>
    <div class="card-body" style="padding:16px;">
      <div class="outreach-form">
        <div class="outreach-field">
          <label class="outreach-label" for="outreach-platform">Plattform</label>
          <select id="outreach-platform" class="outreach-select">
            <option value="instagram">Instagram</option>
            <option value="facebook">Facebook</option>
            <option value="tiktok">TikTok</option>
          </select>
        </div>
        <div class="outreach-field">
          <label class="outreach-label" for="outreach-handle">Handle / Account</label>
          <input id="outreach-handle" class="outreach-input" placeholder="@accountname">
        </div>
        <div class="outreach-field">
          <label class="outreach-label" for="outreach-theme">Thema</label>
          <input id="outreach-theme" class="outreach-input" placeholder="z. B. cyberpunk, portrait, sci-fi">
        </div>
        <div class="outreach-field">
          <label class="outreach-label" for="outreach-url">Post-URL optional</label>
          <input id="outreach-url" class="outreach-input" placeholder="https://...">
        </div>
        <div class="outreach-field full">
          <label class="outreach-label" for="outreach-caption">Kurztext zum Fremdpost</label>
          <textarea id="outreach-caption" class="outreach-textarea" placeholder="Worum geht es im Post, was faellt auf, welche Stimmung hat er?"></textarea>
        </div>
        <div class="outreach-field full">
          <label class="outreach-label" for="outreach-note">Notiz optional</label>
          <input id="outreach-note" class="outreach-input" placeholder="z. B. starkes Licht, gute Komposition, neues Profil">
        </div>
      </div>
      <div class="outreach-actions">
        <button class="btn-icon" onclick="createOutreachSuggestion()">Vorschlaege erzeugen</button>
        <div id="outreach-summary" class="toolbar-note">Noch keine Outreach-Eintraege</div>
      </div>
      <div id="outreach-list" class="outreach-list" style="margin-top:16px;">
        <div class="analytics-no-data">Noch keine Comment-Assist-Eintraege</div>
      </div>
    </div>
  </div>

</main>

<div class="modal" id="image-modal" onclick="closeModal(event)">
  <div class="modal-card" onclick="event.stopPropagation()">
    <div class="modal-head">
      <div>
        <div class="modal-title" id="modal-title">Bildansicht</div>
        <div class="modal-meta" id="modal-status"></div>
      </div>
      <button class="btn-icon" onclick="closeModal()">Schließen</button>
    </div>
    <div class="modal-body">
      <div class="modal-image-wrap">
        <img class="modal-image" id="modal-image" alt="">
      </div>
      <div class="modal-side">
        <div class="modal-meta" id="modal-filename"></div>
        <div class="modal-actions">
          <button class="btn-icon btn-block" id="modal-open-full" onclick="openModalImageInTab()">Original öffnen</button>
          <button class="btn-icon btn-block warn" id="modal-skip" onclick="skipSelectedIfNext()">Dieses nächste Bild überspringen</button>
          <button class="btn-icon btn-block danger" onclick="removeSelectedImage()">Aus Liste entfernen</button>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="modal" id="reel-modal" onclick="closeReelModal(event)">
  <div class="modal-card" onclick="event.stopPropagation()">
    <div class="modal-head">
      <div>
        <div class="modal-title" id="reel-modal-title">Reel Player</div>
        <div class="modal-meta" id="reel-modal-status"></div>
      </div>
      <button class="btn-icon" onclick="closeReelModal()">Schließen</button>
    </div>
    <div class="modal-body">
      <div class="modal-image-wrap">
        <video class="modal-image" id="reel-modal-video" controls preload="metadata"></video>
      </div>
      <div class="modal-side">
        <div class="modal-meta" id="reel-modal-filename"></div>
        <div class="modal-actions">
          <button class="btn-icon btn-block" onclick="openReelInTab()">Original öffnen</button>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
let currentStatus = {};
let queueItems = [];
let queueFilterActiveThemeOnly = false;
let queueFilterUnpostedOnly = false;
let queueSortMode = 'campaign';
let selectedImage = null;
let currentReelStatus = {};
let selectedReelPath = null;
let dashboardReelBusy = false;

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function classify(line) {
  if (line.includes('DRY-RUN'))  return 'dryrun';
  if (line.includes('WARNING'))  return 'warning';
  if (line.includes('ERROR'))    return 'error';
  return 'info';
}

async function fetchStatus() {
  const r = await fetch('/api/status');
  currentStatus = await r.json();

  const dot  = document.getElementById('dot');
  const txt  = document.getElementById('status-text');
  const dryb = document.getElementById('dry-badge');

  dot.className = 'status-dot ' + (currentStatus.running ? 'running' : 'stopped');
  txt.textContent = currentStatus.running ? 'Läuft' : 'Gestoppt';
  dryb.innerHTML = currentStatus.dry_run
    ? '<span class="tag-dryrun">DRY-RUN</span>' : '';

  document.getElementById('kpi-total').textContent    = currentStatus.total_images ?? '–';
  document.getElementById('kpi-posted').textContent   = currentStatus.posted_count ?? '–';
  document.getElementById('kpi-interval').textContent = currentStatus.posting_slots?.length
    ? currentStatus.posting_slots.join(' / ')
    : '–';
  document.getElementById('kpi-platform').textContent = currentStatus.platform ?? '–';
  document.getElementById('kpi-comment-cache').textContent = currentStatus.auto_comment?.cache_size ?? '–';
  document.getElementById('kpi-comment-ollama').textContent = currentStatus.auto_comment?.ollama_used ?? '–';

  document.getElementById('next-time').textContent =
    currentStatus.next_post_time ? 'Nächster Post: ' + currentStatus.next_post_time : '';

  // Vorschau - letztes Bild
  const boxLast  = document.getElementById('box-last');
  const nameLast = document.getElementById('name-last');
  if (currentStatus.last_image) {
    boxLast.classList.add('clickable');
    boxLast.innerHTML = `
      <img src="/api/thumbnail/${encodeURIComponent(currentStatus.last_image)}" alt="" onerror="this.style.display='none'" onclick="openImageModal('${encodeURIComponent(currentStatus.last_image)}', 'Zuletzt gepostet')">
      <div class="preview-label last">Zuletzt gepostet</div>`;
    nameLast.textContent = currentStatus.last_image;
  } else {
    boxLast.classList.remove('clickable');
    boxLast.innerHTML = '<div class="no-image">Noch kein Post</div>';
    nameLast.textContent = '';
  }

  // Vorschau - nächstes Bild
  const boxNext  = document.getElementById('box-next');
  const nameNext = document.getElementById('name-next');
  if (currentStatus.next_image) {
    boxNext.classList.add('clickable');
    boxNext.innerHTML = `
      <img src="/api/thumbnail/${encodeURIComponent(currentStatus.next_image)}" alt="" onerror="this.style.display='none'" onclick="openImageModal('${encodeURIComponent(currentStatus.next_image)}', 'Als Nächstes')">
      <div class="preview-label next">Als Nächstes</div>`;
    nameNext.textContent = currentStatus.next_image;
  } else {
    boxNext.classList.remove('clickable');
    boxNext.innerHTML = '<div class="no-image">Kein Bild</div>';
    nameNext.textContent = '';
  }

  document.getElementById('skip-next-top').disabled = !currentStatus.next_image;
}

function renderCampaignOverview(campaign) {
  const el = document.getElementById('analytics-campaign');
  if (!campaign || !campaign.enabled) {
    el.innerHTML = '<div class="analytics-no-data">Kampagnenmodus ist deaktiviert</div>';
    return;
  }
  const configured = Array.isArray(campaign.configured_campaigns) ? campaign.configured_campaigns : [];
  const themeCounts = campaign.theme_post_counts || {};
  const activeThemePosts = campaign.active_theme ? (themeCounts[campaign.active_theme] || 0) : 0;
  const sourceList = configured.length
    ? `<div class="analytics-source-list">${configured.slice(0, 6).map(item =>
        `<span class="analytics-source-pill">${escapeHtml(item.name || 'Ohne Namen')}: ${escapeHtml((item.themes || []).join(', ') || '–')}</span>`
      ).join('')}</div>`
    : '';
  const campaignOptions = configured.length
    ? `<div class="toolbar-actions" style="margin:10px 0 0;">
        <select class="toolbar-select" id="campaign-select" onchange="setCampaignOverride(this.value)">
          <option value="">Auto / kein Override</option>
          ${configured.map(item => `<option value="${escapeHtml(item.name)}" ${item.name === (campaign.override_campaign || '') ? 'selected' : ''}>${escapeHtml(item.name)}</option>`).join('')}
        </select>
      </div>`
    : '';
  const progressPills = configured.length
    ? `<div class="analytics-source-list">${configured.slice(0, 4).map(item => {
        const targets = item.targets || {};
        const progress = item.progress || {};
        return `<span class="analytics-source-pill">${escapeHtml(item.name)} · Feed ${progress.feed_posts || 0}/${targets.feed_posts || 0} · Stories ${progress.stories || 0}/${targets.stories || 0} · Reels ${progress.reels || 0}/${targets.reels || 0}</span>`;
      }).join('')}</div>`
    : '';
  const themeCalendar = Array.isArray(campaign.theme_calendar) && campaign.theme_calendar.length
    ? `<div class="analytics-source-list">${campaign.theme_calendar.map(item =>
        `<span class="analytics-source-pill">${escapeHtml(item.date)} · ${escapeHtml(item.theme || '–')}</span>`
      ).join('')}</div>`
    : '';
  el.innerHTML = `
    <div class="analytics-meta-card">
      <div class="analytics-meta-line"><strong>Kampagne:</strong> ${escapeHtml(campaign.active_campaign || 'Auto-Serie / keine feste Kampagne')}</div>
      <div class="analytics-meta-line"><strong>Override:</strong> ${escapeHtml(campaign.override_campaign || 'kein Override')}</div>
      <div class="analytics-meta-line"><strong>Thema:</strong> ${escapeHtml(campaign.active_theme || 'kein aktives Thema')}</div>
      <div class="analytics-meta-line"><strong>Posts im aktiven Thema:</strong> ${activeThemePosts}</div>
      <div class="analytics-meta-line"><strong>Fallback auf erkannte Themen:</strong> ${campaign.fallback_to_detected_themes ? 'aktiv' : 'aus'}</div>
      ${campaign.last_updated_at ? `<div class="analytics-meta-line"><strong>Zuletzt aktualisiert:</strong> ${escapeHtml(new Date(campaign.last_updated_at).toLocaleString('de-DE'))}</div>` : ''}
      ${campaignOptions}
      ${sourceList}
      ${progressPills}
      ${themeCalendar}
    </div>`;
}

function renderSmartSlotOverview(smartSlots) {
  const el = document.getElementById('analytics-smart-slots');
  if (!smartSlots || !smartSlots.enabled) {
    el.innerHTML = '<div class="analytics-no-data">Smart Slots sind deaktiviert</div>';
    return;
  }
  const slots = Array.isArray(smartSlots.last_applied_slots) ? smartSlots.last_applied_slots : [];
  const sources = smartSlots.last_sources || {};
  if (!slots.length) {
    el.innerHTML = '<div class="analytics-no-data">Noch keine aktiven Smart-Slot-Entscheidungen gespeichert</div>';
    return;
  }
  el.innerHTML = `
    <div class="analytics-meta-card">
      <div class="analytics-meta-line"><strong>Exploration:</strong> ${Math.round((smartSlots.exploration_rate || 0) * 100)}%</div>
      ${smartSlots.last_updated_at ? `<div class="analytics-meta-line"><strong>Zuletzt berechnet:</strong> ${escapeHtml(new Date(smartSlots.last_updated_at).toLocaleString('de-DE'))}</div>` : ''}
      <div class="analytics-source-list">${slots.map(slot =>
        `<span class="analytics-source-pill">${escapeHtml(slot)} · ${escapeHtml(sources[slot] || 'historical')}</span>`
      ).join('')}</div>
    </div>`;
}

function renderCaptionExperiments(experiments) {
  const el = document.getElementById('analytics-caption-experiments');
  if (!experiments || !experiments.enabled) {
    el.innerHTML = '<div class="analytics-no-data">Caption-A/B-Tests sind deaktiviert</div>';
    return;
  }
  const hookWeights = Object.entries(experiments.hook_weights || {});
  const ctaWeights = Object.entries(experiments.cta_weights || {});
  const hookCounts = experiments.hook_counts || {};
  const ctaCounts = experiments.cta_counts || {};
  if (!hookWeights.length && !ctaWeights.length) {
    el.innerHTML = '<div class="analytics-no-data">Noch keine ausreichenden A/B-Test-Daten</div>';
    return;
  }
  const renderRows = (entries, counts, labelPrefix) => {
    const maxWeight = Math.max(...entries.map(([, weight]) => Number(weight) || 1), 1);
    return entries.sort((a, b) => Number(b[1]) - Number(a[1])).map(([label, weight]) => {
      const pct = Math.round(Math.min(((Number(weight) || 1) / Math.max(maxWeight, 1.5)) * 100, 100));
      const color = Number(weight) > 1.08 ? 'green' : Number(weight) >= 0.95 ? 'yellow' : 'red';
      return `<div class="analytics-bar-row">
        <div class="analytics-bar-label">${escapeHtml(labelPrefix + ': ' + label)}</div>
        <div class="analytics-bar-track"><div class="analytics-bar-fill ${color}" style="width:${pct}%"></div></div>
        <div class="analytics-bar-val">${Number(weight).toFixed(2)}x · ${counts[label] || 0}</div>
      </div>`;
    }).join('');
  };
  const winners = experiments.winners || {};
  const winnerBlock = `
    <div class="analytics-meta-card" style="margin-bottom:12px;">
      <div class="analytics-meta-line"><strong>Gewinner Bildposts:</strong> Hook ${escapeHtml(winners.image?.hook_style || '–')} (${Number(winners.image?.hook_weight || 1).toFixed(2)}x) · CTA ${escapeHtml(winners.image?.cta_style || '–')} (${Number(winners.image?.cta_weight || 1).toFixed(2)}x)</div>
      <div class="analytics-meta-line"><strong>Gewinner Reels:</strong> Hook ${escapeHtml(winners.reel?.hook_style || '–')} (${Number(winners.reel?.hook_weight || 1).toFixed(2)}x) · CTA ${escapeHtml(winners.reel?.cta_style || '–')} (${Number(winners.reel?.cta_weight || 1).toFixed(2)}x)</div>
    </div>`;
  el.innerHTML = `
    <div class="analytics-meta-card" style="margin-bottom:12px;">
      <div class="analytics-meta-line"><strong>Exploration:</strong> ${Math.round((experiments.exploration_rate || 0) * 100)}%</div>
      <div class="analytics-meta-line"><strong>Messung:</strong> Gewichte über Hook- und CTA-Stile mit Engagement-Rückfluss</div>
    </div>
    ${winnerBlock}
    <div class="analytics-grid">
      <div>
        <div class="analytics-section-title">Hook-Stile</div>
        ${renderRows(hookWeights, hookCounts, 'Hook')}
      </div>
      <div>
        <div class="analytics-section-title">CTA-Stile</div>
        ${renderRows(ctaWeights, ctaCounts, 'CTA')}
      </div>
    </div>`;
}

function renderEngagementActions(actions) {
  const alertsEl = document.getElementById('analytics-alerts');
  const recycleEl = document.getElementById('analytics-recycle');
  const alerts = Array.isArray(actions?.alerts) ? actions.alerts : [];
  const recycle = Array.isArray(actions?.recycle_queue) ? actions.recycle_queue : [];
  alertsEl.innerHTML = alerts.length
    ? `<table class="analytics-hashtag-table"><thead><tr><th>Zeit</th><th>Typ</th><th>Meldung</th><th>Score</th></tr></thead><tbody>${alerts.map(item => `
      <tr><td style="color:var(--muted)">${escapeHtml(item.time ? new Date(item.time).toLocaleString('de-DE') : '–')}</td><td>${escapeHtml(item.type || '–')}</td><td>${escapeHtml(item.message || '')}</td><td style="color:var(--muted)">${escapeHtml(item.score ?? '–')}</td></tr>
    `).join('')}</tbody></table>`
    : '<div class="analytics-no-data">Noch keine Alerts</div>';
  recycleEl.innerHTML = recycle.length
    ? `<table class="analytics-hashtag-table"><thead><tr><th>Post</th><th>Format</th><th>Status</th><th>Fällig</th></tr></thead><tbody>${recycle.map(item => `
      <tr><td title="${escapeHtml(item.file || '')}">${escapeHtml(item.file || '–')}</td><td>${escapeHtml((item.formats || []).join(', ') || '–')}</td><td>${escapeHtml(item.status || 'queued')}</td><td style="color:var(--muted)">${escapeHtml(item.due_at ? new Date(item.due_at).toLocaleString('de-DE') : '–')}</td></tr>
    `).join('')}</tbody></table>`
    : '<div class="analytics-no-data">Noch keine Recycle-Kandidaten</div>';
}

function renderContentQualityOverview(quality) {
  const el = document.getElementById('analytics-content-quality');
  const diagnostics = Array.isArray(quality?.diagnostics) ? quality.diagnostics : [];
  const duplicates = Array.isArray(quality?.duplicates) ? quality.duplicates : [];
  const lowestQuality = Array.isArray(quality?.lowest_quality) ? quality.lowest_quality : [];
  if (!diagnostics.length && !duplicates.length && !lowestQuality.length) {
    el.innerHTML = '<div class="analytics-no-data">Noch keine Qualitätsdaten</div>';
    return;
  }
  el.innerHTML = `
    <div class="analytics-meta-card" style="margin-bottom:12px;">
      ${quality?.last_scan_at ? `<div class="analytics-meta-line"><strong>Letzter Scan:</strong> ${escapeHtml(new Date(quality.last_scan_at).toLocaleString('de-DE'))}</div>` : ''}
      <div class="analytics-meta-line"><strong>Ähnliche Bilder:</strong> ${duplicates.length}</div>
      <div class="analytics-meta-line"><strong>Aktuelle Hinweise:</strong> ${diagnostics.length}</div>
    </div>
    <div class="analytics-grid">
      <div>
        <div class="analytics-section-title">Niedrige Qualität</div>
        ${lowestQuality.length ? lowestQuality.map(item => `<div class="analytics-bar-row"><div class="analytics-bar-label">${escapeHtml(item.file)}</div><div class="analytics-bar-track"><div class="analytics-bar-fill red" style="width:${Math.max(6, item.quality_score)}%"></div></div><div class="analytics-bar-val">${item.quality_score}</div></div>`).join('') : '<div class="analytics-no-data">Keine Daten</div>'}
      </div>
      <div>
        <div class="analytics-section-title">Diagnostics</div>
        ${diagnostics.length ? `<table class="analytics-hashtag-table"><thead><tr><th>Datei</th><th>Grund</th></tr></thead><tbody>${diagnostics.map(item => `<tr><td>${escapeHtml(item.file || '–')}</td><td style="color:var(--muted)">${escapeHtml(item.reason || '–')}</td></tr>`).join('')}</tbody></table>` : '<div class="analytics-no-data">Keine Diagnostics</div>'}
      </div>
    </div>`;
}

async function fetchHistory() {
  const r    = await fetch('/api/history');
  const data = await r.json();
  const body = document.getElementById('history-body');
  if (!data.length) {
    body.innerHTML = '<tr><td colspan="4" style="padding:16px;color:var(--muted)">Noch keine Posts</td></tr>';
    return;
  }
  body.innerHTML = data.map((item, i) => {
    const dry = currentStatus.dry_run;
    const badge = dry
      ? '<span class="badge badge-dry">DRY-RUN</span>'
      : '<span class="badge badge-posted">Gepostet</span>';
    const t = item.time ? new Date(item.time).toLocaleString('de-DE') : '–';
    return `<tr>
      <td style="color:var(--muted)">${i + 1}</td>
      <td>${item.file ?? '–'}</td>
      <td style="color:var(--muted);font-size:.75rem">${t}</td>
      <td>${badge}</td>
    </tr>`;
  }).join('');
}

async function fetchQueue() {
  const r    = await fetch('/api/images');
  const data = await r.json();
  queueItems = data;
  renderQueue();
}

function renderQueue() {
  const el   = document.getElementById('queue-list');
  const filterButton = document.getElementById('queue-theme-filter-btn');
  const unpostedButton = document.getElementById('queue-unposted-filter-btn');
  const sortSelect = document.getElementById('queue-sort-select');
  const counter = document.getElementById('queue-counter');
  const activeTheme = currentStatus.campaign?.active_theme || '';
  if (filterButton) {
    filterButton.classList.toggle('active-filter', queueFilterActiveThemeOnly);
    filterButton.textContent = queueFilterActiveThemeOnly
      ? 'Filter: aktives Thema'
      : 'Nur aktives Thema';
    filterButton.disabled = !activeTheme;
  }
  if (unpostedButton) {
    unpostedButton.classList.toggle('active-filter', queueFilterUnpostedOnly);
    unpostedButton.textContent = queueFilterUnpostedOnly
      ? 'Filter: ungepostet'
      : 'Nur ungepostete';
  }
  if (sortSelect) {
    sortSelect.value = queueSortMode;
  }

  let visibleItems = queueItems;
  if (queueFilterActiveThemeOnly && activeTheme) {
    visibleItems = visibleItems.filter(item => item.matches_active_theme);
  }
  if (queueFilterUnpostedOnly) {
    visibleItems = visibleItems.filter(item => item.status !== 'posted');
  }
  visibleItems = [...visibleItems].sort((left, right) => {
    if (queueSortMode === 'theme') {
      return String(left.theme || '').localeCompare(String(right.theme || '')) || left.index - right.index;
    }
    if (queueSortMode === 'unposted') {
      const leftScore = left.status === 'posted' ? 1 : 0;
      const rightScore = right.status === 'posted' ? 1 : 0;
      return leftScore - rightScore || left.index - right.index;
    }
    if (queueSortMode === 'recent') {
      return String(right.posted_at || '').localeCompare(String(left.posted_at || '')) || left.index - right.index;
    }
    const leftCampaign = left.matches_active_theme ? 0 : 1;
    const rightCampaign = right.matches_active_theme ? 0 : 1;
    return leftCampaign - rightCampaign || left.index - right.index;
  });
  if (counter) {
    counter.textContent = `${visibleItems.length} von ${queueItems.length} sichtbar`;
  }

  if (!queueItems.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:.82rem;padding:8px">Keine Bilder im Ordner</div>';
    return;
  }
  if (!visibleItems.length) {
    let emptyMessage = 'Keine Bilder für die aktuellen Filter gefunden';
    if (queueFilterActiveThemeOnly && queueFilterUnpostedOnly) {
      emptyMessage = 'Keine ungeposteten Bilder für das aktuell aktive Thema gefunden';
    } else if (queueFilterActiveThemeOnly) {
      emptyMessage = 'Keine Bilder für das aktuell aktive Thema gefunden';
    } else if (queueFilterUnpostedOnly) {
      emptyMessage = 'Keine ungeposteten Bilder gefunden';
    }
    el.innerHTML = `<div style="color:var(--muted);font-size:.82rem;padding:8px">${escapeHtml(emptyMessage)}</div>`;
    return;
  }

  el.innerHTML = visibleItems.map(img => {
    const badgeMap = {
      posted:  '<span class="badge badge-posted">Gepostet</span>',
      next:    '<span class="badge badge-next">Nächstes</span>',
      pending: '<span class="badge badge-pending">Wartend</span>',
    };
    const themeBadge = img.matches_active_theme
      ? `<span class="badge badge-theme">Kampagne: ${escapeHtml(img.theme || activeTheme)}</span>`
      : '';
    const themeLine = img.theme
      ? `<div class="queue-submeta">Thema: ${escapeHtml(img.theme)}${img.matches_active_theme ? ' · aktiv bevorzugt' : ''}</div>`
      : '';
    const encodedName = encodeURIComponent(img.name);
    const statusLabel = img.status === 'next' ? 'Als Nächstes' : img.status === 'posted' ? 'Bereits gepostet' : 'Wartend';
    const skipButton = img.status === 'next'
      ? `<button class="btn-icon warn" onclick="skipNextImage(event)">Überspringen</button>`
      : '';
    const pinButton = `<button class="btn-icon ${img.pinned ? 'active-filter' : ''}" onclick="pinNextImage('${encodedName}')">${img.pinned ? 'Entpinnen' : 'Pinnen'}</button>`;
    return `<div class="queue-item ${img.matches_active_theme ? 'campaign-match' : ''}">
      <img class="queue-thumb"
           src="/api/thumbnail/${encodedName}"
           alt=""
           onclick="openImageModal('${encodedName}', '${statusLabel}')"
           onerror="this.style.visibility='hidden'">
      <div class="queue-info">
        <div class="queue-name">${escapeHtml(img.name)}</div>
        ${themeLine}${img.quality_score ? `<div class="queue-submeta">Qualität: ${img.quality_score}${img.duplicate_of ? ` · ähnlich zu ${escapeHtml(img.duplicate_of)}` : ''}</div>` : ''}
        <div class="queue-idx">#${img.index + 1}</div>
      </div>
      ${themeBadge}
      ${badgeMap[img.status] ?? ''}
      <div class="queue-actions">
        ${pinButton}
        <button class="btn-icon" onclick="openImageModal('${encodedName}', '${statusLabel}')">Ansehen</button>
        ${skipButton}
        <button class="btn-icon danger" onclick="removeImage('${encodedName}')">Entfernen</button>
      </div>
    </div>`;
  }).join('');
}

function toggleQueueThemeFilter() {
  if (!currentStatus.campaign?.active_theme) return;
  queueFilterActiveThemeOnly = !queueFilterActiveThemeOnly;
  renderQueue();
}

function toggleQueueUnpostedFilter() {
  queueFilterUnpostedOnly = !queueFilterUnpostedOnly;
  renderQueue();
}

function changeQueueSort(value) {
  queueSortMode = value || 'campaign';
  renderQueue();
}

async function pinNextImage(encodedName) {
  const filename = decodeURIComponent(encodedName);
  const item = queueItems.find(entry => entry.name === filename);
  const payload = { filename: item?.pinned ? '' : filename };
  const response = await fetch('/api/images/pin-next', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || !data.ok) {
    alert(data.msg || 'Bild konnte nicht gepinnt werden.');
    return;
  }
  await fetchStatus();
  await fetchQueue();
}

async function setCampaignOverride(campaignName) {
  const response = await fetch('/api/campaign/activate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ campaign_name: campaignName || '' }),
  });
  const data = await response.json();
  if (!response.ok || !data.ok) {
    alert(data.msg || 'Kampagne konnte nicht umgestellt werden.');
    return;
  }
  await fetchStatus();
  await fetchQueue();
  await fetchAnalytics();
}

async function fetchLog() {
  const r    = await fetch('/api/log');
  const data = await r.json();
  const el   = document.getElementById('log-box');
  if (!data.lines || !data.lines.length) {
    el.innerHTML = '<span style="color:var(--muted)">Noch keine Log-Einträge</span>';
    return;
  }
  el.innerHTML = data.lines.map(line => {
    const cls = classify(line);
    return `<div class="log-line-${cls}">${line.replace(/</g,'&lt;')}</div>`;
  }).join('');
  el.scrollTop = el.scrollHeight;
}

async function fetchSchedule() {
  const response = await fetch('/api/schedule');
  const data = await response.json();
  const el = document.getElementById('schedule-list');
  if (!data.entries || !data.entries.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:.82rem;padding:8px">Keine Slots konfiguriert</div>';
    return;
  }
  const labelMap = {
    posted: 'Gepostet',
    next: 'Als Nächstes',
    skipped: 'Übersprungen',
    failed: 'Fehler',
    open: 'Offen',
    pending: 'Geplant',
  };
  el.innerHTML = data.entries.map(item => `
    <div class="schedule-item ${escapeHtml(item.status || 'pending')}">
      <div class="schedule-time">${escapeHtml(item.slot)}</div>
      <div class="schedule-meta">${escapeHtml(labelMap[item.status] || 'Geplant')}<br>${escapeHtml(item.message || '')}</div>
    </div>
  `).join('');
}

async function fetchReels() {
  const [reelsResponse, reelStatusResponse] = await Promise.all([
    fetch('/api/reels'),
    fetch('/api/reels/status'),
  ]);
  const data = await reelsResponse.json();
  currentReelStatus = await reelStatusResponse.json();
  const el = document.getElementById('reel-list');
  renderReelPreview(currentReelStatus);
  if (!data.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:.82rem;padding:8px">Noch keine Reels erzeugt</div>';
    return;
  }

  el.innerHTML = data.map(item => {
    const time = item.time ? new Date(item.time).toLocaleString('de-DE') : '–';
    const encodedPath = encodeURIComponent(item.reel_path || '');
    const sourceCount = Array.isArray(item.source_images) ? item.source_images.length : 0;
    const audioLabel = item.audio_track ? `${item.audio_source}: ${item.audio_track}` : (item.audio_source || '–');
    const publishLabel = item.publish_status || (item.simulation_mode ? 'simulated' : 'created');
    const publishId = item.published_post_id ? ` · Reel-ID ${escapeHtml(item.published_post_id)}` : '';
    return `<div class="reel-item">
      <div>
        <div class="reel-title">${escapeHtml(item.image_name || 'Unbekanntes Bild')}</div>
        <div class="reel-meta">${time} · ${item.duration_seconds || '–'}s · ${sourceCount} Bilder · Slot ${escapeHtml(item.slot || '–')} · Audio ${escapeHtml(audioLabel)} · Status ${escapeHtml(publishLabel)}${publishId}</div>
        <div class="reel-meta">${escapeHtml(item.publish_message || '')}</div>
      </div>
      <div class="reel-actions">
        <button class="btn-icon" onclick="openReelModal('${encodedPath}', '${escapeHtml(item.image_name || 'Unbekanntes Bild')}', '${escapeHtml(publishLabel)}')">Abspielen</button>
        <button class="btn-icon" onclick="window.open('/api/reel-file?path=${encodedPath}', '_blank', 'noopener')">Öffnen</button>
      </div>
    </div>`;
  }).join('');
}

function renderReelPreview(status) {
  const lastBox = document.getElementById('last-reel-box');
  const nextBox = document.getElementById('next-reel-box');

  const lastReel = status.last_reel;
  if (lastReel && lastReel.reel_path) {
    const encodedPath = encodeURIComponent(lastReel.reel_path);
    const label = lastReel.publish_status || (lastReel.simulation_mode ? 'simulated' : 'created');
    const reelIdText = lastReel.published_post_id ? `<br>Reel-ID ${escapeHtml(lastReel.published_post_id)}` : '';
    lastBox.innerHTML = `
      <video controls preload="metadata" src="/api/reel-file?path=${encodedPath}"></video>
      <div class="reel-summary">
        <div class="reel-summary-title">Zuletzt ${escapeHtml(label)}</div>
        <div class="reel-summary-meta">${escapeHtml(lastReel.image_name || 'Unbekanntes Bild')}${reelIdText}<br>${escapeHtml(lastReel.publish_message || '')}</div>
      </div>`;
  } else {
    lastBox.innerHTML = '<div class="no-image">Noch kein Reel simuliert</div>';
  }

  const nextReel = status.next_reel || {};
  if (Array.isArray(nextReel.source_images) && nextReel.source_images.length) {
    const previewBlock = nextReel.preview_path
      ? `<video controls preload="metadata" src="/api/reel-file?path=${encodeURIComponent(nextReel.preview_path)}"></video>`
      : `<div class="reel-preview-stack">${nextReel.source_images.slice(0, 4).map((name, index) =>
          `<div class="reel-preview-tile">
            <img src="/api/source-thumbnail/${encodeURIComponent(name)}" alt="" onerror="this.parentElement.style.display=\'none\'">
            ${index === 0 ? '<strong>Startbild</strong>' : ''}
            <span>${escapeHtml(name)}</span>
          </div>`
        ).join('')}</div>`;
    nextBox.innerHTML = `
      ${previewBlock}
      <div class="reel-summary">
        <div class="reel-summary-title">Als Nächstes</div>
        <div class="reel-summary-meta">${escapeHtml(nextReel.anchor_image || 'Kein Startbild')}<br>${nextReel.image_count || 0} Bilder · ${escapeHtml(status.next_slot || 'Kein Slot geplant')}</div>
        <div class="reel-summary-actions">
          <button class="btn-icon" onclick="window.open('/reels', '_blank', 'noopener')">Bearbeiten</button>
          <button class="btn-icon warn" onclick="previewNextReel()">Vorschau</button>
        </div>
      </div>`;
  } else {
    nextBox.innerHTML = '<div class="no-image">Kein nächstes Reel geplant</div>';
  }
}

async function previewNextReel() {
  setDashboardReelBusy(true, 'Vorschau wird erzeugt...');
  const response = await fetch('/api/reels/preview', { method: 'POST' });
  const payload = await response.json();
  setDashboardReelBusy(false);
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Reel-Vorschau konnte nicht erzeugt werden.');
    return;
  }
  await fetchReels();
}

function setDashboardReelBusy(isBusy, message = 'Reel wird erzeugt...') {
  dashboardReelBusy = isBusy;
  const button = document.getElementById('dashboard-generate-reel');
  const status = document.getElementById('dashboard-reel-status');
  if (button) {
    button.disabled = isBusy;
    button.style.opacity = isBusy ? '.6' : '1';
    button.style.pointerEvents = isBusy ? 'none' : 'auto';
  }
  if (status) {
    status.innerHTML = isBusy ? `<span class="busy-indicator">${escapeHtml(message)}</span>` : '';
  }
}

async function fetchAnalytics() {
  const r = await fetch('/api/analytics');
  const data = await r.json();
  renderCampaignOverview(data.campaign);
  renderSmartSlotOverview(data.smart_slots);
  renderCaptionExperiments(data.caption_experiments);
  renderEngagementActions(data.engagement_actions);
  renderContentQualityOverview(data.content_quality);

  // Engagement trend alert
  const statusEl = document.getElementById('analytics-engagement-status');
  if (data.engagement_trend !== null && data.engagement_trend !== undefined) {
    const isLow = data.engagement_trend < data.engagement_threshold;
    statusEl.innerHTML = isLow
      ? `<div class="analytics-engagement-alert">⚠ Niedriges Engagement erkannt: Ø ${data.engagement_trend} Score (letzte 5 Posts). Schwellenwert: ${data.engagement_threshold}. Bilder oder Posting-Zeiten anpassen.</div>`
      : `<div class="analytics-engagement-ok">✓ Engagement normal: Ø ${data.engagement_trend} Score (letzte 5 Posts mit Daten)</div>`;
  } else {
    statusEl.innerHTML = `<div class="analytics-no-data">Noch keine Engagement-Daten (${data.posts_with_engagement} von ${data.total_posts} Posts mit Daten)</div>`;
  }

  // Hashtags
  const hashEl = document.getElementById('analytics-hashtags');
  if (data.hashtag_performance && data.hashtag_performance.length) {
    const maxScore = data.hashtag_performance[0].avg_score || 1;
    hashEl.innerHTML = `<table class="analytics-hashtag-table">
      <thead><tr><th>Hashtag</th><th>Posts</th><th>Ø Score</th><th></th></tr></thead>
      <tbody>${data.hashtag_performance.slice(0, 10).map(h => {
        const pct = Math.round((h.avg_score / maxScore) * 100);
        const color = pct > 66 ? 'green' : pct > 33 ? 'yellow' : 'red';
        return `<tr>
          <td>${escapeHtml(h.tag)}</td>
          <td style="color:var(--muted)">${h.posts}</td>
          <td style="color:var(--muted)">${h.avg_score}</td>
          <td style="width:80px"><div class="analytics-bar-track"><div class="analytics-bar-fill ${color}" style="width:${pct}%"></div></div></td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
  } else {
    hashEl.innerHTML = '<div class="analytics-no-data">Noch keine Hashtag-Daten</div>';
  }

  const commentEl = document.getElementById('analytics-auto-comments');
  const commentData = data.auto_comment || {};
  const recentComments = Array.isArray(commentData.recent) ? commentData.recent : [];
  if (!commentData.template_used && !commentData.ollama_used && !recentComments.length) {
    commentEl.innerHTML = '<div class="analytics-no-data">Noch keine Daten zur Kommentar-Engine</div>';
  } else {
    commentEl.innerHTML = `
      <div class="analytics-engagement-ok">
        Templates genutzt: ${commentData.template_used ?? 0} · Ollama genutzt: ${commentData.ollama_used ?? 0} · Cache-Treffer: ${commentData.cache_hits ?? 0}<br>
        Ollama erzeugt: ${commentData.ollama_generated ?? 0} · Ollama gefiltert: ${commentData.ollama_filtered ?? 0} · Template-Fallbacks: ${commentData.template_fallbacks ?? 0} · Cache: ${commentData.cache_size ?? 0}
      </div>
      ${recentComments.length ? `<table class="analytics-hashtag-table">
        <thead><tr><th>Zeit</th><th>Quelle</th><th>Status</th><th>Text</th></tr></thead>
        <tbody>${recentComments.map(item => `
          <tr>
            <td style="color:var(--muted)">${escapeHtml(item.time ? new Date(item.time).toLocaleString('de-DE') : '–')}</td>
            <td style="color:var(--muted)">${escapeHtml(item.source || '–')}</td>
            <td style="color:var(--muted)">${escapeHtml(item.status || '–')}</td>
            <td>${escapeHtml(item.text || '')}</td>
          </tr>`).join('')}
        </tbody>
      </table>` : ''}
    `;
  }

  // Weekday performance
  const wdEl = document.getElementById('analytics-weekday');
  const wdEntries = Object.entries(data.weekday_performance || {}).sort((a, b) => b[1] - a[1]);
  if (wdEntries.length) {
    const maxWd = wdEntries[0][1] || 1;
    wdEl.innerHTML = wdEntries.slice(0, 8).map(([slot, score]) => {
      const pct = Math.round((score / maxWd) * 100);
      const color = pct > 66 ? 'green' : pct > 33 ? 'yellow' : 'red';
      return `<div class="analytics-bar-row">
        <div class="analytics-bar-label" title="${escapeHtml(slot)}">${escapeHtml(slot)}</div>
        <div class="analytics-bar-track"><div class="analytics-bar-fill ${color}" style="width:${pct}%"></div></div>
        <div class="analytics-bar-val">${score}</div>
      </div>`;
    }).join('');
  } else {
    wdEl.innerHTML = '<div class="analytics-no-data">Noch keine Zeitdaten</div>';
  }

  const hooksEl = document.getElementById('analytics-hooks');
  const hookEntries = Array.isArray(data.hook_performance) ? data.hook_performance : [];
  if (hookEntries.length) {
    const maxHook = hookEntries[0].avg_score || 1;
    hooksEl.innerHTML = hookEntries.map(item => {
      const pct = Math.round((item.avg_score / maxHook) * 100);
      const color = pct > 66 ? 'green' : pct > 33 ? 'yellow' : 'red';
      return `<div class="analytics-bar-row">
        <div class="analytics-bar-label" title="${escapeHtml(item.label)}">${escapeHtml(item.label)}</div>
        <div class="analytics-bar-track"><div class="analytics-bar-fill ${color}" style="width:${pct}%"></div></div>
        <div class="analytics-bar-val">${item.avg_score}</div>
      </div>`;
    }).join('');
  } else {
    hooksEl.innerHTML = '<div class="analytics-no-data">Noch keine Hook-Daten</div>';
  }

  const ctasEl = document.getElementById('analytics-ctas');
  const ctaEntries = Array.isArray(data.cta_performance) ? data.cta_performance : [];
  if (ctaEntries.length) {
    const maxCta = ctaEntries[0].avg_score || 1;
    ctasEl.innerHTML = ctaEntries.map(item => {
      const pct = Math.round((item.avg_score / maxCta) * 100);
      const color = pct > 66 ? 'green' : pct > 33 ? 'yellow' : 'red';
      return `<div class="analytics-bar-row">
        <div class="analytics-bar-label" title="${escapeHtml(item.label)}">${escapeHtml(item.label)}</div>
        <div class="analytics-bar-track"><div class="analytics-bar-fill ${color}" style="width:${pct}%"></div></div>
        <div class="analytics-bar-val">${item.avg_score}</div>
      </div>`;
    }).join('');
  } else {
    ctasEl.innerHTML = '<div class="analytics-no-data">Noch keine CTA-Daten</div>';
  }

  const formatsEl = document.getElementById('analytics-formats');
  const formatEntries = Array.isArray(data.format_performance) ? data.format_performance : [];
  if (formatEntries.length) {
    formatsEl.innerHTML = `<div class="analytics-pill-row">${formatEntries.map(item => `
      <div class="analytics-pill">
        <strong>${escapeHtml(item.label)}</strong>
        <span>Ø Score ${item.avg_score} · ${item.posts} Posts · Best ${item.best_score}</span>
      </div>`).join('')}</div>`;
  } else {
    formatsEl.innerHTML = '<div class="analytics-no-data">Noch keine Format-Daten</div>';
  }

  const topPostsEl = document.getElementById('analytics-top-posts');
  const topPosts = Array.isArray(data.top_posts) ? data.top_posts : [];
  if (topPosts.length) {
    topPostsEl.innerHTML = `<table class="analytics-hashtag-table">
      <thead><tr><th>Format</th><th>Datei</th><th>Slot</th><th>Score</th></tr></thead>
      <tbody>${topPosts.map(item => `
        <tr>
          <td style="color:var(--muted)">${escapeHtml(item.content_type === 'reel' ? 'Reel' : 'Bild')}</td>
          <td title="${escapeHtml(item.file || '')}">${escapeHtml(item.file || '–')}</td>
          <td style="color:var(--muted)">${escapeHtml(item.slot || '–')}</td>
          <td style="color:var(--muted)">${item.score}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
  } else {
    topPostsEl.innerHTML = '<div class="analytics-no-data">Noch keine Top-Posts</div>';
  }

  // Caption feature weights
  const weightsEl = document.getElementById('analytics-weights');
  const hintEl = document.getElementById('analytics-weights-data-hint');
  const FEATURE_LABELS = {
    starts_with_question: 'Hook: Frage',
    starts_with_exclamation: 'Hook: Ausrufezeichen',
    has_emoji_hook: 'Hook: Emoji',
    ends_with_question: 'Ende: Frage',
    optimal_length: 'Optimale Länge',
  };
  const wEntries = Object.entries(data.caption_weights || {});
  hintEl.textContent = data.posts_with_engagement > 0 ? `(${data.posts_with_engagement} Posts mit Daten)` : '';
  if (wEntries.length) {
    const maxW = Math.max(...wEntries.map(e => e[1]), 1);
    weightsEl.innerHTML = wEntries.sort((a,b) => b[1]-a[1]).map(([feat, w]) => {
      const label = FEATURE_LABELS[feat] || feat;
      const pct = Math.round(Math.min((w / Math.max(maxW, 1.5)) * 100, 100));
      const color = w > 1.15 ? 'green' : w > 0.9 ? 'yellow' : 'red';
      return `<div class="analytics-bar-row">
        <div class="analytics-bar-label">${escapeHtml(label)}</div>
        <div class="analytics-bar-track"><div class="analytics-bar-fill ${color}" style="width:${pct}%"></div></div>
        <div class="analytics-bar-val">${w.toFixed(2)}x</div>
      </div>`;
    }).join('');
  } else {
    weightsEl.innerHTML = '<div class="analytics-no-data">Noch keine Engagement-Daten (mind. 3 Posts)</div>';
  }
}

async function fetchOutreachAssist() {
  const response = await fetch('/api/outreach-assist');
  const data = await response.json();
  renderOutreachAssist(data);
}

function renderOutreachAssist(data) {
  const summaryEl = document.getElementById('outreach-summary');
  const listEl = document.getElementById('outreach-list');
  const items = Array.isArray(data.items) ? data.items : [];
  summaryEl.textContent = `Offen: ${data.pending ?? 0} · Genutzt: ${data.used ?? 0} · Uebersprungen: ${data.skipped ?? 0}`;

  if (!items.length) {
    listEl.innerHTML = '<div class="analytics-no-data">Noch keine Comment-Assist-Eintraege</div>';
    return;
  }

  listEl.innerHTML = items.map(item => {
    const status = item.status || 'pending';
    const badgeClass = status === 'used' ? 'badge-used' : status === 'skipped' ? 'badge-skipped' : 'badge-pending';
    const suggestions = Array.isArray(item.suggestions) ? item.suggestions : [];
    return `
      <div class="outreach-item">
        <div class="outreach-head">
          <div class="outreach-title">${escapeHtml(item.creator_handle || 'Ohne Handle')} <span class="badge ${badgeClass}">${escapeHtml(status)}</span></div>
          <div class="outreach-meta">${escapeHtml((item.platform || 'instagram').toUpperCase())} · ${escapeHtml(item.theme || 'ohne Thema')}</div>
        </div>
        <div class="outreach-notes">${escapeHtml(item.post_caption || 'Kein Fremdpost-Text hinterlegt.')}</div>
        ${item.note ? `<div class="outreach-notes">Notiz: ${escapeHtml(item.note)}</div>` : ''}
        ${item.selected_comment ? `<div class="outreach-notes">Genutzt: ${escapeHtml(item.selected_comment)}</div>` : ''}
        ${item.post_url ? `<div class="outreach-meta"><a href="${escapeHtml(item.post_url)}" target="_blank" rel="noopener">Post oeffnen</a></div>` : ''}
        <div class="outreach-suggestions">
          ${suggestions.map(text => `
            <div class="outreach-suggestion">
              <div class="outreach-suggestion-text">${escapeHtml(text)}</div>
              <div class="outreach-item-actions">
                <button class="btn-icon" onclick="copyOutreachText(${JSON.stringify(text)})">Kopieren</button>
                <button class="btn-icon" onclick="markOutreachStatus('${escapeHtml(item.id)}', 'used', ${JSON.stringify(text)})">Als genutzt</button>
              </div>
            </div>
          `).join('')}
        </div>
        <div class="outreach-item-actions">
          <button class="btn-icon" onclick="regenerateOutreachSuggestions('${escapeHtml(item.id)}')">Neu mischen</button>
          <button class="btn-icon warn" onclick="markOutreachStatus('${escapeHtml(item.id)}', 'skipped', '')">Ueberspringen</button>
          <button class="btn-icon" onclick="markOutreachStatus('${escapeHtml(item.id)}', 'pending', '')">Offen lassen</button>
        </div>
      </div>
    `;
  }).join('');
}

async function createOutreachSuggestion() {
  const payload = {
    platform: document.getElementById('outreach-platform').value,
    creator_handle: document.getElementById('outreach-handle').value.trim(),
    theme: document.getElementById('outreach-theme').value.trim(),
    post_url: document.getElementById('outreach-url').value.trim(),
    post_caption: document.getElementById('outreach-caption').value.trim(),
    note: document.getElementById('outreach-note').value.trim(),
  };
  const response = await fetch('/api/outreach-assist', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) {
    alert(result.error || 'Comment-Assist konnte nicht erstellt werden.');
    return;
  }
  document.getElementById('outreach-caption').value = '';
  document.getElementById('outreach-note').value = '';
  await fetchOutreachAssist();
}

async function regenerateOutreachSuggestions(id) {
  const response = await fetch('/api/outreach-assist/regenerate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id }),
  });
  const result = await response.json();
  if (!response.ok) {
    alert(result.error || 'Vorschlaege konnten nicht neu erzeugt werden.');
    return;
  }
  await fetchOutreachAssist();
}

async function markOutreachStatus(id, status, selectedComment) {
  const response = await fetch('/api/outreach-assist/mark', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, status, selected_comment: selectedComment }),
  });
  const result = await response.json();
  if (!response.ok) {
    alert(result.error || 'Status konnte nicht gespeichert werden.');
    return;
  }
  await fetchOutreachAssist();
}

async function copyOutreachText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (error) {
    console.warn('Clipboard write failed', error);
  }
}

async function clearCaptionCache() {
  if (!confirm('Caption-Cache leeren? Alle gespeicherten Captions werden gelöscht und beim nächsten Post neu generiert.')) return;
  const r = await fetch('/api/state/clear-caption-cache', { method: 'POST' });
  const data = await r.json();
  alert(data.msg || 'Cache geleert.');
  await refresh();
}

async function refresh() {
  await fetchStatus();
  await Promise.all([fetchHistory(), fetchQueue(), fetchLog(), fetchReels(), fetchSchedule(), fetchAnalytics(), fetchOutreachAssist()]);
}

function getImageByName(name) {
  return queueItems.find(item => item.name === name) || null;
}

function openImageModal(encodedName, fallbackStatus = '') {
  const name = decodeURIComponent(encodedName);
  const item = getImageByName(name);
  selectedImage = {
    name,
    status: item?.status || '',
  };

  document.getElementById('modal-title').textContent = name;
  document.getElementById('modal-status').textContent = fallbackStatus || item?.status || '';
  document.getElementById('modal-filename').textContent = name;
  document.getElementById('modal-image').src = '/api/thumbnail/' + encodeURIComponent(name);
  document.getElementById('image-modal').classList.add('open');
  document.getElementById('modal-skip').style.display = selectedImage.status === 'next' ? 'block' : 'none';
}

function closeModal(event) {
  if (event && event.target && event.target.id !== 'image-modal') {
    return;
  }
  document.getElementById('image-modal').classList.remove('open');
}

function openReelModal(encodedPath, title = 'Reel', status = '') {
  selectedReelPath = decodeURIComponent(encodedPath);
  document.getElementById('reel-modal-title').textContent = title;
  document.getElementById('reel-modal-status').textContent = status;
  document.getElementById('reel-modal-filename').textContent = selectedReelPath;
  document.getElementById('reel-modal-video').src = '/api/reel-file?path=' + encodeURIComponent(selectedReelPath);
  document.getElementById('reel-modal').classList.add('open');
}

function closeReelModal(event) {
  if (event && event.target && event.target.id !== 'reel-modal') {
    return;
  }
  const video = document.getElementById('reel-modal-video');
  video.pause();
  video.removeAttribute('src');
  video.load();
  document.getElementById('reel-modal').classList.remove('open');
}

function openReelInTab() {
  if (!selectedReelPath) {
    return;
  }
  window.open('/api/reel-file?path=' + encodeURIComponent(selectedReelPath), '_blank', 'noopener');
}

async function generateNowReel() {
  setDashboardReelBusy(true, 'Reel wird manuell verarbeitet...');
  const response = await fetch('/api/reels/generate-now', { method: 'POST' });
  const payload = await response.json();
  setDashboardReelBusy(false);
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Reel konnte nicht verarbeitet werden.');
    return;
  }
  await fetchReels();
  if (payload.msg) {
    alert(payload.msg);
  }
  if (payload.reel_path) {
    openReelModal(encodeURIComponent(payload.reel_path), 'Manuell verarbeitetes Reel', payload.publish_status || 'manual');
  }
}

async function postNowImage() {
  const button = document.getElementById('post-now-top');
  if (button) {
    button.disabled = true;
    button.style.opacity = '.6';
    button.style.pointerEvents = 'none';
  }

  try {
    const response = await fetch('/api/poster/post-now', { method: 'POST' });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      alert(payload.msg || 'Bild konnte nicht sofort gepostet werden.');
      return;
    }
    alert(payload.msg || 'Bild wurde sofort gepostet.');
    await refresh();
  } finally {
    if (button) {
      button.disabled = false;
      button.style.opacity = '1';
      button.style.pointerEvents = 'auto';
    }
  }
}

function openModalImageInTab() {
  if (!selectedImage) {
    return;
  }
  window.open('/api/thumbnail/' + encodeURIComponent(selectedImage.name), '_blank', 'noopener');
}

async function removeImage(encodedName) {
  const name = decodeURIComponent(encodedName);
  if (!confirm(`Soll ${name} wirklich aus der Liste entfernt werden?`)) {
    return;
  }

  const response = await fetch('/api/images/remove', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: name }),
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Bild konnte nicht entfernt werden.');
    return;
  }

  if (selectedImage?.name === name) {
    closeModal();
  }

  await refresh();
}

async function removeSelectedImage() {
  if (!selectedImage) {
    return;
  }
  await removeImage(encodeURIComponent(selectedImage.name));
}

async function skipNextImage(event) {
  if (event) {
    event.stopPropagation();
  }

  const response = await fetch('/api/images/skip-next', { method: 'POST' });
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Nächstes Bild konnte nicht übersprungen werden.');
    return;
  }

  if (selectedImage && selectedImage.status === 'next') {
    closeModal();
  }

  await refresh();
}

async function skipSelectedIfNext() {
  if (selectedImage?.status !== 'next') {
    return;
  }
  await skipNextImage();
}

async function posterAction(action) {
  await fetch('/api/poster/' + action, { method: 'POST' });
  setTimeout(refresh, 800);
}

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    closeModal();
    closeReelModal();
  }
});

// Auto-Refresh alle 15 Sekunden
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


REELS_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reel Monitor</title>
<style>
  :root {
    --bg: #0b1020;
    --card: #141b2f;
    --border: #26314f;
    --accent: #5eead4;
    --accent-2: #f59e0b;
    --text: #eef2ff;
    --muted: #94a3b8;
    --green: #22c55e;
    --radius: 16px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    min-height: 100vh; color: var(--text);
    font-family: 'Segoe UI', system-ui, sans-serif;
    background:
      radial-gradient(circle at top right, rgba(94,234,212,.12), transparent 30%),
      radial-gradient(circle at bottom left, rgba(245,158,11,.10), transparent 32%),
      var(--bg);
  }
  header {
    padding: 18px 24px; display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid rgba(255,255,255,.06); background: rgba(11,16,32,.72); backdrop-filter: blur(10px);
    position: sticky; top: 0; z-index: 50;
  }
  h1 { font-size: 1.1rem; letter-spacing: .04em; }
  h1 span { color: var(--accent); }
  main { padding: 24px; display: grid; gap: 18px; }
  .grid { display: grid; gap: 18px; grid-template-columns: 1.1fr .9fr; }
  .card {
    background: rgba(20,27,47,.9); border: 1px solid rgba(148,163,184,.14);
    border-radius: var(--radius); overflow: hidden;
  }
  .card-head {
    padding: 14px 18px; border-bottom: 1px solid rgba(148,163,184,.12);
    color: var(--muted); text-transform: uppercase; font-size: .76rem; letter-spacing: .12em;
    display: flex; align-items: center; justify-content: space-between;
  }
  .card-body { padding: 18px; }
  .kpis { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
  .kpi { padding: 16px; border-radius: 14px; background: rgba(255,255,255,.03); border: 1px solid rgba(148,163,184,.12); }
  .kpi-label { color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .12em; }
  .kpi-value { font-size: 1.5rem; margin-top: 10px; font-weight: 700; }
  .queue-grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
  .queue-card { background: rgba(255,255,255,.03); border: 1px solid rgba(148,163,184,.12); border-radius: 14px; overflow: hidden; }
  .queue-card.dragging { opacity: .45; transform: scale(.98); }
  .queue-card.drop-target { border-color: rgba(94,234,212,.55); box-shadow: inset 0 0 0 1px rgba(94,234,212,.35); }
  .queue-card img { width: 100%; aspect-ratio: 9/16; object-fit: cover; display: block; }
  .queue-meta { padding: 12px; }
  .queue-role { color: var(--accent); font-size: .72rem; text-transform: uppercase; letter-spacing: .1em; }
  .queue-name { margin-top: 8px; font-size: .82rem; word-break: break-all; }
  .queue-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  .reel-status-grid { display: grid; gap: 18px; grid-template-columns: 1fr 1fr; }
  .reel-focus-card { background: rgba(255,255,255,.03); border: 1px solid rgba(148,163,184,.12); border-radius: 14px; overflow: hidden; }
  .reel-focus-card video {
    width: 100%; max-height: min(44vh, 560px); object-fit: contain; display: block; background: #050816;
  }
  .reel-focus-stack {
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; padding: 12px; max-height: min(44vh, 560px); overflow: hidden;
  }
  .reel-focus-thumb {
    position: relative; border-radius: 12px; overflow: hidden; border: 1px solid rgba(148,163,184,.16); background: #050816;
  }
  .reel-focus-thumb img { width: 100%; height: min(21vh, 260px); object-fit: cover; display: block; }
  .reel-focus-thumb span {
    position: absolute; left: 8px; right: 8px; bottom: 8px; padding: 5px 8px; border-radius: 999px;
    background: rgba(5,8,22,.78); color: #f8fafc; font-size: .68rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .reel-focus-thumb strong {
    position: absolute; top: 8px; left: 8px; padding: 4px 8px; border-radius: 999px; background: rgba(94,234,212,.18);
    color: var(--accent); font-size: .64rem; letter-spacing: .06em; text-transform: uppercase;
  }
  .reel-focus-meta { padding: 12px; font-size: .8rem; color: var(--muted); line-height: 1.6; }
  .reel-editor { display: grid; gap: 14px; }
  .reel-caption-box {
    width: 100%; min-height: 120px; resize: vertical; border-radius: 12px; border: 1px solid rgba(148,163,184,.16);
    background: rgba(6,10,22,.85); color: var(--text); padding: 12px; font-family: inherit; line-height: 1.5;
  }
  .reel-editor-actions { display: flex; gap: 10px; flex-wrap: wrap; }
  .reel-list { display: grid; gap: 14px; }
  .reel-item { display: grid; gap: 14px; grid-template-columns: 220px minmax(0, 1fr); padding: 14px; border-radius: 14px; background: rgba(255,255,255,.03); border: 1px solid rgba(148,163,184,.12); }
  .reel-item video { width: 100%; border-radius: 12px; background: #050816; }
  .reel-title { font-size: .9rem; font-weight: 700; }
  .reel-meta { margin-top: 8px; color: var(--muted); font-size: .78rem; }
  .thumb-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .thumb-row img { width: 56px; height: 72px; object-fit: cover; border-radius: 10px; border: 1px solid rgba(148,163,184,.18); }
  .log-box {
    background: #060a16; border-radius: 12px; padding: 14px;
    max-height: 260px; overflow-y: auto; font-family: 'Cascadia Code', monospace; font-size: .76rem; line-height: 1.7;
    color: #9fb0c9;
  }
  .btn {
    border: 1px solid rgba(148,163,184,.18); color: var(--text); background: transparent;
    border-radius: 10px; padding: 8px 12px; cursor: pointer;
  }
  .schedule-grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
  .schedule-item { padding: 14px; border-radius: 14px; background: rgba(255,255,255,.03); border: 1px solid rgba(148,163,184,.12); }
  .schedule-item.next { border-color: rgba(94,234,212,.45); }
  .schedule-item.posted { border-color: rgba(34,197,94,.4); }
  .schedule-item.failed { border-color: rgba(239,68,68,.4); }
  .schedule-item.skipped, .schedule-item.open { border-color: rgba(245,158,11,.4); }
  .schedule-time { font-size: .95rem; font-weight: 700; }
  .schedule-meta { margin-top: 8px; color: var(--muted); font-size: .76rem; line-height: 1.5; }
  .busy-indicator {
    display: inline-flex; align-items: center; gap: 8px; font-size: .78rem; color: var(--accent-2);
  }
  .busy-indicator::before {
    content: ''; width: 10px; height: 10px; border-radius: 50%;
    background: var(--accent-2); box-shadow: 0 0 0 0 rgba(245,158,11,.5); animation: pulse 1.2s infinite;
  }
  .muted { color: var(--muted); }
  @media (max-width: 980px) {
    .grid { grid-template-columns: 1fr; }
    .reel-status-grid { grid-template-columns: 1fr; }
    .reel-item { grid-template-columns: 1fr; }
  }
  @media (max-height: 900px) {
    .reel-focus-card video { max-height: min(36vh, 420px); }
    .reel-focus-stack { max-height: min(36vh, 420px); }
    .reel-focus-stack img { height: min(17vh, 190px); }
  }
</style>
</head>
<body>
<header>
  <h1>Auto-Poster <span>Reel Monitor</span></h1>
  <div style="display:flex;gap:10px;align-items:center;">
    <button class="btn" onclick="window.location.href='/'">Hauptdashboard</button>
    <button class="btn" onclick="window.open('/instagram', '_blank', 'noopener')">Instagram</button>
    <button class="btn" onclick="window.open('/music', '_blank', 'noopener')">Musikbibliothek</button>
    <button class="btn" onclick="refreshReelsWindow()">Aktualisieren</button>
  </div>
</header>
<main>
  <div class="kpis" id="reel-kpis"></div>
  <div class="card">
    <div class="card-head">Posting-Zeitplan</div>
    <div class="card-body">
      <div class="schedule-grid" id="reel-schedule">Lädt…</div>
    </div>
  </div>
  <div class="reel-status-grid">
    <div class="card">
      <div class="card-head">Zuletzt simuliertes Reel</div>
      <div class="card-body" id="last-reel-monitor">Lädt…</div>
    </div>
    <div class="card">
      <div class="card-head">Nächstes Reel</div>
      <div class="card-body" id="next-reel-monitor">Lädt…</div>
    </div>
  </div>
  <div class="grid">
    <div class="card">
      <div class="card-head">
        Nächstes Multi-Image-Reel
        <span id="next-slot-label" class="muted"></span>
      </div>
      <div class="card-body">
        <div class="queue-grid" id="reel-queue">Lädt…</div>
      </div>
    </div>
    <div class="card">
      <div class="card-head">Nächstes Reel bearbeiten</div>
      <div class="card-body">
        <div class="reel-editor">
          <textarea id="reel-caption-editor" class="reel-caption-box" placeholder="Caption für das nächste Reel"></textarea>
          <div class="muted" id="reel-caption-meta">Textquelle: –</div>
          <div class="reel-editor-actions">
            <button class="btn" data-reel-action onclick="saveNextReelEdits()">Änderungen speichern</button>
            <button class="btn" data-reel-action onclick="regenerateNextReelCaption()">Text neu generieren</button>
            <button class="btn" data-reel-action onclick="generateReelPreview()">Vorschau erzeugen</button>
            <button class="btn" data-reel-action onclick="generateNowReelWindow()">Reel jetzt posten</button>
            <button class="btn" data-reel-action onclick="resetNextReel()">Zurücksetzen</button>
            <button class="btn" data-reel-action onclick="skipNextReel()">Nächstes Reel überspringen</button>
          </div>
          <div id="reel-busy-status"></div>
          <div class="log-box" id="reel-log">Lädt…</div>
        </div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-head">Zuletzt erzeugte Reels</div>
    <div class="card-body">
      <div class="reel-list" id="generated-reels">Lädt…</div>
    </div>
  </div>
</main>

<script>
let currentReelPlan = null;
let dragReelIndex = null;
let reelWindowBusy = false;

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function classifyLine(line) {
  if (line.includes('ERROR')) return 'error';
  if (line.includes('WARNING')) return 'warning';
  return 'info';
}

function setReelWindowBusy(isBusy, message = 'Reel wird verarbeitet...') {
  reelWindowBusy = isBusy;
  const status = document.getElementById('reel-busy-status');
  const buttons = document.querySelectorAll('[data-reel-action]');
  buttons.forEach(button => {
    button.disabled = isBusy;
    button.style.opacity = isBusy ? '.6' : '1';
    button.style.pointerEvents = isBusy ? 'none' : 'auto';
  });
  if (status) {
    status.innerHTML = isBusy ? `<span class="busy-indicator">${esc(message)}</span>` : '';
  }
}

async function persistDraggedReelOrder() {
  if (!currentReelPlan || !currentReelPlan.source_images) {
    return;
  }
  const response = await fetch('/api/reels/queue/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source_images: currentReelPlan.source_images,
      caption: document.getElementById('reel-caption-editor').value,
    }),
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Reel-Reihenfolge konnte nicht gespeichert werden.');
    return;
  }
  await refreshReelsWindow();
}

function handleReelDragStart(index, event) {
  dragReelIndex = index;
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', String(index));
  event.currentTarget.classList.add('dragging');
}

function handleReelDragOver(index, event) {
  event.preventDefault();
  if (index === 0 || dragReelIndex === null || dragReelIndex === 0 || dragReelIndex === index) {
    return;
  }
  event.dataTransfer.dropEffect = 'move';
  event.currentTarget.classList.add('drop-target');
}

function handleReelDragLeave(event) {
  event.currentTarget.classList.remove('drop-target');
}

async function handleReelDrop(index, event) {
  event.preventDefault();
  event.currentTarget.classList.remove('drop-target');
  const sourceIndex = dragReelIndex;
  dragReelIndex = null;
  if (sourceIndex === null || sourceIndex === 0 || index === 0 || sourceIndex === index || !currentReelPlan?.source_images) {
    return;
  }
  const names = [...currentReelPlan.source_images];
  const [moved] = names.splice(sourceIndex, 1);
  names.splice(index, 0, moved);
  currentReelPlan.source_images = names;
  setReelWindowBusy(true, 'Neue Reihenfolge wird gespeichert...');
  await persistDraggedReelOrder();
  setReelWindowBusy(false);
}

function handleReelDragEnd(event) {
  dragReelIndex = null;
  document.querySelectorAll('.queue-card').forEach(card => card.classList.remove('dragging', 'drop-target'));
  event.currentTarget.classList.remove('dragging');
}

async function fetchReelStatus() {
  const response = await fetch('/api/reels/status');
  const data = await response.json();
  document.getElementById('next-slot-label').textContent = data.next_slot ? 'Nächster Slot: ' + data.next_slot : 'Kein Slot geplant';
  document.getElementById('reel-kpis').innerHTML = `
    <div class="kpi"><div class="kpi-label">Reels aktiv</div><div class="kpi-value">${data.enabled ? 'Ja' : 'Nein'}</div></div>
    <div class="kpi"><div class="kpi-label">Simulation</div><div class="kpi-value">${data.simulation_mode ? 'Ja' : 'Nein'}</div></div>
    <div class="kpi"><div class="kpi-label">Bilder pro Reel</div><div class="kpi-value">${data.images_per_reel ?? '–'}</div></div>
    <div class="kpi"><div class="kpi-label">Länge</div><div class="kpi-value">${data.duration_seconds ?? '–'}s</div></div>
    <div class="kpi"><div class="kpi-label">Erzeugt gesamt</div><div class="kpi-value">${data.generated_count ?? 0}</div></div>
  `;
  renderReelMonitorFocus(data);
}

async function fetchWindowSchedule() {
  const response = await fetch('/api/schedule');
  const data = await response.json();
  const el = document.getElementById('reel-schedule');
  const labelMap = {
    posted: 'Gepostet',
    next: 'Als Nächstes',
    skipped: 'Übersprungen',
    failed: 'Fehler',
    open: 'Offen',
    pending: 'Geplant',
  };
  el.innerHTML = (data.entries || []).map(item => `
    <div class="schedule-item ${esc(item.status || 'pending')}">
      <div class="schedule-time">${esc(item.slot)}</div>
      <div class="schedule-meta">${esc(labelMap[item.status] || 'Geplant')}<br>${esc(item.message || '')}</div>
    </div>
  `).join('') || '<div class="muted">Keine Slots vorhanden.</div>';
}

function renderReelMonitorFocus(data) {
  const lastEl = document.getElementById('last-reel-monitor');
  const nextEl = document.getElementById('next-reel-monitor');
  const lastReel = data.last_reel;
  if (lastReel && lastReel.reel_path) {
    const path = encodeURIComponent(lastReel.reel_path || '');
    const status = lastReel.publish_status || (lastReel.simulation_mode ? 'simulated' : 'created');
    lastEl.innerHTML = `
      <div class="reel-focus-card">
        <video controls preload="metadata" src="/api/reel-file?path=${path}"></video>
        <div class="reel-focus-meta">${esc(lastReel.image_name || 'Unbekanntes Bild')}<br>Status: ${esc(status)}<br>${esc(lastReel.publish_message || '')}</div>
      </div>
    `;
  } else {
    lastEl.innerHTML = '<div class="muted">Noch kein Reel simuliert.</div>';
  }

  const nextReel = data.next_reel || {};
  if (Array.isArray(nextReel.source_images) && nextReel.source_images.length) {
    const previewBlock = nextReel.preview_path
      ? `<video controls preload="metadata" src="/api/reel-file?path=${encodeURIComponent(nextReel.preview_path || '')}"></video>`
      : `<div class="reel-focus-stack">
          ${nextReel.source_images.slice(0, 4).map((name, index) => `<div class="reel-focus-thumb">
            <img src="/api/source-thumbnail/${encodeURIComponent(name)}" alt="${esc(name)}" title="${esc(name)}" onerror="this.parentElement.style.display='none'">
            ${index === 0 ? '<strong>Startbild</strong>' : ''}
            <span>${esc(name)}</span>
          </div>`).join('')}
        </div>`;
    nextEl.innerHTML = `
      <div class="reel-focus-card">
        ${previewBlock}
        <div class="reel-focus-meta">Startbild: ${esc(nextReel.anchor_image || '–')}<br>Bilder: ${nextReel.image_count || 0}<br>Geplanter Slot: ${esc(data.next_slot || 'Kein Slot geplant')}</div>
      </div>
    `;
  } else {
    nextEl.innerHTML = '<div class="muted">Kein nächstes Reel geplant.</div>';
  }
}

async function fetchReelQueue() {
  const response = await fetch('/api/reels/plan');
  const data = await response.json();
  currentReelPlan = data;
  const el = document.getElementById('reel-queue');
  document.getElementById('reel-caption-editor').value = data.caption || '';
  const captionMeta = document.getElementById('reel-caption-meta');
  if (captionMeta) {
    const sourceLabel = data.caption_source ? `Textquelle: ${data.caption_source}` : 'Textquelle: –';
    const timeLabel = data.caption_updated_at ? ` · ${new Date(data.caption_updated_at).toLocaleString('de-DE')}` : '';
    captionMeta.textContent = sourceLabel + timeLabel;
  }
  if (!data.source_images || !data.source_images.length) {
    el.innerHTML = '<div class="muted">Kein Multi-Image-Reel geplant.</div>';
    return;
  }

  el.innerHTML = data.source_images.map((name, index) => `
    <div class="queue-card" ${index > 0 ? 'draggable="true"' : ''}
         ondragstart="handleReelDragStart(${index}, event)"
         ondragover="handleReelDragOver(${index}, event)"
         ondragleave="handleReelDragLeave(event)"
         ondrop="handleReelDrop(${index}, event)"
         ondragend="handleReelDragEnd(event)">
      <img src="/api/source-thumbnail/${encodeURIComponent(name)}" alt="" onerror="this.style.visibility='hidden'">
      <div class="queue-meta">
        <div class="queue-role">${index === 0 ? 'Startbild' : 'Zusatzbild ' + (index + 1)}</div>
        <div class="queue-name">${esc(name)}${index > 0 ? ' · ziehen zum Umsortieren' : ''}</div>
        <div class="queue-actions">
          ${index > 1 ? `<button class="btn" onclick="moveReelImage('${encodeURIComponent(name)}', 'up')">Nach oben</button>` : ''}
          ${index > 0 && index < data.source_images.length - 1 ? `<button class="btn" onclick="moveReelImage('${encodeURIComponent(name)}', 'down')">Nach unten</button>` : ''}
          ${index > 0 ? `<button class="btn" onclick="removeReelImage('${encodeURIComponent(name)}')">Entfernen</button>` : ''}
        </div>
      </div>
    </div>
  `).join('');
}

async function fetchGeneratedReels() {
  const response = await fetch('/api/reels');
  const data = await response.json();
  const el = document.getElementById('generated-reels');
  if (!data.length) {
    el.innerHTML = '<div class="muted">Noch keine Reels erzeugt.</div>';
    return;
  }

  el.innerHTML = data.map(item => {
    const time = item.time ? new Date(item.time).toLocaleString('de-DE') : '–';
    const path = encodeURIComponent(item.reel_path || '');
    const sourceImages = Array.isArray(item.source_images) ? item.source_images : [];
    const audioLabel = item.audio_track ? `${item.audio_source}: ${item.audio_track}` : (item.audio_source || '–');
    const publishLabel = item.publish_status || (item.simulation_mode ? 'simulated' : 'created');
    return `
      <div class="reel-item">
        <video controls preload="metadata" src="/api/reel-file?path=${path}"></video>
        <div>
          <div class="reel-title">${esc(item.image_name || 'Unbekanntes Bild')}</div>
          <div class="reel-meta">${time} · ${item.duration_seconds || '–'}s · ${sourceImages.length} Bilder · Slot ${esc(item.slot || '–')} · Audio ${esc(audioLabel)} · Status ${esc(publishLabel)}</div>
          <div class="reel-meta">${esc(item.publish_message || '')}</div>
          <div class="thumb-row">
            ${sourceImages.map(name => `<img src="/api/source-thumbnail/${encodeURIComponent(name)}" alt="${esc(name)}" title="${esc(name)}" onerror="this.style.visibility='hidden'">`).join('')}
          </div>
          <div class="queue-actions">
            <button class="btn" onclick="deleteGeneratedReel('${path}')">Löschen</button>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

async function saveNextReelEdits() {
  if (!currentReelPlan || !currentReelPlan.source_images) {
    return;
  }
  setReelWindowBusy(true, 'Änderungen werden gespeichert...');
  const response = await fetch('/api/reels/queue/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source_images: currentReelPlan.source_images,
      caption: document.getElementById('reel-caption-editor').value,
    }),
  });
  const payload = await response.json();
  setReelWindowBusy(false);
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Reel konnte nicht aktualisiert werden.');
    return;
  }
  await refreshReelsWindow();
}

async function regenerateNextReelCaption() {
  setReelWindowBusy(true, 'Reel-Text wird neu erzeugt...');
  const response = await fetch('/api/reels/regenerate-caption', { method: 'POST' });
  const payload = await response.json();
  setReelWindowBusy(false);
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Reel-Text konnte nicht neu erzeugt werden.');
    return;
  }
  await refreshReelsWindow();
}

async function generateReelPreview() {
  await saveNextReelEdits();
  setReelWindowBusy(true, 'Vorschau wird erzeugt...');
  const response = await fetch('/api/reels/preview', { method: 'POST' });
  const payload = await response.json();
  setReelWindowBusy(false);
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Vorschau konnte nicht erzeugt werden.');
    return;
  }
  await refreshReelsWindow();
}

async function generateNowReelWindow() {
  setReelWindowBusy(true, 'Reel wird manuell verarbeitet...');
  const response = await fetch('/api/reels/generate-now', { method: 'POST' });
  const payload = await response.json();
  setReelWindowBusy(false);
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Reel konnte nicht verarbeitet werden.');
    return;
  }
  if (payload.msg) {
    alert(payload.msg);
  }
  await refreshReelsWindow();
}

async function skipNextReel() {
  setReelWindowBusy(true, 'Nächstes Reel wird übersprungen...');
  const response = await fetch('/api/reels/skip-next', { method: 'POST' });
  const payload = await response.json();
  setReelWindowBusy(false);
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Reel konnte nicht übersprungen werden.');
    return;
  }
  await refreshReelsWindow();
}

async function resetNextReel() {
  setReelWindowBusy(true, 'Reel-Plan wird zurückgesetzt...');
  const response = await fetch('/api/reels/reset-next', { method: 'POST' });
  const payload = await response.json();
  setReelWindowBusy(false);
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Reel konnte nicht zurückgesetzt werden.');
    return;
  }
  await refreshReelsWindow();
}

async function removeReelImage(encodedName) {
  const name = decodeURIComponent(encodedName);
  setReelWindowBusy(true, 'Bild wird aus dem Reel entfernt...');
  const response = await fetch('/api/reels/queue/remove-image', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: name }),
  });
  const payload = await response.json();
  setReelWindowBusy(false);
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Bild konnte nicht aus dem Reel entfernt werden.');
    return;
  }
  await refreshReelsWindow();
}

async function moveReelImage(encodedName, direction) {
  const name = decodeURIComponent(encodedName);
  setReelWindowBusy(true, 'Reel-Reihenfolge wird aktualisiert...');
  const response = await fetch('/api/reels/queue/move', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: name, direction }),
  });
  const payload = await response.json();
  setReelWindowBusy(false);
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Reel-Bild konnte nicht verschoben werden.');
    return;
  }
  await refreshReelsWindow();
}

async function deleteGeneratedReel(encodedPath) {
  const reelPath = decodeURIComponent(encodedPath);
  if (!confirm('Soll dieses simulierte Reel wirklich gelöscht werden?')) {
    return;
  }
  setReelWindowBusy(true, 'Reel wird gelöscht...');
  const response = await fetch('/api/reels/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reel_path: reelPath }),
  });
  const payload = await response.json();
  setReelWindowBusy(false);
  if (!response.ok || !payload.ok) {
    alert(payload.msg || 'Reel konnte nicht gelöscht werden.');
    return;
  }
  await refreshReelsWindow();
}

async function fetchReelLog() {
  const response = await fetch('/api/log');
  const data = await response.json();
  const lines = (data.lines || []).filter(line => /Reel|Slot|Caption|ERROR|WARNING/i.test(line));
  const el = document.getElementById('reel-log');
  if (!lines.length) {
    el.innerHTML = '<span class="muted">Noch keine Reel-Logeinträge.</span>';
    return;
  }
  el.innerHTML = lines.map(line => `<div class="line-${classifyLine(line)}">${esc(line)}</div>`).join('');
  el.scrollTop = el.scrollHeight;
}

async function refreshReelsWindow() {
  await Promise.all([fetchReelStatus(), fetchReelQueue(), fetchGeneratedReels(), fetchReelLog(), fetchWindowSchedule()]);
}

refreshReelsWindow();
setInterval(refreshReelsWindow, 15000);
</script>
</body>
</html>
"""


MUSIC_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Musikbibliothek</title>
<style>
  :root {
    --bg: #09111f;
    --card: #111a2b;
    --border: #24324d;
    --text: #edf2ff;
    --muted: #94a3b8;
    --accent: #60a5fa;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #f59e0b;
    --radius: 16px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    min-height: 100vh; color: var(--text);
    font-family: 'Segoe UI', system-ui, sans-serif;
    background:
      radial-gradient(circle at top left, rgba(96,165,250,.12), transparent 34%),
      radial-gradient(circle at bottom right, rgba(34,197,94,.08), transparent 28%),
      var(--bg);
  }
  header {
    position: sticky; top: 0; z-index: 40;
    padding: 18px 24px; display: flex; align-items: center; justify-content: space-between;
    background: rgba(9,17,31,.78); backdrop-filter: blur(10px); border-bottom: 1px solid rgba(148,163,184,.12);
  }
  h1 { font-size: 1.1rem; }
  h1 span { color: var(--accent); }
  main { padding: 24px; display: grid; gap: 18px; }
  .kpis { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }
  .kpi, .card {
    background: rgba(17,26,43,.92); border: 1px solid rgba(148,163,184,.12); border-radius: var(--radius);
  }
  .kpi { padding: 16px; }
  .kpi-label { color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .12em; }
  .kpi-value { font-size: 1.55rem; font-weight: 700; margin-top: 10px; }
  .card-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 18px; border-bottom: 1px solid rgba(148,163,184,.12);
    color: var(--muted); font-size: .76rem; letter-spacing: .12em; text-transform: uppercase;
  }
  .card-body { padding: 18px; }
  .track-list { display: grid; gap: 12px; }
  .track-item {
    padding: 14px; border-radius: 14px; border: 1px solid rgba(148,163,184,.12); background: rgba(255,255,255,.03);
    display: grid; gap: 10px;
  }
  .track-title { font-size: .95rem; font-weight: 700; }
  .track-sub { color: var(--muted); font-size: .78rem; }
  .track-meta { display: flex; flex-wrap: wrap; gap: 8px; }
  .badge {
    display: inline-flex; align-items: center; padding: 5px 10px; border-radius: 999px; font-size: .72rem; font-weight: 700;
    border: 1px solid transparent;
  }
  .badge-ok { background: rgba(34,197,94,.12); color: var(--green); border-color: rgba(34,197,94,.3); }
  .badge-bad { background: rgba(239,68,68,.12); color: var(--red); border-color: rgba(239,68,68,.3); }
  .badge-warn { background: rgba(245,158,11,.12); color: var(--yellow); border-color: rgba(245,158,11,.3); }
  .tag { padding: 5px 10px; border-radius: 999px; font-size: .72rem; background: rgba(96,165,250,.12); color: #bfdbfe; }
  .muted { color: var(--muted); }
  .grid { display: grid; gap: 18px; grid-template-columns: 1fr 1fr; }
  .config-list { display: grid; gap: 8px; font-size: .82rem; }
  .actions { display: flex; gap: 10px; align-items: center; }
  .btn {
    border: 1px solid rgba(148,163,184,.18); color: var(--text); background: transparent;
    border-radius: 10px; padding: 8px 12px; cursor: pointer;
  }
  a { color: #bfdbfe; }
  @media (max-width: 980px) {
    .grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<header>
  <h1>Auto-Poster <span>Musikbibliothek</span></h1>
  <div class="actions">
    <button class="btn" onclick="window.location.href='/'">Hauptdashboard</button>
    <button class="btn" onclick="window.location.href='/instagram'">Instagram</button>
    <button class="btn" onclick="window.location.href='/reels'">Reels</button>
    <button class="btn" onclick="refreshMusicLibrary()">Aktualisieren</button>
  </div>
</header>
<main>
  <div class="kpis" id="music-kpis"></div>
  <div class="grid">
    <div class="card">
      <div class="card-head">Bibliotheksstatus</div>
      <div class="card-body">
        <div class="config-list" id="music-config">Lädt…</div>
      </div>
    </div>
    <div class="card">
      <div class="card-head">Matching-Regeln</div>
      <div class="card-body">
        <div class="config-list muted">
          <div>Caption und Bilddateiname werden automatisch auf Stimmungssignale analysiert.</div>
          <div>Bevorzugte Tags kommen aus den Metafeldern moods, genres, keywords, tags und energy.</div>
          <div>Bei gleichem Trefferbild entscheidet zuerst priority, danach die Rotation.</div>
          <div>Ohne Tag-Treffer greifen die Default-Tags aus der Konfiguration.</div>
        </div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-head">Tracks</div>
    <div class="card-body">
      <div class="track-list" id="music-tracks">Lädt…</div>
    </div>
  </div>
</main>
<script>
function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function badgeClass(status) {
  if (status === 'eligible') return 'badge badge-ok';
  if (status === 'missing_metadata') return 'badge badge-warn';
  return 'badge badge-bad';
}

function badgeLabel(status) {
  if (status === 'eligible') return 'Freigegeben';
  if (status === 'missing_metadata') return 'Metadaten fehlen';
  return 'Blockiert';
}

async function refreshMusicLibrary() {
  const response = await fetch('/api/music-library');
  const data = await response.json();
  const summary = data.summary || {};
  const tracks = data.tracks || [];

  document.getElementById('music-kpis').innerHTML = `
    <div class="kpi"><div class="kpi-label">Tracks gesamt</div><div class="kpi-value">${summary.total ?? 0}</div></div>
    <div class="kpi"><div class="kpi-label">Freigegeben</div><div class="kpi-value">${summary.eligible ?? 0}</div></div>
    <div class="kpi"><div class="kpi-label">Blockiert</div><div class="kpi-value">${summary.blocked ?? 0}</div></div>
    <div class="kpi"><div class="kpi-label">Auto-Matching</div><div class="kpi-value">${summary.auto_match_enabled ? 'An' : 'Aus'}</div></div>
  `;

  document.getElementById('music-config').innerHTML = `
    <div><strong>Ordner:</strong> ${esc(summary.folder || '–')}</div>
    <div><strong>Lokale Tracks bevorzugen:</strong> ${summary.prefer_local_tracks ? 'Ja' : 'Nein'}</div>
    <div><strong>Bibliothek aktiv:</strong> ${summary.enabled ? 'Ja' : 'Nein'}</div>
    <div><strong>Default-Tags:</strong> ${Array.isArray(summary.default_tags) && summary.default_tags.length ? summary.default_tags.map(tag => `<span class="tag">${esc(tag)}</span>`).join(' ') : '–'}</div>
  `;

  const list = document.getElementById('music-tracks');
  if (!tracks.length) {
    list.innerHTML = '<div class="muted">Keine Tracks im Musikordner gefunden.</div>';
    return;
  }

  list.innerHTML = tracks.map(track => `
    <div class="track-item">
      <div>
        <div class="track-title">${esc(track.title || track.file)}</div>
        <div class="track-sub">${esc(track.file)}${track.artist ? ' · ' + esc(track.artist) : ''}</div>
      </div>
      <div class="track-meta">
        <span class="${badgeClass(track.status)}">${badgeLabel(track.status)}</span>
        ${track.license_status ? `<span class="tag">Lizenz: ${esc(track.license_status)}</span>` : ''}
        <span class="tag">Commercial Use: ${track.commercial_use ? 'Ja' : 'Nein'}</span>
        ${track.energy ? `<span class="tag">Energy: ${esc(track.energy)}</span>` : ''}
        <span class="tag">Priority: ${esc(track.priority ?? 0)}</span>
        ${track.attribution_required ? '<span class="tag">Attribution noetig</span>' : ''}
      </div>
      <div class="track-meta">
        ${(track.allowed_platforms || []).map(platform => `<span class="tag">${esc(platform)}</span>`).join('') || '<span class="muted">Keine Plattformen</span>'}
      </div>
      <div class="track-meta">
        ${(track.tags || []).map(tag => `<span class="tag">${esc(tag)}</span>`).join('') || '<span class="muted">Keine Matching-Tags</span>'}
      </div>
      <div class="track-sub">${esc(track.reason || '')}</div>
      ${track.source_url ? `<div class="track-sub"><a href="${esc(track.source_url)}" target="_blank" rel="noopener">Lizenznachweis öffnen</a></div>` : ''}
    </div>
  `).join('');
}

refreshMusicLibrary();
setInterval(refreshMusicLibrary, 20000);
</script>
</body>
</html>
"""

INSTAGRAM_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Instagram Monitor</title>
<style>
  :root {
    --bg: #0a0f1d;
    --card: #131a2c;
    --border: #24314f;
    --accent: #f97316;
    --accent-2: #ec4899;
    --text: #eef2ff;
    --muted: #94a3b8;
    --green: #22c55e;
    --yellow: #f59e0b;
    --red: #ef4444;
    --radius: 16px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    min-height: 100vh; color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif;
    background:
      radial-gradient(circle at top left, rgba(249,115,22,.12), transparent 32%),
      radial-gradient(circle at top right, rgba(236,72,153,.10), transparent 28%),
      var(--bg);
  }
  header {
    padding: 18px 24px; display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid rgba(255,255,255,.06); background: rgba(10,15,29,.74); backdrop-filter: blur(10px);
    position: sticky; top: 0; z-index: 50;
  }
  h1 { font-size: 1.1rem; letter-spacing: .04em; }
  h1 span { color: var(--accent-2); }
  main { padding: 24px; display: grid; gap: 18px; }
  .actions { display:flex; gap:10px; align-items:center; }
  .btn {
    border: 1px solid rgba(148,163,184,.18); color: var(--text); background: transparent;
    border-radius: 10px; padding: 8px 12px; cursor: pointer;
  }
  .btn:hover { border-color: var(--accent-2); }
  .grid { display: grid; gap: 18px; grid-template-columns: 1fr 1fr; }
  .card { background: rgba(19,26,44,.92); border: 1px solid rgba(148,163,184,.14); border-radius: var(--radius); overflow: hidden; }
  .card-head {
    padding: 14px 18px; border-bottom: 1px solid rgba(148,163,184,.12);
    color: var(--muted); text-transform: uppercase; font-size: .76rem; letter-spacing: .12em;
  }
  .card-body { padding: 18px; }
  .kpis { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
  .kpi { padding: 16px; border-radius: 14px; background: rgba(255,255,255,.03); border: 1px solid rgba(148,163,184,.12); }
  .kpi-label { color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .12em; }
  .kpi-value { font-size: 1.5rem; margin-top: 10px; font-weight: 700; }
  .config-list, .profile-list { display: grid; gap: 10px; }
  .config-item, .profile-item {
    display: flex; justify-content: space-between; gap: 12px; padding: 10px 12px;
    background: rgba(255,255,255,.03); border: 1px solid rgba(148,163,184,.1); border-radius: 12px;
  }
  .config-item strong, .profile-item strong { color: var(--muted); font-size: .8rem; }
  .status-box {
    padding: 12px 14px; border-radius: 12px; font-size: .84rem; line-height: 1.5;
    border: 1px solid rgba(34,197,94,.28); background: rgba(34,197,94,.07); color: #bbf7d0;
  }
  .status-box.warn { border-color: rgba(245,158,11,.3); background: rgba(245,158,11,.08); color: #fde68a; }
  .status-box.error { border-color: rgba(239,68,68,.35); background: rgba(239,68,68,.08); color: #fecaca; }
  .media-table { width: 100%; border-collapse: collapse; font-size: .8rem; }
  .media-table th { text-align: left; color: var(--muted); font-weight: 600; padding: 6px 8px; border-bottom: 1px solid var(--border); }
  .media-table td { padding: 8px; border-bottom: 1px solid rgba(255,255,255,.05); vertical-align: top; }
  .tag { display:inline-block; padding: 3px 8px; border-radius: 999px; font-size: .68rem; border: 1px solid rgba(255,255,255,.1); }
  .tag.image { color:#fdba74; background: rgba(249,115,22,.1); }
  .tag.reel { color:#f9a8d4; background: rgba(236,72,153,.12); }
  .tag.story { color:#93c5fd; background: rgba(59,130,246,.12); }
  .muted { color: var(--muted); }
  .mono { font-family: 'Cascadia Code', 'Consolas', monospace; font-size: .78rem; word-break: break-all; }
  .media-link { color: #fbcfe8; text-decoration: none; }
  .media-link:hover { text-decoration: underline; }
  @media (max-width: 960px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>Auto-Poster <span>Instagram Monitor</span></h1>
  <div class="actions">
    <button class="btn" onclick="window.location.href='/'">Hauptdashboard</button>
    <button class="btn" onclick="window.location.href='/reels'">Reels</button>
    <button class="btn" onclick="window.location.href='/music'">Musik</button>
    <button class="btn" onclick="refreshInstagramMonitor(true)">Aktualisieren</button>
  </div>
</header>
<main>
  <div class="kpis" id="ig-kpis"></div>
  <div class="grid">
    <div class="card">
      <div class="card-head">Account-Status</div>
      <div class="card-body">
        <div id="ig-status-box" class="status-box">Lädt…</div>
        <div class="profile-list" id="ig-profile" style="margin-top:14px;">Lädt…</div>
      </div>
    </div>
    <div class="card">
      <div class="card-head">Publishing-Konfiguration</div>
      <div class="card-body">
        <div class="config-list" id="ig-config">Lädt…</div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-head">Zuletzt ueber Instagram gepostete Medien</div>
    <div class="card-body" id="ig-media-table">Lädt…</div>
  </div>
</main>
<script>
function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function metricValue(value) {
  return value === null || value === undefined || value === '' ? '–' : String(value);
}

async function refreshInstagramMonitor(force = false) {
  const response = await fetch('/api/instagram/monitor' + (force ? '?refresh=1' : ''));
  const data = await response.json();

  document.getElementById('ig-kpis').innerHTML = `
    <div class="kpi"><div class="kpi-label">Instagram aktiv</div><div class="kpi-value">${data.enabled ? 'Ja' : 'Nein'}</div></div>
    <div class="kpi"><div class="kpi-label">Follower</div><div class="kpi-value">${metricValue((data.profile || {}).followers_count)}</div></div>
    <div class="kpi"><div class="kpi-label">Media gesamt</div><div class="kpi-value">${metricValue((data.profile || {}).media_count)}</div></div>
    <div class="kpi"><div class="kpi-label">Getrackte IG-Medien</div><div class="kpi-value">${metricValue((data.totals || {}).tracked_media)}</div></div>
    <div class="kpi"><div class="kpi-label">Reels</div><div class="kpi-value">${metricValue((data.totals || {}).reels)}</div></div>
    <div class="kpi"><div class="kpi-label">Storys</div><div class="kpi-value">${metricValue((data.totals || {}).stories)}</div></div>
  `;

  const profile = data.profile || {};
  const statusBox = document.getElementById('ig-status-box');
  const statusClass = !data.enabled ? 'status-box warn' : (profile.error ? 'status-box error' : 'status-box');
  statusBox.className = statusClass;
  statusBox.textContent = !data.enabled
    ? 'Instagram-Posting ist derzeit deaktiviert.'
    : (profile.error || 'Instagram-Verbindung aktiv und Monitor-Daten abrufbar.');

  document.getElementById('ig-profile').innerHTML = `
    <div class="profile-item"><strong>Username</strong><span>${esc(profile.username || data.username || '–')}</span></div>
    <div class="profile-item"><strong>Business-ID</strong><span class="mono">${esc(profile.business_account_id || data.business_account_id || '–')}</span></div>
    <div class="profile-item"><strong>Letztes Update</strong><span>${esc(data.last_updated ? new Date(data.last_updated).toLocaleString('de-DE') : '–')}</span></div>
  `;

  document.getElementById('ig-config').innerHTML = `
    <div class="config-item"><strong>Bildposts</strong><span>${data.publish_posts ? 'Aktiv' : 'Aus'}</span></div>
    <div class="config-item"><strong>Reels</strong><span>${data.publish_reels ? 'Aktiv' : 'Aus'}</span></div>
    <div class="config-item"><strong>Storys</strong><span>${data.publish_stories ? 'Aktiv' : 'Aus'}</span></div>
    <div class="config-item"><strong>Public Base URL</strong><span class="mono">${esc(data.public_base_url || '–')}</span></div>
    <div class="config-item"><strong>Public Path</strong><span class="mono">${esc(data.public_path_prefix || '–')}</span></div>
    <div class="config-item"><strong>Remote Staging</strong><span>${data.remote_staging_enabled ? 'Aktiv' : 'Aus'}</span></div>
    <div class="config-item"><strong>Remote Target</strong><span class="mono">${esc(data.remote_target || '–')}</span></div>
  `;

  const mediaEl = document.getElementById('ig-media-table');
  const media = Array.isArray(data.recent_media) ? data.recent_media : [];
  if (!media.length) {
    mediaEl.innerHTML = '<div class="muted">Noch keine ueber Instagram geposteten Medien im State gefunden.</div>';
    return;
  }

  mediaEl.innerHTML = `<table class="media-table">
    <thead>
      <tr><th>Typ</th><th>Datei</th><th>Zeit</th><th>Metriken</th><th>Link</th><th>Status</th></tr>
    </thead>
    <tbody>
      ${media.map(item => {
        const tagClass = item.content_type === 'reel' ? 'reel' : item.content_type === 'story' ? 'story' : 'image';
        const insights = item.insights || {};
        const metricParts = [
          `Likes ${metricValue(item.like_count)}`,
          `Kommentare ${metricValue(item.comments_count)}`,
          `Reach ${metricValue(insights.reach)}`,
          item.content_type === 'reel' ? `Plays ${metricValue(insights.plays)}` : `Impressions ${metricValue(insights.impressions)}`,
          `Saved ${metricValue(insights.saved)}`,
        ];
        const link = item.permalink ? `<a class="media-link" href="${esc(item.permalink)}" target="_blank" rel="noopener">Oeffnen</a>` : '–';
        const status = item.error ? esc(item.error) : esc(item.platform_message || 'OK');
        return `<tr>
          <td><span class="tag ${tagClass}">${esc(item.content_type || 'media')}</span></td>
          <td>${esc(item.file || '–')}<div class="muted">Slot ${esc(item.slot || '–')}</div></td>
          <td>${esc(item.time ? new Date(item.time).toLocaleString('de-DE') : '–')}</td>
          <td>${metricParts.map(part => `<div>${esc(part)}</div>`).join('')}</td>
          <td>${link}</td>
          <td>${status}</td>
        </tr>`;
      }).join('')}
    </tbody>
  </table>`;
}

refreshInstagramMonitor();
setInterval(() => refreshInstagramMonitor(false), 30000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/reels")
def reels_index():
  return render_template_string(REELS_HTML)


@app.route("/instagram")
def instagram_index():
  return render_template_string(INSTAGRAM_HTML)


@app.route("/music")
def music_index():
  return render_template_string(MUSIC_HTML)


# --------------------------------------------------------------------------- #
# Start
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
  host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
  port = int(os.getenv("DASHBOARD_PORT", "5000"))
  print("=" * 55)
  print(f"  Dashboard läuft → http://{host}:{port}")
  print("  Strg+C zum Beenden")
  print("=" * 55)
  app.run(host=host, port=port, debug=False)
