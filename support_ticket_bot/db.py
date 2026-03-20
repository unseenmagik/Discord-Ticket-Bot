from __future__ import annotations

from typing import Any

import aiomysql
import pymysql

from .config import BotSettings


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
