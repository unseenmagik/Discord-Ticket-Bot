import uvicorn

from support_ticket_bot.config import load_settings


if __name__ == "__main__":
    settings = load_settings()
    uvicorn.run(
        "support_ticket_bot.dashboard.app:create_app",
        factory=True,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        reload=False,
    )
