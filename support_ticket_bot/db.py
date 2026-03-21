from __future__ import annotations

from collections import Counter
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
        await self.execute(APP_SETTINGS_TABLE_SQL)

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
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(APP_SETTINGS_TABLE_SQL)

    def get_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM tickets")
                total = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM tickets WHERE status = 'open'")
                open_count = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM tickets WHERE status = 'closed'")
                closed_count = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM tickets WHERE status = 'deleted'")
                deleted_count = cur.fetchone()["c"]
        return {
            "total": total,
            "open": open_count,
            "closed": closed_count,
            "deleted": deleted_count,
        }

    def list_tickets(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM tickets"
        params: list[Any] = []
        if status in {"open", "closed", "deleted"}:
            query += " WHERE status = %s"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                return list(cur.fetchall())

    def get_ticket(self, thread_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM tickets WHERE thread_id = %s", (thread_id,))
                return cur.fetchone()

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
        oldest_open: list[dict[str, Any]] = []
        created_times: list[datetime] = []

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

            if _in_range(deleted_at, start_at, end_at):
                deleted_count += 1

        by_server = _counter_rows(tickets_by_server)
        max_server_count = max((row["count"] for row in by_server), default=0)
        for row in by_server:
            row["width_pct"] = 0 if max_server_count == 0 else max(8, round((row["count"] / max_server_count) * 100, 1))

        oldest_open.sort(key=lambda row: row["age_hours"], reverse=True)
        average_close_hours = None
        if closed_ticket_count:
            average_close_hours = round((total_close_seconds / closed_ticket_count) / 3600, 1)

        if created_times:
            trend_start = start_at or min(created_times)
            trend_end = end_at or max(created_times)
        else:
            trend_end = end_at or now
            trend_start = start_at or (trend_end - timedelta(days=29))
        trend_points = _build_trend_points(created_times, start_at=trend_start, end_at=trend_end)

        return {
            "opened_count": opened_count,
            "closed_count": closed_count,
            "reopened_count": reopened_count,
            "deleted_count": deleted_count,
            "open_count": len(oldest_open),
            "average_close_hours": average_close_hours,
            "trend_points": trend_points,
            "by_server": by_server,
            "top_openers": _counter_rows(top_openers),
            "top_closers": _counter_rows(top_closers),
            "oldest_open": oldest_open[:10],
        }
