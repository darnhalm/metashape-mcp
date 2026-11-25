# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==2.3.0"]
# ///
"""Read one MCP discovery/initialization result per request from the status window."""

import asyncio
import json
import sys

import httpx2
from mcp import Client


def describe_failure(error):
    if isinstance(error, BaseExceptionGroup):
        return describe_failure(error.exceptions[0])
    if isinstance(error, httpx2.ConnectError):
        return "offline", "Не удалось подключиться к серверу."
    if isinstance(error, (TimeoutError, httpx2.TimeoutException)):
        return "timeout", "Сервер не ответил вовремя."
    if isinstance(error, httpx2.HTTPStatusError):
        return "error", f"Сервер вернул HTTP {error.response.status_code}."
    return "error", str(error).strip() or type(error).__name__


async def probe(url):
    try:
        async with asyncio.timeout(4):
            async with Client(url, read_timeout_seconds=3) as client:
                info = client.server_info
                name = f"{info.name} {info.version}".strip() if info else "Сервис MCP"
                return "online", f"{name}  ·  MCP {client.protocol_version}"
    except Exception as error:
        return describe_failure(error)


if __name__ == "__main__":
    # EOF also releases this worker when the window or the host application exits.
    for _ in sys.stdin:
        state, detail = asyncio.run(probe(sys.argv[1]))
        print(json.dumps({"state": state, "detail": detail}, ensure_ascii=True), flush=True)
