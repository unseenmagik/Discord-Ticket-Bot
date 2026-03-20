# Discord Ticket Bot + Web Dashboard

A modular Discord support ticket bot built with `discord.py`, `aiomysql`, and `FastAPI`.

## Included

- Single pinned ticket panel with a **Create Ticket** button
- Ephemeral server picker for pre-set server/admin options
- Each ticket created as a **thread** in the mapped channel
- **Close Ticket** button inside the thread
- **Reopen Ticket** and **Delete Now** buttons in the transcript log channel
- **TXT and HTML transcripts**
- **MariaDB / MySQL** storage instead of SQLite
- **FastAPI web dashboard** for stats and ticket browsing
- **Auto-delete** for closed threads after a configurable delay
- Modular structure with cogs, views, shared config, DB layer, and dashboard app

## Project layout

```text
discord_ticket_bot_web_mysql/
├── bot.py
├── dashboard.py
├── config.ini.example
├── requirements.txt
├── schema.sql
├── README.md
└── support_ticket_bot/
    ├── __init__.py
    ├── bot_core.py
    ├── config.py
    ├── db.py
    ├── logging_setup.py
    ├── transcript.py
    ├── utils.py
    ├── cogs/
    │   ├── __init__.py
    │   └── tickets.py
    ├── views/
    │   ├── __init__.py
    │   └── ticket_views.py
    └── dashboard/
        ├── __init__.py
        ├── app.py
        └── templates/
            ├── base.html
            ├── login.html
            ├── index.html
            └── ticket_detail.html
```

## Installation and setup

1. Clone the repository and enter the project folder:

```bash
git clone <your-repo-url>
cd <your-repo-folder>
```

2. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Create your config file from the example:

```bash
cp config.ini.example config.ini
```

5. Create a MariaDB/MySQL database and user:

```sql
CREATE DATABASE discord_tickets CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'ticketbot'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME';
GRANT ALL PRIVILEGES ON discord_tickets.* TO 'ticketbot'@'127.0.0.1';
FLUSH PRIVILEGES;
```

6. Import the schema:

```bash
mysql -u ticketbot -p discord_tickets < schema.sql
```

7. Edit `config.ini` and set:

- Discord token, guild ID, panel channel ID
- Transcript channel ID
- MariaDB/MySQL connection details in `[database]`
- Dashboard credentials in `[dashboard]`
- Server label → channel ID mappings in `[servers]`

## Start manually

Start the bot:

```bash
source .venv/bin/activate
python bot.py
```

Start the dashboard in a separate terminal:

```bash
source .venv/bin/activate
python dashboard.py
```

Or directly with uvicorn:

```bash
source .venv/bin/activate
uvicorn support_ticket_bot.dashboard.app:create_app --factory --host 127.0.0.1 --port 8000
```

## Start with PM2

Run both processes with PM2 using the virtual environment's Python interpreter:

```bash
pm2 start bot.py --name discord-ticket-bot --interpreter .venv/bin/python
pm2 start dashboard.py --name discord-ticket-dashboard --interpreter .venv/bin/python
pm2 save
pm2 startup
```

Useful PM2 commands:

```bash
pm2 status
pm2 logs discord-ticket-bot
pm2 logs discord-ticket-dashboard
pm2 restart discord-ticket-bot
pm2 restart discord-ticket-dashboard
```

## Slash commands

- `/setup_tickets`
- `/ticket_panel`
- `/close_ticket`
- `/reopen_ticket`
- `/ticket_info`

## Permissions needed

- View Channel
- Send Messages
- Read Message History
- Create Public Threads
- Send Messages in Threads
- Manage Threads
- Embed Links
- Attach Files
- Manage Messages

## Dashboard notes

The dashboard uses FastAPI with an app lifespan pattern for startup/shutdown management. FastAPI recommends lifespan for startup/shutdown logic, and `APIRouter` is the standard way to structure larger apps. citeturn777799search10turn777799search6

The bot uses persistent Discord component views, explicit `custom_id`s, and thread creation from a seed message in the destination channel. Public thread state like archive/lock is supported by `discord.py`. citeturn777799search0

The database layer uses an async MariaDB/MySQL connection pool with `aiomysql.create_pool(...)`, which is the documented pattern for pooled async access. citeturn777799search1turn777799search11
