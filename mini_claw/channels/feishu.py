"""Feishu (Lark) channel implementation using REST API via httpx."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mini_claw.channels.base import Channel, InboundMessage

logger = logging.getLogger(__name__)

FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
TOKEN_URL = f"{FEISHU_BASE_URL}/auth/v3/tenant_access_token/internal"
SEND_MSG_URL = f"{FEISHU_BASE_URL}/im/v1/messages"


class FeishuChannel(Channel):
    """Feishu messaging channel using REST API directly."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        verification_token: str,
        encrypt_key: str = "",
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key

        self._tenant_token: str | None = None
        self._token_expires_at: float = 0
        self._http: httpx.AsyncClient = httpx.AsyncClient(timeout=10)

        # Callback for inbound messages
        self.on_message: Callable[
            [InboundMessage], Coroutine[Any, Any, None]
        ] | None = None

        # Callback for card actions (approval flow)
        self.on_card_action: Callable[
            [dict], Coroutine[Any, Any, None]
        ] | None = None

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _get_tenant_token(self) -> str:
        """Get or refresh the tenant access token."""
        if self._tenant_token and time.time() < self._token_expires_at:
            return self._tenant_token

        resp = await self._http.post(
            TOKEN_URL,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Failed to get tenant token: {data}")

        self._tenant_token = data["tenant_access_token"]
        # Expire 5 minutes early to be safe
        self._token_expires_at = time.time() + data.get("expire", 7200) - 300
        return self._tenant_token

    async def _auth_headers(self) -> dict[str, str]:
        token = await self._get_tenant_token()
        return {"Authorization": f"Bearer {token}"}

    # ------------------------------------------------------------------
    # Channel interface
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, text: str) -> None:
        """Send a text message to a Feishu chat."""
        headers = await self._auth_headers()
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        resp = await self._http.post(
            SEND_MSG_URL,
            params={"receive_id_type": "chat_id"},
            headers=headers,
            json=payload,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("Failed to send message: %s", data)

    async def send_approval_card(
        self,
        chat_id: str,
        approval_id: str,
        tool_name: str,
        tool_args: dict,
        level: str,
    ) -> None:
        """Send an interactive card with approve/reject buttons."""
        card = self._build_approval_card(approval_id, tool_name, tool_args, level)
        headers = await self._auth_headers()
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card),
        }
        resp = await self._http.post(
            SEND_MSG_URL,
            params={"receive_id_type": "chat_id"},
            headers=headers,
            json=payload,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("Failed to send approval card: %s", data)

    # ------------------------------------------------------------------
    # Card builder
    # ------------------------------------------------------------------

    def _build_approval_card(
        self, approval_id: str, tool_name: str, tool_args: dict, level: str
    ) -> dict:
        """Build a Feishu interactive card for tool approval."""
        args_display = json.dumps(tool_args, indent=2, ensure_ascii=False)
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"Tool Approval [{level}]"},
                "template": "orange" if level == "high" else "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**Tool:** {tool_name}\n**Args:**\n```\n{args_display}\n```",
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "Approve"},
                            "type": "primary",
                            "value": json.dumps(
                                {"action": "approve", "approval_id": approval_id}
                            ),
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "Reject"},
                            "type": "danger",
                            "value": json.dumps(
                                {"action": "reject", "approval_id": approval_id}
                            ),
                        },
                    ],
                },
            ],
        }

    # ------------------------------------------------------------------
    # Webhook router
    # ------------------------------------------------------------------

    def create_webhook_router(self) -> APIRouter:
        """Create a FastAPI router for Feishu webhook endpoints."""
        router = APIRouter()

        @router.post("/feishu/webhook")
        async def feishu_webhook(request: Request) -> JSONResponse:
            body = await request.json()

            # Handle url_verification challenge
            if body.get("type") == "url_verification":
                return JSONResponse({"challenge": body.get("challenge", "")})

            # Handle event callback
            header = body.get("header", {})
            event_id = header.get("event_id", "")
            event_type = header.get("event_type", "")

            # Deduplicate: check if already processed
            if event_id and await self._is_event_processed(event_id):
                return JSONResponse({"code": 0, "msg": "duplicate"})

            # Return 200 immediately, process async
            if event_type == "im.message.receive_v1":
                asyncio.create_task(self._handle_message_event(body, event_id))

            return JSONResponse({"code": 0, "msg": "ok"})

        @router.post("/feishu/card_action")
        async def feishu_card_action(request: Request) -> JSONResponse:
            body = await request.json()

            # Process card action asynchronously
            asyncio.create_task(self._handle_card_action(body))

            return JSONResponse({"code": 0})

        return router

    # ------------------------------------------------------------------
    # Event handling internals
    # ------------------------------------------------------------------

    async def _is_event_processed(self, event_id: str) -> bool:
        """Check processed_events table for deduplication.

        Uses a simple in-memory set as fallback. In production, this should
        query the processed_events database table.
        """
        if not hasattr(self, "_processed_events"):
            self._processed_events: set[str] = set()
        return event_id in self._processed_events

    async def _mark_event_processed(self, event_id: str) -> None:
        """Mark an event as processed."""
        if not hasattr(self, "_processed_events"):
            self._processed_events: set[str] = set()
        self._processed_events.add(event_id)

    async def _handle_message_event(self, body: dict, event_id: str) -> None:
        """Process an inbound message event."""
        try:
            event = body.get("event", {})
            message = event.get("message", {})
            sender = event.get("sender", {}).get("sender_id", {})

            # Parse message content
            content_str = message.get("content", "{}")
            content = json.loads(content_str)
            text = content.get("text", "")

            msg = InboundMessage(
                chat_id=message.get("chat_id", ""),
                sender_id=sender.get("open_id", ""),
                text=text,
                event_id=event_id,
                timestamp=int(message.get("create_time", "0")),
            )

            await self._mark_event_processed(event_id)

            if self.on_message:
                await self.on_message(msg)
        except Exception:
            logger.exception("Error handling message event %s", event_id)

    async def _handle_card_action(self, body: dict) -> None:
        """Process a card button click (approval flow)."""
        try:
            action = body.get("action", {})
            value_str = action.get("value", "{}")
            if isinstance(value_str, str):
                value = json.loads(value_str)
            else:
                value = value_str

            if self.on_card_action:
                await self.on_card_action(value)
        except Exception:
            logger.exception("Error handling card action")

