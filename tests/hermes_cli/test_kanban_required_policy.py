"""Import an enrolled fixture provider; never use live policy files or services."""

from dataclasses import dataclass
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType
import uuid

import pytest

from hermes_cli import kanban_policy as policy


pytestmark = pytest.mark.linux_only


@dataclass
class _Fixture:
    root: Path
    registry_dir: Path
    site: Path
    package: str
    distribution: str
    dist_info: str
    sources: dict[str, str]
    identities: list[int]
    registry: policy._RequiredPolicyRegistry

    @property
    def enrollment(self):
        return self.registry_dir / "uid-12345.toml"

    def write_member(self, relative, text):
        filename = self.site / relative
        filename.parent.mkdir(parents=True, exist_ok=True)
        for parent in filename.parents:
            if parent == self.root:
                break
            parent.chmod(0o755)
        filename.write_text(text, encoding="utf-8")
        filename.chmod(0o644)
        self.sources[relative] = text

    def enroll(self, *, uid=12345, replace=None):
        fields = {
            "distribution": self.distribution,
            "version": "1.0",
            "site_packages": str(self.site),
            "dist_info": self.dist_info,
            "entry_point": "required-fixture",
            "entry_point_value": self.package,
        }
        fields.update(replace or {})
        lines = ["schema_version = 1", f"uid = {uid}", "[provider]"]
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in fields.items())
        lines.extend(["[scope]", 'label = "fixture only"', 'home = "/enrolled/fixture/home"',
                      '[scope.project]', 'id = "fixture-project"', "[files]"])
        lines.extend(f"{json.dumps(relative)} = {json.dumps(hashlib.sha256(text.encode()).hexdigest())}"
                     for relative, text in self.sources.items())
        filename = self.registry_dir / f"uid-{uid}.toml"
        filename.write_text("\n".join(lines) + "\n", encoding="utf-8")
        filename.chmod(0o644)


