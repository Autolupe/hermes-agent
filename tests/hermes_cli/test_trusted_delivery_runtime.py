"""Protected runtime identity checks extracted from immutable339a source.

All filesystem trees are temporary; no installed runtime is inspected or changed.
"""

import json
import os

import pytest


def test_trusted_gcloud_requires_exact_reviewed_version_and_tree(monkeypatch):
    from hermes_cli import trusted_delivery_runtime as runtime

    reads = []
    monkeypatch.setattr(
        runtime,
        "require_root_owned_executable",
        lambda path, *, root: path,
    )

    def read(path, **kwargs):
        reads.append((path, kwargs))
        assert path == runtime.TRUSTED_GCLOUD_VERSION_PATH
        return b"576.0.0\n"

    monkeypatch.setattr(runtime, "_read_root_owned_runtime_file", read)
    tree_checks = []
    monkeypatch.setattr(
        runtime,
        "_require_gcloud_tree_digest",
        lambda expected: tree_checks.append(expected),
    )
    assert runtime.require_trusted_gcloud() == runtime.TRUSTED_GCLOUD_PATH
    assert reads == [
        (
            runtime.TRUSTED_GCLOUD_VERSION_PATH,
            {
                "root": runtime.TRUSTED_DELIVERY_RUNTIME_ROOT,
                "maximum_bytes": 64,
            },
        ),
    ]
    assert tree_checks == [runtime.TRUSTED_GCLOUD_TREE_SHA256]
    assert runtime.TRUSTED_GCLOUD_TREE_SHA256 == (
        "9772f5002bb355343613d23a66f0f44d5b4372d9398da3acd466464d5470a041"
    )

    monkeypatch.setattr(
        runtime,
        "_read_root_owned_runtime_file",
        lambda path, **_kwargs: b"576.0.1\n",
    )
    with pytest.raises(runtime.TrustedDeliveryRuntimeError):
        runtime.require_trusted_gcloud()

def test_trusted_gcloud_rejects_executable_or_tree_drift(monkeypatch):
    from hermes_cli import trusted_delivery_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "_read_root_owned_runtime_file",
        lambda *_args, **_kwargs: b"576.0.0\n",
    )

    def executable_rejected(*_args, **_kwargs):
        raise runtime.TrustedDeliveryRuntimeError("executable drift")

    monkeypatch.setattr(runtime, "require_root_owned_executable", executable_rejected)
    with pytest.raises(runtime.TrustedDeliveryRuntimeError, match="executable drift"):
        runtime.require_trusted_gcloud()

    monkeypatch.setattr(
        runtime,
        "require_root_owned_executable",
        lambda path, *, root: path,
    )

    def tree_rejected(_expected):
        raise runtime.TrustedDeliveryRuntimeError("tree drift")

    monkeypatch.setattr(runtime, "_require_gcloud_tree_digest", tree_rejected)
    with pytest.raises(runtime.TrustedDeliveryRuntimeError, match="tree drift"):
        runtime.require_trusted_gcloud()

def test_trusted_gcloud_tree_digest_rejects_tampered_sibling(tmp_path):
    from hermes_cli import trusted_delivery_runtime as runtime

    sdk = tmp_path / "google-cloud-sdk"
    library = sdk / "lib" / "googlecloudsdk"
    library.mkdir(parents=True)
    module = library / "core.py"
    module.write_text("trusted = True\n", encoding="utf-8")
    module.chmod(0o644)
    launcher = sdk / "bin"
    launcher.mkdir()
    (launcher / "gcloud").write_text("#!/bin/sh\n", encoding="utf-8")
    (launcher / "gcloud").chmod(0o755)
    sdk.chmod(0o755)
    (sdk / "lib").chmod(0o755)
    library.chmod(0o755)
    launcher.chmod(0o755)
    before = runtime._gcloud_tree_sha256(sdk, owner_uid=os.getuid())

    module.write_text("trusted = False\n", encoding="utf-8")
    after = runtime._gcloud_tree_sha256(sdk, owner_uid=os.getuid())
    assert before != after

    module.chmod(0o666)
    with pytest.raises(runtime.TrustedDeliveryRuntimeError, match="unsafe"):
        runtime._gcloud_tree_sha256(sdk, owner_uid=os.getuid())

