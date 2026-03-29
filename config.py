from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "poster.log"
LOCK_FILE = BASE_DIR / ".poster.lock"

DEFAULT_POSTING_SLOTS = ["08:00", "13:00", "18:00", "21:00"]
DEFAULT_SUPPORTED_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif", ".webp"]
DEFAULT_AI_DISCLOSURE = "Dieses Bild wurde mit KI erstellt.\nÄhnlichkeiten mit realen Personen sind zufällig."
DEFAULT_SYSTEM_PROMPT = (
    "Du schreibst virale Social-Media-Captions für Facebook und Instagram. "
    "Schreibe auf Deutsch, emotional, direkt und aufmerksamkeitsstark. "
    "Die erste Zeile braucht einen starken Hook. Verwende 2 bis 4 kurze Sätze, "
    "spreche die Leser direkt mit du an, nutze gelegentlich Emojis wie 👀 oder 🔥 "
    "und ende mit einer Frage. Die letzte Zeile muss exakt der KI-Hinweis sein."
)
DEFAULT_USER_PROMPT = (
    "Erstelle {variant_count} verschiedene Caption-Varianten für ein KI-generiertes Bild.\n"
    "Anforderungen:\n"
    "- Deutsch\n"
    "- 2 bis 4 kurze Sätze\n"
    "- Erste Zeile mit starkem Hook\n"
    "- Am Ende eine klare Frage\n"
    "- Einfache direkte Sprache\n"
    "- Gelegentlich Emojis\n"
    "- Fokus auf Kommentare, Likes und Shares\n"
    "- Letzte Zeile exakt: {disclaimer}\n\n"
    "Bildname: {filename}\n"
    "Bildbeschreibung: {description}\n\n"
    "Gib ausschließlich JSON im Format {{\"variants\": [\"...\", \"...\", \"...\"]}} zurück."
)
DEFAULT_REEL_SYSTEM_PROMPT = (
    "Du schreibst virale Kurz-Captions für Social-Media-Reels auf Deutsch. "
    "Der Text muss schneller, direkter und hook-lastiger sein als bei normalen Bildposts. "
    "Die erste Zeile muss sofort Aufmerksamkeit ziehen. Verwende 2 bis 3 kurze Sätze, "
    "sprich die Leser direkt mit du an, nutze gelegentlich Emojis wie 👀, 🔥 oder ✨ "
    "und ende mit einer klaren Frage oder Call-to-Action. Die letzte Zeile muss exakt der KI-Hinweis sein."
)
DEFAULT_REEL_USER_PROMPT = (
    "Erstelle {variant_count} verschiedene Caption-Varianten für ein kurzes Multi-Image-Reel.\n"
    "Anforderungen:\n"
    "- Deutsch\n"
    "- 2 bis 3 kurze Sätze\n"
    "- Erste Zeile mit starkem Reel-Hook\n"
    "- Schneller, dynamischer Stil\n"
    "- Fokus auf Kommentare, Shares und Speichern\n"
    "- Am Ende eine klare Frage oder ein kurzer Call-to-Action\n"
    "- Letzte Zeile exakt: {disclaimer}\n\n"
    "Reel-Name: {filename}\n"
    "Reel-Inhalt: {description}\n\n"
    "Gib ausschließlich JSON im Format {{\"variants\": [\"...\", \"...\", \"...\"]}} zurück."
)


@dataclass(slots=True)
class FacebookSettings:
    page_id: str
    access_token: str


@dataclass(slots=True)
class OpenAISettings:
    enabled: bool
    api_key: str
    model: str
    temperature: float
    timeout_seconds: int
    system_prompt: str
    user_prompt_template: str
    reel_system_prompt: str
    reel_user_prompt_template: str


@dataclass(slots=True)
class OllamaSettings:
    enabled: bool
    base_url: str
    model: str
    temperature: float
    timeout_seconds: int
    vision_model: str
    vision_enabled: bool
    vision_cache: bool


@dataclass(slots=True)
class ReelSettings:
    enabled: bool
    simulation_mode: bool
    publish_to_facebook: bool
    output_folder: Path
    width: int
    height: int
    fps: int
    duration_seconds: int
    images_per_reel: int
    transition_frames: int
    transition_style: str
    zoom_start: float
    zoom_end: float
    text_overlay: bool
    hook_text_max_lines: int
    audio_enabled: bool
    audio_volume: float
    outro_enabled: bool
    outro_duration_seconds: int
    brand_title: str
    brand_subtitle: str
    call_to_action: str
    anchor_cooldown_reels: int
    duplicate_window_reels: int


