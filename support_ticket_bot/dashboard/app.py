from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
import logging
from pathlib import Path
from urllib.parse import quote_plus, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler as fastapi_http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from support_ticket_bot.config import BotSettings, load_settings
from support_ticket_bot.dashboard.auth import (
    DashboardViewer,
    DiscordOAuthError,
    build_discord_authorize_url,
    build_state_cookie,
    build_viewer_cookie,
    build_viewer_from_discord_user,
    cookie_should_be_secure,
    create_state_value,
    discord_oauth_configured,
    exchange_code_for_token,
    fetch_guild_role_map,
    fetch_member_display_map,
    fetch_discord_member_roles,
    fetch_discord_user,
    load_viewer_from_cookie,
    post_discord_bot_message,
    post_discord_bot_embed,
    validate_state_cookie,
)
from support_ticket_bot.db import DashboardDatabase
from support_ticket_bot.transcript import TRANSCRIPTS_DIR
from support_ticket_bot.utils import DEFAULT_MESSAGE_TEMPLATES, utc_now_iso

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
SESSION_COOKIE_NAME = "ticket_dashboard_session"
STATE_COOKIE_NAME = "ticket_dashboard_oauth_state"
INFO_NOTICE_COLOR = 0x3B82F6
STATS_RANGE_LABELS = {
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "year": "Year to date",
    "all": "All time",
    "custom": "Custom range",
}
AUDIT_PAGE_SIZE = 5
log = logging.getLogger(__name__)


def _template_context(request: Request, viewer: DashboardViewer | None, **extra: object) -> dict[str, object]:
    return {
        "request": request,
        "viewer": viewer,
        "user": viewer.display_name if viewer else None,
        "is_admin": viewer.is_admin if viewer else False,
        **extra,
    }


def require_viewer(request: Request) -> DashboardViewer:
    viewer = load_viewer_from_cookie(
        request.app.state.settings.dashboard_secret_key,
        request.cookies.get(SESSION_COOKIE_NAME),
    )
    if viewer is None:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return viewer


def require_admin(viewer: DashboardViewer = Depends(require_viewer)) -> DashboardViewer:
    if not viewer.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return viewer


def _ticket_access_kwargs(viewer: DashboardViewer) -> dict[str, object]:
    allow_all = viewer.is_admin or viewer.has_global_ticket_access
    return {
        "opener_id": None if allow_all else viewer.discord_user_id,
        "channel_ids": None if allow_all else viewer.allowed_channel_ids,
        "allow_all": allow_all,
    }


def _viewer_has_staff_ticket_access(viewer: DashboardViewer) -> bool:
    return viewer.is_admin or viewer.has_global_ticket_access or bool(viewer.allowed_channel_ids)


def _viewer_can_manage_ticket(viewer: DashboardViewer, ticket: dict[str, object]) -> bool:
    if viewer.is_admin or viewer.has_global_ticket_access:
        return True
    target_channel_id = ticket.get("target_channel_id")
    try:
        return int(target_channel_id) in viewer.allowed_channel_ids
    except (TypeError, ValueError):
        return False


def _ticket_detail_url(thread_id: int, *, notice: str | None = None, error: str | None = None) -> str:
    query: dict[str, str] = {}
    if notice:
        query["notice"] = notice
    if error:
        query["error"] = error
    return f"/tickets/{thread_id}" + (f"?{urlencode(query)}" if query else "")


async def _post_ticket_thread_notice(
    settings: BotSettings,
    thread_id: int,
    *,
    title: str,
    description: str,
    color: int = INFO_NOTICE_COLOR,
) -> None:
    try:
        await post_discord_bot_embed(
            settings.token,
            thread_id,
            title=title,
            description=description,
            color=color,
        )
    except DiscordOAuthError as exc:
        log.warning("Failed to send dashboard thread embed notice title=%s thread_id=%s: %s", title, thread_id, exc)
        try:
            await post_discord_bot_message(settings.token, thread_id, f"**{title}**\n{description}")
        except DiscordOAuthError:
            log.exception("Failed to send dashboard fallback thread notice title=%s thread_id=%s", title, thread_id)


