"""CLI channel for local testing."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from mini_claw.channels.base import Channel, InboundMessage


class CLIChannel(Channel):
    """Local testing channel that uses stdin/stdout."""

    channel_type = "cli"

    def __init__(self, name: str = "cli", agent_id: str = "default") -> None:
        super().__init__(name=name)
        self._stream_buffer = ""
        self._running = False
        self._agent_id = agent_id

    async def send(self, chat_id: str, text: str) -> None:
        """Print the message to stdout."""
        # Flush any pending stream
        if self._stream_buffer:
            print()  # Newline after stream
            self._stream_buffer = ""
        print(f"\n[bot -> {chat_id}] {text}")

    async def send_stream_chunk(self, chat_id: str, delta: str) -> None:
        """Print streaming chunks in real-time."""
        if not self._stream_buffer:
            print(f"\n[bot -> {chat_id}] ", end="", flush=True)
        print(delta, end="", flush=True)
        self._stream_buffer += delta

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

    async def start(self) -> None:
        await self.interactive_loop()

    async def stop(self) -> None:
        self._running = False

    async def interactive_loop(self) -> None:
        """Read from stdin and send messages to the gateway."""
        chat_id = "cli_local"
        try:
            sender_id = os.getlogin()
        except OSError:
            sender_id = "cli_user"
        print("MiniClaw CLI (type 'quit' to exit)")
        print("-" * 40)

        loop = asyncio.get_event_loop()
        self._running = True
        while self._running:
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
                channel_name=self.name,
                chat_id=chat_id,
                sender_id=sender_id,
                text=text.strip(),
                event_id=uuid.uuid4().hex,
                timestamp=int(time.time()),
            )
            await self._dispatch_message(msg)
