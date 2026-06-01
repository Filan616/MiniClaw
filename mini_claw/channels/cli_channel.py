"""CLI channel for local testing."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Protocol

from mini_claw.channels.base import Channel, InboundMessage


class Gateway(Protocol):
    """Protocol for the gateway that processes inbound messages."""

    async def handle_message(self, msg: InboundMessage) -> None: ...


class CLIChannel(Channel):
    """Local testing channel that uses stdin/stdout."""

    async def send(self, chat_id: str, text: str) -> None:
        """Print the message to stdout."""
        print(f"\n[bot -> {chat_id}] {text}")

    async def send_approval_card(
        self,
        chat_id: str,
        approval_id: str,
        tool_name: str,
        tool_args: dict,
        level: str,
    ) -> None:
        """Print approval info and auto-approve for testing."""
        args_display = json.dumps(tool_args, indent=2, ensure_ascii=False)
        print(f"\n[APPROVAL {level}] {tool_name}")
        print(f"  Args: {args_display}")
        print(f"  Auto-approving {approval_id} (CLI testing mode)")

    async def interactive_loop(self, gateway: Any) -> None:
        """Read from stdin and send messages to the gateway."""
        chat_id = "cli_local"
        sender_id = "cli_user"
        print("MiniClaw CLI (type 'quit' to exit)")
        print("-" * 40)

        loop = asyncio.get_event_loop()
        while True:
            try:
                text = await loop.run_in_executor(
                    None, lambda: input("\nyou> ")
                )
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                break

            if text.strip().lower() in ("quit", "exit"):
                print("Bye!")
                break

            if not text.strip():
                continue

            msg = InboundMessage(
                chat_id=chat_id,
                sender_id=sender_id,
                text=text.strip(),
                event_id=uuid.uuid4().hex,
                timestamp=int(time.time()),
            )
            await gateway.handle_message(msg)
