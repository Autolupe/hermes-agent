"""Scope-path parsing for kanban worktree cards.

A worktree card may declare the repo-relative path prefixes it intends to
touch in a fenced ``scope-paths`` block inside its body::

    ```scope-paths
    backend/app/services/
    # comments are allowed
    tests/test_services.py
    ```

The dispatcher (``kanban_db.check_claim_guard``) uses these declarations to
refuse spawning two workers whose scopes overlap, and to block a card whose
scope reaches into a protected prefix (CI config, agent instruction files,
deploy manifests). Everything here is pure string handling; the only I/O is
reading the repo's ``ops/autonomy/path-claims.json``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

# Used when the repo carries no readable ``ops/autonomy/path-claims.json``.
# Mirrors the always-protected set from the autonomy gate so a Hermes card
# can never claim CI, deploy, or instruction files even on a checkout that
# predates the claims file.
FALLBACK_PROTECTED_PREFIXES: tuple[str, ...] = (
    ".github/",
    "scripts/autonomous-",
    "scripts/bootstrap-",
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/",
    "ops/autonomy/",
    "backend/deploy/",
    "package.json",
    "package-lock.json",
)

# Characters a declared scope path may contain. Mirrors the host-side
# ``scope_lint.PATH_RE`` so the doctor and the engine agree on what is
# valid: no globs (``*``, ``?``, ``[``), no whitespace, no backslashes.
_SCOPE_PATH_CHARS_RE = re.compile(r"^[A-Za-z0-9._@+\-/]+$")

_SCOPE_BLOCK_RE = re.compile(
    r"^[ \t]*```[ \t]*scope-paths[ \t]*\r?\n(?P<body>.*?)^[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def normalize_scope_path(raw: str) -> str:
    """Validate one declared scope path and return it in canonical form.

    Protects against a card claiming something outside its repo: absolute
    paths, ``..`` segments, and empty entries are rejected. Glob patterns
    (``backend/**``, ``.github/*``), whitespace, and backslashes are
    rejected too, because a literal ``backend/**`` never overlaps any
    real file and would give the card a scope that guards nothing. A
    trailing ``/`` is preserved because it marks a directory prefix.
    """
    value = raw.strip()
    if not value:
        raise ValueError("scope path is empty")
    if not _SCOPE_PATH_CHARS_RE.match(value):
        raise ValueError(
            f"scope path {value!r} contains characters outside [A-Za-z0-9._@+-/] "
            "(globs, spaces and backslashes are not allowed; name a prefix)"
        )
    normalized = value
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise ValueError(f"scope path must be repo-relative, got {value!r}")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError(f"scope path {value!r} names nothing")
    if any(p == ".." for p in parts):
        raise ValueError(f"scope path must not contain '..', got {value!r}")
    result = "/".join(parts)
    if normalized.endswith("/"):
        result += "/"
    return result


def extract_scope_paths(body: Optional[str]) -> Optional[list[str]]:
    """Return the scope paths declared in a card body, or ``None`` when absent.

    Protects the dispatcher from silently trusting a malformed declaration:
    an empty block, absolute paths, or ``..`` raise ``ValueError`` instead of
    yielding a scope that would guard nothing.
    """
    if not body:
        return None
    match = _SCOPE_BLOCK_RE.search(body)
    if match is None:
        return None
    paths: list[str] = []
    for line in match.group("body").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Allow a trailing "# comment" after the path.
        stripped = stripped.split(" #", 1)[0].strip()
        if not stripped:
            continue
        normalized = normalize_scope_path(stripped)
        if normalized not in paths:
            paths.append(normalized)
    if not paths:
        raise ValueError("scope-paths block is empty")
    return paths


def _is_prefix_of(prefix: str, path: str) -> bool:
    """True when ``prefix`` covers ``path`` by whole path segments."""
    if prefix == path:
        return True
    if prefix.endswith("/"):
        return path.startswith(prefix)
    # A file prefix covers only itself; a bare directory name (no slash)
    # still covers everything under it.
    return path.startswith(prefix + "/")


def paths_overlap(a: Optional[list[str]], b: Optional[list[str]]) -> Optional[str]:
    """Return the first path shared by two scope lists, else ``None``.

    Prefix semantics: ``backend/app/`` overlaps ``backend/app/x.py`` and vice
    versa. Protects against two workers editing the same subtree while each
    believes it has the tree to itself. Missing scopes never overlap.
    """
    if not a or not b:
        return None
    for left in a:
        for right in b:
            if _is_prefix_of(left, right):
                return right
            if _is_prefix_of(right, left):
                return left
    return None


def protected_path_hit(paths: Optional[list[str]], protected: list[str]) -> Optional[str]:
    """Return the protected prefix a scope path reaches into, else ``None``.

    Protects instruction/CI/deploy files from being claimed by a worker
    card: a scope that covers a protected prefix, or sits under one, hits.
    """
    if not paths:
        return None
    for path in paths:
        for prefix in protected:
            if _is_prefix_of(prefix, path) or _is_prefix_of(path, prefix):
                return prefix
    return None


def load_protected_prefixes(default_workdir: Optional[str | Path]) -> list[str]:
    """Read protected prefixes from ``<repo>/ops/autonomy/path-claims.json``.

    Combines ``protected_prefixes`` with ``lanes.hermes.exclude``. Any
    read/parse failure falls back to :data:`FALLBACK_PROTECTED_PREFIXES` so
    the guard never opens up just because the claims file is missing.
    """
    fallback = list(FALLBACK_PROTECTED_PREFIXES)
    if not default_workdir:
        return fallback
    try:
        claims_path = Path(default_workdir).expanduser() / "ops" / "autonomy" / "path-claims.json"
        data = json.loads(claims_path.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    if not isinstance(data, dict):
        return fallback
    prefixes: list[str] = []
    raw = data.get("protected_prefixes")
    if isinstance(raw, list):
        prefixes.extend(str(p) for p in raw if isinstance(p, str) and p.strip())
    lanes = data.get("lanes")
    if isinstance(lanes, dict):
        hermes = lanes.get("hermes")
        if isinstance(hermes, dict) and isinstance(hermes.get("exclude"), list):
            prefixes.extend(
                str(p) for p in hermes["exclude"] if isinstance(p, str) and p.strip()
            )
    cleaned: list[str] = []
    for p in prefixes:
        try:
            norm = normalize_scope_path(p)
        except ValueError:
            continue
        if norm not in cleaned:
            cleaned.append(norm)
    return cleaned or fallback


__all__ = [
    "FALLBACK_PROTECTED_PREFIXES",
    "extract_scope_paths",
    "load_protected_prefixes",
    "normalize_scope_path",
    "paths_overlap",
    "protected_path_hit",
]
