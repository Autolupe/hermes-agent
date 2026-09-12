"""Focused acceptance helpers restored from protected source 339a5398.

Only temporary local fixtures are used. Live service probes and legacy worker
execution tests remain outside this support-module integration slice.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import dependency_provisioner as provisioner
from tools.environments import local


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/nonexistent-test-home",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _private_bare_mirror(source: Path, destination: Path) -> Path:
    _git("clone", "--bare", "--no-hardlinks", "--quiet", str(source), str(destination))
    for current, directories, files in os.walk(destination):
        os.chmod(current, 0o770)
        for name in directories:
            os.chmod(Path(current) / name, 0o770)
        for name in files:
            os.chmod(Path(current) / name, 0o660)
    return destination


def _tree_content_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        entry_stat = path.lstat()
        digest.update(relative + b"\0")
        if path.is_symlink():
            digest.update(b"link\0" + os.readlink(path).encode() + b"\n")
        elif path.is_dir():
            digest.update(b"dir\n")
        else:
            digest.update(b"file\0" + path.read_bytes() + b"\n")
    return digest.hexdigest()


def _python_runtime_attestation() -> dict[str, object]:
    runtime = provisioner._expected_python_runtime_binding()
    return {
        "python_runtime": runtime,
        "python_runtime_sha256": hashlib.sha256(
            provisioner._canonical_json(runtime) + b"\n"
        ).hexdigest(),
    }


def _dependency_worktree_fixture(
    monkeypatch,
    tmp_path: Path,
) -> tuple[Path, Path, Path, str]:
    """Create a fresh linked worktree plus its immutable fixture snapshot."""
    source = tmp_path / "dependency-source"
    source.mkdir()
    _git("init", "--quiet", "--initial-branch=main", cwd=source)
    (source / "backend").mkdir()
    (source / "package.json").write_text(
        '{"name":"sandbox-fixture","private":true}\n',
        encoding="utf-8",
    )
    (source / "package-lock.json").write_text(
        '{"name":"sandbox-fixture","lockfileVersion":3,"packages":{}}\n',
        encoding="utf-8",
    )
    (source / "backend" / "pyproject.toml").write_text(
        "[project]\nname='sandbox-fixture'\nversion='0.0.0'\n"
        "requires-python='>=3.11'\ndependencies=[]\n"
        "[project.optional-dependencies]\ndev=[]\n",
        encoding="utf-8",
    )
    (source / "backend" / "uv.lock").write_text(
        "version = 1\nrevision = 3\nrequires-python = '>=3.11'\n",
        encoding="utf-8",
    )
    (source / "backend" / "test_fixture.py").write_text(
        "import unittest\nimport fixture_dep\n\n"
        "class FixtureTest(unittest.TestCase):\n"
        "    def test_dependency(self):\n"
        "        self.assertEqual(fixture_dep.VALUE, 'python-ok')\n",
        encoding="utf-8",
    )
    _git("add", ".", cwd=source)
    _git(
        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "--quiet", "-m", "dependency fixture", cwd=source,
    )
    workspace = tmp_path / "dependency-worker"
    branch = "hermes/dependency-fixture"
    _git("worktree", "add", "--quiet", "-b", branch, str(workspace), cwd=source)

    snapshot_root = tmp_path / "trusted-dependencies"
    snapshot_root.mkdir(mode=0o755)
    monkeypatch.setattr(local, "_DEPENDENCY_SNAPSHOT_ROOT", snapshot_root)
    monkeypatch.setattr(local, "_DEPENDENCY_SNAPSHOT_OWNER_UID", os.getuid())
    node_hashes = local._candidate_manifest_hashes(
        workspace,
        ("package.json", "package-lock.json"),
        label="Node",
    )
    python_hashes = local._candidate_manifest_hashes(
        workspace,
        ("backend/pyproject.toml", "backend/uv.lock"),
        label="backend Python",
    )
    manifest_hashes = node_hashes + python_hashes
    manifest_digest = local._dependency_manifest_digest(workspace, manifest_hashes)
    snapshot = snapshot_root / manifest_digest
    node_modules = snapshot / "node_modules" / "fixture-dep"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text(
        "module.exports = {value: 'node-ok'};\n",
        encoding="utf-8",
    )
    venv = snapshot / "backend" / ".venv"
    venv.parent.mkdir(parents=True)
    proc = subprocess.run(
        ["/usr/bin/python3", "-m", "venv", "--without-pip", "--copies", str(venv)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    site_packages = next((venv / "lib").glob("python*/site-packages"))
    (site_packages / "fixture_dep.py").write_text(
        "VALUE = 'python-ok'\n",
        encoding="utf-8",
    )

    # Simulate the root provisioner's normalized immutable modes. The test
    # runner deliberately uses a permissive umask, so mkdir defaults alone are
    # not a trustworthy fixture for the production ownership contract.
    for current_root, directory_names, file_names in os.walk(snapshot):
        for name in directory_names:
            path = Path(current_root) / name
            if not path.is_symlink():
                os.chmod(path, 0o755)
        for name in file_names:
            path = Path(current_root) / name
            if not path.is_symlink():
                executable = bool(path.stat().st_mode & 0o111)
                os.chmod(path, 0o755 if executable else 0o644)
    os.chmod(snapshot, 0o755)
    os.chmod(snapshot_root, 0o755)

    node_entries, node_bytes = local._validate_dependency_snapshot_tree(
        snapshot / "node_modules",
        label="Node",
    )
    python_entries, python_bytes = local._validate_dependency_snapshot_tree(
        venv,
        label="backend Python",
    )
    attestation = {
        "version": provisioner.SNAPSHOT_ATTESTATION_VERSION,
        "build_policy": provisioner.DEPENDENCY_BUILD_POLICY,
        "manifest_sha256": manifest_digest,
        "manifests": {
            path.relative_to(workspace).as_posix(): digest
            for path, digest in manifest_hashes
        },
        "trees": {
            "node_modules": {
                "sha256": _tree_content_digest(snapshot / "node_modules"),
                "entries": node_entries,
                "bytes": node_bytes,
            },
            "backend/.venv": {
                "sha256": _tree_content_digest(venv),
                "entries": python_entries,
                "bytes": python_bytes,
                "install_mode": provisioner.PYTHON_INSTALL_MODE,
            },
        },
        **_python_runtime_attestation(),
    }
    (snapshot / "attestation.json").write_text(
        json.dumps(attestation, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(snapshot / "attestation.json", 0o644)
    return source, workspace, snapshot, branch


def test_worker_terminal_env_is_clean_and_home_scoped(monkeypatch, tmp_path):
    home = tmp_path / "sandbox-home"
    home.mkdir()
    clean = local.build_delivery_sandbox_environment(
        {
            "LANG": "C.UTF-8",
            "GIT_AUTHOR_NAME": "Hermes",
            "GITHUB_TOKEN": "broad-token",
            "GH_TOKEN": "broad-token",
            "HERMES_HOME": "/home/operator/.hermes/profiles/builder",
            "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
            "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
            "LD_PRELOAD": "/home/operator/escape.so",
        },
        home,
    )

    assert clean["HOME"] == str(home)
    assert clean["GH_CONFIG_DIR"].startswith(str(home))
    assert clean["HERMES_WORKER_NETWORK_POLICY"] == "isolated-no-network"
    assert clean["OPENSSL_CONF"] == "/dev/null"
    assert clean["GIT_ATTR_NOSYSTEM"] == "1"
    assert clean["GIT_AUTHOR_NAME"] == "Hermes"
    assert clean["GIT_COMMITTER_NAME"] == "Hermes"
    assert clean["LANG"] == "C.UTF-8"
    assert clean["TZ"] == "UTC"
    for forbidden in (
        "GITHUB_TOKEN", "GH_TOKEN", "HERMES_HOME", "SSH_AUTH_SOCK",
        "DOCKER_HOST", "LD_PRELOAD",
    ):
        assert forbidden not in clean


def test_delivery_client_environment_uses_own_user_manager(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/foreign-manager")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/foreign-runtime")
    monkeypatch.setenv("GH_TOKEN", "fixture-token")

    environment = local.delivery_systemd_client_environment()

    runtime = f"/run/user/{os.getuid()}"
    assert environment["XDG_RUNTIME_DIR"] == runtime
    assert environment["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime}/bus"
    assert environment["HOME"] == str(tmp_path)
    assert "GH_TOKEN" not in environment


def test_worker_systemd_argv_has_required_boundaries(tmp_path):
    workspace = tmp_path / "workspace"
    sandbox_home = tmp_path / "home"
    workspace.mkdir()
    sandbox_home.mkdir()
    argv = local._worker_systemd_argv(
        command=["/bin/true"],
        workspace=workspace,
        sandbox_home=sandbox_home,
        run_env={"HOME": str(sandbox_home)},
        timeout=60,
        unit_name="hermes-test-unit",
        linked_git=None,
    )

    properties = {
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == "-p"
    }
    assert "PrivatePIDs=yes" in properties
    assert "ProtectProc=invisible" in properties
    assert "ProtectHome=tmpfs" in properties
    assert "ProtectSystem=strict" in properties
    assert "PrivateTmp=yes" in properties
    assert "PrivateDevices=yes" in properties
    assert "NoNewPrivileges=yes" in properties
    assert "CapabilityBoundingSet=" in properties
    assert "RestrictNamespaces=yes" in properties
    assert "PrivateNetwork=yes" in properties
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in properties
    assert f"BindPaths={workspace}" in properties
    assert f"ReadWritePaths={workspace}" in properties
    assert f"BindPaths={sandbox_home}" in properties
    inaccessible = " ".join(
        p for p in properties if p.startswith("InaccessiblePaths=")
    )
    assert "TemporaryFileSystem=/var:ro,nodev,nosuid,noexec,mode=0755" in properties
    assert not inaccessible.startswith("InaccessiblePaths=InaccessiblePaths=")
    assert "-/run" in inaccessible
    assert "-/var" not in inaccessible
    assert "-/etc" in inaccessible
    assert "-/sys/fs/cgroup" in inaccessible


def test_acceptance_projection_uses_exact_mirror_blobs_without_shared_git_mutation(
    tmp_path,
):
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "--quiet", "--initial-branch=main", cwd=source)
    tracked = source / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=source)
    _git(
        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "--quiet", "-m", "base", cwd=source,
    )
    base_sha = _git("rev-parse", "HEAD", cwd=source)
    (source / ".gitattributes").write_text(
        "tracked.txt filter=host-command\n", encoding="utf-8",
    )
    tracked.write_text("candidate\n", encoding="utf-8")
    (source / "inside-link").symlink_to("tracked.txt")
    _git("add", ".gitattributes", "tracked.txt", "inside-link", cwd=source)
    _git(
        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "--quiet", "-m", "candidate", cwd=source,
    )
    candidate_sha = _git("rev-parse", "HEAD", cwd=source)
    mirror = _private_bare_mirror(source, tmp_path / "repository.git")
    sentinel = tmp_path / "filter-executed"
    _git(
        f"--git-dir={mirror}", "config", "filter.host-command.clean",
        f"sh -c 'touch {sentinel}; cat'",
    )
    _git(
        f"--git-dir={mirror}", "config", "filter.host-command.smudge",
        f"sh -c 'touch {sentinel}; cat'",
    )
    before_refs = _git(f"--git-dir={mirror}", "show-ref")
    acceptance_root = tmp_path / "acceptance"
    acceptance_root.mkdir(mode=0o700)

    with local.delivery_acceptance_candidate(
        task_id="t_0123abcd",
        candidate_sha=candidate_sha,
        base_sha=base_sha,
        base_ref="main",
        mirror=mirror,
        acceptance_root=acceptance_root,
    ) as (workspace, sandbox_home, overlay):
        session_root = workspace.parent
        assert tracked.read_text(encoding="utf-8") == "candidate\n"
        assert (workspace / "tracked.txt").read_text(encoding="utf-8") == "candidate\n"
        assert os.readlink(workspace / "inside-link") == "tracked.txt"
        assert not sentinel.exists()
        assert overlay.common_dir == mirror
        assert _git("-C", str(workspace), "rev-parse", "HEAD") == candidate_sha
        assert _git(
            "-C", str(workspace), "rev-parse", "origin/main",
        ) == base_sha
        assert sandbox_home.is_dir()

    assert not session_root.exists()
    assert _git(f"--git-dir={mirror}", "show-ref") == before_refs
    assert not (mirror / "worktrees").exists()
    assert not sentinel.exists()


def test_acceptance_projection_rejects_symlink_escape_and_cleans_session(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "--quiet", "--initial-branch=main", cwd=source)
    (source / "base.txt").write_text("base\n", encoding="utf-8")
    _git("add", "base.txt", cwd=source)
    _git(
        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "--quiet", "-m", "base", cwd=source,
    )
    base_sha = _git("rev-parse", "HEAD", cwd=source)
    (source / "escape").symlink_to("../controller-secret")
    _git("add", "escape", cwd=source)
    _git(
        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "--quiet", "-m", "escape", cwd=source,
    )
    candidate_sha = _git("rev-parse", "HEAD", cwd=source)
    mirror = _private_bare_mirror(source, tmp_path / "repository.git")
    acceptance_root = tmp_path / "acceptance"
    acceptance_root.mkdir(mode=0o700)

    with pytest.raises(local.WorkerTerminalSandboxError, match="symlink escapes"):
        with local.delivery_acceptance_candidate(
            task_id="t_0123abcd",
            candidate_sha=candidate_sha,
            base_sha=base_sha,
            mirror=mirror,
            acceptance_root=acceptance_root,
        ):
            pytest.fail("unsafe candidate must not be yielded")

    assert list(acceptance_root.iterdir()) == []


def test_worker_systemd_mount_grammar_fails_closed(tmp_path):
    workspace = tmp_path / "workspace:etc"
    sandbox_home = tmp_path / "home"
    workspace.mkdir()
    sandbox_home.mkdir()

    with pytest.raises(local.WorkerTerminalSandboxError, match="represented safely"):
        local._worker_systemd_argv(
            command=["/bin/true"],
            workspace=workspace,
            sandbox_home=sandbox_home,
            run_env={"HOME": str(sandbox_home)},
            timeout=60,
            unit_name="hermes-test-unit",
            linked_git=None,
        )


@pytest.mark.parametrize("escape_kind", ["symlink", "hardlink", "pth", "writable"])
def test_dependency_snapshot_rejects_injection_metadata(
    monkeypatch,
    tmp_path,
    escape_kind,
):
    _source, _workspace, snapshot, _branch = _dependency_worktree_fixture(
        monkeypatch,
        tmp_path,
    )
    node_tree = snapshot / "node_modules"
    python_tree = snapshot / "backend" / ".venv"
    if escape_kind == "symlink":
        (node_tree / "escape").symlink_to("/etc/passwd")
        tree, label = node_tree, "Node"
    elif escape_kind == "hardlink":
        os.link(
            node_tree / "fixture-dep" / "index.js",
            node_tree / "fixture-dep" / "hard-linked.js",
        )
        tree, label = node_tree, "Node"
    elif escape_kind == "pth":
        site_packages = next((python_tree / "lib").glob("python*/site-packages"))
        (site_packages / "escape.pth").write_text("/home/ab\n", encoding="utf-8")
        tree, label = python_tree, "backend Python"
    else:
        target = node_tree / "fixture-dep" / "index.js"
        os.chmod(target, 0o664)
        tree, label = node_tree, "Node"

    with pytest.raises(local.WorkerTerminalSandboxError, match="dependency provisioning required"):
        local._validate_dependency_snapshot_tree(tree, label=label)


def test_acceptance_systemd_argv_mounts_candidate_and_git_metadata_read_only(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "--quiet", "--initial-branch=main", cwd=source)
    (source / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=source)
    _git(
        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "--quiet", "-m", "base", cwd=source,
    )
    base_sha = _git("rev-parse", "HEAD", cwd=source)
    mirror = _private_bare_mirror(source, tmp_path / "repository.git")
    acceptance_root = tmp_path / "acceptance"
    acceptance_root.mkdir(mode=0o700)

    with local.delivery_acceptance_candidate(
        task_id="t_0123abcd",
        candidate_sha=base_sha,
        base_sha=base_sha,
        base_ref="main",
        mirror=mirror,
        acceptance_root=acceptance_root,
    ) as (workspace, sandbox_home, overlay):
        argv = local.build_delivery_sandbox_argv(
            command=["/bin/true"],
            workspace=workspace,
            sandbox_home=sandbox_home,
            run_env={"HOME": str(sandbox_home)},
            timeout=60,
            unit_name="hermes-acceptance-test-unit",
            workspace_writable=False,
            git_overlay=overlay,
        )
        properties = {
            argv[index + 1]
            for index, value in enumerate(argv[:-1])
            if value == "-p"
        }
        assert f"BindReadOnlyPaths={workspace}" in properties
        assert f"BindPaths={workspace}" not in properties
        assert f"ReadWritePaths={workspace}" not in properties
        assert f"BindReadOnlyPaths={overlay.private_gitdir}" in properties
        assert f"BindReadOnlyPaths={overlay.common_objects}" in properties
        assert (
            f"BindReadOnlyPaths={overlay.pointer_file}:{workspace / '.git'}"
            in properties
        )


def test_dependency_projection_resolves_exact_trusted_snapshot(monkeypatch, tmp_path):
    _source, workspace, snapshot, _branch = _dependency_worktree_fixture(
        monkeypatch, tmp_path,
    )

    projections = local.resolve_trusted_dependency_projections(workspace)

    assert {(item.source, item.destination) for item in projections} == {
        (snapshot / "node_modules", workspace / "node_modules"),
        (snapshot / "backend" / ".venv", workspace / "backend" / ".venv"),
    }


def test_dependency_snapshot_mismatch_fails_closed(monkeypatch, tmp_path):
    _source, workspace, _snapshot, _branch = _dependency_worktree_fixture(
        monkeypatch, tmp_path,
    )
    (workspace / "package.json").write_text(
        '{"name":"changed-after-provisioning","private":true}\n',
        encoding="utf-8",
    )

    with pytest.raises(
        local.WorkerTerminalSandboxError,
        match="dependency provisioning required",
    ):
        local.resolve_trusted_dependency_projections(workspace)


def test_dependency_projection_destination_symlink_fails_closed(monkeypatch, tmp_path):
    _source, workspace, _snapshot, _branch = _dependency_worktree_fixture(
        monkeypatch, tmp_path,
    )
    outside = tmp_path / "outside-dependencies"
    outside.mkdir()
    (workspace / "node_modules").symlink_to(outside, target_is_directory=True)

    with pytest.raises(local.WorkerTerminalSandboxError, match="destination is symlinked"):
        local.resolve_trusted_dependency_projections(workspace)
    assert not list(outside.iterdir())
