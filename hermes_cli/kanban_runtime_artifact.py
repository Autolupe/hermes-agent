"""Verified SQLite extension bytes and a retained descriptor, not runtime trust.

The private capture routine is useful to a future protected bootstrap and to
fresh-process fixtures. It does not authenticate the running interpreter,
stdlib, dependencies, build headers or whole process. The public bootstrap
therefore remains unsupported. No receipt or completeness flag can enable it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import threading
from types import MethodDescriptorType

from hermes_cli.kanban_policy import RequiredPolicyError, _absolute_path, _require


_CAPABILITY = "sqlite-opened-identity-v1"
_MAX_INVENTORY = 64 * 1024
_MAX_EXTENSION = 4 * 1024 * 1024
# Linux UAPI: linux/fcntl.h, asm-generic/fcntl.h and linux/memfd.h.
# The supported Python build omits these wrappers/constants. These documented
# command/flag values are not architecture-specific raw syscall numbers.
_F_ADD_SEALS = 1024 + 9
_F_GET_SEALS = 1024 + 10
_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008


def _new_memfd():
    libc = ctypes.CDLL(None, use_errno=True)
    create = libc.memfd_create
    create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    create.restype = ctypes.c_int
    descriptor = create(b"hermes-sqlite-extension", 0x0001 | 0x0002)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "Sealed extension capture is unavailable.")
    return descriptor


def bootstrap_required_policy():
    """No protected production startup exists; refuse before provider loading."""
    raise RequiredPolicyError("Complete runtime provenance is unsupported.")


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Extension artifact inventory has duplicate keys.")
        result[key] = value
    return result


def _nonfinite(_value):
    raise RequiredPolicyError("Extension artifact inventory contains a nonfinite number.")


def _digest(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
             "Extension artifact digest is invalid.")
    return value


@dataclass(frozen=True)
class _VerifiedExtensionInventory:
    files: object = field(repr=False, compare=False)
    inventory: object = field(repr=False)
    artifact: object = field(repr=False)
    python_version: tuple[int, int, int]
    extension_suffix: str
    sqlite_version: str

    def unchanged(self):
        self.files.unchanged(self.inventory)
        self.files.unchanged(self.artifact)


def _verify_inventory(runtime, files):
    """Read the closed extension-only inventory using the existing file rules."""
    try:
        _require(type(runtime) is dict and set(runtime) == {
            "capability", "inventory", "inventory_sha256"
        } and runtime["capability"] == _CAPABILITY,
            "Required extension artifact selection is invalid.")
        inventory = files.read(_absolute_path(runtime["inventory"]), limit=_MAX_INVENTORY)
        _require(hashlib.sha256(inventory.data).hexdigest() == _digest(runtime["inventory_sha256"]),
                 "Extension artifact inventory digest does not match.")
        document = json.loads(inventory.data.decode("utf-8"), object_pairs_hook=_object,
                              parse_constant=_nonfinite)
        _require(type(document) is dict and set(document) == {
            "schema_version", "kind", "python", "sqlite_version", "artifact"
        } and type(document["schema_version"]) is int and document["schema_version"] == 1
            and document["kind"] == "sqlite-extension-artifact",
            "Extension artifact inventory schema is invalid.")
        python = document["python"]
        _require(type(python) is dict and set(python) == {
            "implementation", "version", "extension_suffix"
        } and python["implementation"] == "cpython",
            "Extension artifact interpreter description is invalid.")
        version = python["version"]
        suffix = python["extension_suffix"]
        _require(type(version) is list and len(version) == 3
                 and all(type(part) is int and 0 <= part < 1000 for part in version)
                 and type(suffix) is str and len(suffix) <= 128
                 and re.fullmatch(r"\.cpython-[0-9]+-[a-zA-Z0-9_-]+\.so", suffix) is not None,
                 "Extension artifact ABI description is invalid.")
        sqlite_version = document["sqlite_version"]
        _require(type(sqlite_version) is str and len(sqlite_version) <= 32
                 and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", sqlite_version) is not None,
                 "Extension artifact SQLite version is invalid.")
        artifact = document["artifact"]
        _require(type(artifact) is dict and set(artifact) == {"path", "size", "sha256"}
                 and type(artifact["size"]) is int and 0 < artifact["size"] <= _MAX_EXTENSION,
                 "Extension artifact entry is invalid or too large.")
        path = _absolute_path(artifact["path"])
        _require(path.name == "_sqlite3" + suffix, "Extension artifact filename differs from its ABI.")
        snapshot = files.read(path, limit=_MAX_EXTENSION)
        _require(len(snapshot.data) == artifact["size"]
                 and hashlib.sha256(snapshot.data).hexdigest() == _digest(artifact["sha256"]),
                 "Extension artifact bytes do not match their inventory.")
        return _VerifiedExtensionInventory(files, inventory, snapshot, tuple(version), suffix, sqlite_version)
    except RequiredPolicyError:
        raise
    except Exception:
        raise RequiredPolicyError("Extension artifact inventory could not be verified.") from None


class _OriginalConnectionIdentity:
    """Observe one original object; equal tuples never admit a replacement."""

    def __init__(self, capture, connection):
        self._capture = capture
        self._connection = connection
        self._identity = capture._observe(connection)
        self._failed = False

    def check(self, connection):
        _require(not self._failed, "Original connection identity was invalidated.")
        try:
            _require(connection is self._connection, "The original connection object is required.")
            current = self._capture._observe(self._connection)
            _require(current == self._identity, "Original connection incarnation or identity changed.")
            return current
        except BaseException:
            self._failed = True
            raise


class _CapturedSQLiteExtension:
    """Live descriptor from checked extension bytes; not complete runtime proof."""

    def __init__(self, verified, descriptor_fd, module, loader, path):
        self._verified_inventory = verified
        self._fd = descriptor_fd
        info = os.fstat(descriptor_fd)
        self._memfd_identity = (info.st_dev, info.st_ino, info.st_mode, info.st_size)
        self._module = module
        self._loader = loader
        self._path = path
        self._base = module.Connection
        self._descriptor = self._base.__dict__.get("_opened_identity")
        self._context = (os.getpid(), threading.get_ident(), os.getuid(), os.geteuid(), os.getgid(), os.getegid())
        self._failed = False
        self._registration_check = None
        _require(isinstance(self._descriptor, MethodDescriptorType)
                 and self._descriptor.__objclass__ is self._base
                 and self._descriptor.__name__ == "_opened_identity",
                 "The compiled original-connection descriptor is unavailable.")

    def _attach_registration(self, verify):
        _require(self._registration_check is None and callable(verify),
                 "Extension artifact capture already belongs to a registration.")
        self._registration_check = verify

    def check_integrity(self):
        _require(not self._failed and self._fd is not None, "Extension artifact capture was invalidated.")
        try:
            import fcntl

            _require(self._context == (os.getpid(), threading.get_ident(), os.getuid(), os.geteuid(), os.getgid(), os.getegid()),
                     "Extension artifact capture changed process, thread or account.")
            self._verified_inventory.unchanged()
            _require(sys.modules.get("_sqlite3") is self._module
                     and self._module.__spec__ is not None
                     and self._module.__spec__.loader is self._loader
                     and self._module.__spec__.origin == self._path
                     and self._module.__file__ == self._path
                     and self._base.__dict__.get("_opened_identity") is self._descriptor,
                     "Captured extension module identity changed.")
            info = os.fstat(self._fd)
            _require(fcntl.fcntl(self._fd, _F_GET_SEALS) & _SEALS == _SEALS
                     and (info.st_dev, info.st_ino, info.st_mode, info.st_size) == self._memfd_identity
                     and info.st_size == len(self._verified_inventory.artifact.data),
                     "Captured extension bytes are no longer sealed.")
        except BaseException:
            self._failed = True
            raise

    def _observe(self, connection):
        try:
            if self._registration_check is not None:
                self._registration_check()
            self.check_integrity()
            result = self._descriptor(connection)
            _require(type(result) is tuple and len(result) == 6
                     and all(type(value) is int and value >= 0 for value in result),
                     "Compiled connection identity result is invalid.")
            self.check_integrity()
            if self._registration_check is not None:
                self._registration_check()
            return result
        except RequiredPolicyError:
            raise
        except Exception:
            raise RequiredPolicyError("The original compiled connection identity is unavailable.") from None

    def bind_original(self, connection):
        return _OriginalConnectionIdentity(self, connection)

    def close(self):
        self._failed = True
        if self._fd is not None:
            descriptor, self._fd = self._fd, None
            os.close(descriptor)


def _capture_verified_extension(verified):
    """Capture actual checked bytes before SQLite/provider/database imports.

    This private operation does not verify the running interpreter or its
    dependencies. It cannot be selected by CLI, environment or enrollment.
    Production startup must prove that missing trust chain before using it.
    """
    descriptor_fd = None
    try:
        _require(type(verified) is _VerifiedExtensionInventory, "A verified extension inventory is required.")
        _require(sys.platform == "linux" and sys.implementation.name == "cpython"
                 and tuple(sys.version_info[:3]) == verified.python_version
                 and verified.extension_suffix in importlib.machinery.EXTENSION_SUFFIXES
                 and verified.extension_suffix.startswith("." + sys.implementation.cache_tag + "-"),
                 "Extension artifact does not match the running interpreter ABI.")
        import fcntl

        _require(not any(name == "sqlite3" or name.startswith("sqlite3.")
                         or name == "_sqlite3" or name in {
                             "hermes_cli.kanban_db", "hermes_cli.sqlite_safe_read",
                             "hermes_cli.kanban_workspace_policy"
                         } for name in sys.modules),
                 "SQLite or native database code was loaded before descriptor capture.")
        verified.unchanged()
        # Native extension initialization can execute in module_from_spec.
        # Load only the captured approved bytes, not a reopened source pathname.
        descriptor_fd = _new_memfd()
        pending = memoryview(verified.artifact.data)
        while pending:
            written = os.write(descriptor_fd, pending)
            _require(written > 0, "Captured extension bytes could not be written.")
            pending = pending[written:]
        fcntl.fcntl(descriptor_fd, _F_ADD_SEALS, _SEALS)
        verified.unchanged()
        path = f"/proc/self/fd/{descriptor_fd}"
        loader = importlib.machinery.ExtensionFileLoader("_sqlite3", path)
        spec = importlib.util.spec_from_file_location("_sqlite3", path, loader=loader)
        module = importlib.util.module_from_spec(spec)
        # Retain even a later-refused native module. Retrying native loading in
        # this process cannot restore the fresh-start condition.
        sys.modules["_sqlite3"] = module
        loader.exec_module(module)
        _require(module.sqlite_version == verified.sqlite_version,
                 "Captured extension reports another SQLite version.")
        captured = _CapturedSQLiteExtension(verified, descriptor_fd, module, loader, path)
        captured.check_integrity()
        descriptor_fd = None  # The live capture now owns this sealed descriptor.
        return captured
    except RequiredPolicyError:
        raise
    except Exception:
        raise RequiredPolicyError("Extension artifact could not be captured safely.") from None
    finally:
        if descriptor_fd is not None:
            os.close(descriptor_fd)
