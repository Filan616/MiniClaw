"""Web tools: HTTP request support for the agent."""

from __future__ import annotations

from typing import Any

import httpx

from .registry import Tool, ToolContext


async def _http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    *,
    ctx: ToolContext,
) -> str:
    """Perform an HTTP request using httpx."""
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        return f"[ERROR] Unsupported HTTP method: {method}"

    try:
        async with httpx.AsyncClient(timeout=ctx.timeout) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                content=body,
            )
    except httpx.TimeoutException:
        return f"[ERROR] Request timed out after {ctx.timeout}s"
    except httpx.RequestError as exc:
        return f"[ERROR] Request failed: {exc}"

    parts: list[str] = [
        f"HTTP {response.status_code} {response.reason_phrase}",
    ]
    # Include response headers summary
    content_type = response.headers.get("content-type", "")
    parts.append(f"Content-Type: {content_type}")
    parts.append("")
    parts.append(response.text[:16000])  # cap response body

    return "\n".join(parts)


def _get_permission_level(method: str) -> str:
    """Determine permission level based on HTTP method.

    GET/HEAD are read-only (L1), mutating methods are L3.
    """
    if method.upper() in ("GET", "HEAD"):
        return "L1"
    return "L3"


TOOL_HTTP_REQUEST = Tool(
    name="http_request",
    description=(
        "Perform an HTTP request. GET/HEAD require L1 permission; "
        "POST/PUT/PATCH/DELETE require L3."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target URL"},
            "method": {
                "type": "string",
                "description": "HTTP method",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                "default": "GET",
            },
            "headers": {
                "type": "object",
                "description": "Optional request headers",
                "additionalProperties": {"type": "string"},
            },
            "body": {
                "type": "string",
                "description": "Optional request body",
            },
        },
        "required": ["url"],
    },
    handler=_http_request,
    permission_level="L3",  # worst-case; runtime checks actual method
)


WEB_TOOLS: list[Tool] = [TOOL_HTTP_REQUEST]
