from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from auto_comment_texts import DEFAULT_AUTO_COMMENT_TEMPLATES
from story_texts import DEFAULT_STORY_TEXTS

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
    "Die erste Zeile braucht einen starken Scroll-Stopper-Hook. Verwende 2 bis 4 kurze Sätze, "
    "spreche die Leser direkt mit du an, baue Vergleich, Bewertung oder Neugier ein, "
    "nutze gelegentlich Emojis wie 👀 oder 🔥 und ende mit einer klaren Frage oder CTA, "
    "die Kommentare, Saves oder Shares auslöst. Die letzte Zeile muss exakt der KI-Hinweis sein."
)
DEFAULT_USER_PROMPT = (
    "Erstelle {variant_count} verschiedene Caption-Varianten für ein KI-generiertes Bild.\n"
    "Anforderungen:\n"
    "- Deutsch\n"
    "- 2 bis 4 kurze Sätze\n"
    "- Erste Zeile mit starkem Hook, ideal für Scroll-Stop\n"
    "- Am Ende eine klare Frage oder CTA\n"
    "- Einfache direkte Sprache\n"
    "- Gelegentlich Emojis\n"
    "- Fokus auf Kommentare, Saves, Shares und Follows\n"
    "- Bevorzuge Formulierungen wie Vergleiche, Entscheidungen, Bewertung oder Neugier\n"
    "- Letzte Zeile exakt: {disclaimer}\n\n"
    "Bildname: {filename}\n"
    "Bildbeschreibung: {description}\n\n"
    "Gib ausschließlich JSON im Format {{\"variants\": [\"...\", \"...\", \"...\"]}} zurück."
)
DEFAULT_REEL_SYSTEM_PROMPT = (
    "Du schreibst virale Kurz-Captions für Social-Media-Reels auf Deutsch. "
    "Der Text muss schneller, direkter und hook-lastiger sein als bei normalen Bildposts. "
    "Die erste Zeile muss sofort Aufmerksamkeit ziehen. Verwende 2 bis 3 kurze Sätze, "
    "sprich die Leser direkt mit du an, nutze gelegentlich Emojis wie 👀, 🔥 oder ✨, "
    "arbeite mit Vergleich, Auswahl oder Bewertung und ende mit einer klaren Frage oder Call-to-Action, "
    "die Kommentare, Saves oder Shares auslöst. Die letzte Zeile muss exakt der KI-Hinweis sein."
)
DEFAULT_REEL_USER_PROMPT = (
    "Erstelle {variant_count} verschiedene Caption-Varianten für ein kurzes Multi-Image-Reel.\n"
    "Anforderungen:\n"
    "- Deutsch\n"
    "- 2 bis 3 kurze Sätze\n"
    "- Erste Zeile mit starkem Reel-Hook\n"
    "- Schneller, dynamischer Stil\n"
    "- Fokus auf Kommentare, Shares, Speichern und Follows\n"
    "- Verwende möglichst Auswahl-, Vergleichs- oder Bewertungsfragen\n"
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
class InstagramSettings:
    enabled: bool
    business_account_id: str
    access_token: str
    username: str
    publish_posts: bool
    publish_reels: bool
    publish_stories: bool
    public_base_url: str
    public_path_prefix: str
    staging_folder: Path
    remote_staging_enabled: bool
    remote_host: str
    remote_user: str
    remote_path: str
    remote_upload_method: str
    remote_ssh_port: int
    external_url_fallback_enabled: bool
    external_url_fallback_provider: str
    external_url_fallback_expiry: str
    keep_files: int
    auto_cleanup_enabled: bool
    cleanup_ttl_seconds: int
    share_reels_to_feed: bool
    container_check_interval_seconds: float
    container_check_timeout_seconds: int


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
class StorySettings:
    enabled: bool
    publish_to_facebook: bool
    output_folder: Path
    width: int
    height: int
    max_per_day: int
    chance_per_slot: float
    eligible_slots: list[str]
    brand_footer: str
    texts: dict[str, list[str]]


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
    high_engagement_threshold: int
    recycle_low_performers: bool
    recycle_after_hours: int
    recycle_formats: list[str]
    followup_comments_enabled: bool
    low_followup_templates: list[str]
    high_followup_templates: list[str]
    unusual_spike_multiplier: float


@dataclass(slots=True)
class SmartSlotsSettings:
    enabled: bool
    top_slots_count: int
    prefer_historical: bool
    min_data_points: int
    exploration_rate: float


@dataclass(slots=True)
class AutoCommentSettings:
    enabled: bool
    delay_seconds: int
    templates: list[str]
    retroactive: bool
    retroactive_max_age_days: int
    ollama_enabled: bool
    ollama_ratio: float
    ollama_cache_size: int
    style_profile: str
    feed_style: str
    reel_style: str
    repeat_block_count: int


@dataclass(slots=True)
class CaptionScoringSettings:
    enabled: bool
    min_score: int
    max_retries: int


@dataclass(slots=True)
class FollowerTrackingSettings:
    enabled: bool


@dataclass(slots=True)
class CampaignDefinition:
    name: str
    themes: list[str]
    start_date: str
    end_date: str
    days_per_theme: int
    preferred_slots: list[str]
    target_feed_posts: int
    target_stories: int
    target_reels: int


@dataclass(slots=True)
class CampaignSettings:
    enabled: bool
    auto_rotate: bool
    active_campaign_name: str
    fallback_to_detected_themes: bool
    theme_separator: str
    default_days_per_theme: int
    weekday_modes: dict[str, str]
    daily_theme_overrides: dict[str, str]
    campaigns: list[CampaignDefinition]


@dataclass(slots=True)
class CaptionExperimentSettings:
    enabled: bool
    exploration_rate: float
    min_data_points: int


@dataclass(slots=True)
class ContentQualitySettings:
    enabled: bool
    min_score: int
    skip_similar_images: bool
    duplicate_hamming_threshold: int
    theme_whitelist: list[str]
    theme_blacklist: list[str]


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
    instagram: InstagramSettings
    ollama: OllamaSettings
    openai: OpenAISettings
    reels: ReelSettings
    stories: StorySettings
    music_library: MusicLibrarySettings
    watermark: WatermarkSettings
    retry: RetrySettings
    image_validation: ImageValidationSettings
    hashtags: HashtagSettings
    engagement: EngagementTrackingSettings
    smart_slots: SmartSlotsSettings
    auto_comment: AutoCommentSettings
    caption_scoring: CaptionScoringSettings
    campaigns: CampaignSettings
    caption_experiments: CaptionExperimentSettings
    content_quality: ContentQualitySettings
    follower_tracking: FollowerTrackingSettings
    comment_response: CommentResponseSettings


def load_settings() -> AppConfig:
    raw = _load_json(CONFIG_FILE)

    facebook_raw = raw.get("facebook", {})
    instagram_raw = raw.get("instagram", {})
    ollama_raw = raw.get("ollama", {})
    openai_raw = raw.get("openai", {})
    reels_raw = raw.get("reels", {})
    stories_raw = raw.get("stories", {})
    music_raw = raw.get("music_library", {})
    watermark_raw = raw.get("watermark", {})
    retry_raw = raw.get("retry", {})
    validation_raw = raw.get("image_validation", {})
    hashtags_raw = raw.get("hashtags", {})
    engagement_raw = raw.get("engagement", {})
    smart_slots_raw = raw.get("smart_slots", {})
    auto_comment_raw = raw.get("auto_comment", {})
    caption_scoring_raw = raw.get("caption_scoring", {})
    campaigns_raw = raw.get("campaigns", {})
    caption_experiments_raw = raw.get("caption_experiments", {})
    content_quality_raw = raw.get("content_quality", {})
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
    story_slots = _read_list_env("STORIES_SLOTS") or stories_raw.get("eligible_slots") or ["08:00", "21:00"]
    story_slots = [_normalize_slot(slot) for slot in story_slots]
    story_texts_raw = stories_raw.get("texts") or {}
    story_texts = {
        theme: [str(item).strip() for item in story_texts_raw.get(theme, DEFAULT_STORY_TEXTS.get(theme, [])) if str(item).strip()]
        for theme in sorted(set(DEFAULT_STORY_TEXTS) | set(story_texts_raw))
    }
    campaign_definitions: list[CampaignDefinition] = []
    for item in campaigns_raw.get("campaigns", []) or []:
        if not isinstance(item, dict):
            continue
        themes = [str(theme).strip() for theme in item.get("themes", []) if str(theme).strip()]
        preferred_slots = [_normalize_slot(slot) for slot in item.get("preferred_slots", []) if str(slot).strip()]
        campaign_definitions.append(
            CampaignDefinition(
                name=str(item.get("name") or "").strip(),
                themes=themes,
                start_date=str(item.get("start_date") or "").strip(),
                end_date=str(item.get("end_date") or "").strip(),
                days_per_theme=max(1, int(item.get("days_per_theme", campaigns_raw.get("default_days_per_theme", 2)) or 2)),
                preferred_slots=preferred_slots,
                target_feed_posts=max(0, int(item.get("target_feed_posts", 3) or 0)),
                target_stories=max(0, int(item.get("target_stories", 2) or 0)),
                target_reels=max(0, int(item.get("target_reels", 1) or 0)),
            )
        )
    weekday_modes_raw = campaigns_raw.get("weekday_modes") or {}
    weekday_modes = {
        str(day).strip().lower(): (str(mode).strip().lower() or "theme")
        for day, mode in weekday_modes_raw.items()
        if str(day).strip()
    }
    daily_theme_overrides_raw = campaigns_raw.get("daily_theme_overrides") or {}
    daily_theme_overrides = {
        str(day).strip(): str(theme).strip().lower()
        for day, theme in daily_theme_overrides_raw.items()
        if str(day).strip() and str(theme).strip()
    }

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
        instagram=InstagramSettings(
            enabled=_read_bool_env("INSTAGRAM_ENABLED", instagram_raw.get("enabled", False)),
            business_account_id=os.getenv("IG_BUSINESS_ACCOUNT_ID") or instagram_raw.get("business_account_id", ""),
            access_token=os.getenv("IG_ACCESS_TOKEN") or instagram_raw.get("access_token", "") or (os.getenv("FB_PAGE_ACCESS_TOKEN") or facebook_raw.get("access_token", "")),
            username=os.getenv("IG_USERNAME") or instagram_raw.get("username", ""),
            publish_posts=_read_bool_env("INSTAGRAM_PUBLISH_POSTS", instagram_raw.get("publish_posts", False)),
            publish_reels=_read_bool_env("INSTAGRAM_PUBLISH_REELS", instagram_raw.get("publish_reels", False)),
            publish_stories=_read_bool_env("INSTAGRAM_PUBLISH_STORIES", instagram_raw.get("publish_stories", False)),
            public_base_url=(os.getenv("INSTAGRAM_PUBLIC_BASE_URL") or instagram_raw.get("public_base_url", "")).rstrip("/"),
            public_path_prefix="/" + str(os.getenv("INSTAGRAM_PUBLIC_PATH_PREFIX") or instagram_raw.get("public_path_prefix", "/public-media")).strip().strip("/"),
            staging_folder=Path(os.getenv("INSTAGRAM_STAGING_FOLDER") or instagram_raw.get("staging_folder", str(BASE_DIR / "public_media" / "instagram"))),
            remote_staging_enabled=_read_bool_env("INSTAGRAM_REMOTE_STAGING_ENABLED", instagram_raw.get("remote_staging_enabled", False)),
            remote_host=os.getenv("INSTAGRAM_REMOTE_HOST") or instagram_raw.get("remote_host", ""),
            remote_user=os.getenv("INSTAGRAM_REMOTE_USER") or instagram_raw.get("remote_user", ""),
            remote_path=os.getenv("INSTAGRAM_REMOTE_PATH") or instagram_raw.get("remote_path", ""),
            remote_upload_method=(os.getenv("INSTAGRAM_REMOTE_UPLOAD_METHOD") or instagram_raw.get("remote_upload_method", "scp")).strip().lower(),
            remote_ssh_port=int(os.getenv("INSTAGRAM_REMOTE_SSH_PORT") or instagram_raw.get("remote_ssh_port", 22)),
            external_url_fallback_enabled=_read_bool_env("INSTAGRAM_EXTERNAL_URL_FALLBACK_ENABLED", instagram_raw.get("external_url_fallback_enabled", False)),
            external_url_fallback_provider=(os.getenv("INSTAGRAM_EXTERNAL_URL_FALLBACK_PROVIDER") or instagram_raw.get("external_url_fallback_provider", "litterbox")).strip().lower(),
            external_url_fallback_expiry=(os.getenv("INSTAGRAM_EXTERNAL_URL_FALLBACK_EXPIRY") or instagram_raw.get("external_url_fallback_expiry", "72h")).strip() or "72h",
            keep_files=int(os.getenv("INSTAGRAM_KEEP_FILES") or instagram_raw.get("keep_files", 80)),
            auto_cleanup_enabled=_read_bool_env("INSTAGRAM_AUTO_CLEANUP", instagram_raw.get("auto_cleanup_enabled", True)),
            cleanup_ttl_seconds=int(os.getenv("INSTAGRAM_CLEANUP_TTL_SECONDS") or instagram_raw.get("cleanup_ttl_seconds", 1800)),
            share_reels_to_feed=_read_bool_env("INSTAGRAM_SHARE_REELS_TO_FEED", instagram_raw.get("share_reels_to_feed", True)),
            container_check_interval_seconds=float(os.getenv("INSTAGRAM_CONTAINER_CHECK_INTERVAL") or instagram_raw.get("container_check_interval_seconds", 5.0)),
            container_check_timeout_seconds=int(os.getenv("INSTAGRAM_CONTAINER_CHECK_TIMEOUT") or instagram_raw.get("container_check_timeout_seconds", 300)),
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
        stories=StorySettings(
            enabled=_read_bool_env("STORIES_ENABLED", stories_raw.get("enabled", False)),
            publish_to_facebook=_read_bool_env("STORIES_PUBLISH_TO_FACEBOOK", stories_raw.get("publish_to_facebook", True)),
            output_folder=Path(os.getenv("STORIES_OUTPUT_FOLDER") or stories_raw.get("output_folder", str(BASE_DIR / "generated_stories"))),
            width=int(os.getenv("STORIES_WIDTH") or stories_raw.get("width", 1080)),
            height=int(os.getenv("STORIES_HEIGHT") or stories_raw.get("height", 1920)),
            max_per_day=int(os.getenv("STORIES_MAX_PER_DAY") or stories_raw.get("max_per_day", 1)),
            chance_per_slot=float(os.getenv("STORIES_CHANCE_PER_SLOT") or stories_raw.get("chance_per_slot", 0.45)),
            eligible_slots=story_slots,
            brand_footer=os.getenv("STORIES_BRAND_FOOTER") or stories_raw.get("brand_footer", ""),
            texts=story_texts,
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
            high_engagement_threshold=int(os.getenv("ENGAGEMENT_HIGH_THRESHOLD") or engagement_raw.get("high_engagement_threshold", 45)),
            recycle_low_performers=_read_bool_env("ENGAGEMENT_RECYCLE_LOW", engagement_raw.get("recycle_low_performers", True)),
            recycle_after_hours=int(os.getenv("ENGAGEMENT_RECYCLE_AFTER_HOURS") or engagement_raw.get("recycle_after_hours", 30)),
            recycle_formats=_read_list_env("ENGAGEMENT_RECYCLE_FORMATS") or [
                str(item).lower() for item in engagement_raw.get("recycle_formats", ["story", "reel"])
            ],
            followup_comments_enabled=_read_bool_env("ENGAGEMENT_FOLLOWUP_COMMENTS", engagement_raw.get("followup_comments_enabled", True)),
            low_followup_templates=_read_list_env("ENGAGEMENT_LOW_FOLLOWUP_TEMPLATES") or [
                str(item).strip() for item in engagement_raw.get("low_followup_templates", []) if str(item).strip()
            ],
            high_followup_templates=_read_list_env("ENGAGEMENT_HIGH_FOLLOWUP_TEMPLATES") or [
                str(item).strip() for item in engagement_raw.get("high_followup_templates", []) if str(item).strip()
            ],
            unusual_spike_multiplier=float(os.getenv("ENGAGEMENT_UNUSUAL_SPIKE_MULTIPLIER") or engagement_raw.get("unusual_spike_multiplier", 1.8)),
        ),
        smart_slots=SmartSlotsSettings(
            enabled=_read_bool_env("SMART_SLOTS_ENABLED", smart_slots_raw.get("enabled", False)),
            top_slots_count=int(os.getenv("SMART_SLOTS_TOP_COUNT") or smart_slots_raw.get("top_slots_count", 4)),
            prefer_historical=_read_bool_env("SMART_SLOTS_PREFER_HISTORICAL", smart_slots_raw.get("prefer_historical", True)),
            min_data_points=int(os.getenv("SMART_SLOTS_MIN_DATA_POINTS") or smart_slots_raw.get("min_data_points", 20)),
            exploration_rate=float(os.getenv("SMART_SLOTS_EXPLORATION_RATE") or smart_slots_raw.get("exploration_rate", 0.15)),
        ),
        auto_comment=AutoCommentSettings(
            enabled=_read_bool_env("AUTO_COMMENT_ENABLED", auto_comment_raw.get("enabled", False)),
            delay_seconds=int(os.getenv("AUTO_COMMENT_DELAY_SECONDS") or auto_comment_raw.get("delay_seconds", 60)),
            templates=_read_list_env("AUTO_COMMENT_TEMPLATES") or [
                str(t) for t in (auto_comment_raw.get("templates") or DEFAULT_AUTO_COMMENT_TEMPLATES)
            ],
            retroactive=_read_bool_env("AUTO_COMMENT_RETROACTIVE", auto_comment_raw.get("retroactive", False)),
            retroactive_max_age_days=int(os.getenv("AUTO_COMMENT_RETROACTIVE_MAX_AGE_DAYS") or auto_comment_raw.get("retroactive_max_age_days", 30)),
            ollama_enabled=_read_bool_env("AUTO_COMMENT_OLLAMA_ENABLED", auto_comment_raw.get("ollama_enabled", True)),
            ollama_ratio=float(os.getenv("AUTO_COMMENT_OLLAMA_RATIO") or auto_comment_raw.get("ollama_ratio", 0.25)),
            ollama_cache_size=int(os.getenv("AUTO_COMMENT_OLLAMA_CACHE_SIZE") or auto_comment_raw.get("ollama_cache_size", 24)),
            style_profile=(os.getenv("AUTO_COMMENT_STYLE_PROFILE") or auto_comment_raw.get("style_profile", "frech")).lower(),
            feed_style=(os.getenv("AUTO_COMMENT_FEED_STYLE") or auto_comment_raw.get("feed_style", "charmant")).lower(),
            reel_style=(os.getenv("AUTO_COMMENT_REEL_STYLE") or auto_comment_raw.get("reel_style", "direkt")).lower(),
            repeat_block_count=int(os.getenv("AUTO_COMMENT_REPEAT_BLOCK_COUNT") or auto_comment_raw.get("repeat_block_count", 80)),
        ),
        caption_scoring=CaptionScoringSettings(
            enabled=_read_bool_env("CAPTION_SCORING_ENABLED", caption_scoring_raw.get("enabled", True)),
            min_score=int(os.getenv("CAPTION_SCORING_MIN_SCORE") or caption_scoring_raw.get("min_score", 55)),
            max_retries=int(os.getenv("CAPTION_SCORING_MAX_RETRIES") or caption_scoring_raw.get("max_retries", 2)),
        ),
        campaigns=CampaignSettings(
            enabled=_read_bool_env("CAMPAIGNS_ENABLED", campaigns_raw.get("enabled", False)),
            auto_rotate=_read_bool_env("CAMPAIGNS_AUTO_ROTATE", campaigns_raw.get("auto_rotate", True)),
            active_campaign_name=(os.getenv("CAMPAIGNS_ACTIVE_NAME") or campaigns_raw.get("active_campaign_name", "")).strip(),
            fallback_to_detected_themes=_read_bool_env("CAMPAIGNS_FALLBACK_THEMES", campaigns_raw.get("fallback_to_detected_themes", True)),
            theme_separator=str(os.getenv("CAMPAIGNS_THEME_SEPARATOR") or campaigns_raw.get("theme_separator", "_") or "_"),
            default_days_per_theme=max(1, int(os.getenv("CAMPAIGNS_DEFAULT_DAYS_PER_THEME") or campaigns_raw.get("default_days_per_theme", 2))),
            weekday_modes=weekday_modes,
            daily_theme_overrides=daily_theme_overrides,
            campaigns=campaign_definitions,
        ),
        caption_experiments=CaptionExperimentSettings(
            enabled=_read_bool_env("CAPTION_EXPERIMENTS_ENABLED", caption_experiments_raw.get("enabled", False)),
            exploration_rate=float(os.getenv("CAPTION_EXPERIMENTS_EXPLORATION_RATE") or caption_experiments_raw.get("exploration_rate", 0.2)),
            min_data_points=int(os.getenv("CAPTION_EXPERIMENTS_MIN_DATA_POINTS") or caption_experiments_raw.get("min_data_points", 4)),
        ),
        content_quality=ContentQualitySettings(
            enabled=_read_bool_env("CONTENT_QUALITY_ENABLED", content_quality_raw.get("enabled", True)),
            min_score=int(os.getenv("CONTENT_QUALITY_MIN_SCORE") or content_quality_raw.get("min_score", 55)),
            skip_similar_images=_read_bool_env("CONTENT_QUALITY_SKIP_SIMILAR", content_quality_raw.get("skip_similar_images", True)),
            duplicate_hamming_threshold=int(os.getenv("CONTENT_QUALITY_DUPLICATE_HAMMING") or content_quality_raw.get("duplicate_hamming_threshold", 6)),
            theme_whitelist=_read_list_env("CONTENT_THEME_WHITELIST") or [
                str(item).strip().lower() for item in content_quality_raw.get("theme_whitelist", []) if str(item).strip()
            ],
            theme_blacklist=_read_list_env("CONTENT_THEME_BLACKLIST") or [
                str(item).strip().lower() for item in content_quality_raw.get("theme_blacklist", []) if str(item).strip()
            ],
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
                "Schön, dass du fragst! Wenn du magst, bleib gern hier 👀",
                "Freut mich sehr! Was gefällt dir am besten? ✨",
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
