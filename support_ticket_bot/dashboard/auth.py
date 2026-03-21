from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from support_ticket_bot.config import BotSettings

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_AUTH_BASE = "https://discord.com/oauth2/authorize"
OAUTH_SCOPES = ("identify", "guilds.members.read")
DISCORD_HTTP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) DiscordTicketBotDashboard/1.0 Safari/537.36"
)


class DiscordOAuthError(RuntimeError):
    pass


@dataclass(slots=True)
class DashboardViewer:
    discord_user_id: int
    username: str
    display_name: str
    avatar_url: str | None
    role_ids: list[int]
    is_admin: bool
    allowed_channel_ids: list[int]
    has_global_ticket_access: bool


def discord_oauth_configured(settings: BotSettings) -> bool:
    return bool(settings.dashboard_discord_client_id and settings.dashboard_discord_client_secret)


def build_discord_authorize_url(settings: BotSettings, state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.dashboard_discord_client_id,
            "redirect_uri": settings.dashboard_discord_redirect_uri,
            "response_type": "code",
            "scope": " ".join(OAUTH_SCOPES),
            "state": state,
        }
    )
    return f"{DISCORD_AUTH_BASE}?{query}"


def create_state_value() -> str:
    return secrets.token_urlsafe(24)


def sign_value(secret_key: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret_key.encode("utf-8"), body, hashlib.sha256).digest()
    return f"{base64.urlsafe_b64encode(body).decode('ascii')}.{base64.urlsafe_b64encode(signature).decode('ascii')}"


def load_signed_value(secret_key: str, token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    encoded_body, encoded_signature = token.split(".", 1)
    try:
        body = base64.urlsafe_b64decode(encoded_body.encode("ascii"))
        signature = base64.urlsafe_b64decode(encoded_signature.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return None

    expected = hmac.new(secret_key.encode("utf-8"), body, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    expires_at = payload.get("exp")
    if isinstance(expires_at, str):
        try:
            parsed = datetime.fromisoformat(expires_at)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.astimezone(timezone.utc) < datetime.now(timezone.utc):
            return None
    return payload if isinstance(payload, dict) else None


def build_state_cookie(secret_key: str, state: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    return sign_value(secret_key, {"state": state, "exp": expires_at.isoformat()})


def validate_state_cookie(secret_key: str, token: str | None, state: str) -> bool:
    payload = load_signed_value(secret_key, token)
    return bool(payload and payload.get("state") == state)


def build_viewer_cookie(secret_key: str, viewer: DashboardViewer) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    return sign_value(
        secret_key,
        {
            "discord_user_id": viewer.discord_user_id,
            "username": viewer.username,
            "display_name": viewer.display_name,
            "avatar_url": viewer.avatar_url,
            "role_ids": viewer.role_ids,
            "is_admin": viewer.is_admin,
            "allowed_channel_ids": viewer.allowed_channel_ids,
            "has_global_ticket_access": viewer.has_global_ticket_access,
            "exp": expires_at.isoformat(),
        },
    )


def load_viewer_from_cookie(secret_key: str, token: str | None) -> DashboardViewer | None:
    payload = load_signed_value(secret_key, token)
    if payload is None:
        return None
    try:
        return DashboardViewer(
            discord_user_id=int(payload["discord_user_id"]),
            username=str(payload["username"]),
            display_name=str(payload.get("display_name") or payload["username"]),
            avatar_url=str(payload["avatar_url"]) if payload.get("avatar_url") else None,
            role_ids=[int(role_id) for role_id in payload.get("role_ids", [])],
            is_admin=bool(payload.get("is_admin")),
            allowed_channel_ids=[int(channel_id) for channel_id in payload.get("allowed_channel_ids", [])],
            has_global_ticket_access=bool(payload.get("has_global_ticket_access")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def cookie_should_be_secure(settings: BotSettings) -> bool:
    return settings.dashboard_base_url.lower().startswith("https://")


def build_viewer_from_discord_user(
    settings: BotSettings,
    user_payload: dict[str, Any],
    *,
    role_ids: list[int],
) -> DashboardViewer:
    discord_user_id = int(user_payload["id"])
    username = str(user_payload["username"])
    display_name = str(user_payload.get("global_name") or username)
    avatar_hash = user_payload.get("avatar")
    avatar_url = None
    if avatar_hash:
        avatar_url = f"https://cdn.discordapp.com/avatars/{discord_user_id}/{avatar_hash}.png?size=128"

    allowed_channel_ids: set[int] = set()
    for role_id in role_ids:
        allowed_channel_ids.update(settings.dashboard_role_channel_access.get(role_id, []))

    return DashboardViewer(
        discord_user_id=discord_user_id,
        username=username,
        display_name=display_name,
        avatar_url=avatar_url,
        role_ids=role_ids,
        is_admin=discord_user_id in settings.dashboard_admin_user_ids,
        allowed_channel_ids=sorted(allowed_channel_ids),
        has_global_ticket_access=any(role_id in settings.dashboard_role_full_access_ids for role_id in role_ids),
    )


async def exchange_code_for_token(settings: BotSettings, code: str) -> str:
    payload = await _discord_request_json(
        f"{DISCORD_API_BASE}/oauth2/token",
        method="POST",
        form_data={
            "client_id": settings.dashboard_discord_client_id,
            "client_secret": settings.dashboard_discord_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.dashboard_discord_redirect_uri,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/",
        },
    )
    access_token = payload.get("access_token")
    if not access_token:
        raise DiscordOAuthError("Discord did not return an access token.")
    return str(access_token)


async def fetch_discord_user(access_token: str) -> dict[str, Any]:
    return await _discord_request_json(
        f"{DISCORD_API_BASE}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
    )


async def fetch_discord_member_roles(access_token: str, guild_id: int) -> list[int]:
    try:
        payload = await _discord_request_json(
            f"{DISCORD_API_BASE}/users/@me/guilds/{guild_id}/member",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except DiscordOAuthError:
        return []
    roles = payload.get("roles", [])
    return [int(role_id) for role_id in roles if str(role_id).isdigit()]


async def _discord_request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    form_data: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": DISCORD_HTTP_USER_AGENT,
    }
    if headers:
        request_headers.update(headers)
    data = urlencode(form_data).encode("utf-8") if form_data else None

    def _run() -> dict[str, Any]:
        request = Request(url, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise DiscordOAuthError(_format_http_error(exc.code, detail, exc.reason)) from exc
        except URLError as exc:
            raise DiscordOAuthError(f"Discord OAuth request failed: {exc.reason}") from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise DiscordOAuthError("Discord returned invalid JSON.") from exc
        if not isinstance(parsed, dict):
            raise DiscordOAuthError("Discord returned an unexpected response.")
        return parsed

    return await asyncio.to_thread(_run)


def _format_http_error(status_code: int, detail: str, reason: str) -> str:
    parsed: dict[str, Any] | None = None
    try:
        loaded = json.loads(detail)
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, dict):
        parsed = loaded

    if parsed and parsed.get("cloudflare_error") and parsed.get("error_code") == 1010:
        return (
            "Discord blocked the dashboard's OAuth callback request. "
            "The server was identified as an unsupported client signature. "
            "The dashboard now sends browser-compatible headers; please try signing in again."
        )

    if parsed:
        message = parsed.get("message") or parsed.get("detail") or parsed.get("title")
        if message:
            return f"Discord OAuth request failed ({status_code}): {message}"

    return f"Discord OAuth request failed ({status_code}): {detail or reason}"
