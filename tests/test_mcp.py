"""The MCP surface itself, in memory and over HTTP."""

from __future__ import annotations

import socket
import threading
import time

try:
    import httpx2 as httpx  # fastmcp 4 ships httpx2
except ImportError:  # pragma: no cover
    import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from auto_ontology_mcp.server import build_server


async def test_tools_prompts_and_errors(settings, svc):
    mcp = build_server(settings, svc)
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert {"check_readiness", "get_modeling_worklist", "save_table_terms", "run_sql", "search", "fetch"} <= set(tools)
        assert tools["run_sql"].annotations.read_only_hint is True
        assert tools["update_term"].annotations.destructive_hint is True
        schema = tools["save_table_terms"].input_schema
        assert "terms" in schema["required"]

        prompts = {p.name for p in await client.list_prompts()}
        assert prompts == {"build_ontology", "answer_question", "review_ontology", "remember"}

        ready = await client.call_tool("check_readiness", {})
        assert ready.data["datasources"][0]["datasource"] == "demo"

        with pytest.raises(ToolError) as exc:
            await client.call_tool("run_sql", {"sql": "DROP TABLE orders"})
        assert "Only SELECT" in str(exc.value) or "not allowed" in str(exc.value)

        saved = await client.call_tool(
            "save_table_terms",
            {"table": "tags", "terms": [{"name": "Tag", "description": "A merchandising tag.", "columns": [
                {"column": "tag_id", "display_name": "Tag ID"}, {"column": "tag_name", "display_name": "Tag Name"}]}]},
        )
        assert saved.data["saved_terms"][0]["name"] == "Tag"


async def test_datasource_tools_can_be_disabled(settings, svc):
    from dataclasses import replace

    mcp = build_server(replace(settings, allow_datasource_tools=False), svc)
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert "add_datasource" not in names and "scan_datasource" not in names


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_transport_requires_token(settings, svc):
    import uvicorn
    from starlette.middleware import Middleware

    from auto_ontology_mcp.http_auth import TokenMiddleware

    mcp = build_server(settings, svc)
    app = mcp.http_app(middleware=[Middleware(TokenMiddleware, token="s3cret")])
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/health").json()["status"] == "ok"
        init = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        }
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        assert httpx.post(f"{base}/mcp", json=init, headers=headers).status_code == 401
        assert httpx.post(f"{base}/mcp?key=wrong", json=init, headers=headers).status_code == 401
        for url, extra in (
            (f"{base}/mcp", {"Authorization": "Bearer s3cret"}),
            (f"{base}/mcp?key=s3cret", {}),
            (f"{base}/s3cret/mcp", {}),
        ):
            r = httpx.post(url, json=init, headers={**headers, **extra})
            assert r.status_code == 200, (url, r.status_code, r.text[:200])
            assert "auto-ontology" in r.text
    finally:
        server.should_exit = True
        thread.join(timeout=5)


async def test_http_client_roundtrip(settings, svc):
    import uvicorn
    from starlette.middleware import Middleware

    from auto_ontology_mcp.http_auth import TokenMiddleware

    mcp = build_server(settings, svc)
    app = mcp.http_app(middleware=[Middleware(TokenMiddleware, token="tok")])
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        async with Client(f"http://127.0.0.1:{port}/tok/mcp") as client:
            result = await client.call_tool("run_sql", {"sql": "SELECT COUNT(*) AS n FROM customers"})
            assert result.data["rows"] == [[240]]
            found = await client.call_tool("search", {"query": "orders"})
            assert "results" in found.data
    finally:
        server.should_exit = True
        thread.join(timeout=5)
