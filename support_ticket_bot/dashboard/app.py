from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from support_ticket_bot.config import BotSettings, load_settings
from support_ticket_bot.db import DashboardDatabase
from support_ticket_bot.transcript import TRANSCRIPTS_DIR
from support_ticket_bot.utils import DEFAULT_MESSAGE_TEMPLATES, hash_password

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
STATS_RANGE_LABELS = {
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "year": "Year to date",
    "all": "All time",
    "custom": "Custom range",
}


def require_login(request: Request) -> str:
    cookie = request.cookies.get("ticket_dashboard_auth")
    expected = hash_password(f"{request.app.state.settings.dashboard_username}:{request.app.state.settings.dashboard_password}")
    if cookie != expected:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return request.app.state.settings.dashboard_username


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
    selected = range_key if range_key in STATS_RANGE_LABELS else "30d"
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
            selected = "30d"
            start_date = today - timedelta(days=29)
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
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Discord Ticket Dashboard", lifespan=lifespan)
    static_dir = BASE_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return RedirectResponse(url="/static/favicon.svg", status_code=307)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return TEMPLATES.TemplateResponse("login.html", {"request": request, "error": None})

    @app.post("/login", response_class=HTMLResponse)
    async def login_action(request: Request, username: str = Form(...), password: str = Form(...)):
        settings: BotSettings = request.app.state.settings
        if username != settings.dashboard_username or password != settings.dashboard_password:
            return TEMPLATES.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials."}, status_code=401)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            "ticket_dashboard_auth",
            hash_password(f"{username}:{password}"),
            httponly=True,
            samesite="lax",
        )
        return response

    @app.get("/logout")
    async def logout():
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie("ticket_dashboard_auth")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, status_filter: str | None = None, user: str = Depends(require_login)):
        db: DashboardDatabase = request.app.state.db
        stats = db.get_stats()
        tickets = db.list_tickets(status=status_filter, limit=200)
        return TEMPLATES.TemplateResponse(
            "index.html",
            {
                "request": request,
                "stats": stats,
                "tickets": tickets,
                "status_filter": status_filter,
                "user": user,
            },
        )

    @app.get("/stats", response_class=HTMLResponse)
    async def stats_page(
        request: Request,
        range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        user: str = Depends(require_login),
    ):
        db: DashboardDatabase = request.app.state.db
        start_at, end_at, filters = _resolve_stats_range(range, start_date, end_date)
        analytics = db.get_ticket_analytics(start_at=start_at, end_at=end_at)
        return TEMPLATES.TemplateResponse(
            "stats.html",
            {
                "request": request,
                "analytics": analytics,
                "filters": filters,
                "user": user,
            },
        )

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request, saved: int = 0, user: str = Depends(require_login)):
        db: DashboardDatabase = request.app.state.db
        templates = db.get_message_templates()
        return TEMPLATES.TemplateResponse(
            "admin.html",
            {
                "request": request,
                "templates": templates,
                "saved": bool(saved),
                "user": user,
            },
        )

    @app.post("/admin/messages")
    async def save_admin_messages(
        request: Request,
        panel_title: str = Form(...),
        panel_description: str = Form(...),
        thread_embed_title: str = Form(...),
        thread_embed_description: str = Form(...),
        user: str = Depends(require_login),
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

    @app.get("/tickets/{thread_id}", response_class=HTMLResponse)
    async def ticket_detail(thread_id: int, request: Request, user: str = Depends(require_login)):
        db: DashboardDatabase = request.app.state.db
        ticket = db.get_ticket(thread_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return TEMPLATES.TemplateResponse(
            "ticket_detail.html",
            {
                "request": request,
                "ticket": ticket,
                "user": user,
            },
        )

    @app.get("/tickets/{thread_id}/transcript", response_class=HTMLResponse)
    async def ticket_transcript(thread_id: int, request: Request, user: str = Depends(require_login)):
        transcript_path = TRANSCRIPTS_DIR / f"{thread_id}.html"
        if not transcript_path.exists():
            raise HTTPException(status_code=404, detail="Transcript not found")
        return HTMLResponse(content=transcript_path.read_text(encoding="utf-8"))

    return app
