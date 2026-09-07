"""Select an administrator-required Kanban policy without granting admission.

This module is deliberately not wired into dispatch or completion yet. A loaded
provider is not permission to materialize a workspace, launch a worker, or finish
a task. Those boundaries still need live request and opened-database binding.

POSIX enrollment is fixed at /etc/hermes/required-kanban-policies.d/uid-N.toml.
Only initial absence means ordinary Hermes behavior. User config, profiles,
environment overrides and optional plugin discovery cannot remove an obligation.
The isolated service deployment is the concrete external consumer; its provider,
scope and eventual installation are not shipped or activated here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import importlib
import importlib.abc
import importlib.metadata
import importlib.machinery
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import threading
import tomllib
from types import MappingProxyType, ModuleType
from typing import Callable, Mapping
import uuid


_REGISTRY_DIRECTORY = Path("/etc/hermes/required-kanban-policies.d")
_ENTRY_POINT_GROUP = "hermes_agent.plugins"
_MAX_REGISTRATION = 64 * 1024
_MAX_FILE = 512 * 1024
_MAX_PACKAGE = 4 * 1024 * 1024
_IMPORT_LOCK = threading.RLock()


class RequiredPolicyError(RuntimeError):
    """An enrolled policy could not be selected or kept intact."""


class RequiredKanbanPolicy:
    """Base for the selected external provider, not an admission interface.

    The provider registers with ``ctx.register_kanban_policy(instance)``. There
    are intentionally no success, workspace-admission or completion methods in
    this first increment. Registration cannot substitute for either boundary.
    """


@dataclass(frozen=True)
class RequiredPolicyRegistration:
    """One live selection; its generation is not a transferable capability."""

    uid: int
    generation: str
    distribution: str
    entry_point: str
    scope: Mapping[str, object] = field(repr=False)
    provider: RequiredKanbanPolicy = field(repr=False)
    _verify: Callable[[], None] = field(repr=False, compare=False)
    _distribution: importlib.metadata.Distribution = field(repr=False, compare=False)
    _entry_point: importlib.metadata.EntryPoint = field(repr=False, compare=False)

    def check_integrity(self) -> None:
        """Recheck this selection's files and identity; grant no task access."""
        self._verify()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RequiredPolicyError(message)


def _file_identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory_identity(info: os.stat_result) -> tuple:
    # Unrelated additions to /etc do not invalidate an enrolled package.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def _absolute_path(value: object) -> Path:
    _require(isinstance(value, str) and "\0" not in value,
             "Required policy path is invalid.")
    result = Path(value)
    _require(result.is_absolute() and str(result) == value and ".." not in result.parts,
             "Required policy paths must be exact absolute paths.")
    return result


def _relative_path(value: object) -> PurePosixPath:
    _require(isinstance(value, str) and "\0" not in value and "\\" not in value,
             "Required policy package path is invalid.")
    result = PurePosixPath(value)
    _require(not result.is_absolute() and str(result) == value
             and result.parts and ".." not in result.parts,
             "Required policy package paths must stay inside the installation.")
    return result


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    _require(type(value) in (str, int, bool), "Required policy scope contains an unsupported value.")
    return value


@dataclass(frozen=True)
class _Snapshot:
    path: Path
    data: bytes = field(repr=False)
    identity: tuple
    ancestors: tuple


