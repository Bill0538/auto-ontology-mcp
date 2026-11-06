"""Command line: run the MCP server and manage datasources.

``auto-ontology-mcp`` with no arguments serves MCP over stdio, which is what
Claude Desktop, Claude Code and Cursor launch.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shutil
import sys
from pathlib import Path

from . import __version__


def _settings():
    from .config import Settings

    return Settings.from_env()


def _service():
    from .service import OntologyService

    return OntologyService(_settings())


def _print(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def cmd_serve(args: argparse.Namespace) -> None:
    from .server import build_server

    if args.token:
        os.environ["AUTO_ONTOLOGY_MCP_TOKEN"] = args.token
    settings = _settings()
    mcp = build_server(settings)
    for spec in args.datasource or []:
        name, _, url = spec.partition("=")
        if not url:
            sys.exit(f"--datasource expects name=url, got {spec!r}")
        svc = mcp.service  # type: ignore[attr-defined]
        if not svc.store.datasource_by_name(name) or svc.store.datasource_by_name(name)["url"] != url:
            logging.getLogger(__name__).info("registering datasource %s", name)
            svc.add_datasource(name, url, scan=True)

    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
        return

    from starlette.middleware import Middleware

    from .http_auth import TokenMiddleware

    host = args.host or settings.host
    port = args.port or settings.port
    middleware = []
    if settings.token:
        middleware.append(Middleware(TokenMiddleware, token=settings.token))
    elif host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "WARNING: serving on a non-local address without a token. Anyone who can reach this port can read "
            "your data. Set --token or AUTO_ONTOLOGY_MCP_TOKEN.",
            file=sys.stderr,
        )
    print(f"auto-ontology-mcp {__version__} on http://{host}:{port}/mcp", file=sys.stderr)
    if settings.token:
        print(f"  token required: Authorization: Bearer <token>, ?key=<token>, or http://{host}:{port}/<token>/mcp", file=sys.stderr)
    mcp.run(
        transport="http",
        host=host,
        port=port,
        show_banner=False,
        middleware=middleware,
        stateless_http=args.stateless,
        # Without a token, only accept requests addressed to localhost (DNS-rebinding
        # protection). With a token, tunnel hostnames must get through.
        host_origin_protection=False if settings.token else "auto",
    )


def cmd_demo(args: argparse.Namespace) -> None:
    from .demo import build_demo_database

    settings = _settings()
    path = Path(args.path).expanduser() if args.path else settings.home / "demo_retail.sqlite"
    build_demo_database(path)
    svc = _service()
    result = svc.add_datasource("demo", f"sqlite:///{path}", description="Synthetic retail sales database (orders, customers, products).", scan=True)
    _print(result)
    print(
        f"\nDemo database: {path}\nOntology store: {settings.store_path}\n"
        "Connect a client (see README) and ask it to build the ontology, e.g. use the 'build_ontology' prompt.",
        file=sys.stderr,
    )


def cmd_add(args: argparse.Namespace) -> None:
    svc = _service()
    schemas = [s.strip() for s in args.schemas.split(",")] if args.schemas else None
    _print(svc.add_datasource(args.name, args.url, args.description, schemas, scan=not args.no_scan))


def cmd_scan(args: argparse.Namespace) -> None:
    _print(_service().scan_datasource(args.name, profile=not args.no_profile))


def cmd_list(_: argparse.Namespace) -> None:
    _print(_service().check_readiness())


def cmd_remove(args: argparse.Namespace) -> None:
    svc = _service()
    ds = svc.ds(args.name)
    with svc.store.tx() as con:
        con.execute("DELETE FROM datasource WHERE id = ?", (ds["id"],))
    print(f"Removed datasource {ds['name']} and its ontology.")


def cmd_export(args: argparse.Namespace) -> None:
    out = _service().export_ontology(args.name, args.format)
    if args.output:
        Path(args.output).write_text(out["document"], encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(out["document"])


def cmd_import(args: argparse.Namespace) -> None:
    _print(_service().import_ontology(Path(args.file).read_text(encoding="utf-8"), args.name))


def cmd_export_wiki(args: argparse.Namespace) -> None:
    _print(_service().export_wiki(args.datasource, args.output))


def cmd_config(args: argparse.Namespace) -> None:
    """Print client configuration snippets."""
    checkout = Path(__file__).resolve().parents[2]
    if args.source is None and (checkout / "pyproject.toml").exists():
        # Running from a clone: launch that clone, which works for private repositories too.
        uv = shutil.which("uv") or "uv"
        command, cmd_args = uv, ["run", "--directory", str(checkout), "auto-ontology-mcp"]
    else:
        uvx = shutil.which("uvx") or "uvx"
        source = args.source or "git+https://github.com/gogoymh/auto-ontology-mcp"
        command, cmd_args = uvx, ["--from", source, "auto-ontology-mcp"]
    home = os.environ.get("AUTO_ONTOLOGY_HOME")
    server: dict = {"command": command, "args": cmd_args}
    if home:
        server["env"] = {"AUTO_ONTOLOGY_HOME": home}
    print("# Claude Desktop (claude_desktop_config.json) / Cursor (~/.cursor/mcp.json):")
    _print({"mcpServers": {"auto-ontology": server}})
    print("\n# Claude Code:")
    print(f"claude mcp add auto-ontology -- {command} {' '.join(cmd_args)}")
    token = secrets.token_urlsafe(24)
    print("\n# ChatGPT / claude.ai (remote): run the HTTP server behind an HTTPS tunnel, e.g.")
    print(f"auto-ontology-mcp serve --transport http --port 8765 --token {token}")
    print("cloudflared tunnel --url http://127.0.0.1:8765   # or: ngrok http 8765")
    print(f"# then add the connector URL: https://<tunnel-host>/{token}/mcp")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=os.environ.get("AUTO_ONTOLOGY_LOG_LEVEL", "WARNING"), stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="auto-ontology-mcp", description="Auto Ontology as an MCP server (no LLM key needed).")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="run the MCP server (default: stdio)")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--token", default=None, help="shared secret for the HTTP transport")
    p.add_argument("--stateless", action="store_true", help="stateless HTTP (no MCP session ids)")
    p.add_argument("--datasource", action="append", metavar="NAME=URL", help="register (and scan) a datasource at startup")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("demo", help="create the sample retail database and register it as 'demo'")
    p.add_argument("--path", default=None)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("add", help="register a datasource and scan it")
    p.add_argument("name")
    p.add_argument("url", help="SQLAlchemy URL")
    p.add_argument("--description", default=None)
    p.add_argument("--schemas", default=None, help="comma-separated schemas to include")
    p.add_argument("--no-scan", action="store_true")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("scan", help="re-scan a datasource")
    p.add_argument("name")
    p.add_argument("--no-profile", action="store_true")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("list", help="show datasources and readiness")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("remove", help="delete a datasource and its ontology")
    p.add_argument("name")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("export", help="export a datasource's ontology")
    p.add_argument("name")
    p.add_argument("--format", choices=["yaml", "json"], default="yaml")
    p.add_argument("-o", "--output", default=None)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="import an exported ontology into a scanned datasource")
    p.add_argument("name")
    p.add_argument("file")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("export-wiki", help="write the ontology as an Obsidian vault (default: the knowledge store)")
    p.add_argument("--datasource", default=None)
    p.add_argument("-o", "--output", default=None, help="folder (default: ~/.auto-ontology-mcp/wiki/<datasource>)")
    p.set_defaults(func=cmd_export_wiki)

    p = sub.add_parser("config", help="print client configuration snippets")
    p.add_argument("--source", default=None, help="package source for uvx (default: this GitHub repo)")
    p.set_defaults(func=cmd_config)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["serve", *(argv or [])])
    from .service import OntologyError

    try:
        args.func(args)
    except OntologyError as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