def test_trusted_python_runtime_tree_digest_rejects_sibling_tamper_and_link(
    tmp_path,
):
    from hermes_cli import trusted_delivery_runtime as runtime

    root = tmp_path / "runtime"
    package = root / "venv/lib/python3.13/site-packages/reviewed"
    package.mkdir(parents=True)
    module = package / "core.py"
    module.write_bytes(b"safe = True\n")
    for directory in (
        root,
        root / "venv",
        root / "venv/lib",
        root / "venv/lib/python3.13",
        root / "venv/lib/python3.13/site-packages",
        package,
    ):
        directory.chmod(0o755)
    module.chmod(0o644)

    owner = os.getuid()
    before = runtime._immutable_runtime_tree_sha256(root, owner_uid=owner)
    module.write_bytes(b"safe = False\n")
    after = runtime._immutable_runtime_tree_sha256(root, owner_uid=owner)
    assert after != before

    alias = package / "alias.py"
    os.link(module, alias)
    with pytest.raises(runtime.TrustedDeliveryRuntimeError, match="unsafe"):
        runtime._immutable_runtime_tree_sha256(root, owner_uid=owner)

def test_trusted_python_base_and_venv_are_bound_to_exact_provenance(monkeypatch):
    from hermes_cli import trusted_delivery_runtime as runtime

    tree_sha256 = "a" * 64
    provenance = {
        "archive_sha256": runtime.TRUSTED_PYTHON_ARCHIVE_SHA256,
        "archive_url": runtime.TRUSTED_PYTHON_ARCHIVE_URL,
        "build_tag": runtime.TRUSTED_PYTHON_BUILD_TAG,
        "openssl_version": "OpenSSL 3.5.7 7 Apr 2026",
        "python_version": runtime.TRUSTED_PYTHON_VERSION,
        "schema": "hermes-python-runtime/v1",
        "sqlite_source_id": "reviewed-source",
        "sqlite_version": runtime.TRUSTED_SQLITE_VERSION,
        "tree_sha256": tree_sha256,
    }
    monkeypatch.setattr(
        runtime,
        "require_root_owned_executable",
        lambda path, *, root: path,
    )
    monkeypatch.setattr(
        runtime,
        "_read_root_owned_runtime_file",
        lambda *_args, **_kwargs: (
            json.dumps(provenance, sort_keys=True) + "\n"
        ).encode("ascii"),
    )
    monkeypatch.setattr(
        runtime, "_immutable_runtime_tree_sha256", lambda _root: tree_sha256,
    )
    executable, binding = runtime.require_trusted_python_base()
    assert executable == runtime.TRUSTED_PYTHON_PATH
    assert binding["python_runtime_tree_sha256"] == tree_sha256
    assert binding["sqlite_version"] == "3.53.1"

    monkeypatch.setattr(
        runtime, "_immutable_runtime_tree_sha256", lambda _root: "b" * 64,
    )
    with pytest.raises(runtime.TrustedDeliveryRuntimeError, match="tree"):
        runtime.require_trusted_python_base()

    pyvenv = (
        f"home = {runtime.TRUSTED_PYTHON_ROOT / 'bin'}\n"
        "include-system-site-packages = false\n"
        f"version = {runtime.TRUSTED_PYTHON_VERSION}\n"
        f"executable = {runtime.TRUSTED_PYTHON_PATH}\n"
    ).encode("utf-8")
    monkeypatch.setattr(
        runtime,
        "_read_root_owned_runtime_file",
        lambda *_args, **_kwargs: pyvenv,
    )
    runtime._require_venv_uses_trusted_python(runtime.TRUSTED_POLICY_RUNTIME_ROOT)
    pyvenv = pyvenv.replace(
        str(runtime.TRUSTED_PYTHON_PATH).encode(), b"/usr/bin/python3.13",
    )
    with pytest.raises(runtime.TrustedDeliveryRuntimeError, match="hermetic base"):
        runtime._require_venv_uses_trusted_python(
            runtime.TRUSTED_POLICY_RUNTIME_ROOT,
        )
