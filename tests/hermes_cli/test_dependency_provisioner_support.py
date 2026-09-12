"""Protected dependency snapshot tests selected from immutable339a source.

Temporary manifests and copied trees only. Live provisioning, systemd probes,
real project manifests and network build tests are deliberately separate.
"""

from __future__ import annotations

import contextlib

import base64

import hashlib

import json

import os

import sqlite3

import stat

import subprocess

import sys

import time

from types import SimpleNamespace

from pathlib import Path

import pytest

from hermes_cli import dependency_provisioner as provisioner

from tools.environments import local

def _candidate(tmp_path: Path) -> tuple[Path, Path, str]:
    project = tmp_path / "clauseye"
    worktrees = project / ".worktrees"
    task_id = "t_0123abcd"
    workspace = worktrees / task_id
    (workspace / "backend").mkdir(parents=True)
    for directory in (project, worktrees, workspace, workspace / "backend"):
        os.chmod(directory, 0o755)
    (workspace / "package.json").write_text(
        json.dumps({
            "name": "dependency-fixture",
            "version": "1.0.0",
            "private": True,
            "scripts": {"postinstall": "exit 91"},
        }) + "\n",
        encoding="utf-8",
    )
    (workspace / "package-lock.json").write_text(
        json.dumps({
            "name": "dependency-fixture",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {"name": "dependency-fixture", "version": "1.0.0"},
            },
        }) + "\n",
        encoding="utf-8",
    )
    (workspace / "backend" / "pyproject.toml").write_text(
        "[project]\nname='dependency-fixture'\nversion='1.0.0'\n"
        "requires-python='>=3.11'\ndependencies=[]\n"
        "[project.optional-dependencies]\n"
        "dev=[\"six==1.17.0; python_version < '1'\"]\n",
        encoding="utf-8",
    )
    (workspace / "backend" / "uv.lock").write_text(
        "version = 1\nrevision = 3\nrequires-python = '>=3.11'\n"
        "[[package]]\nname='dependency-fixture'\nversion='1.0.0'\n"
        "source={editable='.'}\n[package.optional-dependencies]\ndev=[]\n"
        "[package.metadata]\n"
        "requires-dist=[{name='six',marker=\"python_full_version < '1' and extra == 'dev'\",specifier='==1.17.0'}]\n"
        "provides-extras=['dev']\n",
        encoding="utf-8",
    )
    return project, workspace, task_id

def _bundle(tmp_path: Path) -> provisioner.ManifestBundle:
    project, workspace, task_id = _candidate(tmp_path)
    return provisioner.collect_manifest_bundle(
        task_id,
        workspace,
        project_root=project,
        worker_uid=os.getuid(),
    )

def _through_proc_self_root(path: Path) -> Path:
    return Path("/proc/self/root") / path.resolve(strict=True).relative_to("/")

def _seal_dependency_fixture(root: Path) -> None:
    for current_root, directory_names, file_names in os.walk(root):
        Path(current_root).chmod(0o755)
        for name in [*directory_names, *file_names]:
            path = Path(current_root) / name
            if path.is_symlink():
                continue
            path.chmod(0o755 if path.is_dir() else 0o644)

def _python_import_alias_fixture(root: Path, alias_layer: str) -> None:
    if alias_layer == "lib":
        site_packages = root / "payload" / "python3.13" / "site-packages"
        site_packages.mkdir(parents=True)
        (root / "lib").symlink_to("payload", target_is_directory=True)
    elif alias_layer == "python":
        site_packages = root / "payload" / "python3.13" / "site-packages"
        site_packages.mkdir(parents=True)
        (root / "lib").mkdir()
        (root / "lib" / "python3.13").symlink_to(
            "../payload/python3.13",
            target_is_directory=True,
        )
    else:
        site_packages = root / "payload" / "site-packages"
        site_packages.mkdir(parents=True)
        version = root / "lib" / "python3.13"
        version.mkdir(parents=True)
        (version / "site-packages").symlink_to(
            "../../payload/site-packages",
            target_is_directory=True,
        )
    (site_packages / "sitecustomize.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

def test_registry_hosts_are_readable_under_broker_umask(tmp_path):
    resolution = {
        hostname: ("104.16.0.34",)
        for hostname in provisioner.ALLOWED_REGISTRY_HOSTS
    }
    input_root = tmp_path / "request"
    input_root.mkdir()
    previous_umask = os.umask(0o077)
    try:
        hosts, nsswitch = provisioner._write_registry_hosts(
            input_root,
            resolution,
        )
    finally:
        os.umask(previous_umask)

    assert hosts.stat().st_mode & 0o777 == 0o644
    assert nsswitch.stat().st_mode & 0o777 == 0o644
    assert hosts.parent.stat().st_mode & 0o777 == 0o755
    assert "registry.npmjs.org" in hosts.read_text(encoding="ascii")
    assert nsswitch.read_bytes() == b"hosts: files\n"

def test_collect_bundle_is_exact_and_rejects_manifest_symlink(tmp_path):
    bundle = _bundle(tmp_path)
    assert bundle.has_node is True
    assert bundle.has_python is True
    assert bundle.digest == provisioner._manifest_digest(bundle.hashes)
    assert set(bundle.files) == {
        "package.json", "package-lock.json",
        "backend/pyproject.toml", "backend/uv.lock",
    }

    lock = bundle.workspace / "backend" / "uv.lock"
    original = lock.read_bytes()
    lock.unlink()
    target = tmp_path / "outside.lock"
    target.write_bytes(original)
    lock.symlink_to(target)
    with pytest.raises(provisioner.DependencyProvisionError, match="symlinked|missing"):
        provisioner.collect_manifest_bundle(
            bundle.task_id,
            bundle.workspace,
            project_root=bundle.project_root,
            worker_uid=bundle.worker_uid,
        )

@pytest.mark.parametrize(
    ("manifest", "replacement", "message"),
    [
        (
            "package-lock.json",
            json.dumps({
                "lockfileVersion": 3,
                "packages": {"node_modules/evil": {"resolved": "http://169.254.169.254/pkg.tgz"}},
            }),
            "allowlist",
        ),
        (
            "backend/uv.lock",
            "version=1\n[[package]]\nname='evil'\nversion='1'\nsource={git='https://github.com/evil/repo'}\n",
            "local, Git",
        ),
    ],
)
def test_bundle_rejects_non_registry_or_local_sources(
    tmp_path,
    manifest,
    replacement,
    message,
):
    project, workspace, task_id = _candidate(tmp_path)
    (workspace / manifest).write_text(replacement, encoding="utf-8")
    with pytest.raises(provisioner.DependencyProvisionError, match=message):
        provisioner.collect_manifest_bundle(
            task_id, workspace, project_root=project, worker_uid=os.getuid(),
        )

def test_npm_artifacts_require_integrity_and_bundles_require_pinned_parent():
    package = b'{"name":"fixture","private":true}'
    lock = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "fixture"},
            "node_modules/pkg": {
                "resolved": "https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz",
            },
        },
    }
    with pytest.raises(provisioner.DependencyProvisionError, match="integrity is missing"):
        provisioner._validate_node_manifests(package, json.dumps(lock).encode())

    integrity = "sha512-" + base64.b64encode(b"x" * 64).decode("ascii")
    lock["packages"]["node_modules/pkg"].update({
        "integrity": integrity,
        "bundleDependencies": ["child"],
    })
    lock["packages"]["node_modules/pkg/node_modules/child"] = {"inBundle": True}
    provisioner._validate_node_manifests(package, json.dumps(lock).encode())
    lock["packages"]["node_modules/orphan"] = {"inBundle": True}
    with pytest.raises(provisioner.DependencyProvisionError, match="pinned parent"):
        provisioner._validate_node_manifests(package, json.dumps(lock).encode())

