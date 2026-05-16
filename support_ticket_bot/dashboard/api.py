from __future__ import annotations

import hmac
import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from support_ticket_bot.config import BotSettings
from support_ticket_bot.db import DashboardDatabase
from support_ticket_bot.utils import utc_now_iso


log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["external-api"])


class CreateTicketRequest(BaseModel):
    opener_discord_user_id: int
    opener_display_name: str | None = Field(default=None, max_length=255)
    server_label: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=8000)
    idempotency_key: str | None = Field(default=None, max_length=120)
    caller_label: str | None = Field(default=None, max_length=64)

    @field_validator("opener_discord_user_id", mode="before")
    @classmethod
    def _coerce_discord_id(cls, value: object) -> int:
        if not isinstance(value, str) or not value.isdigit():
            raise ValueError(
                "opener_discord_user_id must be sent as a JSON string of digits "
                "(Discord snowflakes exceed JavaScript's safe integer range)."
            )
        return int(value)


def require_api_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    settings: BotSettings = request.app.state.settings
    expected = settings.api_token or ""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="External API token is not configured.",
        )
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
        )


def _request_to_payload(row: dict) -> dict:
    thread_id = row.get("created_thread_id")
    return {
        "request_id": row["request_id"],
        "status": row["status"],
        "created_thread_id": str(thread_id) if thread_id is not None else None,
        "error_message": row.get("error_message"),
        "server_label": row.get("server_label"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@router.post("/tickets", dependencies=[Depends(require_api_token)])
async def create_ticket(request: Request, payload: CreateTicketRequest):
    settings: BotSettings = request.app.state.settings
    db: DashboardDatabase = request.app.state.db

    if payload.server_label not in settings.server_targets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown server_label '{payload.server_label}'.",
        )

    caller_label = (
        (payload.caller_label or settings.api_default_caller_label or "external").strip()
        or "external"
    )

    if payload.idempotency_key:
        existing = db.find_external_ticket_request_by_idempotency(
            external_caller=caller_label,
            idempotency_key=payload.idempotency_key,
        )
        if existing is not None:
            return JSONResponse(
                _request_to_payload(existing),
                status_code=status.HTTP_200_OK,
            )

    request_id = str(uuid.uuid4())
    now = utc_now_iso()
    db.insert_external_ticket_request(
        request_id=request_id,
        idempotency_key=payload.idempotency_key,
        external_caller=caller_label,
        server_label=payload.server_label,
        opener_id=payload.opener_discord_user_id,
        opener_name=payload.opener_display_name,
        title=payload.title,
        body=payload.body,
        created_at=now,
    )

    from .app import _log_external_audit_event

    _log_external_audit_event(
        request,
        actor_discord_user_id=payload.opener_discord_user_id,
        actor_display_name=payload.opener_display_name or caller_label,
        event_type="external_ticket_request_queued",
        metadata={
            "source": "external_api",
            "external_caller": caller_label,
            "request_id": request_id,
            "server_label": payload.server_label,
        },
    )

    log.info(
        "External ticket request queued request_id=%s caller=%s opener_id=%s server_label=%s",
        request_id,
        caller_label,
        payload.opener_discord_user_id,
        payload.server_label,
    )

    return JSONResponse(
        {"request_id": request_id, "status": "queued"},
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.get("/tickets/requests/{request_id}", dependencies=[Depends(require_api_token)])
async def get_ticket_request(request: Request, request_id: str):
    db: DashboardDatabase = request.app.state.db
    row = db.get_external_ticket_request(request_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown request_id.",
        )
    return _request_to_payload(row)