@pytest.fixture
def enrolled(tmp_path, monkeypatch):
    root = tmp_path / "protected-fixture"
    root.mkdir(mode=0o755)
    registry_dir = root / "required-kanban-policies.d"
    registry_dir.mkdir(mode=0o755)
    site = root / "site-packages"
    site.mkdir(mode=0o755)
    package = "required_fixture_" + uuid.uuid4().hex
    distribution = package.replace("_", "-")
    dist_info = package + "-1.0.dist-info"
    identities = [12345, 12345]
    files = policy._ProtectedFiles(root=root, owner_uid=os.geteuid())
    registry = policy._RequiredPolicyRegistry(registry_dir, files=files,
                                             identity=lambda: tuple(identities))
    f = _Fixture(root, registry_dir, site, package, distribution, dist_info, {}, identities, registry)
    f.write_member(f"{dist_info}/METADATA", f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 1.0\n")
    f.write_member(f"{dist_info}/entry_points.txt", f"[hermes_agent.plugins]\nrequired-fixture = {package}\n")
    f.write_member(f"{package}/__init__.py",
                   "from .provider import Policy\n"
                   "IMPORT_COUNT = 1\n"
                   "def register(ctx):\n"
                   "    ctx.register_kanban_policy(Policy(ctx.scope['label']))\n")
    f.write_member(f"{package}/provider.py",
                   "from hermes_cli.kanban_policy import RequiredKanbanPolicy\n"
                   "class Policy(RequiredKanbanPolicy):\n"
                   "    def __init__(self, label):\n"
                   "        self.label = label\n"
                   "    def late_import(self):\n"
                   "        from .later import VALUE\n"
                   "        return VALUE\n")
    f.write_member(f"{package}/later.py", "VALUE = 'verified later import'\n")
    f.enroll()
    # Production exposes no way to select these fixture anchors. The private
    # library object above exercises the same ownership, descriptor and loader
    # code while the runner's temporary HERMES_HOME remains independent.
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    original_meta_path = list(sys.meta_path)
    yield f
    sys.meta_path[:] = original_meta_path
    for name in list(sys.modules):
        if name == package or name.startswith(package + "."):
            del sys.modules[name]


def test_real_installed_entrypoint_and_relative_modules_are_imported(enrolled):
    f = enrolled
    selected = f.registry.select()
    assert isinstance(selected.provider, policy.RequiredKanbanPolicy)
    assert selected.provider.label == "fixture only"
    assert type(selected.provider).__module__ == f.package + ".provider"
    assert sys.modules[f.package].__file__ == str(f.site / f.package / "__init__.py")
    assert selected.provider.late_import() == "verified later import"
    selected.check_integrity()
    assert f.registry.select() is selected
    assert selected.generation
    assert not hasattr(selected, "admit_workspace")


def test_no_enrollment_preserves_ordinary_behavior_without_import(enrolled):
    f = enrolled
    f.enrollment.unlink()
    assert f.registry.select() is None
    assert f.package not in sys.modules
    f.registry_dir.rmdir()
    assert f.registry.select() is None


def test_production_registry_is_independent_of_private_legacy_hermes_ancestry():
    requested = []

    class AbsentFiles:
        def read(self, path, **kwargs):
            requested.append(path)
            assert kwargs["missing_ok"] is True
            return None

    registry = policy._RequiredPolicyRegistry(policy._REGISTRY_DIRECTORY,
                                             files=AbsentFiles(), identity=lambda: (12345, 12345))
    assert registry.select() is None
    assert requested == [Path("/etc/hermes-required-kanban-policies.d/uid-12345.toml")]


def test_unreadable_fixture_registry_stays_failed_closed(enrolled, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("fixture denied")

    with monkeypatch.context() as blocked:
        blocked.setattr(policy.os, "open", denied)
        with pytest.raises(policy.RequiredPolicyError, match="read safely"):
            enrolled.registry.select()
    with pytest.raises(policy.RequiredPolicyError, match="invalidated"):
        enrolled.registry.select()


def test_other_uid_enrollment_does_not_change_ordinary_behavior(enrolled):
    f = enrolled
    f.identities[:] = [55555, 55555]
    assert f.registry.select() is None
    assert f.package not in sys.modules


@pytest.mark.parametrize("identities", [(12345, 54321), (54321, 12345)])
def test_either_os_uid_establishes_obligation_before_provider_load(enrolled, identities):
    f = enrolled
    f.identities[:] = identities
    with pytest.raises(policy.RequiredPolicyError, match="real and effective"):
        f.registry.select()
    assert f.package not in sys.modules


def test_os_identity_changes_invalidate_existing_registration(enrolled):
    selected = enrolled.registry.select()
    enrolled.identities[:] = [54321, 54321]
    with pytest.raises(policy.RequiredPolicyError):
        selected.check_integrity()
    enrolled.identities[:] = [12345, 12345]
    with pytest.raises(policy.RequiredPolicyError):
        enrolled.registry.select()


def test_selector_identity_drift_cannot_revert_to_ordinary_behavior(enrolled):
    f = enrolled
    selected = f.registry.select()
    f.identities[:] = [54321, 54321]
    with pytest.raises(policy.RequiredPolicyError):
        f.registry.select()
    f.identities[:] = [12345, 12345]
    with pytest.raises(policy.RequiredPolicyError):
        f.registry.select()
    with pytest.raises(policy.RequiredPolicyError):
        selected.check_integrity()


def test_scope_is_protected_data_not_an_opt_in_match_or_success_receipt(enrolled, monkeypatch):
    f = enrolled
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n  disabled: [required-fixture]\n", encoding="utf-8")
    monkeypatch.setenv("HOME", "/somewhere-else")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(home / "missing"))
    monkeypatch.setenv("HERMES_REQUIRED_KANBAN_POLICIES_DIR", str(home / "missing"))
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda: pytest.fail("Global plugin discovery was used"))
    selected = f.registry.select()
    assert selected.uid == 12345
    assert selected.scope["home"] == "/enrolled/fixture/home"
    with pytest.raises(TypeError):
        selected.scope["home"] = "/somewhere-else"
    with pytest.raises(TypeError):
        selected.scope["project"]["id"] = "other-project"


@pytest.mark.parametrize("content", ["broken=[", "schema_version=1\nuid=12345\n", "", "x" * 65537])
def test_malformed_empty_or_large_enrollment_cannot_disable_requirement(enrolled, content):
    enrolled.enrollment.write_text(content, encoding="utf-8")
    with pytest.raises(policy.RequiredPolicyError):
        enrolled.registry.select()
    assert enrolled.package not in sys.modules


