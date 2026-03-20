from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from support_ticket_bot.config import BotSettings, load_settings
from support_ticket_bot.db import DashboardDatabase
from support_ticket_bot.utils import hash_password

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
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Discord Ticket Dashboard", lifespan=lifespan)
    static_dir = BASE_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

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

    return app