@dataclass(slots=True)
class MusicLibrarySettings:
    enabled: bool
    folder: Path
    prefer_local_tracks: bool
    auto_match_enabled: bool
    require_metadata: bool
    require_commercial_use: bool
    approved_status: str
    allowed_platforms: list[str]
    default_tags: list[str]
    extensions: list[str]


@dataclass(slots=True)
class WatermarkSettings:
    enabled: bool
    image_path: Path
    position: str
    width_ratio: float
    opacity: float
    margin_px: int


@dataclass(slots=True)
class RetrySettings:
    enabled: bool
    max_attempts: int
    delay_seconds: float


@dataclass(slots=True)
class ImageValidationSettings:
    enabled: bool
    min_width: int
    min_height: int
    max_file_size_mb: float


@dataclass(slots=True)
class HashtagSettings:
    enabled: bool
    tags: list[str]
    count: int
    strategy: str  # "random", "fixed", "all"


@dataclass(slots=True)
class EngagementTrackingSettings:
    enabled: bool
    delay_hours: int
    low_engagement_threshold: int
    low_engagement_last_n: int


@dataclass(slots=True)
class SmartSlotsSettings:
    enabled: bool
    top_slots_count: int
    prefer_historical: bool
    min_data_points: int


@dataclass(slots=True)
class AutoCommentSettings:
    enabled: bool
    delay_seconds: int
    templates: list[str]
    retroactive: bool
    retroactive_max_age_days: int


@dataclass(slots=True)
class CaptionScoringSettings:
    enabled: bool
    min_score: int
    max_retries: int


@dataclass(slots=True)
class FollowerTrackingSettings:
    enabled: bool


@dataclass(slots=True)
class CommentResponseSettings:
    enabled: bool
    check_interval_hours: int
    max_responses_per_post: int
    lookback_days: int
    templates: list[str]


@dataclass(slots=True)
class AppConfig:
    platform: str
    images_folder: Path
    descriptions_folder: Path | None
    history_file: Path
    log_file: Path
    posting_slots: list[str]
    max_posts_per_day: int
    supported_extensions: list[str]
    selection_mode: str
    dry_run: bool
    delete_after_post: bool
    loop: bool
    loop_clear_caption_cache: bool
    poll_interval_seconds: int
    caption_template: str
    ai_disclosure: str
    caption_provider: str
    caption_variant_count: int
    caption_selection_strategy: str
    facebook: FacebookSettings
    ollama: OllamaSettings
    openai: OpenAISettings
    reels: ReelSettings
    music_library: MusicLibrarySettings
    watermark: WatermarkSettings
    retry: RetrySettings
    image_validation: ImageValidationSettings
    hashtags: HashtagSettings
    engagement: EngagementTrackingSettings
    smart_slots: SmartSlotsSettings
    auto_comment: AutoCommentSettings
    caption_scoring: CaptionScoringSettings
    follower_tracking: FollowerTrackingSettings
    comment_response: CommentResponseSettings