@pytest.mark.parametrize(
    "artifact",
    [
        {"url": "https://files.pythonhosted.org/pkg.whl", "size": 1},
        {
            "url": "https://files.pythonhosted.org/pkg.whl",
            "hash": "sha256:" + "a" * 64,
        },
    ],
)
def test_uv_registry_artifacts_require_hash_and_size(artifact):
    pyproject = (
        b"[project]\nname='fixture'\nversion='1.0.0'\n"
        b"[project.optional-dependencies]\ndev=[]\n"
    )
    lock = (
        "version=1\n[[package]]\nname='dep'\nversion='1.0.0'\n"
        "source={registry='https://pypi.org/simple'}\n"
        f"wheels=[{json.dumps(artifact).replace(': ', '=')}]\n"
    )
    # JSON object syntax is not TOML; build the exact inline table explicitly.
    fields = ",".join(
        f"{key}={json.dumps(value)}" for key, value in artifact.items()
    )
    lock = (
        "version=1\n[[package]]\nname='dep'\nversion='1.0.0'\n"
        "source={registry='https://pypi.org/simple'}\n"
        f"wheels=[{{{fields}}}]\n"
    )
    with pytest.raises(provisioner.DependencyProvisionError, match="hash|size"):
        provisioner._validate_python_manifests(pyproject, lock.encode())

def test_dynamic_builder_unit_is_credential_free_and_registry_confined(
    monkeypatch,
    tmp_path,
):
    bundle = _bundle(tmp_path)
    monkeypatch.setattr(
        provisioner,
        "_trusted_executable",
        lambda path, expected_sha256=None: Path(path),
    )
    argv = provisioner.build_dynamic_worker_argv(
        bundle=bundle,
        input_root=tmp_path / "root-input",
        state_name="hermes-dependency-build-0123456789abcdef",
        addresses=("203.0.113.10", "2001:db8::10"),
    )
    properties = {
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == "-p"
    }
    assert "DynamicUser=yes" in properties
    assert "IPAddressDeny=any" in properties
    assert "IPAddressAllow=203.0.113.10" in properties
    assert "IPAddressAllow=2001:db8::10" in properties
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in properties
    assert "Type=notify" in properties
    assert "NotifyAccess=main" in properties
    assert not any(value.startswith("StateDirectory=") for value in properties)
    assert any(
        value.startswith("TemporaryFileSystem=/output:rw,nodev,nosuid,size=")
        for value in properties
    )
    assert f"LimitFSIZE={provisioner.MAX_TREE_FILE_BYTES}" in properties
    assert "PrivatePIDs=yes" not in properties
    assert "PrivateUsers=yes" in properties
    assert "ProtectProc=invisible" in properties
    assert "ProcSubset=pid" in properties
    assert (
        "TemporaryFileSystem=/run:ro,nodev,nosuid,noexec "
        "/etc:ro,nodev,nosuid,noexec"
    ) in properties
    inaccessible = " ".join(p for p in properties if p.startswith("InaccessiblePaths="))
    for hidden in (
        "-/home", "-/root",
        "-/var/lib/hermes-delivery-control", "-/var/lib/ucf",
        "-/var/lib/postgresql", "-/var/lib/docker", "-/var/cache",
    ):
        assert hidden in inaccessible
    rendered = " ".join(argv)
    for forbidden in ("GITHUB_TOKEN", "GH_TOKEN", "AWS_", "SSH_AUTH_SOCK", "HERMES_HOME"):
        assert forbidden not in rendered
    assert "--no-install-project" not in rendered  # fixed builder module owns package argv
    assert "/etc/resolv.conf" not in rendered
    assert "/.network/hosts:/etc/hosts" in rendered
    assert "/.network/nsswitch.conf:/etc/nsswitch.conf" in rendered
    command_index = argv.index(str(provisioner.BUILDER_PYTHON))
    assert argv[command_index:command_index + 4] == [
        str(provisioner.BUILDER_PYTHON), "-I", "-B",
        str(provisioner.BUILDER_ENTRYPOINT),
    ]
    assert "builder-entrypoint=isolated-no-bytecode" in (
        provisioner.DEPENDENCY_BUILD_POLICY
    )
    assert argv[command_index:command_index + 16].count("--python") == 1
    python_index = argv.index("--python", command_index)
    assert argv[python_index + 1] == str(provisioner.BUILDER_PYTHON)
    assert str(provisioner.BUILDER_PYTHON).startswith(
        "/usr/lib/hermes-delivery-control/"
    )
    assert "/usr/lib/hermes-worker-runtime" not in " ".join(argv)