def test_declared_uid_must_match_the_enrolled_filename(enrolled):
    text = enrolled.enrollment.read_text(encoding="utf-8").replace("uid = 12345", "uid = 54321")
    enrolled.enrollment.write_text(text, encoding="utf-8")
    with pytest.raises(policy.RequiredPolicyError, match="schema or user"):
        enrolled.registry.select()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "writable", "directory", "fifo", "ancestor-link", "ancestor-writable"])
def test_unsafe_enrollment_and_ancestors_are_refused(enrolled, kind):
    f = enrolled
    if kind == "symlink":
        saved = f.enrollment.with_suffix(".saved")
        f.enrollment.rename(saved)
        f.enrollment.symlink_to(saved)
    elif kind == "hardlink":
        os.link(f.enrollment, f.enrollment.with_suffix(".linked"))
    elif kind == "writable":
        f.enrollment.chmod(0o666)
    elif kind == "directory":
        f.enrollment.unlink()
        f.enrollment.mkdir()
    elif kind == "fifo":
        f.enrollment.unlink()
        os.mkfifo(f.enrollment)
    elif kind == "ancestor-link":
        saved = f.registry_dir.with_name("saved-registry")
        f.registry_dir.rename(saved)
        f.registry_dir.symlink_to(saved, target_is_directory=True)
    else:
        f.registry_dir.chmod(0o777)
    with pytest.raises(policy.RequiredPolicyError):
        f.registry.select()
    assert f.package not in sys.modules


def test_wrong_file_owner_is_not_accepted(enrolled):
    f = enrolled
    other_files = policy._ProtectedFiles(root=f.root, owner_uid=os.geteuid() + 1)
    registry = policy._RequiredPolicyRegistry(f.registry_dir, files=other_files, identity=lambda: (12345, 12345))
    with pytest.raises(policy.RequiredPolicyError, match="ancestry"):
        registry.select()


def test_disappearing_registration_denies_live_selection_but_initial_absence_is_ordinary(enrolled):
    f = enrolled
    selected = f.registry.select()
    f.enrollment.unlink()
    with pytest.raises(policy.RequiredPolicyError):
        f.registry.select()
    with pytest.raises(policy.RequiredPolicyError):
        selected.check_integrity()
    # A fresh process after root-authorized removal is not an old live lease.
    fresh = policy._RequiredPolicyRegistry(f.registry_dir, files=f.registry.files, identity=lambda: (12345, 12345))
    assert fresh.select() is None


@pytest.mark.parametrize("kind", ["same-bytes-replacement", "scope-change", "registry-replacement", "module-change", "metadata-change"])
def test_cached_registration_rejects_file_or_ancestry_drift(enrolled, kind):
    f = enrolled
    selected = f.registry.select()
    if kind == "same-bytes-replacement":
        new = f.enrollment.with_suffix(".replacement")
        new.write_bytes(f.enrollment.read_bytes())
        new.replace(f.enrollment)
    elif kind == "scope-change":
        f.enrollment.write_text(f.enrollment.read_text().replace("fixture only", "different scope"))
    elif kind == "registry-replacement":
        saved = f.registry_dir.with_name("saved-registry")
        f.registry_dir.rename(saved)
        f.registry_dir.mkdir()
        (saved / f.enrollment.name).rename(f.enrollment)
    elif kind == "module-change":
        (f.site / f.package / "provider.py").write_text("raise RuntimeError('must never run')\n")
    else:
        (f.site / f.dist_info / "METADATA").write_text("Name: other\nVersion: 1.0\n")
    with pytest.raises(policy.RequiredPolicyError):
        selected.check_integrity()
    with pytest.raises(policy.RequiredPolicyError):
        f.registry.select()


@pytest.mark.parametrize("fields", [{"distribution": "wrong-distribution"}, {"version": "2.0"},
                                   {"entry_point": "missing"}, {"entry_point_value": "other_package"},
                                   {"dist_info": "../escape.dist-info"}, {"site_packages": "relative"}])
def test_exact_distribution_and_entrypoint_must_match(enrolled, fields):
    enrolled.enroll(replace=fields)
    with pytest.raises(policy.RequiredPolicyError):
        enrolled.registry.select()
    assert enrolled.package not in sys.modules


