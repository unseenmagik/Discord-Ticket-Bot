# Operations and Deployment

## Logging

The bot writes logs to:

```text
logs/discord-ticket-bot.log
```

The dashboard uses the same logging setup when started via `python dashboard.py`.

## Dashboard hosting notes

By default the dashboard runs on:

```text
http://127.0.0.1:8000
```

Common options:

- keep `host = 127.0.0.1` if serving through a reverse proxy or tunnel
- use `base_url` as the public URL that transcript links should use
- make sure `discord_redirect_uri` exactly matches the Discord Developer Portal setting

If exposing the dashboard publicly, secure it properly behind HTTPS and a reverse proxy.

## Database-backed workflow queues

The bot processes several queued actions from the database:

- `ticket_thread_notices`
- `ticket_thread_member_sync`

These power dashboard-to-Discord updates, including:

- thread notices for assignment and tag changes
- adding/removing assigned users from ticket threads

The bot also maintains cached guild directory tables for the dashboard:

- `guild_member_directory`
- `guild_role_directory`

These are used by the Managed Access tab so the dashboard does not need to call Discord directly for role/member names.

## Troubleshooting tips

If dashboard access summaries show unknown users or roles:

- confirm `[discord] guild_id` matches the intended server
- confirm the bot is actually in that guild
- restart the bot so it refreshes the cached guild directory tables
- then refresh the dashboard

If dashboard-originated thread changes do not appear immediately:

- check the bot logs
- remember queued actions may appear a few seconds after the web action

If transcript links use `127.0.0.1` instead of your public hostname:

- update `[dashboard] base_url`
- restart bot and dashboard