def test_python_builder_uses_locked_dev_extra_and_exact_interpreter(
    monkeypatch,
    tmp_path,
):
    input_root = tmp_path / "input"
    (input_root / "backend").mkdir(parents=True)
    (input_root / "backend" / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='1'\n"
        "[project.optional-dependencies]\ndev=[]\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        venv_bin = output / "backend" / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_bytes(b"fixture")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(provisioner.subprocess, "run", fake_run)
    monkeypatch.setattr(
        provisioner,
        "_probe_python_runtime",
        lambda _python, *, expected_prefix: (
            provisioner._expected_python_runtime_binding()
        ),
    )
    provisioner.build_stage(
        input_root,
        output,
        node=Path("/fixture/node"),
        npm_cli=Path("/fixture/npm-cli.js"),
        uv=Path("/fixture/uv"),
        python=Path(sys.executable),
    )

    assert calls == [[
        "/fixture/uv", "sync", "--project", str(input_root / "backend"),
        "--frozen", "--extra", "dev", "--python", sys.executable,
        "--no-managed-python", "--no-python-downloads",
        "--no-install-project", "--no-install-workspace",
        "--no-install-local", "--no-editable", "--link-mode", "copy",
    ]]
    assert json.loads((output / provisioner.PYTHON_RUNTIME_METADATA).read_text()) == (
        provisioner._expected_python_runtime_binding()
    )

def test_runtime_probe_argv_disables_bytecode_in_all_modes(monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    expected = provisioner._expected_python_runtime_binding()

    def fake_run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return SimpleNamespace(returncode=0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(provisioner.subprocess, "run", fake_run)
    monkeypatch.setattr(
        provisioner,
        "_validate_python_runtime_payload",
        lambda _payload, *, expected_executable, expected_prefix: expected,
    )

    assert provisioner._probe_python_runtime(
        Path(sys.executable),
        expected_prefix=Path(sys.prefix),
    ) == expected
    assert provisioner._probe_python_runtime(
        Path(sys.executable),
        expected_prefix=Path(sys.base_prefix),
        no_site=True,
    ) == expected

    assert [call[0] for call in calls] == [
        [sys.executable, "-I", "-B", "-c", provisioner._PYTHON_RUNTIME_PROBE],
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            provisioner._PYTHON_RUNTIME_PROBE,
        ],
    ]
    assert all(call[1]["cwd"] == "/" for call in calls)

def test_python_runtime_probe_rejects_wrong_sqlite_source_id(
    monkeypatch,
):
    prefix = Path(sys.prefix)
    base_prefix = Path(sys.base_prefix)
    monkeypatch.setattr(
        provisioner,
        "TRUSTED_PYTHON_VERSION",
        ".".join(str(value) for value in sys.version_info[:3]),
    )
    monkeypatch.setattr(
        provisioner, "TRUSTED_SQLITE_VERSION", sqlite3.sqlite_version,
    )
    monkeypatch.setattr(provisioner, "TRUSTED_SQLITE_SOURCE_ID", "reviewed-source")
    monkeypatch.setattr(provisioner, "TRUSTED_PYTHON_BASE_PREFIX", base_prefix)
    payload = {
        "base_prefix": str(base_prefix),
        "dont_write_bytecode": True,
        "executable": sys.executable,
        "fts5": True,
        "prefix": str(prefix),
        "python_version": provisioner.TRUSTED_PYTHON_VERSION,
        "sqlite_origin": "built-in",
        "sqlite_source_id": "wrong-source",
        "sqlite_version": sqlite3.sqlite_version,
    }

    with pytest.raises(provisioner.DependencyProvisionError, match="SQLite policy"):
        provisioner._validate_python_runtime_payload(
            payload,
            expected_executable=Path(sys.executable),
            expected_prefix=prefix,
        )

def test_python_runtime_probe_rejects_bytecode_enabled(monkeypatch):
    prefix = Path(sys.prefix)
    base_prefix = Path(sys.base_prefix)
    monkeypatch.setattr(
        provisioner,
        "TRUSTED_PYTHON_VERSION",
        ".".join(str(value) for value in sys.version_info[:3]),
    )
    monkeypatch.setattr(
        provisioner, "TRUSTED_SQLITE_VERSION", sqlite3.sqlite_version,
    )
    monkeypatch.setattr(provisioner, "TRUSTED_SQLITE_SOURCE_ID", "reviewed-source")
    monkeypatch.setattr(provisioner, "TRUSTED_PYTHON_BASE_PREFIX", base_prefix)
    payload = {
        "base_prefix": str(base_prefix),
        "dont_write_bytecode": False,
        "executable": sys.executable,
        "fts5": True,
        "prefix": str(prefix),
        "python_version": provisioner.TRUSTED_PYTHON_VERSION,
        "sqlite_origin": "built-in",
        "sqlite_source_id": "reviewed-source",
        "sqlite_version": sqlite3.sqlite_version,
    }

    with pytest.raises(provisioner.DependencyProvisionError, match="no-bytecode"):
        provisioner._validate_python_runtime_payload(
            payload,
            expected_executable=Path(sys.executable),
            expected_prefix=prefix,
        )

def test_root_rejects_forged_builder_runtime_metadata(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    forged = provisioner._expected_python_runtime_binding()
    forged["sqlite_source_id"] = "forged-by-site-hook"
    (output / provisioner.PYTHON_RUNTIME_METADATA).write_bytes(
        provisioner._canonical_json(forged) + b"\n"
    )

    with pytest.raises(provisioner.DependencyProvisionError, match="reviewed policy"):
        provisioner._read_python_runtime_metadata(output)

@pytest.mark.parametrize("name", ["sitecustomize.py", "usercustomize.py"])
def test_promoter_rejects_python_startup_customization(tmp_path, name):
    venv = tmp_path / ".venv"
    site_packages = venv / "lib" / "python3.13" / "site-packages"
    site_packages.mkdir(parents=True)
    startup = site_packages / name
    startup.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")

    with pytest.raises(
        provisioner.DependencyProvisionError,
        match="startup customization",
    ):
        provisioner._validate_python_metadata(startup, venv)

def test_promoter_accepts_nested_python_startup_module(tmp_path):
    venv = tmp_path / "python-tree"
    nested = (
        venv
        / "lib"
        / "python3.13"
        / "site-packages"
        / "opentelemetry"
        / "instrumentation"
        / "auto_instrumentation"
        / "sitecustomize.py"
    )
    nested.parent.mkdir(parents=True)
    nested.write_text("VALUE = 1\n", encoding="utf-8")
    (venv / "lib64").symlink_to("lib", target_is_directory=True)

    destination = tmp_path / "copied-python-tree"
    provisioner._copy_tree_no_follow(
        venv,
        destination,
        python_tree=True,
        owner_uid=os.getuid(),
    )

    assert (destination / nested.relative_to(venv)).read_text(encoding="utf-8") == (
        "VALUE = 1\n"
    )

@pytest.mark.parametrize("entry_kind", ["file", "symlink", "package"])
def test_copy_rejects_top_level_python_startup_entry(tmp_path, entry_kind):
    source = tmp_path / "python-tree"
    site_packages = source / "lib" / "python3.13" / "site-packages"
    site_packages.mkdir(parents=True)
    startup = site_packages / "sitecustomize.py"
    if entry_kind == "file":
        startup.write_text("VALUE = 1\n", encoding="utf-8")
    elif entry_kind == "symlink":
        target = site_packages / "package" / "startup.py"
        target.parent.mkdir()
        target.write_text("VALUE = 1\n", encoding="utf-8")
        startup.symlink_to(target.relative_to(site_packages))
    else:
        startup = site_packages / "sitecustomize"
        startup.mkdir()
        (startup / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(
        provisioner.DependencyProvisionError,
        match="startup customization",
    ):
        provisioner._copy_tree_no_follow(
            source,
            tmp_path / "copied-python-tree",
            python_tree=True,
            owner_uid=os.getuid(),
        )

def test_consumer_accepts_nested_python_startup_module(monkeypatch, tmp_path):
    source = tmp_path / "python-tree"
    nested = (
        source
        / "lib"
        / "python3.13"
        / "site-packages"
        / "opentelemetry"
        / "instrumentation"
        / "auto_instrumentation"
        / "sitecustomize.py"
    )
    nested.parent.mkdir(parents=True)
    nested.write_text("VALUE = 1\n", encoding="utf-8")
    (source / "lib64").symlink_to("lib", target_is_directory=True)
    _seal_dependency_fixture(source)
    monkeypatch.setattr(local, "_DEPENDENCY_SNAPSHOT_OWNER_UID", os.getuid())

    entries, byte_count = local._validate_dependency_snapshot_tree(
        source,
        label="backend Python",
    )

    assert entries == 8
    assert byte_count == len(b"VALUE = 1\n")

@pytest.mark.parametrize("entry_kind", ["file", "symlink", "package"])
def test_consumer_rejects_top_level_python_startup_entry(
    monkeypatch,
    tmp_path,
    entry_kind,
):
    source = tmp_path / "python-tree"
    site_packages = source / "lib" / "python3.13" / "site-packages"
    site_packages.mkdir(parents=True)
    startup = site_packages / "usercustomize.py"
    if entry_kind == "file":
        startup.write_text("VALUE = 1\n", encoding="utf-8")
    elif entry_kind == "symlink":
        target = site_packages / "package" / "startup.py"
        target.parent.mkdir()
        target.write_text("VALUE = 1\n", encoding="utf-8")
        startup.symlink_to(target.relative_to(site_packages))
    else:
        startup = site_packages / "usercustomize"
        startup.mkdir()
        (startup / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    _seal_dependency_fixture(source)
    monkeypatch.setattr(local, "_DEPENDENCY_SNAPSHOT_OWNER_UID", os.getuid())

    with pytest.raises(
        local.WorkerTerminalSandboxError,
        match="startup customization",
    ):
        local._validate_dependency_snapshot_tree(source, label="backend Python")

@pytest.mark.parametrize("alias_layer", ["lib", "python", "site-packages"])
def test_copy_rejects_python_startup_through_import_root_alias(
    tmp_path,
    alias_layer,
):
    source = tmp_path / "python-tree"
    source.mkdir()
    _python_import_alias_fixture(source, alias_layer)

    with pytest.raises(
        provisioner.DependencyProvisionError,
        match="import-root layout is unsafe",
    ):
        provisioner._copy_tree_no_follow(
            source,
            tmp_path / "copied-python-tree",
            python_tree=True,
            owner_uid=os.getuid(),
        )

@pytest.mark.parametrize("alias_layer", ["lib", "python", "site-packages"])
def test_consumer_rejects_python_startup_through_import_root_alias(
    monkeypatch,
    tmp_path,
    alias_layer,
):
    source = tmp_path / "python-tree"
    source.mkdir()
    _python_import_alias_fixture(source, alias_layer)
    _seal_dependency_fixture(source)
    monkeypatch.setattr(local, "_DEPENDENCY_SNAPSHOT_OWNER_UID", os.getuid())

    with pytest.raises(
        local.WorkerTerminalSandboxError,
        match="unsafe import-root layout",
    ):
        local._validate_dependency_snapshot_tree(source, label="backend Python")

def test_copy_accepts_internal_npm_link_through_proc_root(tmp_path):
    source = tmp_path / "node_modules"
    package_bin = source / "fixture" / "bin"
    package_bin.mkdir(parents=True)
    (package_bin / "tool.js").write_text("console.log('ok')\n", encoding="utf-8")
    links = source / ".bin"
    links.mkdir()
    (links / "fixture-tool").symlink_to("../fixture/bin/tool.js")

    destination = tmp_path / "copied-node_modules"
    provisioner._copy_tree_no_follow(
        _through_proc_self_root(source),
        destination,
        python_tree=False,
        owner_uid=os.getuid(),
    )

    copied = destination / ".bin" / "fixture-tool"
    assert copied.is_symlink()
    assert os.readlink(copied) == "../fixture/bin/tool.js"
    assert copied.resolve(strict=True) == destination / "fixture" / "bin" / "tool.js"

def test_copy_accepts_internal_python_link_through_proc_root(tmp_path):
    source = tmp_path / "python-tree"
    library = source / "lib"
    library.mkdir(parents=True)
    (library / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "lib64").symlink_to("lib", target_is_directory=True)

    destination = tmp_path / "copied-python-tree"
    provisioner._copy_tree_no_follow(
        _through_proc_self_root(source),
        destination,
        python_tree=True,
        owner_uid=os.getuid(),
    )

    copied = destination / "lib64"
    assert copied.is_symlink()
    assert os.readlink(copied) == "lib"
    assert (copied / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"

def test_copy_dealiases_internal_hardlinks_through_proc_root(tmp_path):
    source = tmp_path / "node_modules"
    first = source / "platform-package" / "bin" / "tool"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"platform-binary\n")
    second = source / "wrapper-package" / "bin" / "tool"
    second.parent.mkdir(parents=True)
    os.link(first, second)
    first_info = first.stat()
    assert first_info.st_nlink == 2
    assert second.stat().st_ino == first_info.st_ino

    destination = tmp_path / "copied-node_modules"
    provisioner._copy_tree_no_follow(
        _through_proc_self_root(source),
        destination,
        python_tree=False,
        owner_uid=os.getuid(),
    )

    copied_first = destination / first.relative_to(source)
    copied_second = destination / second.relative_to(source)
    assert copied_first.read_bytes() == copied_second.read_bytes() == b"platform-binary\n"
    copied_first_info = copied_first.stat()
    copied_second_info = copied_second.stat()
    assert copied_first_info.st_nlink == copied_second_info.st_nlink == 1
    assert (copied_first_info.st_dev, copied_first_info.st_ino) != (
        copied_second_info.st_dev,
        copied_second_info.st_ino,
    )

def test_copy_rejects_outside_subtree_hardlink_alias(tmp_path):
    source = tmp_path / "node_modules"
    source.mkdir()
    outside = tmp_path / "package-cache-entry"
    outside.write_bytes(b"cached-package-bytes\n")
    linked = source / "package.js"
    os.link(outside, linked)
    assert outside.stat().st_nlink == linked.stat().st_nlink == 2

    destination = tmp_path / "copied-node_modules"
    with pytest.raises(
        provisioner.DependencyProvisionError,
        match="hardlink escapes the frozen builder tree",
    ):
        provisioner._copy_tree_no_follow(
            _through_proc_self_root(source),
            destination,
            python_tree=False,
            owner_uid=os.getuid(),
        )
    assert not destination.exists()
    assert outside.read_bytes() == linked.read_bytes() == b"cached-package-bytes\n"
    assert outside.stat().st_nlink == linked.stat().st_nlink == 2

def test_copy_rejects_relative_pth_exposing_nested_startup_module(tmp_path):
    source = tmp_path / "python-tree"
    site_packages = source / "lib" / "python3.13" / "site-packages"
    nested = site_packages / "nested"
    nested.mkdir(parents=True)
    (nested / "sitecustomize.py").write_text("VALUE = 1\n", encoding="utf-8")
    (site_packages / "fixture.pth").write_text(
        "nested\n",
        encoding="utf-8",
    )

    with pytest.raises(
        provisioner.DependencyProvisionError,
        match=r"\.pth metadata contains a path addition",
    ):
        provisioner._copy_tree_no_follow(
            _through_proc_self_root(source),
            tmp_path / "copied-python-tree",
            python_tree=True,
            owner_uid=os.getuid(),
        )

def test_consumer_rejects_relative_pth_exposing_nested_startup_module(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "python-tree"
    site_packages = source / "lib" / "python3.13" / "site-packages"
    nested = site_packages / "nested"
    nested.mkdir(parents=True)
    (nested / "sitecustomize.py").write_text("VALUE = 1\n", encoding="utf-8")
    (site_packages / "fixture.pth").write_text("nested\n", encoding="utf-8")
    _seal_dependency_fixture(source)
    monkeypatch.setattr(local, "_DEPENDENCY_SNAPSHOT_OWNER_UID", os.getuid())

    with pytest.raises(
        local.WorkerTerminalSandboxError,
        match=r"\.pth metadata contains a path addition",
    ):
        local._validate_dependency_snapshot_tree(source, label="backend Python")

def test_copy_rejects_symlinked_pth_path_addition(tmp_path):
    source = tmp_path / "python-tree"
    site_packages = source / "lib" / "python3.13" / "site-packages"
    nested = site_packages / "nested"
    nested.mkdir(parents=True)
    target = site_packages / "package-metadata"
    target.write_text("nested\n", encoding="utf-8")
    (site_packages / "fixture.pth").symlink_to(target.name)

    with pytest.raises(
        provisioner.DependencyProvisionError,
        match=r"\.pth metadata contains a path addition",
    ):
        provisioner._copy_tree_no_follow(
            source,
            tmp_path / "copied-python-tree",
            python_tree=True,
            owner_uid=os.getuid(),
        )

def test_consumer_rejects_symlinked_pth_path_addition(monkeypatch, tmp_path):
    source = tmp_path / "python-tree"
    site_packages = source / "lib" / "python3.13" / "site-packages"
    nested = site_packages / "nested"
    nested.mkdir(parents=True)
    target = site_packages / "package-metadata"
    target.write_text("nested\n", encoding="utf-8")
    (site_packages / "fixture.pth").symlink_to(target.name)
    _seal_dependency_fixture(source)
    monkeypatch.setattr(local, "_DEPENDENCY_SNAPSHOT_OWNER_UID", os.getuid())

    with pytest.raises(
        local.WorkerTerminalSandboxError,
        match=r"\.pth metadata contains a path addition",
    ):
        local._validate_dependency_snapshot_tree(source, label="backend Python")

@pytest.mark.parametrize(
    ("metadata_name", "payload", "message"),
    [
        ("fixture.egg-link", "ignored\n", "editable metadata"),
        ("__editable__.fixture.pth", "ignored\n", "editable metadata"),
        ("direct_url.json", '{"url":"file:///tmp/package"}\n', "local/editable"),
    ],
)
def test_copy_rejects_symlinked_python_injection_metadata(
    tmp_path,
    metadata_name,
    payload,
    message,
):
    source = tmp_path / "python-tree"
    site_packages = source / "lib" / "python3.13" / "site-packages"
    site_packages.mkdir(parents=True)
    target = site_packages / "package-metadata"
    target.write_text(payload, encoding="utf-8")
    (site_packages / metadata_name).symlink_to(target.name)

    with pytest.raises(provisioner.DependencyProvisionError, match=message):
        provisioner._copy_tree_no_follow(
            source,
            tmp_path / "copied-python-tree",
            python_tree=True,
            owner_uid=os.getuid(),
        )

@pytest.mark.parametrize(
    ("metadata_name", "payload", "message"),
    [
        ("fixture.egg-link", "ignored\n", "editable metadata"),
        ("__editable__.fixture.pth", "ignored\n", "editable metadata"),
        ("direct_url.json", '{"url":"file:///tmp/package"}\n', "local/editable"),
    ],
)
def test_consumer_rejects_symlinked_python_injection_metadata(
    monkeypatch,
    tmp_path,
    metadata_name,
    payload,
    message,
):
    source = tmp_path / "python-tree"
    site_packages = source / "lib" / "python3.13" / "site-packages"
    site_packages.mkdir(parents=True)
    target = site_packages / "package-metadata"
    target.write_text(payload, encoding="utf-8")
    (site_packages / metadata_name).symlink_to(target.name)
    _seal_dependency_fixture(source)
    monkeypatch.setattr(local, "_DEPENDENCY_SNAPSHOT_OWNER_UID", os.getuid())

    with pytest.raises(local.WorkerTerminalSandboxError, match=message):
        local._validate_dependency_snapshot_tree(source, label="backend Python")

def test_copy_rejects_leading_space_pth_path_addition(tmp_path):
    source = tmp_path / "python-tree"
    site_packages = source / "lib" / "python3.13" / "site-packages"
    injected = site_packages / " import nested"
    injected.mkdir(parents=True)
    (injected / "sitecustomize.py").write_text("VALUE = 1\n", encoding="utf-8")
    (site_packages / "fixture.pth").write_text(
        " import nested\n",
        encoding="utf-8",
    )

    with pytest.raises(
        provisioner.DependencyProvisionError,
        match=r"\.pth metadata contains a path addition",
    ):
        provisioner._copy_tree_no_follow(
            source,
            tmp_path / "copied-python-tree",
            python_tree=True,
            owner_uid=os.getuid(),
        )

def test_consumer_rejects_leading_space_pth_path_addition(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "python-tree"
    site_packages = source / "lib" / "python3.13" / "site-packages"
    injected = site_packages / " import nested"
    injected.mkdir(parents=True)
    (injected / "sitecustomize.py").write_text("VALUE = 1\n", encoding="utf-8")
    (site_packages / "fixture.pth").write_text(
        " import nested\n",
        encoding="utf-8",
    )
    _seal_dependency_fixture(source)
    monkeypatch.setattr(local, "_DEPENDENCY_SNAPSHOT_OWNER_UID", os.getuid())

    with pytest.raises(
        local.WorkerTerminalSandboxError,
        match=r"\.pth metadata contains a path addition",
    ):
        local._validate_dependency_snapshot_tree(source, label="backend Python")

def test_copy_relocates_python_shebang_through_proc_root(tmp_path):
    source = tmp_path / "python-tree"
    scripts = source / "bin"
    scripts.mkdir(parents=True)
    script = scripts / "fixture-tool"
    script.write_text(
        f"#!{source.resolve(strict=True)}/bin/python\nprint('ok')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    destination = tmp_path / "copied-python-tree"
    provisioner._copy_tree_no_follow(
        _through_proc_self_root(source),
        destination,
        python_tree=True,
        owner_uid=os.getuid(),
    )

    payload = (destination / "bin" / "fixture-tool").read_bytes()
    relocated_header = (
        b"#!/bin/sh\n"
        b"'''exec' \"$(dirname -- \"$0\")/python\" \"$0\" \"$@\"\n"
        b"' '''\n"
    )
    assert payload.startswith(relocated_header)
    assert str(source).encode("utf-8") not in payload

def test_copy_streams_large_python_bin_elf_executable(tmp_path):
    source = tmp_path / "python-tree"
    scripts = source / "bin"
    scripts.mkdir(parents=True)
    executable = scripts / "ruff"
    payload = b"\x7fELF" + b"\0" * provisioner.MAX_RELOCATABLE_SCRIPT_BYTES
    executable.write_bytes(payload)
    executable.chmod(0o755)

    destination = tmp_path / "copied-python-tree"
    provisioner._copy_tree_no_follow(
        source,
        destination,
        python_tree=True,
        owner_uid=os.getuid(),
    )

    copied = destination / "bin" / "ruff"
    assert copied.read_bytes() == payload
    assert copied.stat().st_mode & 0o111

def test_copy_rejects_untrusted_python_bin_executable_format(tmp_path):
    source = tmp_path / "python-tree"
    scripts = source / "bin"
    scripts.mkdir(parents=True)
    executable = scripts / "untrusted"
    executable.write_bytes(b"not a script or ELF\n")
    executable.chmod(0o755)

    destination = tmp_path / "copied-python-tree"
    with pytest.raises(
        provisioner.DependencyProvisionError,
        match="Python bin executable has an untrusted format",
    ):
        provisioner._copy_tree_no_follow(
            source,
            destination,
            python_tree=True,
            owner_uid=os.getuid(),
        )
    assert not (destination / "bin" / "untrusted").exists()

def test_copy_rejects_relative_escape_through_proc_root(tmp_path):
    source = tmp_path / "node_modules"
    source.mkdir()
    outside = tmp_path / "outside.js"
    outside.write_text("console.log('outside')\n", encoding="utf-8")
    (source / "escape").symlink_to(os.path.relpath(outside, source))

    with pytest.raises(
        provisioner.DependencyProvisionError,
        match="escaping symlink",
    ):
        provisioner._copy_tree_no_follow(
            _through_proc_self_root(source),
            tmp_path / "copied-node_modules",
            python_tree=False,
            owner_uid=os.getuid(),
        )

def test_copy_rejects_broken_link_through_proc_root(tmp_path):
    source = tmp_path / "node_modules"
    source.mkdir()
    (source / "broken").symlink_to("missing.js")

    with pytest.raises(
        provisioner.DependencyProvisionError,
        match="escaping symlink",
    ):
        provisioner._copy_tree_no_follow(
            _through_proc_self_root(source),
            tmp_path / "copied-node_modules",
            python_tree=False,
            owner_uid=os.getuid(),
        )

def test_copy_rejects_relative_escape_reentry_through_proc_root(tmp_path):
    source = tmp_path / "node_modules"
    source.mkdir()
    (source / "inside.js").write_text("console.log('inside')\n", encoding="utf-8")
    destination = tmp_path / "copied-node_modules"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "back-inside").symlink_to(destination / "inside.js")
    (source / "escape-reentry").symlink_to("../outside/back-inside")

    with pytest.raises(
        provisioner.DependencyProvisionError,
        match="escaping symlink",
    ):
        provisioner._copy_tree_no_follow(
            _through_proc_self_root(source),
            destination,
            python_tree=False,
            owner_uid=os.getuid(),
        )

def test_copy_rejects_unexpected_numeric_proc_root_suffix(tmp_path):
    source = tmp_path / "node_modules"
    source.mkdir()
    proc_source = (
        Path("/proc")
        / str(os.getpid())
        / "root"
        / source.resolve(strict=True).relative_to("/")
    )

    with pytest.raises(
        provisioner.DependencyProvisionError,
        match="proc-root path is invalid",
    ):
        provisioner._copy_tree_no_follow(
            proc_source,
            tmp_path / "copied-node_modules",
            python_tree=False,
            owner_uid=os.getuid(),
        )

@pytest.mark.parametrize(
    ("target", "unsafe_mode"),
    (("directory", 0o700), ("file", 0o600), ("file", 0o700)),
)
def test_consumer_rejects_unprojectable_snapshot_entry_modes(
    monkeypatch,
    tmp_path,
    target,
    unsafe_mode,
):
    root = tmp_path / "node_modules"
    directory = root / "fixture"
    directory.mkdir(parents=True)
    payload = directory / "index.js"
    payload.write_text("module.exports = 1\n", encoding="utf-8")
    root.chmod(0o755)
    directory.chmod(0o755)
    payload.chmod(0o644)
    monkeypatch.setattr(local, "_DEPENDENCY_SNAPSHOT_OWNER_UID", os.getuid())

    changed = directory if target == "directory" else payload
    changed.chmod(unsafe_mode)
    with pytest.raises(
        local.WorkerTerminalSandboxError,
        match="reviewed cross-UID mode",
    ):
        local._validate_dependency_snapshot_tree(root, label="Node")

@pytest.mark.parametrize("attack", ["hardlink", "symlink", "editable"])
def test_promoter_rejects_tree_injection(tmp_path, attack):
    bundle = _bundle(tmp_path)
    output = tmp_path / "output"
    node = output / "node_modules" / "fixture"
    venv = output / "backend" / ".venv"
    node.mkdir(parents=True)
    venv.mkdir(parents=True)
    target = node / "index.js"
    target.write_text("module.exports = 1\n", encoding="utf-8")
    if attack == "hardlink":
        os.link(target, node / "duplicate.js")
    elif attack == "symlink":
        (node / "escape").symlink_to("/etc/passwd")
    else:
        (venv / "__editable__.candidate.pth").write_text(
            str(bundle.workspace / "backend") + "\n", encoding="utf-8",
        )
    with pytest.raises(provisioner.DependencyProvisionError):
        provisioner.promote_snapshot(
            bundle,
            output,
            snapshot_root=tmp_path / "snapshots",
            owner_uid=os.getuid(),
        )

def test_missing_snapshot_auto_requests_then_revalidates(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    calls = {"resolve": 0, "request": 0}
    expected = (object(),)

    def resolve_once(candidate):
        assert Path(candidate) == workspace
        calls["resolve"] += 1
        if calls["resolve"] == 1:
            raise local.WorkerTerminalSandboxError("dependency provisioning required")
        return expected

    monkeypatch.setattr(local, "_resolve_trusted_dependency_projections_once", resolve_once)
    monkeypatch.setattr(
        provisioner,
        "request_dependency_snapshot",
        lambda candidate: calls.__setitem__("request", calls["request"] + 1),
    )
    monkeypatch.setattr(
        local, "_DEPENDENCY_SNAPSHOT_ROOT", local._PRODUCTION_DEPENDENCY_SNAPSHOT_ROOT,
    )
    assert local.resolve_trusted_dependency_projections(workspace) is expected
    assert calls == {"resolve": 2, "request": 1}

def test_default_only_legacy_snapshot_is_bypassed_and_reprovisioned(
    monkeypatch,
    tmp_path,
):
    bundle = _bundle(tmp_path)
    legacy_contract = hashlib.sha256()
    for relative, digest in sorted(bundle.hashes.items()):
        legacy_contract.update(relative.encode())
        legacy_contract.update(b"\0")
        legacy_contract.update(digest.encode("ascii"))
        legacy_contract.update(b"\n")
    legacy_digest = legacy_contract.hexdigest()
    assert legacy_digest != bundle.digest

    snapshot_root = tmp_path / "production-snapshots"
    snapshot_root.mkdir(mode=0o755)
    legacy = snapshot_root / legacy_digest
    legacy.mkdir(mode=0o755)
    (legacy / "attestation.json").write_text(
        json.dumps({
            "version": 1,
            "manifest_sha256": legacy_digest,
            "manifests": dict(bundle.hashes),
            "trees": {},
        }),
        encoding="utf-8",
    )
    (legacy / "attestation.json").chmod(0o644)
    calls = 0

    def provision(_workspace):
        nonlocal calls
        calls += 1
        assert legacy.is_dir()
        snapshot = snapshot_root / bundle.digest
        (snapshot / "node_modules").mkdir(parents=True, mode=0o755)
        (snapshot / "backend" / ".venv").mkdir(parents=True, mode=0o755)
        snapshot.chmod(0o755)
        (snapshot / "backend").chmod(0o755)
        (snapshot / "backend" / ".venv").chmod(0o755)
        runtime = provisioner._expected_python_runtime_binding()
        attestation = {
            "version": provisioner.SNAPSHOT_ATTESTATION_VERSION,
            "build_policy": provisioner.DEPENDENCY_BUILD_POLICY,
            "manifest_sha256": bundle.digest,
            "manifests": dict(bundle.hashes),
            "python_runtime": runtime,
            "python_runtime_sha256": hashlib.sha256(
                provisioner._canonical_json(runtime) + b"\n"
            ).hexdigest(),
            "trees": {
                "node_modules": {
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "entries": 0,
                    "bytes": 0,
                },
                "backend/.venv": {
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "entries": 0,
                    "bytes": 0,
                    "install_mode": provisioner.PYTHON_INSTALL_MODE,
                },
            },
        }
        (snapshot / "attestation.json").write_text(
            json.dumps(attestation, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
        (snapshot / "attestation.json").chmod(0o644)
        return bundle.digest

    monkeypatch.setattr(local, "_DEPENDENCY_SNAPSHOT_ROOT", snapshot_root)
    monkeypatch.setattr(local, "_PRODUCTION_DEPENDENCY_SNAPSHOT_ROOT", snapshot_root)
    monkeypatch.setattr(local, "_DEPENDENCY_SNAPSHOT_OWNER_UID", os.getuid())
    monkeypatch.setattr(provisioner, "request_dependency_snapshot", provision)

    projections = local.resolve_trusted_dependency_projections(bundle.workspace)

    assert calls == 1
    current = snapshot_root / bundle.digest
    assert {projection.source for projection in projections} == {
        current / "node_modules",
        current / "backend" / ".venv",
    }
    assert legacy.is_dir()

def test_request_protocol_binds_run_and_high_entropy_claim():
    request = provisioner._parse_request(json.dumps({
        "schema": provisioner.PROTOCOL_SCHEMA,
        "task_id": "t_0123abcd",
        "workspace": "/home/ab/code/clauseye-contra-rope/.worktrees/t_0123abcd",
        "run_id": 41,
        "claim_lock": "worker:0123456789abcdef0123456789abcdef",
    }).encode())
    assert request.run_id == 41
    assert request.task_id == "t_0123abcd"
    assert request.claim_lock.endswith("0123456789abcdef")
    for bad in ("short", "contains space", ""):
        with pytest.raises(provisioner.DependencyProvisionError, match="claim"):
            provisioner._parse_request(json.dumps({
                "schema": provisioner.PROTOCOL_SCHEMA,
                "task_id": "t_0123abcd",
                "workspace": "/home/ab/code/clauseye-contra-rope/.worktrees/t_0123abcd",
                "run_id": 41,
                "claim_lock": bad,
            }).encode())