def load_settings() -> AppConfig:
    load_dotenv(BASE_DIR / ".env")
    raw = _load_json(CONFIG_FILE)

    facebook_raw = raw.get("facebook", {})
    ollama_raw = raw.get("ollama", {})
    openai_raw = raw.get("openai", {})
    reels_raw = raw.get("reels", {})
    music_raw = raw.get("music_library", {})
    watermark_raw = raw.get("watermark", {})
    retry_raw = raw.get("retry", {})
    validation_raw = raw.get("image_validation", {})
    hashtags_raw = raw.get("hashtags", {})
    engagement_raw = raw.get("engagement", {})
    smart_slots_raw = raw.get("smart_slots", {})
    auto_comment_raw = raw.get("auto_comment", {})
    caption_scoring_raw = raw.get("caption_scoring", {})
    follower_tracking_raw = raw.get("follower_tracking", {})
    comment_response_raw = raw.get("comment_response", {})

    images_folder = Path(_env_or_config("IMAGES_FOLDER", raw, "images_folder", default=str(BASE_DIR / "images")))
    descriptions_raw = _env_or_config("IMAGE_DESCRIPTIONS_FOLDER", raw, "image_descriptions_folder", default="")
    descriptions_folder = Path(descriptions_raw) if descriptions_raw else None

    posting_slots = _read_list_env("POSTING_SLOTS") or raw.get("posting_slots") or DEFAULT_POSTING_SLOTS
    posting_slots = [_normalize_slot(slot) for slot in posting_slots]

    ai_disclosure = _env_or_config("AI_DISCLOSURE", raw, "ai_disclosure", default=DEFAULT_AI_DISCLOSURE)
    system_prompt = openai_raw.get("system_prompt") or os.getenv("OPENAI_SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT
    user_prompt_template = openai_raw.get("user_prompt_template") or os.getenv("OPENAI_USER_PROMPT_TEMPLATE") or DEFAULT_USER_PROMPT
    reel_system_prompt = openai_raw.get("reel_system_prompt") or os.getenv("OPENAI_REEL_SYSTEM_PROMPT") or DEFAULT_REEL_SYSTEM_PROMPT
    reel_user_prompt_template = openai_raw.get("reel_user_prompt_template") or os.getenv("OPENAI_REEL_USER_PROMPT_TEMPLATE") or DEFAULT_REEL_USER_PROMPT

    return AppConfig(
        platform=_env_or_config("PLATFORM", raw, "platform", default="facebook"),
        images_folder=images_folder,
        descriptions_folder=descriptions_folder,
        history_file=Path(_env_or_config("POST_HISTORY_FILE", raw, "history_file", default=str(STATE_FILE))),
        log_file=Path(_env_or_config("LOG_FILE", raw, "log_file", default=str(LOG_FILE))),
        posting_slots=posting_slots,
        max_posts_per_day=int(_env_or_config("MAX_POSTS_PER_DAY", raw, "max_posts_per_day", default=len(posting_slots))),
        supported_extensions=[ext.lower() for ext in (raw.get("supported_extensions") or DEFAULT_SUPPORTED_EXTENSIONS)],
        selection_mode=_env_or_config("SELECTION_MODE", raw, "selection_mode", default="random").lower(),
        dry_run=_read_bool_env("DRY_RUN", raw.get("dry_run", False)),
        delete_after_post=_read_bool_env("DELETE_AFTER_POST", raw.get("delete_after_post", False)),
        loop=_read_bool_env("LOOP", raw.get("loop", True)),
        loop_clear_caption_cache=_read_bool_env("LOOP_CLEAR_CAPTION_CACHE", raw.get("loop_clear_caption_cache", True)),
        poll_interval_seconds=int(_env_or_config("POLL_INTERVAL_SECONDS", raw, "poll_interval_seconds", default=20)),
        caption_template=raw.get("caption_template", "").strip(),
        ai_disclosure=ai_disclosure.strip(),
        caption_provider=_env_or_config("CAPTION_PROVIDER", raw, "caption_provider", default="ollama").lower(),
        caption_variant_count=int(_env_or_config("CAPTION_VARIANT_COUNT", raw, "caption_variant_count", default=3)),
        caption_selection_strategy=_env_or_config(
            "CAPTION_SELECTION_STRATEGY",
            raw,
            "caption_selection_strategy",
            default="random",
        ).lower(),
        facebook=FacebookSettings(
            page_id=os.getenv("FB_PAGE_ID") or facebook_raw.get("page_id", ""),
            access_token=os.getenv("FB_PAGE_ACCESS_TOKEN") or facebook_raw.get("access_token", ""),
        ),
        ollama=OllamaSettings(
            enabled=_read_bool_env("OLLAMA_ENABLED", ollama_raw.get("enabled", True)),
            base_url=os.getenv("OLLAMA_BASE_URL") or ollama_raw.get("base_url", "http://127.0.0.1:11434"),
            model=os.getenv("OLLAMA_MODEL") or ollama_raw.get("model", "qwen2.5:14b"),
            temperature=float(os.getenv("OLLAMA_TEMPERATURE") or ollama_raw.get("temperature", 0.9)),
            timeout_seconds=int(os.getenv("OLLAMA_TIMEOUT_SECONDS") or ollama_raw.get("timeout_seconds", 90)),
            vision_model=os.getenv("OLLAMA_VISION_MODEL") or ollama_raw.get("vision_model", "llava:13b"),
            vision_enabled=_read_bool_env("OLLAMA_VISION_ENABLED", ollama_raw.get("vision_enabled", False)),
            vision_cache=_read_bool_env("OLLAMA_VISION_CACHE", ollama_raw.get("vision_cache", True)),
        ),
        openai=OpenAISettings(
            enabled=_read_bool_env("OPENAI_ENABLED", openai_raw.get("enabled", True)),
            api_key=os.getenv("OPENAI_API_KEY", ""),
            model=os.getenv("OPENAI_MODEL") or openai_raw.get("model", "gpt-4.1-mini"),
            temperature=float(os.getenv("OPENAI_TEMPERATURE") or openai_raw.get("temperature", 0.9)),
            timeout_seconds=int(os.getenv("OPENAI_TIMEOUT_SECONDS") or openai_raw.get("timeout_seconds", 60)),
            system_prompt=system_prompt,
            user_prompt_template=user_prompt_template,
            reel_system_prompt=reel_system_prompt,
            reel_user_prompt_template=reel_user_prompt_template,
        ),
        reels=ReelSettings(
            enabled=_read_bool_env("REELS_ENABLED", reels_raw.get("enabled", True)),
            simulation_mode=_read_bool_env("REELS_SIMULATION_MODE", reels_raw.get("simulation_mode", True)),
            publish_to_facebook=_read_bool_env("REELS_PUBLISH_TO_FACEBOOK", reels_raw.get("publish_to_facebook", False)),
            output_folder=Path(os.getenv("REELS_OUTPUT_FOLDER") or reels_raw.get("output_folder", str(BASE_DIR / "generated_reels"))),
            width=int(os.getenv("REELS_WIDTH") or reels_raw.get("width", 720)),
            height=int(os.getenv("REELS_HEIGHT") or reels_raw.get("height", 1280)),
            fps=int(os.getenv("REELS_FPS") or reels_raw.get("fps", 24)),
            duration_seconds=int(os.getenv("REELS_DURATION_SECONDS") or reels_raw.get("duration_seconds", 10)),
            images_per_reel=int(os.getenv("REELS_IMAGES_PER_REEL") or reels_raw.get("images_per_reel", 4)),
            transition_frames=int(os.getenv("REELS_TRANSITION_FRAMES") or reels_raw.get("transition_frames", 10)),
            transition_style=(os.getenv("REELS_TRANSITION_STYLE") or reels_raw.get("transition_style", "hybrid")).lower(),
            zoom_start=float(os.getenv("REELS_ZOOM_START") or reels_raw.get("zoom_start", 1.0)),
            zoom_end=float(os.getenv("REELS_ZOOM_END") or reels_raw.get("zoom_end", 1.12)),
            text_overlay=_read_bool_env("REELS_TEXT_OVERLAY", reels_raw.get("text_overlay", True)),
            hook_text_max_lines=int(os.getenv("REELS_HOOK_TEXT_MAX_LINES") or reels_raw.get("hook_text_max_lines", 3)),
            audio_enabled=_read_bool_env("REELS_AUDIO_ENABLED", reels_raw.get("audio_enabled", True)),
            audio_volume=float(os.getenv("REELS_AUDIO_VOLUME") or reels_raw.get("audio_volume", 0.22)),
            outro_enabled=_read_bool_env("REELS_OUTRO_ENABLED", reels_raw.get("outro_enabled", True)),
            outro_duration_seconds=int(os.getenv("REELS_OUTRO_DURATION_SECONDS") or reels_raw.get("outro_duration_seconds", 2)),
            brand_title=os.getenv("REELS_BRAND_TITLE") or reels_raw.get("brand_title", "AI Muse Feed"),
            brand_subtitle=os.getenv("REELS_BRAND_SUBTITLE") or reels_raw.get("brand_subtitle", "AI-Influencer Reels automatisch erzeugt"),
            call_to_action=os.getenv("REELS_CALL_TO_ACTION") or reels_raw.get("call_to_action", "Folgen, speichern, kommentieren"),
            anchor_cooldown_reels=int(os.getenv("REELS_ANCHOR_COOLDOWN_REELS") or reels_raw.get("anchor_cooldown_reels", 3)),
            duplicate_window_reels=int(os.getenv("REELS_DUPLICATE_WINDOW_REELS") or reels_raw.get("duplicate_window_reels", 12)),
        ),
        music_library=MusicLibrarySettings(
            enabled=_read_bool_env("MUSIC_LIBRARY_ENABLED", music_raw.get("enabled", True)),
            folder=Path(os.getenv("MUSIC_LIBRARY_FOLDER") or music_raw.get("folder", str(BASE_DIR / "music"))),
            prefer_local_tracks=_read_bool_env("MUSIC_LIBRARY_PREFER_LOCAL", music_raw.get("prefer_local_tracks", True)),
            auto_match_enabled=_read_bool_env("MUSIC_LIBRARY_AUTO_MATCH", music_raw.get("auto_match_enabled", True)),
            require_metadata=_read_bool_env("MUSIC_LIBRARY_REQUIRE_METADATA", music_raw.get("require_metadata", True)),
            require_commercial_use=_read_bool_env("MUSIC_LIBRARY_REQUIRE_COMMERCIAL_USE", music_raw.get("require_commercial_use", True)),
            approved_status=(os.getenv("MUSIC_LIBRARY_APPROVED_STATUS") or music_raw.get("approved_status", "approved")).lower(),
            allowed_platforms=_read_list_env("MUSIC_LIBRARY_ALLOWED_PLATFORMS") or [
                str(item).lower() for item in music_raw.get("allowed_platforms", ["facebook", "instagram", "reels"])
            ],
            default_tags=_read_list_env("MUSIC_LIBRARY_DEFAULT_TAGS") or [
                str(item).lower() for item in music_raw.get("default_tags", ["modern", "social"])
            ],
            extensions=[
                ext.lower()
                for ext in (_read_list_env("MUSIC_LIBRARY_EXTENSIONS") or music_raw.get("extensions", [".mp3", ".wav", ".m4a", ".aac"]))
            ],
        ),
        watermark=WatermarkSettings(
            enabled=_read_bool_env("WATERMARK_ENABLED", watermark_raw.get("enabled", True)),
            image_path=Path(os.getenv("WATERMARK_IMAGE_PATH") or watermark_raw.get("image_path", str(BASE_DIR / "assets" / "watermark.png"))),
            position=(os.getenv("WATERMARK_POSITION") or watermark_raw.get("position", "bottom-right")).lower(),
            width_ratio=float(os.getenv("WATERMARK_WIDTH_RATIO") or watermark_raw.get("width_ratio", 0.22)),
            opacity=float(os.getenv("WATERMARK_OPACITY") or watermark_raw.get("opacity", 0.92)),
            margin_px=int(os.getenv("WATERMARK_MARGIN_PX") or watermark_raw.get("margin_px", 28)),
        ),
        retry=RetrySettings(
            enabled=_read_bool_env("RETRY_ENABLED", retry_raw.get("enabled", True)),
            max_attempts=int(os.getenv("RETRY_MAX_ATTEMPTS") or retry_raw.get("max_attempts", 3)),
            delay_seconds=float(os.getenv("RETRY_DELAY_SECONDS") or retry_raw.get("delay_seconds", 15.0)),
        ),
        image_validation=ImageValidationSettings(
            enabled=_read_bool_env("IMAGE_VALIDATION_ENABLED", validation_raw.get("enabled", True)),
            min_width=int(os.getenv("IMAGE_VALIDATION_MIN_WIDTH") or validation_raw.get("min_width", 400)),
            min_height=int(os.getenv("IMAGE_VALIDATION_MIN_HEIGHT") or validation_raw.get("min_height", 400)),
            max_file_size_mb=float(os.getenv("IMAGE_VALIDATION_MAX_FILE_SIZE_MB") or validation_raw.get("max_file_size_mb", 10.0)),
        ),
        hashtags=HashtagSettings(
            enabled=_read_bool_env("HASHTAGS_ENABLED", hashtags_raw.get("enabled", False)),
            tags=_read_list_env("HASHTAGS_TAGS") or [str(t) for t in hashtags_raw.get("tags", [])],
            count=int(os.getenv("HASHTAGS_COUNT") or hashtags_raw.get("count", 5)),
            strategy=(os.getenv("HASHTAGS_STRATEGY") or hashtags_raw.get("strategy", "random")).lower(),
        ),
        engagement=EngagementTrackingSettings(
            enabled=_read_bool_env("ENGAGEMENT_ENABLED", engagement_raw.get("enabled", False)),
            delay_hours=int(os.getenv("ENGAGEMENT_DELAY_HOURS") or engagement_raw.get("delay_hours", 24)),
            low_engagement_threshold=int(os.getenv("ENGAGEMENT_LOW_THRESHOLD") or engagement_raw.get("low_engagement_threshold", 5)),
            low_engagement_last_n=int(os.getenv("ENGAGEMENT_LOW_LAST_N") or engagement_raw.get("low_engagement_last_n", 5)),
        ),
        smart_slots=SmartSlotsSettings(
            enabled=_read_bool_env("SMART_SLOTS_ENABLED", smart_slots_raw.get("enabled", False)),
            top_slots_count=int(os.getenv("SMART_SLOTS_TOP_COUNT") or smart_slots_raw.get("top_slots_count", 4)),
            prefer_historical=_read_bool_env("SMART_SLOTS_PREFER_HISTORICAL", smart_slots_raw.get("prefer_historical", True)),
            min_data_points=int(os.getenv("SMART_SLOTS_MIN_DATA_POINTS") or smart_slots_raw.get("min_data_points", 20)),
        ),
        auto_comment=AutoCommentSettings(
            enabled=_read_bool_env("AUTO_COMMENT_ENABLED", auto_comment_raw.get("enabled", False)),
            delay_seconds=int(os.getenv("AUTO_COMMENT_DELAY_SECONDS") or auto_comment_raw.get("delay_seconds", 60)),
            templates=_read_list_env("AUTO_COMMENT_TEMPLATES") or [str(t) for t in auto_comment_raw.get("templates", [
                "Was denkst du? Folge uns für mehr KI-Kunst! 👀",
                "Echt oder KI? Schreib es uns in die Kommentare! 🔥",
                "Welches Detail fällt dir als erstes auf? ✨",
            ])],
            retroactive=_read_bool_env("AUTO_COMMENT_RETROACTIVE", auto_comment_raw.get("retroactive", False)),
            retroactive_max_age_days=int(os.getenv("AUTO_COMMENT_RETROACTIVE_MAX_AGE_DAYS") or auto_comment_raw.get("retroactive_max_age_days", 30)),
        ),
        caption_scoring=CaptionScoringSettings(
            enabled=_read_bool_env("CAPTION_SCORING_ENABLED", caption_scoring_raw.get("enabled", True)),
            min_score=int(os.getenv("CAPTION_SCORING_MIN_SCORE") or caption_scoring_raw.get("min_score", 55)),
            max_retries=int(os.getenv("CAPTION_SCORING_MAX_RETRIES") or caption_scoring_raw.get("max_retries", 2)),
        ),
        follower_tracking=FollowerTrackingSettings(
            enabled=_read_bool_env("FOLLOWER_TRACKING_ENABLED", follower_tracking_raw.get("enabled", False)),
        ),
        comment_response=CommentResponseSettings(
            enabled=_read_bool_env("COMMENT_RESPONSE_ENABLED", comment_response_raw.get("enabled", False)),
            check_interval_hours=int(os.getenv("COMMENT_RESPONSE_CHECK_INTERVAL") or comment_response_raw.get("check_interval_hours", 6)),
            max_responses_per_post=int(os.getenv("COMMENT_RESPONSE_MAX_PER_POST") or comment_response_raw.get("max_responses_per_post", 3)),
            lookback_days=int(os.getenv("COMMENT_RESPONSE_LOOKBACK_DAYS") or comment_response_raw.get("lookback_days", 3)),
            templates=_read_list_env("COMMENT_RESPONSE_TEMPLATES") or [str(t) for t in comment_response_raw.get("templates", [
                "Danke! 🙏 Was ist dein Lieblingsdetail?",
                "Schön dass du fragst! Folge uns für mehr 👀",
                "Freut uns sehr! Was gefällt dir am besten? ✨",
            ])],
        ),
    )


def setup_logging(log_file: Path):
    log_file.parent.mkdir(parents=True, exist_ok=True)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _env_or_config(env_name: str, config: dict[str, Any], key: str, default: Any = None) -> Any:
    env_value = os.getenv(env_name)
    if env_value not in (None, ""):
        return env_value
    return config.get(key, default)


def _read_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_list_env(name: str) -> list[str]:
    value = os.getenv(name, "").strip()
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _normalize_slot(slot: str) -> str:
    hour_text, minute_text = slot.strip().split(":", maxsplit=1)
    hour = int(hour_text)
    minute = int(minute_text)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Ungueltiger Posting-Slot: {slot}")
    return f"{hour:02d}:{minute:02d}"