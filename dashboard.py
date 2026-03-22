import uvicorn

from support_ticket_bot.config import load_settings
from support_ticket_bot.logging_setup import setup_logging


if __name__ == "__main__":
    setup_logging()
    settings = load_settings()
    uvicorn.run(
        "support_ticket_bot.dashboard.app:create_app",
        factory=True,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        reload=False,
    )
