"""Feishu (Lark) channel implementation.

Receive: long-connection (WebSocket) via ``lark_oapi.ws.Client``.
Send:    REST API via httpx.

Long-connection mode means we don't need a public Webhook URL,
``verification_token``, or ``encrypt_key``. The SDK opens an outbound
WebSocket to Feishu, identifies itself with ``app_id`` / ``app_secret``,
and events are pushed down that connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Callable, Coroutine

import httpx
import lark_oapi as lark
import lark_oapi.ws.client as _lark_ws_client_module
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import (
    P2ImMessageReceiveV1,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from mini_claw.channels.base import Channel, InboundMessage

logger = logging.getLogger(__name__)

FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
TOKEN_URL = f"{FEISHU_BASE_URL}/auth/v3/tenant_access_token/internal"
SEND_MSG_URL = f"{FEISHU_BASE_URL}/im/v1/messages"


class FeishuChannel(Channel):
    """Feishu messaging channel: WS for receive, REST for send."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        log_level: lark.LogLevel = lark.LogLevel.INFO,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._log_level = log_level

        self._tenant_token: str | None = None
        self._token_expires_at: float = 0
        self._http: httpx.AsyncClient = httpx.AsyncClient(timeout=10)

        # Inbound dispatch
        self.on_message: Callable[
            [InboundMessage], Coroutine[Any, Any, None]
        ] | None = None
        self.on_card_action: Callable[
            [dict], Coroutine[Any, Any, None]
        ] | None = None

        # Long-connection state
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_client: lark.ws.Client | None = None

        # Streaming state: track active stream per chat
        self._active_stream: dict[str, dict[str, Any]] = {}
        self._stream_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _get_tenant_token(self) -> str:
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
        self._token_expires_at = time.time() + data.get("expire", 7200) - 300
        return self._tenant_token

    async def _auth_headers(self) -> dict[str, str]:
        token = await self._get_tenant_token()
        return {"Authorization": f"Bearer {token}"}

    # ------------------------------------------------------------------
    # Channel interface — send (REST)
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, text: str) -> None:
        try:
            headers = await self._auth_headers()
        except Exception:
            logger.exception("获取 tenant_access_token 失败，无法发送消息")
            return

        # Finalize any active stream for this chat
        async with self._stream_lock:
            if chat_id in self._active_stream:
                del self._active_stream[chat_id]

        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        logger.info("发送消息 -> chat=%s text=%r", chat_id, text[:80])
        resp = await self._http.post(
            SEND_MSG_URL,
            params={"receive_id_type": "chat_id"},
            headers=headers,
            json=payload,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("发送消息失败: %s", data)
        else:
            logger.info("发送消息成功 message_id=%s", data.get("data", {}).get("message_id", "?"))

    async def send_stream_chunk(self, chat_id: str, delta: str) -> None:
        """Accumulate streaming chunks and send batched updates to Feishu.

        Feishu doesn't have native streaming, so we accumulate text and
        send updates every ~1 second via message editing.
        """
        async with self._stream_lock:
            if chat_id not in self._active_stream:
                # Start a new stream
                self._active_stream[chat_id] = {
                    "text": delta,
                    "message_id": None,
                    "last_update": time.time(),
                }
                # Send initial message
                try:
                    headers = await self._auth_headers()
                    payload = {
                        "receive_id": chat_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": delta}),
                    }
                    resp = await self._http.post(
                        SEND_MSG_URL,
                        params={"receive_id_type": "chat_id"},
                        headers=headers,
                        json=payload,
                    )
                    data = resp.json()
                    if data.get("code") == 0:
                        msg_id = data.get("data", {}).get("message_id")
                        self._active_stream[chat_id]["message_id"] = msg_id
                except Exception:
                    logger.exception("Failed to send initial streaming message")
            else:
                # Accumulate
                stream_state = self._active_stream[chat_id]
                stream_state["text"] += delta
                now = time.time()
                # Update message if >1s since last update
                if now - stream_state["last_update"] >= 1.0:
                    stream_state["last_update"] = now
                    message_id = stream_state["message_id"]
                    if message_id:
                        # Feishu doesn't support message editing for text messages easily,
                        # so as a fallback we just send a new message. For production,
                        # consider using interactive cards that support updates.
                        pass  # For now, skip intermediate updates

    async def send_approval_card(
        self,
        chat_id: str,
        approval_id: str,
        tool_name: str,
        tool_args: dict,
        level: str,
    ) -> None:
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
                            "value": {
                                "action": "approve",
                                "approval_id": approval_id,
                            },
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "Reject"},
                            "type": "danger",
                            "value": {
                                "action": "reject",
                                "approval_id": approval_id,
                            },
                        },
                    ],
                },
            ],
        }

    # ------------------------------------------------------------------
    # Long-connection lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the long-connection client on a background thread."""
        if self._ws_thread is not None and self._ws_thread.is_alive():
            return

        self._main_loop = asyncio.get_running_loop()

        handler = (
            lark.EventDispatcherHandler.builder("", "", self._log_level)
            .register_p2_im_message_receive_v1(self._on_message_event)
            .register_p2_card_action_trigger(self._on_card_action_event)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=handler,
            log_level=self._log_level,
        )

        self._ws_thread = threading.Thread(
            target=self._run_ws_loop,
            name="feishu-ws",
            daemon=True,
        )
        self._ws_thread.start()
        logger.info("Feishu 长连接已启动 (app_id=%s)", self.app_id)

    async def stop(self) -> None:
        """Best-effort shutdown. The SDK does not expose a clean stop, so the
        daemon thread is left to die with the process."""
        try:
            await self._http.aclose()
        except Exception:  # pragma: no cover
            pass

    def _run_ws_loop(self) -> None:
        """Body of the WS background thread.

        ``lark_oapi.ws.client`` resolves its event loop via its module-level
        ``loop`` symbol, set at import time on whichever thread imported it.
        We rebind it to a fresh loop owned by this thread, otherwise
        ``Client.start()`` would try to drive a loop bound to the main
        thread and fail.
        """
        ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(ws_loop)
        _lark_ws_client_module.loop = ws_loop
        try:
            assert self._ws_client is not None
            self._ws_client.start()
        except Exception:
            logger.exception("Feishu 长连接异常退出")

    # ------------------------------------------------------------------
    # Event handlers (called by lark on the WS thread)
    # ------------------------------------------------------------------

    def _on_message_event(self, event: P2ImMessageReceiveV1) -> None:
        try:
            data = event.event
            if data is None or data.message is None:
                logger.warning("飞书消息事件缺少 data/message，已忽略")
                return
            msg_obj = data.message
            sender = data.sender

            content_str = msg_obj.content or "{}"
            try:
                content = json.loads(content_str)
            except json.JSONDecodeError:
                content = {}
            text = content.get("text", "") if isinstance(content, dict) else ""

            sender_open_id = ""
            if sender is not None and sender.sender_id is not None:
                sender_open_id = sender.sender_id.open_id or ""

            event_id = ""
            if event.header is not None:
                event_id = event.header.event_id or ""

            msg = InboundMessage(
                chat_id=msg_obj.chat_id or "",
                sender_id=sender_open_id,
                text=text,
                event_id=event_id,
                timestamp=int(msg_obj.create_time or 0),
            )

            logger.info(
                "飞书消息收到 chat=%s sender=%s text=%r event_id=%s msg_type=%s",
                msg.chat_id, msg.sender_id, msg.text, msg.event_id,
                msg_obj.message_type,
            )
            if self.on_message is None:
                logger.warning("on_message 未注册，丢弃消息 %s", msg.event_id)
                return
            self._dispatch_async(self.on_message, msg)
        except Exception:
            logger.exception("处理飞书消息事件失败")

    def _on_card_action_event(
        self, event: P2CardActionTrigger
    ) -> P2CardActionTriggerResponse:
        try:
            data = event.event
            value: dict = {}
            if data is not None and data.action is not None and data.action.value:
                value = dict(data.action.value)
            self._dispatch_async(self.on_card_action, value)
        except Exception:
            logger.exception("处理飞书卡片动作失败")
        # Always ack with an empty response; the gateway updates the card
        # asynchronously via the REST API if needed.
        return P2CardActionTriggerResponse({})

    def _dispatch_async(
        self,
        callback: Callable[[Any], Coroutine[Any, Any, None]] | None,
        payload: Any,
    ) -> None:
        """Schedule ``callback(payload)`` on the FastAPI main loop."""
        if callback is None or self._main_loop is None:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                callback(payload), self._main_loop
            )
        except RuntimeError:
            logger.warning("主事件循环已关闭，丢弃事件")
            return

        def _log_error(fut: Any) -> None:
            try:
                fut.result()
            except Exception:
                logger.exception("飞书事件回调执行失败")

        future.add_done_callback(_log_error)
