from __future__ import annotations

from collections import Counter
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import aiomysql
import pymysql

from .config import BotSettings
from .utils import DEFAULT_MESSAGE_TEMPLATES

APP_SETTINGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS app_settings (
    setting_key VARCHAR(100) PRIMARY KEY,
    setting_value TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""
APP_SETTINGS_TABLE_NAME = "app_settings"
AUDIT_LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dashboard_audit_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    event_type VARCHAR(100) NOT NULL,
    actor_discord_user_id BIGINT NOT NULL,
    actor_username VARCHAR(255) NOT NULL,
    actor_display_name VARCHAR(255) NOT NULL,
    ticket_thread_id BIGINT NULL,
    metadata_json TEXT NULL,
    created_at VARCHAR(64) NOT NULL,
    INDEX idx_dashboard_audit_created_at (created_at),
    INDEX idx_dashboard_audit_actor (actor_discord_user_id),
    INDEX idx_dashboard_audit_event_type (event_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""
AUDIT_LOG_TABLE_NAME = "dashboard_audit_log"
INTERNAL_NOTES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ticket_internal_notes (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    thread_id BIGINT NOT NULL,
    author_discord_user_id BIGINT NOT NULL,
    author_display_name VARCHAR(255) NOT NULL,
    note_text TEXT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    INDEX idx_ticket_internal_notes_thread_created (thread_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""
INTERNAL_NOTES_TABLE_NAME = "ticket_internal_notes"
TAG_DEFINITIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ticket_tags (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    tag_key VARCHAR(100) NOT NULL UNIQUE,
    tag_name VARCHAR(100) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    created_by_discord_user_id BIGINT NULL,
    created_by_display_name VARCHAR(255) NULL,
    INDEX idx_ticket_tags_name (tag_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""
TAG_DEFINITIONS_TABLE_NAME = "ticket_tags"
TAG_ASSIGNMENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ticket_tag_assignments (
    ticket_thread_id BIGINT NOT NULL,
    tag_id BIGINT NOT NULL,
    assigned_at VARCHAR(64) NOT NULL,
    assigned_by_discord_user_id BIGINT NULL,
    assigned_by_display_name VARCHAR(255) NULL,
    PRIMARY KEY (ticket_thread_id, tag_id),
    INDEX idx_ticket_tag_assignments_tag_id (tag_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""
TAG_ASSIGNMENTS_TABLE_NAME = "ticket_tag_assignments"
THREAD_NOTICE_QUEUE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ticket_thread_notices (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    thread_id BIGINT NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    color INT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    processed_at VARCHAR(64) NULL,
    INDEX idx_ticket_thread_notices_processed_created (processed_at, created_at),
    INDEX idx_ticket_thread_notices_thread (thread_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""
THREAD_NOTICE_QUEUE_TABLE_NAME = "ticket_thread_notices"
THREAD_MEMBER_SYNC_QUEUE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ticket_thread_member_sync (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    thread_id BIGINT NOT NULL,
    discord_user_id BIGINT NOT NULL,
    action VARCHAR(16) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    processed_at VARCHAR(64) NULL,
    INDEX idx_ticket_thread_member_sync_processed_created (processed_at, created_at),
    INDEX idx_ticket_thread_member_sync_thread (thread_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""
THREAD_MEMBER_SYNC_QUEUE_TABLE_NAME = "ticket_thread_member_sync"
TICKET_SCHEMA_UPDATES: tuple[tuple[str, str], ...] = (
    ("assignee_discord_user_id", "ALTER TABLE tickets ADD COLUMN assignee_discord_user_id BIGINT NULL"),
    ("assignee_display_name", "ALTER TABLE tickets ADD COLUMN assignee_display_name VARCHAR(255) NULL"),
    ("assigned_at", "ALTER TABLE tickets ADD COLUMN assigned_at VARCHAR(64) NULL"),
    (
        "assigned_by_discord_user_id",
        "ALTER TABLE tickets ADD COLUMN assigned_by_discord_user_id BIGINT NULL",
    ),
    (
        "assigned_by_display_name",
        "ALTER TABLE tickets ADD COLUMN assigned_by_display_name VARCHAR(255) NULL",
    ),
)


def _clean_tag_name(value: str) -> str:
    return " ".join(str(value).strip().split())


def _tag_key(value: str) -> str:
    return _clean_tag_name(value).casefold()


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _counter_rows(counter: Counter[str], *, limit: int = 10) -> list[dict[str, Any]]:
    return [{"label": label, "count": count} for label, count in counter.most_common(limit)]


def _in_range(value: datetime | None, start_at: datetime | None, end_at: datetime | None) -> bool:
    if value is None:
        return False
    if start_at is not None and value < start_at:
        return False
    if end_at is not None and value > end_at:
        return False
    return True


def _build_trend_points(
    created_times: list[datetime],
    *,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    created_counter: Counter[str] = Counter()
    span_days = max(1, (end_at.date() - start_at.date()).days + 1)

    if span_days <= 45:
        current = start_at.date()
        end_date = end_at.date()
        while current <= end_date:
            created_counter[current.isoformat()] = 0
            current += timedelta(days=1)
        for created_at in created_times:
            created_counter[created_at.date().isoformat()] += 1
        points = [
            {
                "label": datetime.fromisoformat(day).strftime("%d %b"),
                "count": count,
            }
            for day, count in sorted(created_counter.items())
        ]
    elif span_days <= 180:
        bucket_start = start_at.date()
        end_date = end_at.date()
        while bucket_start <= end_date:
            created_counter[bucket_start.isoformat()] = 0
            bucket_start += timedelta(days=7)
        for created_at in created_times:
            delta_days = (created_at.date() - start_at.date()).days
            bucket_start = start_at.date() + timedelta(days=(delta_days // 7) * 7)
            created_counter[bucket_start.isoformat()] += 1
        points = [
            {
                "label": f"Week of {datetime.fromisoformat(day).strftime('%d %b')}",
                "count": count,
            }
            for day, count in sorted(created_counter.items())
        ]
    else:
        month_cursor = start_at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_month = end_at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while month_cursor <= end_month:
            created_counter[month_cursor.strftime("%Y-%m")] = 0
            if month_cursor.month == 12:
                month_cursor = month_cursor.replace(year=month_cursor.year + 1, month=1)
            else:
                month_cursor = month_cursor.replace(month=month_cursor.month + 1)
        for created_at in created_times:
            created_counter[created_at.strftime("%Y-%m")] += 1
        points = [
            {
                "label": datetime.strptime(month, "%Y-%m").strftime("%b %Y"),
                "count": count,
            }
            for month, count in sorted(created_counter.items())
        ]

    max_count = max((point["count"] for point in points), default=0)
    for point in points:
        if point["count"] == 0 or max_count == 0:
            point["width_pct"] = 0
        else:
            point["width_pct"] = max(8, round((point["count"] / max_count) * 100, 1))
    return points


class TicketDatabase:
    def __init__(self, settings: BotSettings):
        self.settings = settings
        self.pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        if self.pool is not None:
            return
        self.pool = await aiomysql.create_pool(
            host=self.settings.db_host,
            port=self.settings.db_port,
            user=self.settings.db_user,
            password=self.settings.db_password,
            db=self.settings.db_name,
            minsize=self.settings.db_minsize,
            maxsize=self.settings.db_maxsize,
            charset=self.settings.db_charset,
            autocommit=True,
        )
        if not await self._table_exists(APP_SETTINGS_TABLE_NAME):
            await self.execute(APP_SETTINGS_TABLE_SQL)
        if not await self._table_exists(AUDIT_LOG_TABLE_NAME):
            await self.execute(AUDIT_LOG_TABLE_SQL)
        if not await self._table_exists(INTERNAL_NOTES_TABLE_NAME):
            await self.execute(INTERNAL_NOTES_TABLE_SQL)
        if not await self._table_exists(TAG_DEFINITIONS_TABLE_NAME):
            await self.execute(TAG_DEFINITIONS_TABLE_SQL)
        if not await self._table_exists(TAG_ASSIGNMENTS_TABLE_NAME):
            await self.execute(TAG_ASSIGNMENTS_TABLE_SQL)
        if not await self._table_exists(THREAD_NOTICE_QUEUE_TABLE_NAME):
            await self.execute(THREAD_NOTICE_QUEUE_TABLE_SQL)
        if not await self._table_exists(THREAD_MEMBER_SYNC_QUEUE_TABLE_NAME):
            await self.execute(THREAD_MEMBER_SYNC_QUEUE_TABLE_SQL)
        await self._ensure_ticket_schema_updates()

    async def close(self) -> None:
        if self.pool is not None:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> int:
        assert self.pool is not None, "Database pool is not initialized"
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                return cur.rowcount

    async def fetchone(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        assert self.pool is not None, "Database pool is not initialized"
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                row = await cur.fetchone()
                return dict(row) if row else None

    async def fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        assert self.pool is not None, "Database pool is not initialized"
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
                return [dict(row) for row in rows]

    async def _table_exists(self, table_name: str) -> bool:
        row = await self.fetchone(
            """
            SELECT 1 AS present
            FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
            LIMIT 1
            """,
            (self.settings.db_name, table_name),
        )
        return row is not None

    async def _column_exists(self, table_name: str, column_name: str) -> bool:
        row = await self.fetchone(
            """
            SELECT 1 AS present
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s AND column_name = %s
            LIMIT 1
            """,
            (self.settings.db_name, table_name, column_name),
        )
        return row is not None

    async def _ensure_ticket_schema_updates(self) -> None:
        for column_name, alter_sql in TICKET_SCHEMA_UPDATES:
            if not await self._column_exists("tickets", column_name):
                await self.execute(alter_sql)

    async def create_ticket(
        self,
        *,
        thread_id: int,
        guild_id: int,
        opener_id: int,
        opener_name: str,
        server_label: str,
        target_channel_id: int,
        seed_message_id: int,
        created_at: str,
    ) -> None:
        await self.execute(
            """
            INSERT INTO tickets (
                thread_id, guild_id, opener_id, opener_name, server_label,
                target_channel_id, seed_message_id, created_at, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open')
            ON DUPLICATE KEY UPDATE
                guild_id = VALUES(guild_id),
                opener_id = VALUES(opener_id),
                opener_name = VALUES(opener_name),
                server_label = VALUES(server_label),
                target_channel_id = VALUES(target_channel_id),
                seed_message_id = VALUES(seed_message_id),
                created_at = VALUES(created_at),
                status = 'open'
            """,
            (
                thread_id,
                guild_id,
                opener_id,
                opener_name,
                server_label,
                target_channel_id,
                seed_message_id,
                created_at,
            ),
        )

    async def get_ticket(self, thread_id: int) -> dict[str, Any] | None:
        return await self.fetchone("SELECT * FROM tickets WHERE thread_id = %s", (thread_id,))

    async def get_open_ticket_for_user(self, opener_id: int, server_label: str) -> dict[str, Any] | None:
        return await self.fetchone(
            "SELECT * FROM tickets WHERE opener_id = %s AND server_label = %s AND status = 'open' LIMIT 1",
            (opener_id, server_label),
        )

    async def close_ticket(
        self,
        *,
        thread_id: int,
        closed_at: str,
        closed_by_id: int,
        closed_by_name: str,
        log_message_id: int | None = None,
        transcript_message_url: str | None = None,
    ) -> None:
        await self.execute(
            """
            UPDATE tickets
            SET status = 'closed',
                closed_at = %s,
                closed_by_id = %s,
                closed_by_name = %s,
                log_message_id = COALESCE(%s, log_message_id),
                transcript_message_url = COALESCE(%s, transcript_message_url)
            WHERE thread_id = %s
            """,
            (closed_at, closed_by_id, closed_by_name, log_message_id, transcript_message_url, thread_id),
        )

    async def reopen_ticket(self, *, thread_id: int, reopened_at: str, reopened_by_id: int, reopened_by_name: str) -> None:
        await self.execute(
            """
            UPDATE tickets
            SET status = 'open',
                closed_at = NULL,
                closed_by_id = NULL,
                closed_by_name = NULL,
                reopened_at = %s,
                reopened_by_id = %s,
                reopened_by_name = %s
            WHERE thread_id = %s
            """,
            (reopened_at, reopened_by_id, reopened_by_name, thread_id),
        )

    async def mark_deleted(self, *, thread_id: int, deleted_at: str, deleted_by_id: int | None, deleted_by_name: str | None) -> None:
        await self.execute(
            """
            UPDATE tickets
            SET status = 'deleted',
                deleted_at = %s,
                deleted_by_id = %s,
                deleted_by_name = %s
            WHERE thread_id = %s
            """,
            (deleted_at, deleted_by_id, deleted_by_name, thread_id),
        )

    async def set_log_message_id(self, thread_id: int, log_message_id: int) -> None:
        await self.execute(
            "UPDATE tickets SET log_message_id = %s WHERE thread_id = %s",
            (log_message_id, thread_id),
        )

    async def assign_ticket(
        self,
        *,
        thread_id: int,
        assignee_discord_user_id: int,
        assignee_display_name: str,
        assigned_at: str,
        assigned_by_discord_user_id: int,
        assigned_by_display_name: str,
    ) -> None:
        await self.execute(
            """
            UPDATE tickets
            SET assignee_discord_user_id = %s,
                assignee_display_name = %s,
                assigned_at = %s,
                assigned_by_discord_user_id = %s,
                assigned_by_display_name = %s
            WHERE thread_id = %s
            """,
            (
                assignee_discord_user_id,
                assignee_display_name,
                assigned_at,
                assigned_by_discord_user_id,
                assigned_by_display_name,
                thread_id,
            ),
        )

    async def clear_ticket_assignee(self, *, thread_id: int) -> None:
        await self.execute(
            """
            UPDATE tickets
            SET assignee_discord_user_id = NULL,
                assignee_display_name = NULL,
                assigned_at = NULL,
                assigned_by_discord_user_id = NULL,
                assigned_by_display_name = NULL
            WHERE thread_id = %s
            """,
            (thread_id,),
        )

    async def add_audit_event(
        self,
        *,
        event_type: str,
        actor_discord_user_id: int,
        actor_username: str,
        actor_display_name: str,
        ticket_thread_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: str,
    ) -> None:
        await self.execute(
            """
            INSERT INTO dashboard_audit_log (
                event_type,
                actor_discord_user_id,
                actor_username,
                actor_display_name,
                ticket_thread_id,
                metadata_json,
                created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_type,
                actor_discord_user_id,
                actor_username,
                actor_display_name,
                ticket_thread_id,
                json.dumps(metadata, sort_keys=True) if metadata else None,
                created_at,
            ),
        )

    async def enqueue_thread_notice(
        self,
        *,
        thread_id: int,
        title: str,
        description: str,
        color: int,
        created_at: str,
    ) -> None:
        await self.execute(
            """
            INSERT INTO ticket_thread_notices (
                thread_id,
                title,
                description,
                color,
                created_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (thread_id, title, description, color, created_at),
        )

    async def list_pending_thread_notices(self, *, limit: int = 25) -> list[dict[str, Any]]:
        return await self.fetchall(
            """
            SELECT *
            FROM ticket_thread_notices
            WHERE processed_at IS NULL
            ORDER BY created_at ASC, id ASC
            LIMIT %s
            """,
            (limit,),
        )

    async def mark_thread_notice_processed(self, *, notice_id: int, processed_at: str) -> None:
        await self.execute(
            """
            UPDATE ticket_thread_notices
            SET processed_at = %s
            WHERE id = %s
            """,
            (processed_at, notice_id),
        )

    async def enqueue_thread_member_sync(
        self,
        *,
        thread_id: int,
        discord_user_id: int,
        action: str,
        created_at: str,
    ) -> None:
        await self.execute(
            """
            INSERT INTO ticket_thread_member_sync (
                thread_id,
                discord_user_id,
                action,
                created_at
            ) VALUES (%s, %s, %s, %s)
            """,
            (thread_id, discord_user_id, action, created_at),
        )

    async def list_pending_thread_member_syncs(self, *, limit: int = 25) -> list[dict[str, Any]]:
        return await self.fetchall(
            """
            SELECT *
            FROM ticket_thread_member_sync
            WHERE processed_at IS NULL
            ORDER BY created_at ASC, id ASC
            LIMIT %s
            """,
            (limit,),
        )

    async def mark_thread_member_sync_processed(self, *, sync_id: int, processed_at: str) -> None:
        await self.execute(
            """
            UPDATE ticket_thread_member_sync
            SET processed_at = %s
            WHERE id = %s
            """,
            (processed_at, sync_id),
        )

    async def list_tag_definitions(self) -> list[dict[str, Any]]:
        return await self.fetchall("SELECT * FROM ticket_tags ORDER BY tag_name ASC, id ASC")

    async def get_tag_definition_by_name(self, tag_name: str) -> dict[str, Any] | None:
        key = _tag_key(tag_name)
        if not key:
            return None
        return await self.fetchone("SELECT * FROM ticket_tags WHERE tag_key = %s LIMIT 1", (key,))

    async def create_tag_definition(
        self,
        *,
        tag_name: str,
        created_by_discord_user_id: int | None,
        created_by_display_name: str | None,
        created_at: str,
    ) -> dict[str, Any]:
        clean_name = _clean_tag_name(tag_name)
        key = _tag_key(clean_name)
        await self.execute(
            """
            INSERT INTO ticket_tags (
                tag_key,
                tag_name,
                created_at,
                created_by_discord_user_id,
                created_by_display_name
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (key, clean_name, created_at, created_by_discord_user_id, created_by_display_name),
        )
        created = await self.get_tag_definition_by_name(clean_name)
        assert created is not None
        return created

    async def update_tag_definition(self, *, tag_id: int, tag_name: str) -> dict[str, Any] | None:
        clean_name = _clean_tag_name(tag_name)
        key = _tag_key(clean_name)
        await self.execute(
            """
            UPDATE ticket_tags
            SET tag_key = %s,
                tag_name = %s
            WHERE id = %s
            """,
            (key, clean_name, tag_id),
        )
        return await self.fetchone("SELECT * FROM ticket_tags WHERE id = %s LIMIT 1", (tag_id,))

    async def delete_tag_definition(self, tag_id: int) -> None:
        await self.execute("DELETE FROM ticket_tag_assignments WHERE tag_id = %s", (tag_id,))
        await self.execute("DELETE FROM ticket_tags WHERE id = %s", (tag_id,))

    async def list_ticket_tags(self, thread_id: int) -> list[dict[str, Any]]:
        return await self.fetchall(
            """
            SELECT tt.*, tta.assigned_at, tta.assigned_by_discord_user_id, tta.assigned_by_display_name
            FROM ticket_tag_assignments AS tta
            INNER JOIN ticket_tags AS tt ON tt.id = tta.tag_id
            WHERE tta.ticket_thread_id = %s
            ORDER BY tt.tag_name ASC, tt.id ASC
            """,
            (thread_id,),
        )

    async def add_ticket_tag(
        self,
        *,
        thread_id: int,
        tag_id: int,
        assigned_at: str,
        assigned_by_discord_user_id: int | None,
        assigned_by_display_name: str | None,
    ) -> None:
        await self.execute(
            """
            INSERT INTO ticket_tag_assignments (
                ticket_thread_id,
                tag_id,
                assigned_at,
                assigned_by_discord_user_id,
                assigned_by_display_name
            ) VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                assigned_at = VALUES(assigned_at),
                assigned_by_discord_user_id = VALUES(assigned_by_discord_user_id),
                assigned_by_display_name = VALUES(assigned_by_display_name)
            """,
            (thread_id, tag_id, assigned_at, assigned_by_discord_user_id, assigned_by_display_name),
        )

    async def remove_ticket_tag(self, *, thread_id: int, tag_id: int) -> None:
        await self.execute(
            "DELETE FROM ticket_tag_assignments WHERE ticket_thread_id = %s AND tag_id = %s",
            (thread_id, tag_id),
        )

    async def list_open_tickets(self) -> list[dict[str, Any]]:
        return await self.fetchall("SELECT * FROM tickets WHERE status = 'open'")

    async def list_closed_tickets(self) -> list[dict[str, Any]]:
        return await self.fetchall("SELECT * FROM tickets WHERE status = 'closed'")

    async def list_tickets_with_log_controls(self) -> list[dict[str, Any]]:
        return await self.fetchall(
            "SELECT * FROM tickets WHERE log_message_id IS NOT NULL AND status IN ('open', 'closed')"
        )

    async def get_message_templates(self) -> dict[str, str]:
        templates = DEFAULT_MESSAGE_TEMPLATES.copy()
        rows = await self.fetchall("SELECT setting_key, setting_value FROM app_settings")
        for row in rows:
            key = row["setting_key"]
            if key in templates:
                templates[key] = row["setting_value"]
        return templates

    async def set_message_templates(self, templates: dict[str, str]) -> None:
        for key, value in templates.items():
            await self.execute(
                """
                INSERT INTO app_settings (setting_key, setting_value)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
                """,
                (key, value),
            )


class DashboardDatabase:
    def __init__(self, settings: BotSettings):
        self.settings = settings

    def _connect(self):
        return pymysql.connect(
            host=self.settings.db_host,
            port=self.settings.db_port,
            user=self.settings.db_user,
            password=self.settings.db_password,
            database=self.settings.db_name,
            charset=self.settings.db_charset,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )

    def ensure_app_settings_table(self) -> None:
        if self._table_exists(APP_SETTINGS_TABLE_NAME):
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(APP_SETTINGS_TABLE_SQL)

    def ensure_dashboard_audit_table(self) -> None:
        if self._table_exists(AUDIT_LOG_TABLE_NAME):
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(AUDIT_LOG_TABLE_SQL)

    def ensure_internal_notes_table(self) -> None:
        if self._table_exists(INTERNAL_NOTES_TABLE_NAME):
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(INTERNAL_NOTES_TABLE_SQL)

    def ensure_tag_tables(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                if not self._table_exists(TAG_DEFINITIONS_TABLE_NAME):
                    cur.execute(TAG_DEFINITIONS_TABLE_SQL)
                if not self._table_exists(TAG_ASSIGNMENTS_TABLE_NAME):
                    cur.execute(TAG_ASSIGNMENTS_TABLE_SQL)

    def ensure_thread_notice_queue_table(self) -> None:
        if self._table_exists(THREAD_NOTICE_QUEUE_TABLE_NAME):
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(THREAD_NOTICE_QUEUE_TABLE_SQL)

    def ensure_thread_member_sync_queue_table(self) -> None:
        if self._table_exists(THREAD_MEMBER_SYNC_QUEUE_TABLE_NAME):
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(THREAD_MEMBER_SYNC_QUEUE_TABLE_SQL)

    def _table_exists(self, table_name: str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 AS present
                    FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                    LIMIT 1
                    """,
                    (self.settings.db_name, table_name),
                )
                return cur.fetchone() is not None

    def _column_exists(self, table_name: str, column_name: str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 AS present
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s AND column_name = %s
                    LIMIT 1
                    """,
                    (self.settings.db_name, table_name, column_name),
                )
                return cur.fetchone() is not None

    def ensure_ticket_schema_updates(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                for column_name, alter_sql in TICKET_SCHEMA_UPDATES:
                    cur.execute(
                        """
                        SELECT 1 AS present
                        FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s AND column_name = %s
                        LIMIT 1
                        """,
                        (self.settings.db_name, "tickets", column_name),
                    )
                    if cur.fetchone() is None:
                        cur.execute(alter_sql)

    def _ticket_access_filter_sql(
        self,
        *,
        opener_id: int | None,
        channel_ids: list[int] | None,
        allow_all: bool,
    ) -> tuple[str, list[Any]]:
        if allow_all:
            return "", []

        filters: list[str] = []
        params: list[Any] = []
        if opener_id is not None:
            filters.append("opener_id = %s")
            params.append(opener_id)
        if channel_ids:
            placeholders = ", ".join(["%s"] * len(channel_ids))
            filters.append(f"target_channel_id IN ({placeholders})")
            params.extend(channel_ids)

        if not filters:
            return " WHERE 1 = 0", []
        return " WHERE (" + " OR ".join(filters) + ")", params

    def _attach_ticket_tags(self, tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not tickets:
            return tickets

        self.ensure_tag_tables()
        thread_ids = [ticket["thread_id"] for ticket in tickets if ticket.get("thread_id") is not None]
        if not thread_ids:
            for ticket in tickets:
                ticket["tags"] = []
            return tickets

        placeholders = ", ".join(["%s"] * len(thread_ids))
        tag_rows: list[dict[str, Any]]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        tta.ticket_thread_id,
                        tt.id,
                        tt.tag_name,
                        tt.tag_key,
                        tta.assigned_at,
                        tta.assigned_by_discord_user_id,
                        tta.assigned_by_display_name
                    FROM ticket_tag_assignments AS tta
                    INNER JOIN ticket_tags AS tt ON tt.id = tta.tag_id
                    WHERE tta.ticket_thread_id IN ({placeholders})
                    ORDER BY tt.tag_name ASC, tt.id ASC
                    """,
                    tuple(thread_ids),
                )
                tag_rows = list(cur.fetchall())

        tags_by_ticket: dict[int, list[dict[str, Any]]] = {}
        for row in tag_rows:
            tags_by_ticket.setdefault(row["ticket_thread_id"], []).append(row)
        for ticket in tickets:
            ticket["tags"] = tags_by_ticket.get(ticket["thread_id"], [])
        return tickets

    def get_stats(
        self,
        *,
        opener_id: int | None = None,
        channel_ids: list[int] | None = None,
        allow_all: bool = False,
    ) -> dict[str, int]:
        access_sql, access_params = self._ticket_access_filter_sql(
            opener_id=opener_id,
            channel_ids=channel_ids,
            allow_all=allow_all,
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS c FROM tickets{access_sql}", tuple(access_params))
                total = cur.fetchone()["c"]
                status_prefix = " AND" if access_sql else " WHERE"
                cur.execute(
                    f"SELECT COUNT(*) AS c FROM tickets{access_sql}{status_prefix} status = 'open'",
                    tuple(access_params),
                )
                open_count = cur.fetchone()["c"]
                cur.execute(
                    f"SELECT COUNT(*) AS c FROM tickets{access_sql}{status_prefix} status = 'closed'",
                    tuple(access_params),
                )
                closed_count = cur.fetchone()["c"]
                cur.execute(
                    f"SELECT COUNT(*) AS c FROM tickets{access_sql}{status_prefix} status = 'deleted'",
                    tuple(access_params),
                )
                deleted_count = cur.fetchone()["c"]
        return {
            "total": total,
            "open": open_count,
            "closed": closed_count,
            "deleted": deleted_count,
        }

    def list_tickets(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        opener_id: int | None = None,
        channel_ids: list[int] | None = None,
        allow_all: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM tickets"
        params: list[Any] = []
        access_sql, access_params = self._ticket_access_filter_sql(
            opener_id=opener_id,
            channel_ids=channel_ids,
            allow_all=allow_all,
        )
        query += access_sql
        params.extend(access_params)
        if status in {"open", "closed", "deleted"}:
            query += " AND status = %s" if access_sql else " WHERE status = %s"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = list(cur.fetchall())
        return self._attach_ticket_tags(rows)

    def get_ticket(
        self,
        thread_id: int,
        *,
        opener_id: int | None = None,
        channel_ids: list[int] | None = None,
        allow_all: bool = False,
    ) -> dict[str, Any] | None:
        access_sql, access_params = self._ticket_access_filter_sql(
            opener_id=opener_id,
            channel_ids=channel_ids,
            allow_all=allow_all,
        )
        query = "SELECT * FROM tickets WHERE thread_id = %s"
        params: list[Any] = [thread_id]
        if access_sql:
            query += " AND " + access_sql.removeprefix(" WHERE ")
            params.extend(access_params)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                row = cur.fetchone()
        if row is None:
            return None
        self._attach_ticket_tags([row])
        return row

    def get_message_templates(self) -> dict[str, str]:
        templates = DEFAULT_MESSAGE_TEMPLATES.copy()
        self.ensure_app_settings_table()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT setting_key, setting_value FROM app_settings")
                for row in cur.fetchall():
                    key = row["setting_key"]
                    if key in templates:
                        templates[key] = row["setting_value"]
        return templates

    def set_message_templates(self, templates: dict[str, str]) -> None:
        self.ensure_app_settings_table()
        with self._connect() as conn:
            with conn.cursor() as cur:
                for key, value in templates.items():
                    cur.execute(
                        """
                        INSERT INTO app_settings (setting_key, setting_value)
                        VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
                        """,
                        (key, value),
                    )

    def assign_ticket(
        self,
        *,
        thread_id: int,
        assignee_discord_user_id: int,
        assignee_display_name: str,
        assigned_at: str,
        assigned_by_discord_user_id: int,
        assigned_by_display_name: str,
    ) -> None:
        self.ensure_ticket_schema_updates()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tickets
                    SET assignee_discord_user_id = %s,
                        assignee_display_name = %s,
                        assigned_at = %s,
                        assigned_by_discord_user_id = %s,
                        assigned_by_display_name = %s
                    WHERE thread_id = %s
                    """,
                    (
                        assignee_discord_user_id,
                        assignee_display_name,
                        assigned_at,
                        assigned_by_discord_user_id,
                        assigned_by_display_name,
                        thread_id,
                    ),
                )

    def clear_ticket_assignee(self, *, thread_id: int) -> None:
        self.ensure_ticket_schema_updates()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tickets
                    SET assignee_discord_user_id = NULL,
                        assignee_display_name = NULL,
                        assigned_at = NULL,
                        assigned_by_discord_user_id = NULL,
                        assigned_by_display_name = NULL
                    WHERE thread_id = %s
                    """,
                    (thread_id,),
                )

    def remove_ticket_record(self, *, thread_id: int) -> None:
        self.ensure_internal_notes_table()
        self.ensure_tag_tables()
        self.ensure_thread_notice_queue_table()
        self.ensure_thread_member_sync_queue_table()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ticket_internal_notes WHERE thread_id = %s", (thread_id,))
                cur.execute("DELETE FROM ticket_tag_assignments WHERE ticket_thread_id = %s", (thread_id,))
                cur.execute("DELETE FROM ticket_thread_notices WHERE thread_id = %s", (thread_id,))
                cur.execute("DELETE FROM ticket_thread_member_sync WHERE thread_id = %s", (thread_id,))
                cur.execute("DELETE FROM tickets WHERE thread_id = %s", (thread_id,))

    def list_ticket_notes(self, thread_id: int) -> list[dict[str, Any]]:
        self.ensure_internal_notes_table()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM ticket_internal_notes
                    WHERE thread_id = %s
                    ORDER BY created_at ASC, id ASC
                    """,
                    (thread_id,),
                )
                return list(cur.fetchall())

    def add_ticket_note(
        self,
        *,
        thread_id: int,
        author_discord_user_id: int,
        author_display_name: str,
        note_text: str,
        created_at: str,
    ) -> None:
        self.ensure_internal_notes_table()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ticket_internal_notes (
                        thread_id,
                        author_discord_user_id,
                        author_display_name,
                        note_text,
                        created_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        thread_id,
                        author_discord_user_id,
                        author_display_name,
                        note_text,
                        created_at,
                    ),
                )

    def list_tag_definitions(self) -> list[dict[str, Any]]:
        self.ensure_tag_tables()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM ticket_tags ORDER BY tag_name ASC, id ASC")
                return list(cur.fetchall())

    def get_tag_definition_by_name(self, tag_name: str) -> dict[str, Any] | None:
        self.ensure_tag_tables()
        key = _tag_key(tag_name)
        if not key:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM ticket_tags WHERE tag_key = %s LIMIT 1", (key,))
                return cur.fetchone()

    def create_tag_definition(
        self,
        *,
        tag_name: str,
        created_by_discord_user_id: int | None,
        created_by_display_name: str | None,
        created_at: str,
    ) -> dict[str, Any]:
        self.ensure_tag_tables()
        clean_name = _clean_tag_name(tag_name)
        key = _tag_key(clean_name)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ticket_tags (
                        tag_key,
                        tag_name,
                        created_at,
                        created_by_discord_user_id,
                        created_by_display_name
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (key, clean_name, created_at, created_by_discord_user_id, created_by_display_name),
                )
        created = self.get_tag_definition_by_name(clean_name)
        assert created is not None
        return created

    def update_tag_definition(self, *, tag_id: int, tag_name: str) -> dict[str, Any] | None:
        self.ensure_tag_tables()
        clean_name = _clean_tag_name(tag_name)
        key = _tag_key(clean_name)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ticket_tags
                    SET tag_key = %s,
                        tag_name = %s
                    WHERE id = %s
                    """,
                    (key, clean_name, tag_id),
                )
                cur.execute("SELECT * FROM ticket_tags WHERE id = %s LIMIT 1", (tag_id,))
                return cur.fetchone()

    def delete_tag_definition(self, tag_id: int) -> None:
        self.ensure_tag_tables()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ticket_tag_assignments WHERE tag_id = %s", (tag_id,))
                cur.execute("DELETE FROM ticket_tags WHERE id = %s", (tag_id,))

    def list_ticket_tags(self, thread_id: int) -> list[dict[str, Any]]:
        self.ensure_tag_tables()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT tt.*, tta.assigned_at, tta.assigned_by_discord_user_id, tta.assigned_by_display_name
                    FROM ticket_tag_assignments AS tta
                    INNER JOIN ticket_tags AS tt ON tt.id = tta.tag_id
                    WHERE tta.ticket_thread_id = %s
                    ORDER BY tt.tag_name ASC, tt.id ASC
                    """,
                    (thread_id,),
                )
                return list(cur.fetchall())

    def add_ticket_tag(
        self,
        *,
        thread_id: int,
        tag_id: int,
        assigned_at: str,
        assigned_by_discord_user_id: int | None,
        assigned_by_display_name: str | None,
    ) -> None:
        self.ensure_tag_tables()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ticket_tag_assignments (
                        ticket_thread_id,
                        tag_id,
                        assigned_at,
                        assigned_by_discord_user_id,
                        assigned_by_display_name
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        assigned_at = VALUES(assigned_at),
                        assigned_by_discord_user_id = VALUES(assigned_by_discord_user_id),
                        assigned_by_display_name = VALUES(assigned_by_display_name)
                    """,
                    (thread_id, tag_id, assigned_at, assigned_by_discord_user_id, assigned_by_display_name),
                )

    def remove_ticket_tag(self, *, thread_id: int, tag_id: int) -> None:
        self.ensure_tag_tables()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM ticket_tag_assignments WHERE ticket_thread_id = %s AND tag_id = %s",
                    (thread_id, tag_id),
                )

    def add_audit_event(
        self,
        *,
        event_type: str,
        actor_discord_user_id: int,
        actor_username: str,
        actor_display_name: str,
        ticket_thread_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: str,
    ) -> None:
        self.ensure_dashboard_audit_table()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO dashboard_audit_log (
                        event_type,
                        actor_discord_user_id,
                        actor_username,
                        actor_display_name,
                        ticket_thread_id,
                        metadata_json,
                        created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event_type,
                        actor_discord_user_id,
                        actor_username,
                        actor_display_name,
                        ticket_thread_id,
                        json.dumps(metadata, sort_keys=True) if metadata else None,
                        created_at,
                    ),
                )

    def enqueue_thread_notice(
        self,
        *,
        thread_id: int,
        title: str,
        description: str,
        color: int,
        created_at: str,
    ) -> None:
        self.ensure_thread_notice_queue_table()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ticket_thread_notices (
                        thread_id,
                        title,
                        description,
                        color,
                        created_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (thread_id, title, description, color, created_at),
                )

    def enqueue_thread_member_sync(
        self,
        *,
        thread_id: int,
        discord_user_id: int,
        action: str,
        created_at: str,
    ) -> None:
        self.ensure_thread_member_sync_queue_table()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ticket_thread_member_sync (
                        thread_id,
                        discord_user_id,
                        action,
                        created_at
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (thread_id, discord_user_id, action, created_at),
                )

    def count_audit_events(self) -> int:
        self.ensure_dashboard_audit_table()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM dashboard_audit_log")
                row = cur.fetchone()
        return int(row["c"]) if row else 0

    def list_audit_events(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        self.ensure_dashboard_audit_table()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM dashboard_audit_log
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
                rows = list(cur.fetchall())

        for row in rows:
            metadata_json = row.get("metadata_json")
            if metadata_json:
                try:
                    row["metadata"] = json.loads(metadata_json)
                except json.JSONDecodeError:
                    row["metadata"] = {"raw": metadata_json}
            else:
                row["metadata"] = {}
        return rows

    def get_ticket_analytics(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM tickets ORDER BY created_at DESC")
                tickets = list(cur.fetchall())

        now = datetime.now(timezone.utc)
        opened_count = 0
        closed_count = 0
        reopened_count = 0
        deleted_count = 0
        total_close_seconds = 0.0
        closed_ticket_count = 0

        tickets_by_server: Counter[str] = Counter()
        top_openers: Counter[str] = Counter()
        top_closers: Counter[str] = Counter()
        top_reopeners: Counter[str] = Counter()
        top_deleters: Counter[str] = Counter()
        oldest_open: list[dict[str, Any]] = []
        created_times: list[datetime] = []
        reopened_times: list[datetime] = []
        deleted_times: list[datetime] = []

        for ticket in tickets:
            opener_name = ticket.get("opener_name") or str(ticket.get("opener_id") or "Unknown user")
            server_label = ticket.get("server_label") or "Unknown server"
            created_at = _parse_iso_datetime(ticket.get("created_at"))
            closed_at = _parse_iso_datetime(ticket.get("closed_at"))
            reopened_at = _parse_iso_datetime(ticket.get("reopened_at"))
            deleted_at = _parse_iso_datetime(ticket.get("deleted_at"))

            if _in_range(created_at, start_at, end_at):
                opened_count += 1
                top_openers[opener_name] += 1
                tickets_by_server[server_label] += 1
                if created_at is not None:
                    created_times.append(created_at)
                    if ticket.get("status") == "open":
                        oldest_open.append(
                            {
                                "thread_id": ticket["thread_id"],
                                "server_label": server_label,
                                "opener_name": opener_name,
                                "created_at": ticket.get("created_at"),
                                "age_hours": round((now - created_at).total_seconds() / 3600, 1),
                            }
                        )

            if _in_range(closed_at, start_at, end_at):
                closed_count += 1
                if created_at is not None and closed_at is not None and closed_at >= created_at:
                    total_close_seconds += (closed_at - created_at).total_seconds()
                    closed_ticket_count += 1
                closed_by_name = ticket.get("closed_by_name")
                if closed_by_name:
                    top_closers[closed_by_name] += 1

            if _in_range(reopened_at, start_at, end_at):
                reopened_count += 1
                if reopened_at is not None:
                    reopened_times.append(reopened_at)
                reopened_by_name = ticket.get("reopened_by_name")
                if reopened_by_name:
                    top_reopeners[reopened_by_name] += 1

            if _in_range(deleted_at, start_at, end_at):
                deleted_count += 1
                if deleted_at is not None:
                    deleted_times.append(deleted_at)
                deleted_by_name = ticket.get("deleted_by_name")
                if deleted_by_name:
                    top_deleters[deleted_by_name] += 1

        by_server = _counter_rows(tickets_by_server)
        max_server_count = max((row["count"] for row in by_server), default=0)
        for row in by_server:
            row["width_pct"] = 0 if max_server_count == 0 else max(8, round((row["count"] / max_server_count) * 100, 1))

        oldest_open.sort(key=lambda row: row["age_hours"], reverse=True)
        average_close_hours = None
        if closed_ticket_count:
            average_close_hours = round((total_close_seconds / closed_ticket_count) / 3600, 1)

        activity_times = [*created_times, *reopened_times, *deleted_times]
        if activity_times:
            trend_start = start_at or min(activity_times)
            trend_end = end_at or max(activity_times)
        else:
            trend_end = end_at or now
            trend_start = start_at or (trend_end - timedelta(days=29))
        opened_trend_points = _build_trend_points(created_times, start_at=trend_start, end_at=trend_end)
        reopened_trend_points = _build_trend_points(reopened_times, start_at=trend_start, end_at=trend_end)
        deleted_trend_points = _build_trend_points(deleted_times, start_at=trend_start, end_at=trend_end)

        return {
            "opened_count": opened_count,
            "closed_count": closed_count,
            "reopened_count": reopened_count,
            "deleted_count": deleted_count,
            "open_count": len(oldest_open),
            "average_close_hours": average_close_hours,
            "opened_trend_points": opened_trend_points,
            "reopened_trend_points": reopened_trend_points,
            "deleted_trend_points": deleted_trend_points,
            "by_server": by_server,
            "top_openers": _counter_rows(top_openers),
            "top_closers": _counter_rows(top_closers),
            "top_reopeners": _counter_rows(top_reopeners),
            "top_deleters": _counter_rows(top_deleters),
            "oldest_open": oldest_open[:10],
        }
