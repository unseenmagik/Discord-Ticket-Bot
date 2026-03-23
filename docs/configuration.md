# Configuration Reference

## Main sections

The project reads from `config.ini`.

Important sections:

- `[discord]`
- `[logs]`
- `[tickets]`
- `[support]`
- `[servers]`
- `[database]`
- `[dashboard]`
- `[dashboard_role_access]`

## Discord

Typical required values:

- `token`
- `guild_id`
- `panel_channel_id`
- `message_content_intent`

`guild_id` is especially important because:

- slash commands are synced to that guild
- dashboard access summaries resolve against that guild
- role and member lookups for managed access depend on it

## Ticket settings

Key options:

- `thread_name_prefix`
- `auto_archive_duration`
- `delete_closed_threads_after_hours`
- `allow_thread_owner_close`
- `allow_thread_owner_reopen`
- `close_requires_staff`
- `interaction_delete_after_seconds`
- `hidden_thread_tag_names`
- `embed_color`

`hidden_thread_tag_names` is a comma-separated list of managed tag names that should not appear in the ticket thread's Quick Tags buttons when a ticket is opened. Those tags still remain available in slash commands and the dashboard.

## Transcript settings

In `[logs]`:

- `transcript_channel_id`
- `save_txt_transcript`
- `save_html_transcript`

## Support roles

In `[support]`:

- `role_ids` is the comma-separated list of support/staff roles

## Queue mapping

In `[servers]`, each key/value pair is:

- `label = target_channel_id`

Example:

```ini
[servers]
Billing = 123456789012345678
Technical Support = 234567890123456789
```

## Dashboard

In `[dashboard]`:

- `enabled`
- `host`
- `port`
- `secret_key`
- `base_url`
- `discord_client_id`
- `discord_client_secret`
- `discord_redirect_uri`
- `admin_user_ids`

`base_url` should be your public dashboard URL so transcript links and dashboard links point to the correct host.

## Dashboard role access

`[dashboard_role_access]` maps Discord role IDs to ticket queue visibility.

Rules:

- left side = Discord role ID
- right side = comma-separated tracked queue channel IDs, or `*`
- users always see tickets they opened themselves
- matching roles add queue visibility
- `*` grants full ticket visibility, but not admin-page access

Example:

```ini
[dashboard_role_access]
456789012345678901 = 123456789012345678,234567890123456789
345678901234567890 = *
```
