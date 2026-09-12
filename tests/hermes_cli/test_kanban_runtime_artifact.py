"""Source-only artifact checks; compiled cases run in fresh private processes.

The retained compiled fixture is not installed or downloaded by these tests.
Its absence is an explicit skip, not evidence of a supported production runtime.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import uuid


_REPOSITORY = Path(__file__).resolve().parents[2]
_COMPILED = Path(
    "/home/ab/activation-recovery-20260907/"
    "sqlite-native-connection-identity.3WbSNg/"
    "_sqlite3.cpython-311-x86_64-linux-gnu.so"
)
_COMPILED_HASH = "bc27399bacb08b450346e770f0c4eb0db40c4b2c5edde14fc533deb9b11bcd94"
_COMPILED_SUFFIX = ".cpython-311-x86_64-linux-gnu.so"


_PROVIDER = '''\
import json
from pathlib import Path
import sys
Path({marker!r}).write_text(json.dumps({{
    "sqlite_loaded": "_sqlite3" in sys.modules,
    "native_database_loaded": "hermes_cli.kanban_db" in sys.modules,
}}))
from hermes_cli.kanban_policy import RequiredKanbanPolicy, RequiredWorkspaceAdmission

class Admission(RequiredWorkspaceAdmission):
    def __init__(self, capture, request):
        self.request = request
        self.binding = capture.bind_original(request.connection)
        self.boundaries = []
        self.closed = False
        self.cancelled = False
    def checkpoint(self, boundary, observation):
        self.binding.check(self.request.connection)
        self.boundaries.append(boundary)
    def cancel(self, reason):
        assert self.request.cancelled
        self.cancelled = True
    def close(self):
        self.closed = True

class Provider(RequiredKanbanPolicy):
    def __init__(self, capture):
        self.capture = capture
        self.admissions = []
    def open_workspace_request(self, request):
        admission = Admission(self.capture, request)
        self.admissions.append(admission)
        return admission

def register(context):
    context.register_kanban_policy(Provider(context._runtime_identity))
'''


class _Fixture:
    def __init__(self, private, binary=None):
        from hermes_cli import kanban_policy as policy

        self.private = private
        self.root = private / "protected"
        self.registry_dir = self.root / "registrations"
        self.site = self.root / "site-packages"
        self.runtime_dir = self.root / "runtime"
        self.package = "runtime_fixture_" + uuid.uuid4().hex
        self.dist = self.package + "-1.0.dist-info"
        self.marker = private / "provider-imported.json"
        for directory in (self.root, self.registry_dir, self.site, self.runtime_dir,
                          self.site / self.package, self.site / self.dist):
            directory.mkdir(mode=0o755)
        suffix = _COMPILED_SUFFIX if binary else sysconfig.get_config_var("EXT_SUFFIX")
        self.artifact = self.runtime_dir / ("_sqlite3" + suffix)
        if binary is None:
            # Data-only tests never pass these inert bytes to the native loader.
            self.artifact.write_bytes(b"inert source-only artifact fixture\n")
        else:
            shutil.copyfile(binary, self.artifact)
        self.artifact.chmod(0o644)
        self.inventory_file = self.runtime_dir / "inventory.json"
        self.inventory = {
            "schema_version": 1,
            "kind": "sqlite-extension-artifact",
            "python": {"implementation": "cpython",
                       "version": [3, 11, 15] if binary else list(sys.version_info[:3]),
                       "extension_suffix": suffix},
            "sqlite_version": "3.53.1",
            "artifact": {"path": str(self.artifact), "size": self.artifact.stat().st_size,
                         "sha256": hashlib.sha256(self.artifact.read_bytes()).hexdigest()},
        }
        self.runtime = {
            "capability": "sqlite-opened-identity-v1",
            "inventory": str(self.inventory_file),
            "inventory_sha256": "",
        }
        self.write_inventory()
        self.members = {
            f"{self.dist}/METADATA": f"Metadata-Version: 2.1\nName: {self.package}\nVersion: 1.0\n",
            f"{self.dist}/entry_points.txt": f"[hermes_agent.plugins]\nfixture = {self.package}\n",
            f"{self.package}/__init__.py": _PROVIDER.format(marker=str(self.marker)),
        }
        for relative, source in self.members.items():
            path = self.site / relative
            path.write_text(source)
            path.chmod(0o644)
        self.enrollment = self.registry_dir / f"uid-{os.getuid()}.toml"
        self.files = policy._ProtectedFiles(root=self.root, owner_uid=os.getuid())
        self.enroll()

    def write_inventory(self, raw=None):
        self.inventory_file.write_text(json.dumps(self.inventory) if raw is None else raw)
        self.inventory_file.chmod(0o644)
        self.runtime["inventory_sha256"] = hashlib.sha256(self.inventory_file.read_bytes()).hexdigest()

    def enroll(self, schema=2):
        fields = {
            "distribution": self.package, "version": "1.0", "site_packages": str(self.site),
            "dist_info": self.dist, "entry_point": "fixture", "entry_point_value": self.package,
        }
        lines = [f"schema_version = {schema}", f"uid = {os.getuid()}", "[provider]"]
        lines += [f"{key} = {json.dumps(value)}" for key, value in fields.items()]
        lines += ["[scope]", 'label = "private source-only fixture"']
        if schema == 2:
            lines += ["[runtime]"]
            lines += [f"{key} = {json.dumps(value)}" for key, value in self.runtime.items()]
        lines += ["[files]"]
        lines += [f"{json.dumps(relative)} = {json.dumps(hashlib.sha256(source.encode()).hexdigest())}"
                  for relative, source in self.members.items()]
        self.enrollment.write_text("\n".join(lines) + "\n")
        self.enrollment.chmod(0o644)

    def registry(self, loader=None):
        from hermes_cli import kanban_policy as policy

        return policy._RequiredPolicyRegistry(
            self.registry_dir, files=self.files,
            identity=lambda: (os.getuid(), os.geteuid()), _runtime_artifact_loader=loader)


@contextmanager
def _refuses(expected=None):
    """Never accept an assertion or missing test API wrapped as a refusal."""
    from hermes_cli.kanban_policy import RequiredPolicyError

    try:
        yield
    except RequiredPolicyError as error:
        if expected is not None:
            assert expected in str(error), str(error)
        seen = set()
        cause = error
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            assert not isinstance(cause, (AssertionError, AttributeError)), repr(cause)
            cause = cause.__cause__ or cause.__context__
    else:
        raise AssertionError("the bounded operation did not refuse")


def _child(case, private, binary, purelib):
    # No inherited PYTHONPATH, startup customization, credentials or live home.
    sys.path[:0] = [str(_REPOSITORY), purelib]
    os.umask(0o022)
    home = private / "hermes-home"
    home.mkdir(mode=0o700)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_KANBAN_HOME"] = str(home)
    ordinary = case in ("fresh_absence", "fresh_schema_one")
    if ordinary:
        # Simulate an unavailable optional module, not another host OS. Ordinary
        # selection and public refusal must not require Linux capture helpers.
        import builtins

        original_import = builtins.__import__

        def without_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                raise ModuleNotFoundError("fixture fcntl is unavailable")
            return original_import(name, *args, **kwargs)

        builtins.__import__ = without_fcntl
    from hermes_cli import kanban_policy as policy
    from hermes_cli import kanban_runtime_artifact as runtime

    assert not any(name in sys.modules for name in ("sqlite3", "_sqlite3", "hermes_cli.kanban_db"))
    f = _Fixture(private, binary)
    if ordinary:
        # Neither case consults a retained compiled fixture or an inventory.
        f.artifact.unlink()
        f.inventory_file.unlink()
        if case == "fresh_absence":
            f.enrollment.unlink()
            assert f.registry().select() is None
            assert not f.marker.exists()
        else:
            f.enroll(schema=1)
            selected = f.registry().select()
            assert selected.provider.capture is None
            selected.check_integrity()
            assert json.loads(f.marker.read_text()) == {
                "sqlite_loaded": False, "native_database_loaded": False,
            }
        assert not any(name in sys.modules for name in ("sqlite3", "_sqlite3", "hermes_cli.kanban_db"))
        with _refuses("Complete runtime provenance is unsupported"):
            runtime.bootstrap_required_policy()
        return
    if case.startswith("preloaded_"):
        import importlib

        name = {"preloaded_sqlite": "sqlite3", "preloaded_extension": "_sqlite3",
                "preloaded_native": "hermes_cli.kanban_db"}[case]
        importlib.import_module(name)
        with _refuses():
            f.registry(runtime._capture_verified_extension).select()
        assert not f.marker.exists()
        return
    if case in ("wrong_abi", "wrong_sqlite_version"):
        if case == "wrong_abi":
            f.inventory["python"]["version"] = [3, 12, 0]
        else:
            f.inventory["sqlite_version"] = "0.0.0"
        f.write_inventory()
        f.enroll()
        with _refuses():
            f.registry(runtime._capture_verified_extension).select()
        assert not f.marker.exists()
        if case == "wrong_abi":
            assert "_sqlite3" not in sys.modules
        return

    registry = f.registry(runtime._capture_verified_extension)
    selected = registry.select()
    provider = selected.provider
    capture = provider.capture
    assert capture is not None
    assert json.loads(f.marker.read_text()) == {
        "sqlite_loaded": True, "native_database_loaded": False,
    }
    assert registry.select() is selected
    capture.check_integrity()
    extension = sys.modules["_sqlite3"]
    import fcntl

    assert extension.__file__.startswith("/proc/self/fd/")
    retained_fd = os.open(extension.__file__, os.O_RDONLY | os.O_CLOEXEC)
    try:
        # Linux UAPI F_GET_SEALS and SEAL/SHRINK/GROW/WRITE bits. This Python
        # build lacks their symbolic constants; fcntl still calls the kernel.
        needed_seals = 0x0001 | 0x0002 | 0x0004 | 0x0008
        assert fcntl.fcntl(retained_fd, 1034) & needed_seals == needed_seals
        with os.fdopen(retained_fd, "rb", closefd=False) as stream:
            assert hashlib.sha256(stream.read()).hexdigest() == _COMPILED_HASH
    finally:
        os.close(retained_fd)
    original_base = extension.Connection
    descriptor = original_base.__dict__["_opened_identity"]
    original_init = original_base.__dict__["__init__"]
    import sqlite3

    connection = None
    other = None
    try:
        if case in ("native_request", "native_launch_refusal"):
            # Only this temporary registry is substituted; actual native claim,
            # original TrackedConnection, registration and checkpoints execute.
            policy._production_registry = registry
            from hermes_cli import kanban_db as kb
            from hermes_cli.sqlite_safe_read import TrackedConnection

            kb._fire_kanban_lifecycle_hook = lambda *args, **kwargs: None
            kb._fire_worker_spawned_hook = lambda *args, **kwargs: None
            connection = kb.connect(db_path=private / "board.db")
            assert type(connection) is TrackedConnection
            task_id = kb.create_task(connection, title="private runtime capture", assignee="default")
            task = kb.claim_task(connection, task_id)
            assert task is not None
            manager = kb.required_workspace_request(
                    connection, task_id=task.id, expected_run_id=task.current_run_id,
                    expected_claim_lock=task.claim_lock, board="default", lane="ready")
            if case == "native_launch_refusal":
                from hermes_cli.kanban_workspace_policy import require_supported_worker_launch

                with _refuses("Required controlled worker launch is unsupported"):
                    with manager as request:
                        require_supported_worker_launch(request)
                admission = provider.admissions[-1]
                assert request.cancelled and admission.cancelled and admission.closed
                restored = kb.get_task(connection, task.id)
                assert restored.worker_pid is None and restored.status == "ready"
                assert restored.current_run_id is None and restored.claim_lock is None
                assert restored.consecutive_failures == 0
                run = connection.execute(
                    "SELECT status, outcome, worker_pid FROM task_runs WHERE id = ?",
                    (task.current_run_id,),
                ).fetchone()
                assert tuple(run) == ("reclaimed", "reclaimed", None)
            else:
                with manager as request:
                    assert request.connection is connection
                    request.checkpoint("runtime_fixture_original")
                    admission = provider.admissions[-1]
                    assert admission.binding.check(connection) == descriptor(connection)
                assert admission.closed and not admission.cancelled
                assert admission.boundaries == ["request_open", "runtime_fixture_original", "request_close"]
                assert kb.get_task(connection, task.id).status == "running"
            return

        class FixtureConnection(sqlite3.Connection):
            pass

        filename = private / "identity.db"
        connection = sqlite3.connect(filename, factory=FixtureConnection)
        binding = capture.bind_original(connection)
        initial = binding.check(connection)
        assert type(initial) is tuple and len(initial) == 6
        assert all(type(item) is int for item in initial)
        assert initial == descriptor(connection)
        if case == "distinct_equal_tuple":
            other = sqlite3.connect(filename)
            assert descriptor(other) == initial
            with _refuses():
                binding.check(other)
            with _refuses():
                binding.check(connection)
        elif case == "captured_aliases":
            connection._opened_identity = lambda: ("instance substitution",)
            FixtureConnection._opened_identity = lambda self: ("class substitution",)
            extension.Connection = object
            assert connection._opened_identity() == ("instance substitution",)
            assert binding.check(connection) == initial
            capture.check_integrity()
        elif case in ("closed_connection", "reinitialized_connection"):
            if case == "closed_connection":
                connection.close()
            else:
                original_init(connection, filename)
            with _refuses():
                binding.check(connection)
            with _refuses():
                binding.check(connection)
        elif case in ("inventory_drift", "artifact_drift", "enrollment_drift"):
            path = {"inventory_drift": f.inventory_file, "artifact_drift": f.artifact,
                    "enrollment_drift": f.enrollment}[case]
            saved = path.read_bytes()
            path.write_bytes(saved + b"\n")
            with _refuses():
                selected.check_integrity()
            path.write_bytes(saved)
            with _refuses():
                registry.select()
            with _refuses():
                binding.check(connection)
        elif case in ("module_replacement", "module_origin", "module_loader"):
            if case == "module_replacement":
                from types import ModuleType

                sys.modules["_sqlite3"] = ModuleType("_sqlite3")
            elif case == "module_origin":
                extension.__spec__.origin = str(private / "unrelated.so")
            else:
                extension.__spec__.loader = object()
            with _refuses():
                capture.check_integrity()
            with _refuses():
                binding.check(connection)
        elif case == "thread_change":
            import threading

            observed = []

            def check_elsewhere():
                try:
                    binding.check(connection)
                except policy.RequiredPolicyError:
                    observed.append("refused")

            worker = threading.Thread(target=check_elsewhere)
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive() and observed == ["refused"]
            with _refuses():
                binding.check(connection)
        elif case == "closed_capture":
            capture.close()
            with _refuses():
                capture.check_integrity()
            with _refuses():
                binding.check(connection)
        else:
            raise AssertionError(f"unknown child case: {case}")
    finally:
        if other is not None:
            other.close()
        if connection is not None:
            connection.close()
        capture.close()


if __name__ == "__main__":
    child_case, child_private, child_binary, child_purelib = sys.argv[1:]
    _child(child_case, Path(child_private), None if child_binary == "-" else Path(child_binary), child_purelib)
    print(json.dumps({"case": child_case, "passed": True}))
    raise SystemExit(0)


import pytest

from hermes_cli import kanban_policy as policy
from hermes_cli import kanban_runtime_artifact as runtime


pytestmark = pytest.mark.linux_only


def _run_fresh_child(tmp_path, case, binary=None):
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()), case,
         str(tmp_path), str(binary) if binary else "-", sysconfig.get_path("purelib")],
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "UTC"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"case": case, "passed": True}


@pytest.fixture
def fixture(tmp_path):
    old_umask = os.umask(0o022)
    previous_meta_path = list(sys.meta_path)
    f = _Fixture(tmp_path)
    try:
        yield f
    finally:
        os.umask(old_umask)
        sys.meta_path[:] = previous_meta_path
        for name in list(sys.modules):
            if name == f.package or name.startswith(f.package + "."):
                del sys.modules[name]


def test_absence_and_schema_one_do_not_request_runtime_capture(fixture):
    f = fixture
    f.enrollment.unlink()
    assert f.registry().select() is None
    assert not f.marker.exists()
    f.enroll(schema=1)
    selected = f.registry().select()
    assert selected.provider.capture is None
    selected.check_integrity()


@pytest.mark.parametrize("case", ["fresh_absence", "fresh_schema_one"])
def test_ordinary_compatibility_in_fresh_process_without_runtime_artifact(tmp_path, case):
    _run_fresh_child(tmp_path, case)


def test_schema_two_normal_startup_refuses_before_provider_execution(fixture):
    registry = fixture.registry()
    with _refuses():
        registry.select()
    assert not fixture.marker.exists()
    fixture.enrollment.unlink()
    with _refuses():
        registry.select()


def test_valid_inventory_verification_does_not_load_native_code(fixture):
    before = {name: sys.modules.get(name) for name in ("sqlite3", "_sqlite3", "hermes_cli.kanban_db")}
    assert runtime._verify_inventory(fixture.runtime, fixture.files) is not None
    assert before == {name: sys.modules.get(name) for name in before}
    assert not fixture.marker.exists()


def test_refusal_harness_rejects_wrapped_test_errors():
    with pytest.raises(AssertionError):
        with _refuses():
            try:
                raise AssertionError("broken test setup")
            except AssertionError as error:
                raise policy.RequiredPolicyError("wrapped setup failure") from error


def test_public_bootstrap_is_unconditionally_unsupported_before_selection(monkeypatch):
    def must_not_select(*args, **kwargs):
        raise AssertionError("public bootstrap executed policy selection")

    monkeypatch.setattr(policy, "select_required_policy", must_not_select)
    monkeypatch.setattr(policy._RequiredPolicyRegistry, "select", must_not_select)
    with pytest.raises(policy.RequiredPolicyError, match="Complete runtime provenance is unsupported"):
        runtime.bootstrap_required_policy()
    with pytest.raises(TypeError):
        runtime.bootstrap_required_policy(verified=True)


@pytest.mark.parametrize("change", [
    "capability", "runtime_extra", "runtime_type", "inventory_hash", "inventory_relative",
    "inventory_extra", "schema_bool", "kind", "python_extra", "python_type", "version_type",
    "version_bool", "version_length", "implementation", "suffix", "sqlite_type",
    "artifact_extra", "artifact_type", "artifact_relative", "artifact_parent",
    "artifact_nul", "artifact_size_bool", "artifact_size_zero", "artifact_size_large",
    "artifact_size_mismatch", "artifact_hash", "artifact_digest_type",
])
def test_inventory_contract_refuses_invalid_data_without_import(fixture, change):
    f = fixture
    if change == "capability":
        f.runtime["capability"] = "unrecognized-capability"
    elif change == "runtime_extra":
        f.runtime["verified"] = True
    elif change == "runtime_type":
        f.runtime = []
    elif change == "inventory_hash":
        f.runtime["inventory_sha256"] = "0" * 64
    elif change == "inventory_relative":
        f.runtime["inventory"] = "inventory.json"
    else:
        changes = {
            "inventory_extra": (f.inventory, "verified", True),
            "schema_bool": (f.inventory, "schema_version", True),
            "kind": (f.inventory, "kind", "interpreter-approved"),
            "python_extra": (f.inventory["python"], "verified", True),
            "python_type": (f.inventory, "python", []),
            "version_type": (f.inventory["python"], "version", "3.11.15"),
            "version_bool": (f.inventory["python"], "version", [3, True, 15]),
            "version_length": (f.inventory["python"], "version", [3, 11]),
            "implementation": (f.inventory["python"], "implementation", "pypy"),
            "suffix": (f.inventory["python"], "extension_suffix", "/tmp/foreign.so"),
            "sqlite_type": (f.inventory, "sqlite_version", True),
            "artifact_extra": (f.inventory["artifact"], "verified", True),
            "artifact_type": (f.inventory, "artifact", []),
            "artifact_relative": (f.inventory["artifact"], "path", "_sqlite3.so"),
            "artifact_parent": (f.inventory["artifact"], "path", str(f.runtime_dir) + "/../other.so"),
            "artifact_nul": (f.inventory["artifact"], "path", str(f.artifact) + "\0"),
            "artifact_size_bool": (f.inventory["artifact"], "size", True),
            "artifact_size_zero": (f.inventory["artifact"], "size", 0),
            "artifact_size_large": (f.inventory["artifact"], "size", 2**40),
            "artifact_size_mismatch": (f.inventory["artifact"], "size", f.artifact.stat().st_size + 1),
            "artifact_hash": (f.inventory["artifact"], "sha256", "0" * 64),
            "artifact_digest_type": (f.inventory["artifact"], "sha256", True),
        }
        mapping, key, value = changes[change]
        mapping[key] = value
        f.write_inventory()
    with _refuses():
        runtime._verify_inventory(f.runtime, f.files)
    assert not f.marker.exists()


@pytest.mark.parametrize("raw", ["", "{", "[]", "null",
                                  '{"schema_version":NaN}', " " * 65537])
def test_invalid_or_ambiguous_json_is_refused(fixture, raw):
    fixture.write_inventory(raw)
    with _refuses():
        runtime._verify_inventory(fixture.runtime, fixture.files)


def test_duplicate_keys_in_otherwise_valid_inventory_are_refused(fixture):
    raw = json.dumps(fixture.inventory).replace(
        '"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1)
    fixture.write_inventory(raw)
    with _refuses():
        runtime._verify_inventory(fixture.runtime, fixture.files)


@pytest.mark.parametrize("member", ["inventory_file", "artifact"])
@pytest.mark.parametrize("change", ["symlink", "hardlink", "writable", "directory", "fifo", "missing"])
def test_runtime_members_require_protected_regular_files(fixture, member, change):
    path = getattr(fixture, member)
    if change == "symlink":
        original = path.with_name(path.name + ".original")
        path.rename(original)
        path.symlink_to(original)
    elif change == "hardlink":
        os.link(path, path.with_name(path.name + ".linked"))
    elif change == "writable":
        path.chmod(0o666)
    else:
        path.unlink()
        if change == "directory":
            path.mkdir()
        elif change == "fifo":
            os.mkfifo(path)
    with _refuses():
        runtime._verify_inventory(fixture.runtime, fixture.files)


@pytest.mark.parametrize("change", ["owner", "ancestor_writable", "ancestor_link"])
def test_runtime_ancestry_and_owner_are_checked(fixture, change):
    f = fixture
    if change == "owner":
        f.files = policy._ProtectedFiles(root=f.root, owner_uid=os.getuid() + 1)
    elif change == "ancestor_writable":
        f.runtime_dir.chmod(0o777)
    else:
        original = f.runtime_dir.with_name("runtime-original")
        f.runtime_dir.rename(original)
        f.runtime_dir.symlink_to(original, target_is_directory=True)
    with _refuses():
        runtime._verify_inventory(f.runtime, f.files)


@pytest.mark.parametrize("case", [
    "native_request", "native_launch_refusal", "captured_aliases", "distinct_equal_tuple", "closed_connection",
    "reinitialized_connection", "inventory_drift", "artifact_drift", "enrollment_drift",
    "module_replacement", "module_origin", "module_loader", "thread_change", "closed_capture",
    "preloaded_sqlite", "preloaded_extension", "preloaded_native", "wrong_abi", "wrong_sqlite_version",
])
def test_actual_compiled_capture_in_fresh_process(tmp_path, case, record_property):
    if not _COMPILED.is_file():
        pytest.skip("retained source-only compiled fixture is unavailable; no artifact was installed")
    if (sys.implementation.name != "cpython" or tuple(sys.version_info[:3]) != (3, 11, 15)
            or sysconfig.get_config_var("EXT_SUFFIX") != _COMPILED_SUFFIX):
        pytest.skip("retained compiled fixture requires matching CPython 3.11.15 and extension ABI")
    assert _COMPILED.stat().st_size == 1305528
    assert hashlib.sha256(_COMPILED.read_bytes()).hexdigest() == _COMPILED_HASH
    record_property("child_case", case)
    _run_fresh_child(tmp_path, case, _COMPILED)
