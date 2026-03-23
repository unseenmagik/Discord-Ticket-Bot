# Discord Ticket Bot + Web Dashboard

A Discord support ticket bot with a FastAPI dashboard, MariaDB/MySQL storage, HTML/TXT transcripts, Discord OAuth login, managed tags, staff assignment, and dashboard-to-Discord thread sync.

![Dashboard overview](docs/images/dashboard-overview.png)

## Highlights

- Ticket creation from a pinned Discord panel
- Tickets created as threads in mapped queue channels
- Ticket lifecycle DMs for created, closed, and reopened tickets
- HTML and TXT transcripts
- Discord OAuth dashboard with role-aware access
- Staff assignment, internal notes, and managed tags
- Dashboard actions synced back into Discord by the bot
- Transcript browsing, stats, audit log, and admin controls

## Quick Start

```bash
git clone https://github.com/unseenmagik/Discord-Ticket-Bot
cd Discord-Ticket-Bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.ini.example config.ini
```

Create your MariaDB/MySQL database, import [schema.sql](schema.sql), update `config.ini`, then start:

```bash
python bot.py
python dashboard.py
```

## Documentation

- [Installation and setup](docs/installation.md)
- [Configuration reference](docs/configuration.md)
- [Dashboard guide](docs/dashboard.md)
- [Discord commands](docs/discord-commands.md)
- [Operations and deployment](docs/operations.md)

## Project Layout

```text
Discord-Ticket-Bot/
├── bot.py
├── dashboard.py
├── config.ini.example
├── requirements.txt
├── schema.sql
├── README.md
├── docs/
└── support_ticket_bot/
```

## Core Requirements

- Python 3.11+
- MariaDB/MySQL
- A Discord bot token
- A Discord application configured for dashboard OAuth

## License / Usage

This repository currently documents setup and operation in-repo through the `/docs` folder so documentation changes can stay in PRs alongside code changes.
