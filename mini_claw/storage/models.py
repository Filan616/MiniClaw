"""Pydantic models for storage entities."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Session(BaseModel):
    chat_id: str
    agent_id: str
    created_at: int
    updated_at: int


class Message(BaseModel):
    id: Optional[int] = None
    chat_id: str
    agent_id: str
    run_id: Optional[str] = None
    role: str
    content: Optional[str] = None
    tool_calls: Optional[str] = None
    tool_call_id: Optional[str] = None
    created_at: int = 0


class AgentRun(BaseModel):
    id: str
    chat_id: str
    agent_id: str
    status: str = "running"
    user_message: Optional[str] = None
    final_answer: Optional[str] = None
    iterations: int = 0
    seen_calls: Optional[str] = None
    pending_approval_id: Optional[str] = None
    pending_tool_call: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created_at: int = 0
    updated_at: int = 0


class Job(BaseModel):
    id: str
    chat_id: str
    agent_id: str
    type: str = "interactive"
    status: str = "queued"
    instruction: str = ""
    run_id: Optional[str] = None
    result: Optional[str] = None
    created_at: int = 0
    updated_at: int = 0


class PendingApproval(BaseModel):
    id: str
    run_id: str
    chat_id: str
    agent_id: str
    tool_name: str
    tool_args: str = ""
    status: str = "pending"
    created_at: int = 0
    expires_at: int = 0


class ScheduledTask(BaseModel):
    id: str
    chat_id: str
    agent_id: str
    cron: str
    instruction: str
    enabled: bool = True
    created_at: int = 0
