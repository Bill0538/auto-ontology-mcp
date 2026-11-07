"""Originals in the Obsidian vault.

The knowledge store keeps summaries and excerpts; the full original stays in
the user's Obsidian vault, where it can be read, annotated and linked. A
source points at its original by a vault-relative path (``sources.original``).
The original is either a note already in the vault, markdown text the model
transcribed (written as a new note), or a local file copied in (a PDF, an
image). Existing files are never overwritten: a different original with the
same name gets a numbered name instead.
"""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .errors import KnowledgeError

ORIGINAL_EXTENSIONS = frozenset(
    {
        ".md", ".markdown", ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".heic",
        ".html", ".htm", ".csv", ".json", ".epub", ".docx", ".pptx", ".xlsx",
        ".mp3", ".m4a", ".wav", ".mp4", ".mov", ".webm",
    }
)
MAX_ORIGINAL_BYTES = 100 * 1024 * 1024
SAVED_BY = "auto-ontology-mcp"
_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_HEADING_UNSAFE = re.compile(r"[#|\[\]^]+")


def safe_filename(name: str) -> str:
    cleaned = _UNSAFE.sub("-", name or "").strip(" .-")
    return (cleaned or "untitled")[:120]


def wikilink(path: str, heading: str | None = None, label: str | None = None) -> str:
    """``[[path#heading|label]]`` for a vault-relative path; markdown notes drop the extension."""
    target = path[:-3] if path.lower().endswith(".md") else path
    if heading:
        clean = _HEADING_UNSAFE.sub(" ", heading).strip()
        if clean:
            target = f"{target}#{clean}"
    return f"[[{target}|{label}]]" if label else f"[[{target}]]"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _free_name(folder: Path, stem: str, suffix: str, same: Any) -> Path:
    """First ``stem suffix``, ``stem (2) suffix``, ... that is free or already holds the same original."""
    for n in range(1, 1000):
        candidate = folder / (f"{stem}{suffix}" if n == 1 else f"{stem} ({n}){suffix}")
        if not candidate.exists() or same(candidate):
            return candidate
    raise KnowledgeError(f"Too many originals named {stem!r}; pick another title.")


def _note_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5:].lstrip("\n")
    return text


def link_existing(vault: Path, original: str) -> str:
    """Validate a note or file that is already in the vault; returns its vault-relative path."""
    raw = Path(original).expanduser()
    path = (raw if raw.is_absolute() else vault / raw).resolve()
    if not _inside(path, vault.resolve()):
        raise KnowledgeError(f"original {original!r} is outside the Obsidian vault ({vault}).")
    if not path.is_file():
        raise KnowledgeError(f"original {original!r} does not exist in the vault ({vault}).")
    return path.relative_to(vault.resolve()).as_posix()


def write_text(vault: Path, folder: Path, source: dict[str, Any], text: str) -> str:
    """Write transcribed text as a new note; returns its vault-relative path."""
    body = (text or "").strip()
    if not body:
        raise KnowledgeError("original_text is empty.")
    folder.mkdir(parents=True, exist_ok=True)
    title = source.get("title") or "untitled"
    stem = safe_filename(title[:-3] if title.lower().endswith(".md") else title)
    path = _free_name(folder, stem, ".md", lambda p: _note_body(p).strip() == body)
    if not path.exists():
        meta = {
            "title": title,
            "source_id": source.get("id"),
            "kind": source.get("kind"),
            "uri": source.get("uri"),
            "author": source.get("author"),
            "published_on": source.get("published_on"),
            "saved_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "saved_by": SAVED_BY,
        }
        head = yaml.safe_dump({k: v for k, v in meta.items() if v not in (None, "")}, allow_unicode=True, sort_keys=False)
        path.write_text(f"---\n{head}---\n\n{body}\n", encoding="utf-8")
    return path.resolve().relative_to(vault.resolve()).as_posix()


def copy_file(vault: Path, folder: Path, file: str) -> str:
    """Copy a local file (PDF, image, document) into the vault; returns its vault-relative path."""
    raw = Path(file).expanduser()
    if not raw.is_absolute():
        raise KnowledgeError(f"original_file must be an absolute path (got {file!r}).")
    try:
        path = raw.resolve(strict=True)
    except OSError as exc:
        raise KnowledgeError(f"original_file {file!r} does not exist.") from exc
    if not path.is_file():
        raise KnowledgeError(f"original_file {file!r} is not a file.")
    if any(part.startswith(".") for part in path.parts[1:]):
        raise KnowledgeError("original_file cannot come from a hidden file or folder.")
    if path.suffix.lower() not in ORIGINAL_EXTENSIONS:
        raise KnowledgeError(
            f"original_file type {path.suffix or '(none)'} is not copied; allowed: {', '.join(sorted(ORIGINAL_EXTENSIONS))}."
        )
    size = path.stat().st_size
    if size > MAX_ORIGINAL_BYTES:
        raise KnowledgeError(f"original_file is {size // (1024 * 1024)} MB; the limit is {MAX_ORIGINAL_BYTES // (1024 * 1024)} MB.")
    if _inside(path, vault.resolve()):
        return path.relative_to(vault.resolve()).as_posix()
    folder.mkdir(parents=True, exist_ok=True)
    data = path.read_bytes()
    target = _free_name(folder, safe_filename(path.stem), path.suffix.lower(), lambda p: p.is_file() and p.read_bytes() == data)
    if not target.exists():
        shutil.copyfile(path, target)
    return target.resolve().relative_to(vault.resolve()).as_posix()