def test_duplicate_entrypoint_is_not_resolved_by_name(enrolled):
    f = enrolled
    f.write_member(f"{f.dist_info}/entry_points.txt",
                   f"[hermes_agent.plugins]\nrequired-fixture = {f.package}\nrequired-fixture = {f.package}:register\n")
    f.enroll()
    with pytest.raises(policy.RequiredPolicyError):
        f.registry.select()
    assert f.package not in sys.modules


def test_package_hash_checked_before_any_provider_code_executes(enrolled):
    f = enrolled
    (f.site / f.package / "__init__.py").write_text("raise AssertionError('unverified code executed')\n")
    with pytest.raises(policy.RequiredPolicyError, match="digest"):
        f.registry.select()
    assert f.package not in sys.modules


def test_wrong_origin_preimported_package_cannot_be_reused(enrolled):
    f = enrolled
    fake = ModuleType(f.package)
    fake.__file__ = "/untrusted/fake/__init__.py"
    sys.modules[f.package] = fake
    with pytest.raises(policy.RequiredPolicyError, match="already imported"):
        f.registry.select()
    assert sys.modules[f.package] is fake


def test_ambient_shadow_package_never_executes(enrolled, monkeypatch):
    f = enrolled
    shadow = f.root / "untrusted-path"
    (shadow / f.package).mkdir(parents=True)
    (shadow / f.package / "__init__.py").write_text("raise AssertionError('shadow code executed')\n")
    monkeypatch.syspath_prepend(str(shadow))
    selected = f.registry.select()
    assert selected.provider.label == "fixture only"
    assert sys.modules[f.package].__file__ == str(f.site / f.package / "__init__.py")


def test_later_import_cannot_load_an_unlisted_package_member(enrolled):
    f = enrolled
    del f.sources[f"{f.package}/later.py"]
    f.enroll()
    selected = f.registry.select()
    with pytest.raises(policy.RequiredPolicyError, match="unenrolled"):
        selected.provider.late_import()


def test_later_import_rechecks_protected_registration(enrolled):
    selected = enrolled.registry.select()
    enrolled.enrollment.unlink()
    with pytest.raises(policy.RequiredPolicyError):
        selected.provider.late_import()


@pytest.mark.parametrize("body", ["raise RuntimeError('fixture registration error')", "return None",
                                  "ctx.register_kanban_policy({'success': True})",
                                  "ctx.register_kanban_policy(Policy('first')); ctx.register_kanban_policy(Policy('second'))"])
def test_provider_error_or_missing_duplicate_or_json_registration_denies(enrolled, body):
    f = enrolled
    f.write_member(f"{f.package}/__init__.py", f"from .provider import Policy\ndef register(ctx):\n    {body}\n")
    f.enroll()
    with pytest.raises(policy.RequiredPolicyError):
        f.registry.select()
    assert f.package not in sys.modules


def test_module_origin_replacement_invalidates_existing_registration(enrolled):
    f = enrolled
    selected = f.registry.select()
    sys.modules[f.package].__spec__.origin = "/untrusted/origin.py"
    with pytest.raises(policy.RequiredPolicyError, match="identity or origin"):
        selected.check_integrity()
    with pytest.raises(policy.RequiredPolicyError):
        f.registry.select()


def test_truncated_descriptor_read_cannot_look_like_absent_registration(enrolled, monkeypatch):
    real_read = os.read
    calls = 0

    def interrupted_read(fd, count):
        nonlocal calls
        calls += 1
        return real_read(fd, min(count, 8)) if calls == 1 else b""

    monkeypatch.setattr(policy.os, "read", interrupted_read)
    with pytest.raises(policy.RequiredPolicyError, match="changed while reading"):
        enrolled.registry.select()
    assert enrolled.package not in sys.modules


def test_enrollment_unlinked_after_open_cannot_look_like_initial_absence(enrolled, monkeypatch):
    f = enrolled
    real_stat = os.stat
    unlinked = False

    def unlink_before_final_stat(path, *args, **kwargs):
        nonlocal unlinked
        if path == f.enrollment.name and kwargs.get("dir_fd") is not None:
            f.enrollment.unlink()
            unlinked = True
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(policy.os, "stat", unlink_before_final_stat)
    with pytest.raises(policy.RequiredPolicyError):
        f.registry.select()
    with pytest.raises(policy.RequiredPolicyError):
        f.registry.select()
    assert unlinked
    assert f.package not in sys.modules