def _admin_url(
    *,
    saved: int | None = None,
    notice: str | None = None,
    error: str | None = None,
    audit_page: int | None = None,
) -> str:
    query: dict[str, str] = {}
    if saved:
        query["saved"] = str(saved)
    if notice:
        query["notice"] = notice
    if error:
        query["error"] = error
    if audit_page and audit_page > 1:
        query["audit_page"] = str(audit_page)
    return "/admin" + (f"?{urlencode(query)}" if query else "")


def _queue_label_map(settings: BotSettings) -> dict[int, str]:
    return {channel_id: label for label, channel_id in settings.server_targets.items()}


def _build_role_access_summary(settings: BotSettings, role_name_map: dict[int, str]) -> list[dict[str, object]]:
    queue_labels = _queue_label_map(settings)
    rows: list[dict[str, object]] = []

    for role_id in sorted(settings.dashboard_role_channel_access):
        channel_ids = settings.dashboard_role_channel_access[role_id]
        rows.append(
            {
                "role_id": role_id,
                "role_name": role_name_map.get(role_id, "Unknown role"),
                "access_scope": "Selected queues",
                "queues": [
                    {
                        "channel_id": channel_id,
                        "label": queue_labels.get(channel_id, "Unlabeled queue"),
                    }
                    for channel_id in channel_ids
                ],
            }
        )

    for role_id in sorted(settings.dashboard_role_full_access_ids):
        rows.append(
            {
                "role_id": role_id,
                "role_name": role_name_map.get(role_id, "Unknown role"),
                "access_scope": "All tracked queues",
                "queues": [],
            }
        )

    return rows


async def _build_admin_user_rows(settings: BotSettings) -> list[dict[str, object]]:
    if not settings.dashboard_admin_user_ids:
        return []
    try:
        name_map = await fetch_member_display_map(settings.token, settings.guild_id, settings.dashboard_admin_user_ids)
    except DiscordOAuthError:
        name_map = {}
    return [
        {
            "user_id": user_id,
            "display_name": name_map.get(user_id, "Unknown user"),
        }
        for user_id in sorted(settings.dashboard_admin_user_ids)
    ]


async def _build_access_summary_context(settings: BotSettings) -> dict[str, object]:
    try:
        role_name_map = await fetch_guild_role_map(settings.token, settings.guild_id)
    except DiscordOAuthError:
        role_name_map = {}
    return {
        "admin_user_rows": await _build_admin_user_rows(settings),
        "role_access_rows": _build_role_access_summary(settings, role_name_map),
    }


