"""
Minimal MCP client for trajectory-mcp's Streamable HTTP transport.

Handles the SSE response format (event: message / data: {...}) and the
two-phase session lifecycle: initialize → session_id header → subsequent calls.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

import config


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict


@dataclass
class CallResult:
    raw: Any
    duration_ms: int
    is_error: bool
    error_message: str = ""


class MCPClient:
    def __init__(self, url: str = config.MCP_URL, token: str = config.MCP_TOKEN):
        self._endpoint = url.rstrip("/") + "/mcp"
        self._token = token
        self._session_id: str | None = None
        self._req_id = 0
        self._http = httpx.Client(timeout=120)

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _url(self) -> str:
        return f"{self._endpoint}?token={self._token}"

    def _parse_sse(self, text: str) -> Any:
        """Extract the JSON payload from an SSE response."""
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(text)

    def _extract_tool_result(self, rpc_response: dict) -> tuple[Any, bool, str]:
        """
        Returns (result, is_error, error_message).
        MCP wraps tool output in result.content[0].text as a JSON string.
        """
        if "error" in rpc_response:
            msg = rpc_response["error"].get("message", str(rpc_response["error"]))
            return None, True, msg

        result = rpc_response.get("result", {})

        if result.get("isError"):
            content = result.get("content", [{}])
            msg = content[0].get("text", "unknown error") if content else "unknown error"
            return None, True, msg

        content = result.get("content", [])
        if content and isinstance(content[0], dict):
            text = content[0].get("text", "")
            try:
                return json.loads(text), False, ""
            except json.JSONDecodeError:
                return {"text": text}, False, ""

        return result, False, ""

    def initialize(self) -> dict:
        resp = self._http.post(
            self._url(),
            json={
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "assay", "version": "1.0"},
                },
            },
            headers=self._headers(),
        )
        resp.raise_for_status()
        self._session_id = resp.headers.get("mcp-session-id")
        data = self._parse_sse(resp.text)
        return data.get("result", {})

    def list_tools(self) -> list[Tool]:
        resp = self._http.post(
            self._url(),
            json={"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {}},
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = self._parse_sse(resp.text)
        tools_raw = data.get("result", {}).get("tools", [])
        return [
            Tool(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            )
            for t in tools_raw
        ]

    def call_tool(self, name: str, arguments: dict) -> CallResult:
        t0 = time.monotonic()
        try:
            resp = self._http.post(
                self._url(),
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                headers=self._headers(),
            )
            resp.raise_for_status()
            duration_ms = int((time.monotonic() - t0) * 1000)
            data = self._parse_sse(resp.text)
            raw, is_error, error_msg = self._extract_tool_result(data)
            return CallResult(raw=raw, duration_ms=duration_ms, is_error=is_error, error_message=error_msg)
        except httpx.TimeoutException:
            duration_ms = int((time.monotonic() - t0) * 1000)
            return CallResult(raw=None, duration_ms=duration_ms, is_error=True, error_message="HTTP timeout")
        except Exception as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            return CallResult(raw=None, duration_ms=duration_ms, is_error=True, error_message=str(e))

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
