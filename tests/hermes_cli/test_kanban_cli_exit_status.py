"""Regression coverage for Kanban CLI process exit status propagation."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]


def _tree_snapshot(root: Path) -> dict[str, tuple]:
    """Capture path/type/mode/content without following symlinks."""
    entries: dict[str, tuple] = {}

    def visit(path: Path) -> None:
        info = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            entries[relative] = ("symlink", mode, os.readlink(path))
            return
        if stat.S_ISREG(info.st_mode):
            entries[relative] = ("file", mode, path.read_bytes())
            return
        if stat.S_ISDIR(info.st_mode):
            entries[relative] = (
                "directory",
                mode,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child)
            return
        entries[relative] = ("other", mode, stat.S_IFMT(info.st_mode))

    visit(root)
    return entries


def _run_hermes(home: Path, *args: str, marker: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if marker:
        env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    else:
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _run_dispatch_boundary_probe(
    home: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    # Adversarial service environment: the probe must explicitly choose a
    # safe parent instead of letting tempfile create/delete inside live state.
    env["TMPDIR"] = str(home)
    for name in (
        "HERMES_CONFIG",
        "HERMES_ENV",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.dispatch_boundary_probe", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_delegated_child_kanban_cli_refusal_returns_nonzero_exit_status(tmp_path):
    """A printed Kanban mutation refusal must not look like CLI success."""
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes(home, "kanban", "create", "exit status probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    refused = _run_hermes(
        home,
        "kanban",
        "comment",
        task_id,
        "must be refused",
        marker=True,
    )

    assert refused.returncode == 1
    assert "delegate_task child contexts cannot mutate Kanban tasks via the CLI" in refused.stderr


@pytest.mark.linux_only
def test_dispatch_boundary_probe_is_stdout_only_and_never_writes_live_root(
    tmp_path,
):
    home = tmp_path / "live-hermes"
    live_halt = home / "state" / "halt.json"
    live_halt.parent.mkdir(parents=True)
    live_halt.write_bytes(b"keep these exact live bytes\n")
    (home / "canary.bin").write_bytes(b"\x00exact canary bytes\xff")
    (home / "empty-dir").mkdir()
    (home / "canary-link").symlink_to("canary.bin")
    before = _tree_snapshot(home)

    proc = _run_dispatch_boundary_probe(home)

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert proc.stdout.count("\n") == 1
    payload = json.loads(proc.stdout)
    assert payload == {
        "schema_version": 1,
        "contract": "hermes-kanban-dispatch-boundary",
        "state": "verified",
        "probe_scope": "temporary_shared_root",
        "shared_halt_path": "state/halt.json",
        "live_writes_performed": False,
        "checks": {
            "absent_brakes_allow": True,
            "dispatch_pause_regular_blocks": True,
            "dispatch_pause_broken_symlink_blocks": True,
            "halt_regular_blocks": True,
            "halt_broken_symlink_blocks": True,
            "profile_shared_root_halt_blocks": True,
            "lookup_errors_fail_closed": True,
            "halt_blocks_final_spawn_edge": True,
            "estop_blocks_final_spawn_edge": True,
            "halt_blocks_gateway_auto_decompose_edge": True,
        },
    }
    assert _tree_snapshot(home) == before


@pytest.mark.linux_only
def test_dispatch_boundary_probe_rejects_args_before_runtime_import(tmp_path):
    home = tmp_path / "live-hermes-with-extra-arg"
    home.mkdir()
    (home / "canary.bin").write_bytes(b"must remain exact")
    before = _tree_snapshot(home)

    proc = _run_dispatch_boundary_probe(home, "--unexpected")

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert proc.stderr == ""
    assert _tree_snapshot(home) == before


def test_removed_kanban_dispatch_boundary_surface_cannot_claim_verification(
    tmp_path,
):
    home = tmp_path / "removed-command-hermes"
    home.mkdir()

    proc = _run_hermes(
        home,
        "kanban",
        "dispatch-boundary",
        "--self-test",
        "--json",
    )

    assert proc.returncode != 0
    assert "hermes-kanban-dispatch-boundary" not in proc.stdout
