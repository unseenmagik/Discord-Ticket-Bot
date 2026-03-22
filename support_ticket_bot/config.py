from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class BotSettings:
    token: str
    message_content_intent: bool
    guild_id: int
    panel_channel_id: int
    transcript_channel_id: int
    thread_name_prefix: str
    auto_archive_duration: int
    delete_closed_threads_after_hours: int
    allow_thread_owner_close: bool
    allow_thread_owner_reopen: bool
    close_requires_staff: bool
    interaction_delete_after_seconds: float
    embed_color: int
    support_role_ids: list[int]
    save_txt_transcript: bool
    save_html_transcript: bool
    server_targets: dict[str, int]
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    db_minsize: int
    db_maxsize: int
    db_charset: str
    dashboard_enabled: bool
    dashboard_host: str
    dashboard_port: int
    dashboard_secret_key: str
    dashboard_base_url: str
    dashboard_discord_client_id: str
    dashboard_discord_client_secret: str
    dashboard_discord_redirect_uri: str
    dashboard_admin_user_ids: list[int]
    dashboard_role_channel_access: dict[int, list[int]]
    dashboard_role_full_access_ids: list[int]


def _parse_bool(config: ConfigParser, section: str, key: str, fallback: bool) -> bool:
    return config.getboolean(section, key, fallback=fallback)


def _parse_int_list(raw: str) -> list[int]:
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def _parse_role_channel_access(config: ConfigParser, section: str) -> tuple[dict[int, list[int]], list[int]]:
    role_channel_access: dict[int, list[int]] = {}
    full_access_role_ids: list[int] = []
    if not config.has_section(section):
        return role_channel_access, full_access_role_ids

    for role_id_raw, channels_raw in config.items(section):
        role_id = int(role_id_raw.strip())
        value = channels_raw.strip()
        if value == "*":
            full_access_role_ids.append(role_id)
            continue
        role_channel_access[role_id] = _parse_int_list(value)
    return role_channel_access, full_access_role_ids


def load_settings(config_path: str | Path = "config.ini") -> BotSettings:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path.resolve()}")

    config = ConfigParser()
    config.optionxform = str
    config.read(path, encoding="utf-8")

    token = config.get("discord", "token")
    message_content_intent = _parse_bool(config, "discord", "message_content_intent", False)
    guild_id = config.getint("discord", "guild_id")
    panel_channel_id = config.getint("discord", "panel_channel_id")

    transcript_channel_id = config.getint("logs", "transcript_channel_id", fallback=0)
    save_txt_transcript = _parse_bool(config, "logs", "save_txt_transcript", True)
    save_html_transcript = _parse_bool(config, "logs", "save_html_transcript", True)

    thread_name_prefix = config.get("tickets", "thread_name_prefix", fallback="ticket")
    auto_archive_duration = config.getint("tickets", "auto_archive_duration", fallback=1440)
    delete_closed_threads_after_hours = config.getint(
        "tickets", "delete_closed_threads_after_hours", fallback=72
    )
    allow_thread_owner_close = _parse_bool(config, "tickets", "allow_thread_owner_close", True)
    allow_thread_owner_reopen = _parse_bool(config, "tickets", "allow_thread_owner_reopen", True)
    close_requires_staff = _parse_bool(config, "tickets", "close_requires_staff", False)
    interaction_delete_after_seconds = config.getfloat("tickets", "interaction_delete_after_seconds", fallback=30.0)

    embed_color_raw = config.get("tickets", "embed_color", fallback="0x5865F2")
    embed_color = int(embed_color_raw, 16) if embed_color_raw.lower().startswith("0x") else int(embed_color_raw)

    role_ids_raw = config.get("support", "role_ids", fallback="")
    support_role_ids = _parse_int_list(role_ids_raw)

    server_targets: dict[str, int] = {}
    if config.has_section("servers"):
        for label, channel_id in config.items("servers"):
            server_targets[label] = int(channel_id)
    if not server_targets:
        raise ValueError("No server targets found in [servers] section.")

    db_host = config.get("database", "host")
    db_port = config.getint("database", "port", fallback=3306)
    db_user = config.get("database", "user")
    db_password = config.get("database", "password")
    db_name = config.get("database", "name")
    db_minsize = config.getint("database", "minsize", fallback=1)
    db_maxsize = config.getint("database", "maxsize", fallback=10)
    db_charset = config.get("database", "charset", fallback="utf8mb4")

    dashboard_enabled = _parse_bool(config, "dashboard", "enabled", True)
    dashboard_host = config.get("dashboard", "host", fallback="127.0.0.1")
    dashboard_port = config.getint("dashboard", "port", fallback=8000)
    dashboard_secret_key = config.get("dashboard", "secret_key", fallback="change-me")
    dashboard_base_url = config.get("dashboard", "base_url", fallback=f"http://{dashboard_host}:{dashboard_port}")
    dashboard_discord_client_id = config.get("dashboard", "discord_client_id", fallback="")
    dashboard_discord_client_secret = config.get("dashboard", "discord_client_secret", fallback="")
    dashboard_discord_redirect_uri = config.get(
        "dashboard",
        "discord_redirect_uri",
        fallback=f"{dashboard_base_url.rstrip('/')}/auth/discord/callback",
    )
    dashboard_admin_user_ids = _parse_int_list(config.get("dashboard", "admin_user_ids", fallback=""))
    dashboard_role_channel_access, dashboard_role_full_access_ids = _parse_role_channel_access(
        config,
        "dashboard_role_access",
    )

    return BotSettings(
        token=token,
        message_content_intent=message_content_intent,
        guild_id=guild_id,
        panel_channel_id=panel_channel_id,
        transcript_channel_id=transcript_channel_id,
        thread_name_prefix=thread_name_prefix,
        auto_archive_duration=auto_archive_duration,
        delete_closed_threads_after_hours=delete_closed_threads_after_hours,
        allow_thread_owner_close=allow_thread_owner_close,
        allow_thread_owner_reopen=allow_thread_owner_reopen,
        close_requires_staff=close_requires_staff,
        interaction_delete_after_seconds=interaction_delete_after_seconds,
        embed_color=embed_color,
        support_role_ids=support_role_ids,
        save_txt_transcript=save_txt_transcript,
        save_html_transcript=save_html_transcript,
        server_targets=server_targets,
        db_host=db_host,
        db_port=db_port,
        db_user=db_user,
        db_password=db_password,
        db_name=db_name,
        db_minsize=db_minsize,
        db_maxsize=db_maxsize,
        db_charset=db_charset,
        dashboard_enabled=dashboard_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        dashboard_secret_key=dashboard_secret_key,
        dashboard_base_url=dashboard_base_url,
        dashboard_discord_client_id=dashboard_discord_client_id,
        dashboard_discord_client_secret=dashboard_discord_client_secret,
        dashboard_discord_redirect_uri=dashboard_discord_redirect_uri,
        dashboard_admin_user_ids=dashboard_admin_user_ids,
        dashboard_role_channel_access=dashboard_role_channel_access,
        dashboard_role_full_access_ids=dashboard_role_full_access_ids,
    )