class _ProtectedFiles:
    """Descriptor-relative reads; private constructor parameters support fixtures.

    Production always uses root ownership and the filesystem root. No public
    selector, CLI, config or environment value can change those anchors.
    """

    def __init__(self, root: Path = Path("/"), owner_uid: int = 0):
        self.root = root
        self.owner_uid = owner_uid

    def read(self, path: Path, *, limit: int, missing_ok: bool = False) -> _Snapshot | None:
        directory_fds = []
        file_fd = None
        try:
            relative = path.relative_to(self.root)
            _require(relative.parts and ".." not in relative.parts, "Required policy path escaped its anchor.")
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            current = os.open(self.root, flags)
            directory_fds.append(current)
            ancestors = []
            for component in (None, *relative.parts[:-1]):
                if component is not None:
                    current = os.open(component, flags, dir_fd=current)
                    directory_fds.append(current)
                info = os.fstat(current)
                _require(stat.S_ISDIR(info.st_mode) and info.st_uid == self.owner_uid
                         and not info.st_mode & 0o022,
                         "Required policy ancestry is not protected.")
                ancestors.append(_directory_identity(info))
            file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                              | os.O_CLOEXEC, dir_fd=current)
            before = os.fstat(file_fd)
            _require(stat.S_ISREG(before.st_mode) and before.st_uid == self.owner_uid
                     and not before.st_mode & 0o022 and before.st_nlink == 1
                     and 0 < before.st_size <= limit,
                     "Required policy file is not a protected bounded regular file.")
            chunks = []
            remaining = before.st_size + 1
            while remaining:
                chunk = os.read(file_fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.stat(relative.name, dir_fd=current, follow_symlinks=False)
            _require(len(data) == before.st_size and _file_identity(before) == _file_identity(after)
                     and _file_identity(before) == _file_identity(os.fstat(file_fd))
                     and tuple(ancestors) == tuple(_directory_identity(os.fstat(fd)) for fd in directory_fds),
                     "Required policy file changed while reading.")
            return _Snapshot(path, data, _file_identity(before), tuple(ancestors))
        except FileNotFoundError:
            if missing_ok and file_fd is None:
                return None
            raise RequiredPolicyError("An enrolled policy file is missing.") from None
        except (OSError, ValueError):
            raise RequiredPolicyError("Required policy file could not be read safely.") from None
        finally:
            if file_fd is not None:
                os.close(file_fd)
            for fd in reversed(directory_fds):
                os.close(fd)

    def unchanged(self, snapshot: _Snapshot) -> None:
        current = self.read(snapshot.path, limit=max(_MAX_FILE, len(snapshot.data)))
        _require(current == snapshot, "Required policy registration or package changed.")


class _RegistrationContext:
    """A single owned registration, separate from best-effort observer hooks."""

    def __init__(self, scope: Mapping[str, object], package: str):
        self.scope = scope
        self._package = package
        self._provider = None

    def register_kanban_policy(self, provider: RequiredKanbanPolicy) -> None:
        _require(self._provider is None and isinstance(provider, RequiredKanbanPolicy)
                 and (type(provider).__module__ == self._package
                      or type(provider).__module__.startswith(self._package + ".")),
                 "The selected package must register exactly one required policy.")
        self._provider = provider


class _VerifiedPackage(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Import only captured, verified source, including later relative imports.

    Bytecode, namespace packages, ambient path lookups and unlisted package
    members cannot replace an enrolled module. Dependencies outside this package
    remain the responsibility of the protected interpreter/deployment.
    """

    def __init__(self, package: str, modules: dict, verify: Callable[[], None]):
        self.package = package
        self.sources = modules
        self.verify = verify
        self.loaded: dict[str, ModuleType] = {}
        self.alive = True

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.package and not fullname.startswith(self.package + "."):
            return None
        _require(self.alive, "Required policy package is no longer registered.")
        self.verify()
        _require(fullname in self.sources, "Required policy imported an unenrolled package member.")
        snapshot, package = self.sources[fullname]
        spec = importlib.machinery.ModuleSpec(fullname, self, origin=str(snapshot.path), is_package=package)
        spec.has_location = True
        if package:
            spec.submodule_search_locations = [str(snapshot.path.parent)]
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        self.verify()
        snapshot, _package = self.sources[module.__name__]
        self.loaded[module.__name__] = module
        exec(compile(snapshot.data, str(snapshot.path), "exec", dont_inherit=True), module.__dict__)

    def check_modules(self):
        _require(self.alive and any(finder is self for finder in sys.meta_path),
                 "Required policy import registration changed.")
        for name, module in self.loaded.items():
            snapshot, package = self.sources[name]
            _require(sys.modules.get(name) is module and module.__spec__ is not None
                     and module.__spec__.loader is self and module.__spec__.origin == str(snapshot.path)
                     and module.__file__ == str(snapshot.path)
                     and (not package or module.__path__ == [str(snapshot.path.parent)]),
                     "Required policy module identity or origin changed.")

    def discard(self):
        self.alive = False
        sys.meta_path[:] = [finder for finder in sys.meta_path if finder is not self]
        for name, module in self.loaded.items():
            if sys.modules.get(name) is module:
                del sys.modules[name]


class _RequiredPolicyRegistry:
    """Private library seam for hermetic fixtures; production has fixed anchors."""

    def __init__(self, directory: Path, *, files: _ProtectedFiles | None = None,
                 identity: Callable[[], tuple[int, int]] | None = None):
        self.directory = directory
        self.files = files or _ProtectedFiles()
        self.identity = identity or (lambda: (os.getuid(), os.geteuid()))
        self._selected: dict[int, RequiredPolicyRegistration] = {}
        self._seen: dict[int, _Snapshot] = {}
        self._failed = False
        self._lock = threading.RLock()

    def select(self) -> RequiredPolicyRegistration | None:
        with self._lock:
            _require(not self._failed, "Required policy selection was invalidated.")
            try:
                return self._select()
            except RequiredPolicyError:
                # An unsafe or disappearing enrollment is not initial absence
                # on a later call. Recovery requires a new registry/process.
                self._failed = True
                raise

    def _select(self) -> RequiredPolicyRegistration | None:
        # Check the existing obligation before consulting the current UID:
        # an identity change cannot turn a live enrollment into absence.
        for selected in self._selected.values():
            selected.check_integrity()
        real_uid, effective_uid = self.identity()
        _require(type(real_uid) is int and real_uid >= 0
                 and type(effective_uid) is int and effective_uid >= 0,
                 "Required policy needs the operating-system user identity.")
        enrollments = {}
        for uid in dict.fromkeys((real_uid, effective_uid)):
            filename = self.directory / f"uid-{uid}.toml"
            snapshot = self.files.read(filename, limit=_MAX_REGISTRATION, missing_ok=uid not in self._seen)
            if snapshot is not None:
                previous = self._seen.setdefault(uid, snapshot)
                _require(previous == snapshot, "Required policy enrollment changed in this process.")
                enrollments[uid] = snapshot
        if not enrollments:
            return None
        _require(real_uid == effective_uid, "An enrolled policy cannot run with differing real and effective users.")
        if real_uid in self._selected:
            selected = self._selected[real_uid]
            selected.check_integrity()
            return selected
        selected = self._load(real_uid, enrollments[real_uid])
        self._selected[real_uid] = selected
        return selected

    def _load(self, uid: int, enrollment: _Snapshot) -> RequiredPolicyRegistration:
        try:
            document = tomllib.loads(enrollment.data.decode("utf-8"))
            _require(set(document) == {"schema_version", "uid", "provider", "files", "scope"}
                     and type(document["schema_version"]) is int and document["schema_version"] == 1
                     and type(document["uid"]) is int and document["uid"] == uid,
                     "Required policy enrollment schema or user does not match.")
            provider = document["provider"]
            _require(isinstance(provider, dict) and set(provider) == {
                "distribution", "version", "site_packages", "dist_info", "entry_point", "entry_point_value"
            } and all(isinstance(value, str) and value for value in provider.values()),
                "Required policy provider selection is incomplete.")
            site = _absolute_path(provider["site_packages"])
            dist_info = _relative_path(provider["dist_info"])
            _require(len(dist_info.parts) == 1 and dist_info.name.endswith(".dist-info"),
                     "Required policy must name one installed distribution.")
            entry_value = provider["entry_point_value"]
            _require(re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?::register)?", entry_value) is not None,
                     "Required policy entry point must name a module or its register function.")
            module_name = entry_value.split(":")[0]
            package = module_name.split(".")[0]
            scope = document["scope"]
            _require(isinstance(scope, dict) and bool(scope), "Required policy scope is missing.")
            scope = _freeze(scope)
            hashes = document["files"]
            _require(isinstance(hashes, dict) and 0 < len(hashes) <= 128,
                     "Required policy package manifest is missing or too large.")
            snapshots = {str(dist_info / "METADATA"), str(dist_info / "entry_points.txt")}
            _require(snapshots.issubset(hashes), "Required policy distribution metadata is not enrolled.")
            snapshots = {}
            modules = {}
            total_bytes = 0
            for relative, digest in hashes.items():
                member = _relative_path(relative)
                _require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
                         "Required policy package digest is invalid.")
                snapshot = self.files.read(site.joinpath(*member.parts), limit=_MAX_FILE)
                total_bytes += len(snapshot.data)
                _require(total_bytes <= _MAX_PACKAGE, "Required policy package is too large.")
                _require(hashlib.sha256(snapshot.data).hexdigest() == digest,
                         "Required policy package digest does not match.")
                snapshots[relative] = snapshot
                if member.parts[0] == package and member.suffix == ".py":
                    components = member.with_suffix("").parts
                    is_package = components[-1] == "__init__"
                    if is_package:
                        components = components[:-1]
                    _require(components and all(component.isidentifier() for component in components),
                             "Required policy module path is unsupported.")
                    name = ".".join(components)
                    _require(name not in modules, "Required policy module has ambiguous origins.")
                    modules[name] = (snapshot, is_package)
                else:
                    _require(relative in {str(dist_info / "METADATA"), str(dist_info / "entry_points.txt")},
                             "Required policy manifest contains an unsupported package member.")
            _require(package in modules and modules[package][1] and module_name in modules,
                     "Required policy must include its concrete package and entry module.")
            for name in modules:
                parent = name.rpartition(".")[0]
                _require(not parent or (parent in modules and modules[parent][1]),
                         "Required policy namespace packages are unsupported.")
            distribution = importlib.metadata.PathDistribution(site / str(dist_info))
            normalized = lambda name: re.sub(r"[-_.]+", "-", name).lower()
            _require(normalized(distribution.metadata.get("Name", "")) == normalized(provider["distribution"])
                     and distribution.version == provider["version"],
                     "Required policy installed distribution identity differs.")
            entries = [entry for entry in distribution.entry_points
                       if entry.group == _ENTRY_POINT_GROUP and entry.name == provider["entry_point"]]
            _require(len(entries) == 1 and entries[0].value == entry_value,
                     "Required policy installed entry point differs or is ambiguous.")
            # Retain the original selected object; do not globally rediscover by name.
            entry = entries[0]
            return self._import(uid, enrollment, tuple(snapshots.values()), scope, provider,
                                distribution, entry, package, modules)
        except RequiredPolicyError:
            raise
        except Exception:
            raise RequiredPolicyError("Required policy enrollment or distribution could not be loaded.") from None

    def _import(self, uid, enrollment, snapshots, scope, provider, distribution, entry, package, modules):
        invalid = False

        def verify_files():
            nonlocal invalid
            _require(not invalid, "Required policy registration was invalidated.")
            try:
                _require(self.identity() == (uid, uid), "Required policy operating-system identity changed.")
                self.files.unchanged(enrollment)
                for snapshot in snapshots:
                    self.files.unchanged(snapshot)
            except Exception:
                invalid = True
                raise RequiredPolicyError("Required policy registration no longer matches its protected files or user.") from None

        finder = _VerifiedPackage(package, modules, verify_files)

        def verify():
            nonlocal invalid
            verify_files()
            try:
                finder.check_modules()
            except RequiredPolicyError:
                invalid = True
                raise

        with _IMPORT_LOCK:
            verify_files()
            _require(not any(name == package or name.startswith(package + ".") for name in sys.modules),
                     "Required policy package was already imported outside this registration.")
            sys.meta_path.insert(0, finder)
            try:
                module = importlib.import_module(entry.module)
                register = getattr(module, entry.attr or "register", None)
                _require(callable(register) and getattr(register, "__module__", None) in finder.loaded,
                         "Required policy entry point does not expose its own registration function.")
                context = _RegistrationContext(scope, package)
                _require(register(context) is None and context._provider is not None,
                         "Required policy did not register its provider.")
                verify()
                return RequiredPolicyRegistration(uid, uuid.uuid4().hex, provider["distribution"], entry.value,
                                                  scope, context._provider, verify, distribution, entry)
            except BaseException:
                finder.discard()
                raise


_production_registry = _RequiredPolicyRegistry(_REGISTRY_DIRECTORY)


def select_required_policy() -> RequiredPolicyRegistration | None:
    """Load a required provider for this OS identity, with no caller overrides.

    This currently has no production callers. It performs no workspace, board,
    credential or completion action. Native non-POSIX installations retain their
    ordinary behavior; this enrollment contract is POSIX-only.
    """
    if os.name != "posix":
        return None
    return _production_registry.select()
