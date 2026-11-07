"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    home: Path
    """Directory holding the ontology store (and the demo database)."""

    store_path: Path
    """SQLite file that holds catalogs, terms, relationships and logs."""

    max_rows: int
    """Upper bound on rows ``run_sql`` returns to the model."""

    query_timeout_s: int
    """Per-statement timeout applied to queries against source databases."""

    profile_sample_rows: int
    """Rows sampled per table when profiling columns."""

    allow_datasource_tools: bool
    """Expose ``add_datasource`` / ``scan_datasource`` over MCP."""

    token: str | None
    """Shared secret required by the HTTP transport, when set."""

    host: str
    port: int

    vault: Path | None = None
    """Obsidian vault for originals (AUTO_ONTOLOGY_VAULT). Unset: the knowledge export folder is the vault."""

    @classmethod
    def from_env(cls) -> "Settings":
        home = Path(os.environ.get("AUTO_ONTOLOGY_HOME", "~/.auto-ontology-mcp")).expanduser()
        store = os.environ.get("AUTO_ONTOLOGY_STORE")
        return cls(
            home=home,
            store_path=Path(store).expanduser() if store else home / "ontology.db",
            max_rows=max(1, _int("AUTO_ONTOLOGY_MAX_ROWS", 500)),
            query_timeout_s=max(1, _int("AUTO_ONTOLOGY_QUERY_TIMEOUT_S", 30)),
            profile_sample_rows=max(10, _int("AUTO_ONTOLOGY_PROFILE_ROWS", 1000)),
            allow_datasource_tools=_bool("AUTO_ONTOLOGY_ALLOW_DATASOURCE_TOOLS", True),
            token=os.environ.get("AUTO_ONTOLOGY_MCP_TOKEN") or None,
            host=os.environ.get("AUTO_ONTOLOGY_MCP_HOST", "127.0.0.1"),
            port=_int("AUTO_ONTOLOGY_MCP_PORT", 8765),
            vault=Path(os.environ["AUTO_ONTOLOGY_VAULT"]).expanduser() if os.environ.get("AUTO_ONTOLOGY_VAULT") else None,
        )
