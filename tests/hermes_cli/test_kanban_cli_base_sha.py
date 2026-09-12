"""Exercise immutable worktree bases through parsed CLI args and a real DB."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


# These fixtures audit workspace lifecycle and ownership without delivering code.
_WORKSPACE_AUDIT_CONTRACT = """```acceptance-contract
domain: ops
target: artifact-file
tier1:
  - cmd: "test -d ."
    expect_exit: 0
tier2:
  - "The workspace audit preserves its base, ownership and lifecycle evidence."
tier3: "Local filesystem audit complete; no repository change is delivered."
```"""


BASE_SHA = "a" * 40


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in (
        "HERMES_KANBAN_HOME", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    kb.init_db()
    return home


def _command(capsys, *arguments):
    parser = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", *arguments])
    code = kc.kanban_command(args)
    output = capsys.readouterr()
    return code, output.out, output.err


@pytest.mark.parametrize("value", [
    "", " ", "main", "a" * 7, "a" * 39, "a" * 41,
    "A" * 40, "g" * 40, "0" * 40, " " + BASE_SHA,
    BASE_SHA + " ", BASE_SHA + "\n", BASE_SHA[:20] + " " + BASE_SHA[20:],
])
def test_invalid_base_sha_does_not_create_task(kanban_home, capsys, value):
    code, output, error = _command(
        capsys, "create", "invalid base", "--workspace", "worktree",
        "--base-sha", value, "--json",
    )

    assert code == 2
    assert not output
    assert "--base-sha" in error
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("workspace", ["scratch", "dir"])
def test_base_sha_requires_resolved_worktree(kanban_home, tmp_path, capsys, workspace):
    flag = f"dir:{tmp_path}" if workspace == "dir" else workspace
    code, output, error = _command(
        capsys, "create", "non-worktree base", "--workspace", flag,
        "--base-sha", BASE_SHA, "--json",
    )

    assert code != 0
    assert not output
    assert "worktree" in error
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("pinned_path", [False, True])
def test_explicit_base_roundtrips_create_show_and_list(
    kanban_home, tmp_path, capsys, pinned_path,
):
    workspace = f"worktree:{tmp_path / 'checkout'}" if pinned_path else "worktree"
    code, output, error = _command(
        capsys, "create", "parent continuation", "--workspace", workspace,
        "--base-sha", BASE_SHA, "--branch", "wt/parent-continuation", "--json",
    )
    assert code == 0, error
    created = json.loads(output)
    assert created["worktree_base_sha"] == BASE_SHA
    assert created["branch_name"] == "wt/parent-continuation"
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, created["id"]).worktree_base_sha == BASE_SHA

    code, output, error = _command(capsys, "show", created["id"], "--json")
    assert code == 0, error
    assert json.loads(output)["task"]["worktree_base_sha"] == BASE_SHA

    code, output, error = _command(capsys, "show", created["id"])
    assert code == 0, error
    assert f"worktree_base_sha: {BASE_SHA}" in output

    code, output, error = _command(capsys, "list", "--json")
    assert code == 0, error
    assert next(t for t in json.loads(output) if t["id"] == created["id"])[
        "worktree_base_sha"
    ] == BASE_SHA


@pytest.mark.parametrize("project_source", ["explicit", "board"])
def test_project_inferred_worktree_accepts_base(
    kanban_home, tmp_path, capsys, project_source,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pdb.connect_closing() as conn:
        project_id = pdb.create_project(conn, name="Parent Project", primary_path=str(repo))
    arguments = ["create", "project child", "--base-sha", BASE_SHA, "--json"]
    if project_source == "explicit":
        arguments.extend(["--project", project_id])
    else:
        kb.write_board_metadata("default", project_id=project_id)

    code, output, error = _command(capsys, *arguments)

    assert code == 0, error
    task = json.loads(output)
    assert task["workspace_kind"] == "worktree"
    assert task["project_id"] == project_id
    assert task["worktree_base_sha"] == BASE_SHA
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, task["id"]).worktree_base_sha == BASE_SHA


@pytest.mark.parametrize("workspace", ["scratch", "worktree"])
def test_omitted_base_is_unselected_at_creation(kanban_home, capsys, workspace):
    code, output, error = _command(
        capsys, "create", "default base", "--workspace", workspace, "--json",
    )
    assert code == 0, error
    task = json.loads(output)
    assert task["worktree_base_sha"] is None
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, task["id"]).worktree_base_sha is None


def _git(repo, *arguments):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=CLI Test",
         "-c", "user.email=cli-test@example.invalid", "-c", "commit.gpgsign=false",
         *arguments],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_claim_persists_default_main_base_without_inheriting_feature_head(
    kanban_home, tmp_path, capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "base.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "main base")
    main_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "feature/unrelated")
    (repo / "feature.txt").write_text("not part of the child\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "unrelated feature")
    feature_sha = _git(repo, "rev-parse", "HEAD")
    workspace = repo / ".worktrees" / "child"

    code, output, error = _command(
        capsys, "create", "claim base", "--workspace", f"worktree:{workspace}",
        "--body", _WORKSPACE_AUDIT_CONTRACT,
        "--initial-status", "blocked", "--json",
    )
    assert code == 0, error
    task = json.loads(output)
    assert task["worktree_base_sha"] is None
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task["id"],))
        conn.commit()

    code, output, error = _command(capsys, "claim", task["id"])

    assert code == 0, error
    assert f"Claimed {task['id']}" in output
    with kb.connect_closing() as conn:
        claimed = kb.get_task(conn, task["id"])
    assert claimed.worktree_base_sha == main_sha
    assert _git(Path(claimed.workspace_path), "rev-parse", "HEAD") == main_sha
    assert not (Path(claimed.workspace_path) / "feature.txt").exists()
    assert _git(repo, "rev-parse", "HEAD") == feature_sha
