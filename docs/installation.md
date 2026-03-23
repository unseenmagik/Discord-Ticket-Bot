# Installation and Setup

## Clone and install

```bash
git clone https://github.com/unseenmagik/Discord-Ticket-Bot
cd Discord-Ticket-Bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.ini.example config.ini
```

## Database setup

Create a MariaDB/MySQL database and user:

```sql
CREATE DATABASE discord_tickets CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'ticketbot'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME';
GRANT ALL PRIVILEGES ON discord_tickets.* TO 'ticketbot'@'127.0.0.1';
FLUSH PRIVILEGES;
```

Import the schema:

```bash
mysql -u ticketbot -p discord_tickets < schema.sql
```

## Configure `config.ini`

At minimum, set:

- Discord token, guild ID, and panel channel ID
- transcript channel ID
- database connection values
- server label to channel mappings in `[servers]`
- dashboard OAuth values in `[dashboard]`

Important ticket options:

- `message_content_intent = true` if transcripts should include full message text
- `interaction_delete_after_seconds` to control ephemeral reply lifetime
- `allow_thread_owner_reopen = true` if ticket openers should be able to reopen their own tickets

If you enable `message_content_intent`, also enable Message Content Intent in the Discord Developer Portal.

## Start manually

Bot:

```bash
source .venv/bin/activate
python bot.py
```

Dashboard:

```bash
source .venv/bin/activate
python dashboard.py
```

Or with uvicorn:

```bash
source .venv/bin/activate
uvicorn support_ticket_bot.dashboard.app:create_app --factory --host 127.0.0.1 --port 8000
```

## Start with PM2

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
