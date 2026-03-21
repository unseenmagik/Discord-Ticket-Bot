from __future__ import annotations

from contextlib import asynccontextmanager
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


def require_login(request: Request) -> str:
    cookie = request.cookies.get("ticket_dashboard_auth")
    expected = hash_password(f"{request.app.state.settings.dashboard_username}:{request.app.state.settings.dashboard_password}")
    if cookie != expected:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return request.app.state.settings.dashboard_username


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
        db.set_message_templates(
            values
        )
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