def _log_dashboard_audit_event(
    request: Request,
    *,
    viewer: DashboardViewer,
    event_type: str,
    ticket_thread_id: int | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    db: DashboardDatabase = request.app.state.db
    db.add_audit_event(
        event_type=event_type,
        actor_discord_user_id=viewer.discord_user_id,
        actor_username=viewer.username,
        actor_display_name=viewer.display_name,
        ticket_thread_id=ticket_thread_id,
        metadata=metadata,
        created_at=utc_now_iso(),
    )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _resolve_stats_range(
    range_key: str | None,
    start_date_value: str | None,
    end_date_value: str | None,
) -> tuple[datetime | None, datetime | None, dict[str, str | None]]:
    selected = range_key if range_key in STATS_RANGE_LABELS else "7d"
    today = datetime.now(timezone.utc).date()

    start_date = None
    end_date = None

    if selected == "7d":
        start_date = today - timedelta(days=6)
        end_date = today
    elif selected == "30d":
        start_date = today - timedelta(days=29)
        end_date = today
    elif selected == "90d":
        start_date = today - timedelta(days=89)
        end_date = today
    elif selected == "year":
        start_date = date(today.year, 1, 1)
        end_date = today
    elif selected == "custom":
        parsed_start = _parse_date(start_date_value)
        parsed_end = _parse_date(end_date_value)
        if parsed_start and parsed_end:
            start_date, end_date = sorted((parsed_start, parsed_end))
        elif parsed_start:
            start_date = parsed_start
            end_date = parsed_start
        elif parsed_end:
            start_date = parsed_end
            end_date = parsed_end
        else:
            selected = "7d"
            start_date = today - timedelta(days=6)
            end_date = today

    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc) if start_date else None
    end_at = datetime.combine(end_date, time.max, tzinfo=timezone.utc) if end_date else None
    if selected == "all":
        start_at = None
        end_at = None

    if start_date and end_date:
        label = f"{start_date.isoformat()} to {end_date.isoformat()}"
    else:
        label = STATS_RANGE_LABELS[selected]

    return start_at, end_at, {
        "selected": selected,
        "label": label,
        "start_date": start_date.isoformat() if start_date else "",
        "end_date": end_date.isoformat() if end_date else "",
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    app.state.db = DashboardDatabase(settings)
    app.state.db.ensure_app_settings_table()
    app.state.db.ensure_dashboard_audit_table()
    app.state.db.ensure_internal_notes_table()
    app.state.db.ensure_tag_tables()
    app.state.db.ensure_ticket_schema_updates()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Discord Ticket Dashboard", lifespan=lifespan)
    static_dir = BASE_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        viewer = load_viewer_from_cookie(
            request.app.state.settings.dashboard_secret_key,
            request.cookies.get(SESSION_COOKIE_NAME),
        )
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            return TEMPLATES.TemplateResponse(
                "403.html",
                _template_context(
                    request,
                    viewer,
                    message=getattr(exc, "detail", None) or "You do not have permission to access this page.",
                ),
                status_code=status.HTTP_403_FORBIDDEN,
            )
        return await fastapi_http_exception_handler(request, exc)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return RedirectResponse(url="/static/favicon.svg", status_code=307)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: str | None = None):
        settings: BotSettings = request.app.state.settings
        return TEMPLATES.TemplateResponse(
            "login.html",
            _template_context(
                request,
                None,
                error=error,
                oauth_configured=discord_oauth_configured(settings),
            ),
        )

    @app.get("/auth/discord/start")
    async def discord_login_start(request: Request):
        settings: BotSettings = request.app.state.settings
        if not discord_oauth_configured(settings):
            return RedirectResponse(
                url="/login?error=" + quote_plus("Discord OAuth is not configured yet."),
                status_code=303,
            )

        state = create_state_value()
        response = RedirectResponse(
            url=build_discord_authorize_url(settings, state),
            status_code=303,
        )
        response.set_cookie(
            STATE_COOKIE_NAME,
            build_state_cookie(settings.dashboard_secret_key, state),
            httponly=True,
            samesite="lax",
            secure=cookie_should_be_secure(settings),
            max_age=600,
        )
        return response

    @app.get("/auth/discord/callback")
    async def discord_login_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        settings: BotSettings = request.app.state.settings
        if error:
            return RedirectResponse(url="/login?error=" + quote_plus(f"Discord login failed: {error}"), status_code=303)
        if not code or not state:
            return RedirectResponse(url="/login?error=" + quote_plus("Missing Discord OAuth callback data."), status_code=303)
        if not validate_state_cookie(settings.dashboard_secret_key, request.cookies.get(STATE_COOKIE_NAME), state):
            return RedirectResponse(url="/login?error=" + quote_plus("Discord login state was invalid or expired."), status_code=303)

        try:
            access_token = await exchange_code_for_token(settings, code)
            user_payload = await fetch_discord_user(access_token)
            role_ids = await fetch_discord_member_roles(access_token, settings.guild_id)
            viewer = build_viewer_from_discord_user(settings, user_payload, role_ids=role_ids)
        except DiscordOAuthError as exc:
            return RedirectResponse(url="/login?error=" + quote_plus(str(exc)), status_code=303)

        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            build_viewer_cookie(settings.dashboard_secret_key, viewer),
            httponly=True,
            samesite="lax",
            secure=cookie_should_be_secure(settings),
            max_age=60 * 60 * 24 * 7,
        )
        _log_dashboard_audit_event(
            request,
            viewer=viewer,
            event_type="dashboard_login",
            metadata={"is_admin": viewer.is_admin, "has_global_ticket_access": viewer.has_global_ticket_access},
        )
        response.delete_cookie(STATE_COOKIE_NAME)
        return response

    @app.get("/logout")
    async def logout():
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE_NAME)
        response.delete_cookie(STATE_COOKIE_NAME)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, status_filter: str | None = None, viewer: DashboardViewer = Depends(require_viewer)):
        db: DashboardDatabase = request.app.state.db
        access_kwargs = _ticket_access_kwargs(viewer)
        stats = db.get_stats(**access_kwargs)
        tickets = db.list_tickets(status=status_filter, limit=200, **access_kwargs)
        return TEMPLATES.TemplateResponse(
            "index.html",
            _template_context(
                request,
                viewer,
                stats=stats,
                tickets=tickets,
                status_filter=status_filter,
                limited_access=not (viewer.is_admin or viewer.has_global_ticket_access),
                show_staff_ticket_fields=_viewer_has_staff_ticket_access(viewer),
            ),
        )

    @app.get("/stats", response_class=HTMLResponse)
    async def stats_page(
        request: Request,
        range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        viewer: DashboardViewer = Depends(require_admin),
    ):
        db: DashboardDatabase = request.app.state.db
        start_at, end_at, filters = _resolve_stats_range(range, start_date, end_date)
        analytics = db.get_ticket_analytics(start_at=start_at, end_at=end_at)
        return TEMPLATES.TemplateResponse(
            "stats.html",
            _template_context(
                request,
                viewer,
                analytics=analytics,
                filters=filters,
            ),
        )

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page(
        request: Request,
        saved: int = 0,
        notice: str | None = None,
        error: str | None = None,
        audit_page: int = 1,
        viewer: DashboardViewer = Depends(require_admin),
    ):
        db: DashboardDatabase = request.app.state.db
        settings: BotSettings = request.app.state.settings
        templates = db.get_message_templates()
        tag_definitions = db.list_tag_definitions()
        total_audit_events = db.count_audit_events()
        total_audit_pages = max(1, (total_audit_events + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)
        audit_page = min(max(audit_page, 1), total_audit_pages)
        audit_events = db.list_audit_events(limit=AUDIT_PAGE_SIZE, offset=(audit_page - 1) * AUDIT_PAGE_SIZE)
        access_context = await _build_access_summary_context(settings)
        return TEMPLATES.TemplateResponse(
            "admin.html",
            _template_context(
                request,
                viewer,
                templates=templates,
                saved=bool(saved),
                notice=notice,
                error=error,
                admin_user_rows=access_context["admin_user_rows"],
                role_access_rows=access_context["role_access_rows"],
                tag_definitions=tag_definitions,
                audit_events=audit_events,
                audit_page=audit_page,
                audit_total_pages=total_audit_pages,
                audit_total_events=total_audit_events,
                audit_has_prev=audit_page > 1,
                audit_has_next=audit_page < total_audit_pages,
                audit_prev_page=audit_page - 1,
                audit_next_page=audit_page + 1,
            ),
        )

    @app.post("/admin/messages")
    async def save_admin_messages(
        request: Request,
        panel_title: str = Form(...),
        panel_description: str = Form(...),
        thread_embed_title: str = Form(...),
        thread_embed_description: str = Form(...),
        viewer: DashboardViewer = Depends(require_admin),
    ):
        db: DashboardDatabase = request.app.state.db
        values = {
            "panel_title": panel_title.strip() or DEFAULT_MESSAGE_TEMPLATES["panel_title"],
            "panel_description": panel_description.strip() or DEFAULT_MESSAGE_TEMPLATES["panel_description"],
            "thread_embed_title": thread_embed_title.strip() or DEFAULT_MESSAGE_TEMPLATES["thread_embed_title"],
            "thread_embed_description": thread_embed_description.strip()
            or DEFAULT_MESSAGE_TEMPLATES["thread_embed_description"],
        }
        db.set_message_templates(values)
        return RedirectResponse(url="/admin?saved=1", status_code=303)

    @app.post("/admin/tags")
    async def create_admin_tag(
        request: Request,
        tag_name: str = Form(...),
        viewer: DashboardViewer = Depends(require_admin),
    ):
        db: DashboardDatabase = request.app.state.db
        cleaned_name = " ".join(tag_name.strip().split())
        if not cleaned_name:
            return RedirectResponse(url=_admin_url(error="Tag name cannot be empty."), status_code=303)

        existing = db.get_tag_definition_by_name(cleaned_name)
        if existing is not None:
            return RedirectResponse(
                url=_admin_url(error=f'Tag "{existing["tag_name"]}" already exists.'),
                status_code=303,
            )

        created = db.create_tag_definition(
            tag_name=cleaned_name,
            created_by_discord_user_id=viewer.discord_user_id,
            created_by_display_name=viewer.display_name,
            created_at=utc_now_iso(),
        )
        _log_dashboard_audit_event(
            request,
            viewer=viewer,
            event_type="tag_created",
            metadata={"tag_id": created["id"], "tag_name": created["tag_name"]},
        )
        return RedirectResponse(url=_admin_url(notice=f'Tag "{created["tag_name"]}" created.'), status_code=303)

    @app.post("/admin/tags/{tag_id}/delete")
    async def delete_admin_tag(
        tag_id: int,
        request: Request,
        viewer: DashboardViewer = Depends(require_admin),
    ):
        db: DashboardDatabase = request.app.state.db
        tag = next((item for item in db.list_tag_definitions() if item["id"] == tag_id), None)
        if tag is None:
            return RedirectResponse(url=_admin_url(error="Tag not found."), status_code=303)

        db.delete_tag_definition(tag_id)
        _log_dashboard_audit_event(
            request,
            viewer=viewer,
            event_type="tag_deleted",
            metadata={"tag_id": tag_id, "tag_name": tag["tag_name"]},
        )
        return RedirectResponse(url=_admin_url(notice=f'Tag "{tag["tag_name"]}" deleted.'), status_code=303)

    @app.get("/tickets/{thread_id}", response_class=HTMLResponse)
    async def ticket_detail(
        thread_id: int,
        request: Request,
        notice: str | None = None,
        error: str | None = None,
        viewer: DashboardViewer = Depends(require_viewer),
    ):
        db: DashboardDatabase = request.app.state.db
        ticket = db.get_ticket(thread_id, **_ticket_access_kwargs(viewer))
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        transcript_path = TRANSCRIPTS_DIR / f"{thread_id}.html"
        transcript_url = ticket.get("transcript_message_url") or (
            f"/tickets/{thread_id}/transcript" if transcript_path.exists() else None
        )
        can_manage_ticket = _viewer_can_manage_ticket(viewer, ticket)
        internal_notes = db.list_ticket_notes(thread_id) if can_manage_ticket else []
        available_tags = db.list_tag_definitions()
        current_tag_ids = {tag["id"] for tag in ticket.get("tags", [])}
        return TEMPLATES.TemplateResponse(
            "ticket_detail.html",
            _template_context(
                request,
                viewer,
                ticket=ticket,
                transcript_url=transcript_url,
                can_manage_ticket=can_manage_ticket,
                internal_notes=internal_notes,
                available_tags=available_tags,
                current_tag_ids=current_tag_ids,
                notice=notice,
                error=error,
            ),
        )

    @app.post("/tickets/{thread_id}/assign")
    async def assign_ticket_to_self(
        thread_id: int,
        request: Request,
        viewer: DashboardViewer = Depends(require_viewer),
    ):
        db: DashboardDatabase = request.app.state.db
        settings: BotSettings = request.app.state.settings
        ticket = db.get_ticket(thread_id, **_ticket_access_kwargs(viewer))
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if not _viewer_can_manage_ticket(viewer, ticket):
            raise HTTPException(status_code=403, detail="You do not have permission to assign this ticket.")
        if ticket.get("status") != "open":
            return RedirectResponse(
                url=_ticket_detail_url(thread_id, error="Only open tickets can be assigned."),
                status_code=303,
            )

        db.assign_ticket(
            thread_id=thread_id,
            assignee_discord_user_id=viewer.discord_user_id,
            assignee_display_name=viewer.display_name,
            assigned_at=utc_now_iso(),
            assigned_by_discord_user_id=viewer.discord_user_id,
            assigned_by_display_name=viewer.display_name,
        )
        action_label = "reassigned" if ticket.get("assignee_discord_user_id") else "assigned"
        await _post_ticket_thread_notice(
            settings,
            thread_id,
            title="Ticket Reassigned" if ticket.get("assignee_discord_user_id") else "Ticket Assigned",
            description=f"This ticket has been {action_label} to <@{viewer.discord_user_id}> by <@{viewer.discord_user_id}>.",
        )
        _log_dashboard_audit_event(
            request,
            viewer=viewer,
            event_type="ticket_assigned",
            ticket_thread_id=thread_id,
            metadata={"assignee_display_name": viewer.display_name, "assignee_discord_user_id": viewer.discord_user_id},
        )
        return RedirectResponse(
            url=_ticket_detail_url(thread_id, notice="Ticket assigned to you."),
            status_code=303,
        )

    @app.post("/tickets/{thread_id}/unassign")
    async def unassign_ticket(
        thread_id: int,
        request: Request,
        viewer: DashboardViewer = Depends(require_viewer),
    ):
        db: DashboardDatabase = request.app.state.db
        settings: BotSettings = request.app.state.settings
        ticket = db.get_ticket(thread_id, **_ticket_access_kwargs(viewer))
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if not _viewer_can_manage_ticket(viewer, ticket):
            raise HTTPException(status_code=403, detail="You do not have permission to update this ticket.")
        if ticket.get("status") != "open":
            return RedirectResponse(
                url=_ticket_detail_url(thread_id, error="Only open tickets can be unassigned."),
                status_code=303,
            )
        if not ticket.get("assignee_discord_user_id"):
            return RedirectResponse(
                url=_ticket_detail_url(thread_id, notice="Ticket was already unassigned."),
                status_code=303,
            )

        previous_assignee = ticket.get("assignee_display_name") or ticket.get("assignee_discord_user_id")
        db.clear_ticket_assignee(thread_id=thread_id)
        await _post_ticket_thread_notice(
            settings,
            thread_id,
            title="Ticket Unassigned",
            description=f"This ticket has been unassigned by <@{viewer.discord_user_id}>.",
        )
        _log_dashboard_audit_event(
            request,
            viewer=viewer,
            event_type="ticket_unassigned",
            ticket_thread_id=thread_id,
            metadata={"previous_assignee": previous_assignee},
        )
        return RedirectResponse(
            url=_ticket_detail_url(thread_id, notice="Ticket unassigned."),
            status_code=303,
        )

    @app.post("/tickets/{thread_id}/notes")
    async def add_ticket_note(
        thread_id: int,
        request: Request,
        note_text: str = Form(...),
        viewer: DashboardViewer = Depends(require_viewer),
    ):
        db: DashboardDatabase = request.app.state.db
        ticket = db.get_ticket(thread_id, **_ticket_access_kwargs(viewer))
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if not _viewer_can_manage_ticket(viewer, ticket):
            raise HTTPException(status_code=403, detail="You do not have permission to add internal notes.")

        cleaned_note = note_text.strip()
        if not cleaned_note:
            return RedirectResponse(
                url=_ticket_detail_url(thread_id, error="Internal note cannot be empty."),
                status_code=303,
            )
        if ticket.get("status") == "deleted":
            return RedirectResponse(
                url=_ticket_detail_url(thread_id, error="Cannot add notes to a deleted ticket."),
                status_code=303,
            )

        db.add_ticket_note(
            thread_id=thread_id,
            author_discord_user_id=viewer.discord_user_id,
            author_display_name=viewer.display_name,
            note_text=cleaned_note,
            created_at=utc_now_iso(),
        )
        _log_dashboard_audit_event(
            request,
            viewer=viewer,
            event_type="ticket_internal_note_added",
            ticket_thread_id=thread_id,
            metadata={"note_length": len(cleaned_note)},
        )
        return RedirectResponse(
            url=_ticket_detail_url(thread_id, notice="Internal note added."),
            status_code=303,
        )

    @app.post("/tickets/{thread_id}/tags")
    async def add_ticket_tag(
        thread_id: int,
        request: Request,
        tag_id: int = Form(...),
        viewer: DashboardViewer = Depends(require_viewer),
    ):
        db: DashboardDatabase = request.app.state.db
        settings: BotSettings = request.app.state.settings
        ticket = db.get_ticket(thread_id, **_ticket_access_kwargs(viewer))
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if not _viewer_can_manage_ticket(viewer, ticket):
            raise HTTPException(status_code=403, detail="You do not have permission to update ticket tags.")
        if ticket.get("status") != "open":
            return RedirectResponse(
                url=_ticket_detail_url(thread_id, error="Only open tickets can be updated."),
                status_code=303,
            )

        tag = next((item for item in db.list_tag_definitions() if item["id"] == tag_id), None)
        if tag is None:
            return RedirectResponse(url=_ticket_detail_url(thread_id, error="Tag not found."), status_code=303)
        if any(existing["id"] == tag_id for existing in ticket.get("tags", [])):
            return RedirectResponse(
                url=_ticket_detail_url(thread_id, notice=f'Tag "{tag["tag_name"]}" was already applied.'),
                status_code=303,
            )

        db.add_ticket_tag(
            thread_id=thread_id,
            tag_id=tag_id,
            assigned_at=utc_now_iso(),
            assigned_by_discord_user_id=viewer.discord_user_id,
            assigned_by_display_name=viewer.display_name,
        )
        await _post_ticket_thread_notice(
            settings,
            thread_id,
            title="Tag Added",
            description=f'The tag "{tag["tag_name"]}" was added to this ticket by <@{viewer.discord_user_id}>.',
        )
        _log_dashboard_audit_event(
            request,
            viewer=viewer,
            event_type="ticket_tag_added",
            ticket_thread_id=thread_id,
            metadata={"tag_id": tag_id, "tag_name": tag["tag_name"], "source": "dashboard"},
        )
        return RedirectResponse(
            url=_ticket_detail_url(thread_id, notice=f'Tag "{tag["tag_name"]}" added.'),
            status_code=303,
        )

    @app.post("/tickets/{thread_id}/tags/{tag_id}/remove")
    async def remove_ticket_tag(
        thread_id: int,
        tag_id: int,
        request: Request,
        viewer: DashboardViewer = Depends(require_viewer),
    ):
        db: DashboardDatabase = request.app.state.db
        settings: BotSettings = request.app.state.settings
        ticket = db.get_ticket(thread_id, **_ticket_access_kwargs(viewer))
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if not _viewer_can_manage_ticket(viewer, ticket):
            raise HTTPException(status_code=403, detail="You do not have permission to update ticket tags.")
        if ticket.get("status") != "open":
            return RedirectResponse(
                url=_ticket_detail_url(thread_id, error="Only open tickets can be updated."),
                status_code=303,
            )

        existing_tag = next((tag for tag in ticket.get("tags", []) if tag["id"] == tag_id), None)
        if existing_tag is None:
            return RedirectResponse(url=_ticket_detail_url(thread_id, notice="Tag was already removed."), status_code=303)

        db.remove_ticket_tag(thread_id=thread_id, tag_id=tag_id)
        await _post_ticket_thread_notice(
            settings,
            thread_id,
            title="Tag Removed",
            description=f'The tag "{existing_tag["tag_name"]}" was removed from this ticket by <@{viewer.discord_user_id}>.',
        )
        _log_dashboard_audit_event(
            request,
            viewer=viewer,
            event_type="ticket_tag_removed",
            ticket_thread_id=thread_id,
            metadata={"tag_id": tag_id, "tag_name": existing_tag["tag_name"], "source": "dashboard"},
        )
        return RedirectResponse(
            url=_ticket_detail_url(thread_id, notice=f'Tag "{existing_tag["tag_name"]}" removed.'),
            status_code=303,
        )

    @app.get("/tickets/{thread_id}/transcript", response_class=HTMLResponse)
    async def ticket_transcript(thread_id: int, request: Request, viewer: DashboardViewer = Depends(require_viewer)):
        db: DashboardDatabase = request.app.state.db
        ticket = db.get_ticket(thread_id, **_ticket_access_kwargs(viewer))
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        transcript_path = TRANSCRIPTS_DIR / f"{thread_id}.html"
        if not transcript_path.exists():
            raise HTTPException(status_code=404, detail="Transcript not found")
        _log_dashboard_audit_event(
            request,
            viewer=viewer,
            event_type="ticket_transcript_view",
            ticket_thread_id=thread_id,
            metadata={"status": ticket.get("status"), "server_label": ticket.get("server_label")},
        )
        return HTMLResponse(content=transcript_path.read_text(encoding="utf-8"))

    return app
