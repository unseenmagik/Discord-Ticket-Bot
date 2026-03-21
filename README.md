# Discord Ticket Bot + Web Dashboard

A modular Discord support ticket bot built with `discord.py`, `aiomysql`, and `FastAPI`.

## Included

- Single pinned ticket panel with a **Create Ticket** button
- Ephemeral server picker for pre-set server/admin options
- Each ticket created as a **thread** in the mapped channel
- **Close Ticket** button inside the thread
- **Reopen Ticket** and **Delete Now** buttons in the transcript log channel
- **TXT and HTML transcripts**
- **Transcript DM** sent to the ticket opener when a ticket is closed
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
            ├── stats.html
            └── ticket_detail.html
```

## Installation and setup

1. Clone the repository and enter the project folder:

```bash
git clone https://github.com/unseenmagik/Discord-Ticket-Bot
cd Discord-Ticket-Bot
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

5. Create a MariaDB/MySQL database, create a user, and set that user's password in the SQL below by replacing `CHANGE_ME`:

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
- `message_content_intent = true` if you want ticket transcripts to include the actual message text
- Transcript channel ID
- MariaDB/MySQL connection details in `[database]`, using the same database name, username, and password you created in step 5
- Dashboard Discord OAuth settings in `[dashboard]`
- `interaction_delete_after_seconds` in `[tickets]` to control how long ephemeral action replies stay visible
- Server label → channel ID mappings in `[servers]`
- Optional role-based dashboard ticket access in `[dashboard_role_access]`

If you enable `message_content_intent`, you must also enable the Message Content Intent for your bot in the Discord Developer Portal.

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

## Logging

The bot writes logs to both the console and:

```text
logs/discord-ticket-bot.log
```

Ticket lifecycle events such as open, close, reopen, and delete are logged there.

## Slash commands

- `/setup_tickets`
- `/ticket_panel`
- `/close_ticket`
- `/reopen_ticket`
- `/add_ticket_user`
- `/ticket_info`

## Permissions needed

Discord bot permissions:

- View Channel
- Send Messages
- Read Message History
- Create Public Threads
- Send Messages in Threads
- Manage Threads
- Embed Links
- Attach Files
- Manage Messages

Discord Developer Portal privileged intents:

- Presence Intent
- Server Members Intent
- Message Content Intent

## WebUI

The dashboard is a web UI for viewing ticket stats and browsing saved tickets.

By default, it runs on:

```text
http://127.0.0.1:8000
```

You can change this in the `[dashboard]` section of `config.ini`:

- `host` sets the bind address
- `port` sets the web port
- `base_url` should match the public URL you want the dashboard to use in ticket links
- `discord_client_id` and `discord_client_secret` should come from your Discord application
- `discord_redirect_uri` must match the redirect URI configured in the Discord Developer Portal
- `admin_user_ids` lists the Discord users who can see all dashboard pages, including `Stats` and `Admin`

Optional dashboard ticket access by Discord role can be configured in `[dashboard_role_access]`:

- Each key is a Discord role ID
- Each value is either a comma-separated list of tracked ticket channel IDs or `*`
- Users always see tickets they opened themselves
- A matching role grants access to tickets for the configured channel IDs
- `*` grants access to all tracked tickets, but does not grant admin-page access

To access the WebUI:

1. Start the dashboard with `python dashboard.py` or run it with PM2
2. Open your browser and go to `http://127.0.0.1:8000` or your configured `base_url`
3. Click `Sign in with Discord` and complete the OAuth flow with a Discord account

Inside the WebUI you can:

- View total, open, closed, and deleted ticket counts for the tickets your Discord account is allowed to access
- Filter tickets by status
- Open an individual ticket detail page by clicking `Open`
- Open the saved HTML transcript for a closed ticket directly in your browser
- If your Discord user is listed in `admin_user_ids`, also access the full `Stats` and `Admin` pages
- If you are denied access to an admin-only page, the dashboard now shows a friendly `403` page instead of a raw framework error

## Admin page

The dashboard `Admin` page lets you change:

- The ticket panel title
- The ticket panel description
- The initial thread message title
- The initial thread message description
- Review configured admin user IDs and role-based queue visibility rules
- Review recent dashboard audit events such as successful logins and transcript views

These changes apply to new ticket panels and new tickets after they are saved.

Supported placeholders for the ticket panel message:

- `{guild_name}`
- `{panel_channel_mention}`

Supported placeholders for the initial thread message:

- `{guild_name}`
- `{server_label}`
- `{user_mention}`
- `{user_name}`
- `{thread_id}`

You can also hardcode Discord mentions directly:

- User mention: `<@1234567890>`
- Role mention: `<@&1234567890>`
- Channel mention: `<#1234567890>`

Example thread message:

```text
**Server:** {server_label}
**Opened by:** {user_mention}
Moderator ping: <@&1234567890>
**Ticket ID:** `{thread_id}`
```

Example panel description:

```text
Welcome to {guild_name}.
Press **Create Ticket** below to open a support request in {panel_channel_mention}.
```

If your bot is running on a remote server, keep `host = 127.0.0.1` if you only want local access through a reverse proxy or SSH tunnel. If you want the dashboard to listen publicly, change `host` to `0.0.0.0` and secure it properly before exposing it to the internet.

## Dashboard notes

- The dashboard uses FastAPI and server-rendered templates for the login page, overview page, stats page, admin page, and individual ticket detail pages.
- Discord OAuth requires a redirect URI in the Discord Developer Portal that exactly matches `discord_redirect_uri` in `config.ini`.
- The dashboard requests the `identify` and `guilds.members.read` OAuth scopes so it can identify the user and read their roles in the configured guild.
- Successful dashboard logins and transcript views are written to the `dashboard_audit_log` table and shown on the `Admin` page.
- Closed ticket log messages can link back to the dashboard using the `base_url` configured in `[dashboard]`.
- The bot stores ticket data in MariaDB/MySQL, and the dashboard reads from the same database to display ticket history and statistics.
- Full transcript message text requires the Discord Message Content Intent to be enabled both in `config.ini` and in the Discord Developer Portal.
