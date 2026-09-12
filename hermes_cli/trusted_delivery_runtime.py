"""Fixed root-owned executables used by the trusted delivery controller."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import posixpath
import stat
from pathlib import Path


TRUSTED_DELIVERY_RUNTIME_ROOT = Path("/opt/hermes-delivery-control")
TRUSTED_GCLOUD_SDK_ROOT = TRUSTED_DELIVERY_RUNTIME_ROOT / "google-cloud-sdk"
TRUSTED_GCLOUD_PATH = TRUSTED_GCLOUD_SDK_ROOT / "bin" / "gcloud"
TRUSTED_GCLOUD_VERSION_PATH = TRUSTED_GCLOUD_SDK_ROOT / "VERSION"
TRUSTED_GCLOUD_PROVENANCE_PATH = (
    TRUSTED_DELIVERY_RUNTIME_ROOT / "google-cloud-sdk.archive.json"
)
TRUSTED_GCLOUD_VERSION = "576.0.0"
TRUSTED_GCLOUD_ARCHIVE_URL = (
    "https://storage.googleapis.com/cloud-sdk-release/"
    "google-cloud-cli-576.0.0-linux-x86_64.tar.gz"
)
TRUSTED_GCLOUD_ARCHIVE_SHA256 = (
    "7094a08e8fc3772cdbfb1a8a1920300f52fec5e370c9f9c803c2a3c8824a32c2"
)
TRUSTED_GCLOUD_TREE_SHA256 = (
    "9772f5002bb355343613d23a66f0f44d5b4372d9398da3acd466464d5470a041"
)
TRUSTED_PYTHON_BUILD_TAG = "20260623"
TRUSTED_PYTHON_VERSION = "3.13.14"
TRUSTED_SQLITE_VERSION = "3.53.1"
TRUSTED_OPENSSL_VERSION = "3.5.7"
TRUSTED_PYTHON_ARCHIVE_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    "20260623/cpython-3.13.14%2B20260623-x86_64-unknown-linux-gnu-"
    "install_only_stripped.tar.gz"
)
TRUSTED_PYTHON_ARCHIVE_SHA256 = (
    "459ed79967acc207bef2ff5124dac35d74d5108528e37b15395d14e2922f2c92"
)
TRUSTED_PYTHON_ROOT = TRUSTED_DELIVERY_RUNTIME_ROOT / (
    f"python-{TRUSTED_PYTHON_VERSION}+{TRUSTED_PYTHON_BUILD_TAG}"
)
TRUSTED_PYTHON_PATH = TRUSTED_PYTHON_ROOT / "bin" / "python3.13"
TRUSTED_PYTHON_PROVENANCE_PATH = TRUSTED_DELIVERY_RUNTIME_ROOT / (
    f"python-{TRUSTED_PYTHON_VERSION}+{TRUSTED_PYTHON_BUILD_TAG}.archive.json"
)
TRUSTED_POLICY_RUNTIME_ROOT = Path("/usr/lib/hermes-delivery-control")
TRUSTED_POLICY_PYTHON = TRUSTED_POLICY_RUNTIME_ROOT / "venv" / "bin" / "python"
TRUSTED_WORKER_RUNTIME_ROOT = Path("/usr/lib/hermes-worker-runtime")
TRUSTED_WORKER_PYTHON = TRUSTED_WORKER_RUNTIME_ROOT / "venv" / "bin" / "python"
TRUSTED_GH_PATH = Path("/usr/bin/gh")


class TrustedDeliveryRuntimeError(RuntimeError):
    """A fixed control-plane executable is absent or mutable."""


def require_root_owned_executable(path: Path, *, root: Path) -> Path:
    """Validate one exact executable and every component below its fixed root."""

    path = Path(path)
    root = Path(root)
    try:
        path.relative_to(root)
        if path.resolve(strict=True) != path or root.resolve(strict=True) != root:
            raise TrustedDeliveryRuntimeError("trusted executable path is symlinked")
        relative = path.relative_to(root)
        candidates = [root]
        current = root
        for part in relative.parts:
            current = current / part
            candidates.append(current)
        for index, candidate in enumerate(candidates):
            metadata = candidate.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != 0
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise TrustedDeliveryRuntimeError(
                    "trusted executable path metadata is unsafe"
                )
            if index < len(candidates) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise TrustedDeliveryRuntimeError(
                    "trusted executable parent is not a directory"
                )
        executable = candidates[-1].lstat()
        if (
            not stat.S_ISREG(executable.st_mode)
            or executable.st_nlink != 1
            or not executable.st_mode & stat.S_IXUSR
            or not os.access(path, os.X_OK)
        ):
            raise TrustedDeliveryRuntimeError(
                "trusted executable metadata is invalid"
            )
    except TrustedDeliveryRuntimeError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrustedDeliveryRuntimeError(
            "trusted executable is unavailable"
        ) from exc
    return path


def _read_root_owned_runtime_file(
    path: Path,
    *,
    root: Path,
    maximum_bytes: int,
    exact_mode: int | None = None,
    allow_empty: bool = False,
    owner_uid: int = 0,
) -> bytes:
    """Read one bounded immutable runtime file without following links."""

    path = Path(path)
    root = Path(root)
    descriptor = -1
    try:
        path.relative_to(root)
        if root.resolve(strict=True) != root:
            raise TrustedDeliveryRuntimeError("trusted runtime root is symlinked")
        current = root
        for part in path.relative_to(root).parts[:-1]:
            current = current / part
            metadata = current.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != owner_uid
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise TrustedDeliveryRuntimeError(
                    "trusted runtime file parent metadata is unsafe"
                )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != owner_uid
            or before.st_nlink != 1
            or mode & 0o022
            or (exact_mode is not None and mode != exact_mode)
            or (not allow_empty and before.st_size <= 0)
            or before.st_size > maximum_bytes
        ):
            raise TrustedDeliveryRuntimeError(
                "trusted runtime file metadata is unsafe"
            )
        raw = os.read(descriptor, maximum_bytes + 1)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or len(raw) > maximum_bytes
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise TrustedDeliveryRuntimeError(
                "trusted runtime file changed while being read"
            )
        return raw
    except TrustedDeliveryRuntimeError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrustedDeliveryRuntimeError(
            "trusted runtime file is unavailable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _gcloud_tree_sha256(root: Path, *, owner_uid: int = 0) -> str:
    """Hash and metadata-validate every executable SDK input."""

    digest = hashlib.sha256()
    entries = 0
    try:
        for current_raw, directories, files in os.walk(
            root, topdown=True, followlinks=False,
        ):
            current = Path(current_raw)
            directories.sort()
            files.sort()
            for name in [*directories, *files]:
                path = current / name
                metadata = path.lstat()
                relative = path.relative_to(root).as_posix()
                entries += 1
                if entries > 200_000 or metadata.st_uid != owner_uid:
                    raise TrustedDeliveryRuntimeError(
                        "trusted gcloud tree metadata is unsafe"
                    )
                if stat.S_ISDIR(metadata.st_mode) and not path.is_symlink():
                    if stat.S_IMODE(metadata.st_mode) & 0o022:
                        raise TrustedDeliveryRuntimeError(
                            "trusted gcloud directory is writable"
                        )
                    digest.update(
                        f"D\0{relative}\0{stat.S_IMODE(metadata.st_mode):04o}\n".encode()
                    )
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    target = os.readlink(path)
                    if posixpath.isabs(target):
                        raise TrustedDeliveryRuntimeError(
                            "trusted gcloud symlink is absolute"
                        )
                    combined = posixpath.normpath(
                        posixpath.join(posixpath.dirname(relative), target)
                    )
                    if combined == ".." or combined.startswith("../"):
                        raise TrustedDeliveryRuntimeError(
                            "trusted gcloud symlink escapes its tree"
                        )
                    digest.update(f"L\0{relative}\0{target}\n".encode())
                    if name in directories:
                        directories.remove(name)
                    continue
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                ):
                    raise TrustedDeliveryRuntimeError(
                        "trusted gcloud file metadata is unsafe"
                    )
                raw = _read_root_owned_runtime_file(
                    path,
                    root=root,
                    maximum_bytes=max(1, metadata.st_size),
                    allow_empty=True,
                    owner_uid=owner_uid,
                )
                digest.update(
                    f"F\0{relative}\0{stat.S_IMODE(metadata.st_mode):04o}\0{len(raw)}\0".encode()
                )
                digest.update(raw)
                digest.update(b"\n")
    except TrustedDeliveryRuntimeError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrustedDeliveryRuntimeError(
            "trusted gcloud tree is unavailable"
        ) from exc
    return digest.hexdigest()


def _require_gcloud_tree_digest(expected: str) -> None:
    if _gcloud_tree_sha256(TRUSTED_GCLOUD_SDK_ROOT) != expected:
        raise TrustedDeliveryRuntimeError(
            "trusted gcloud installed tree does not match provenance"
        )


def require_trusted_gcloud() -> Path:
    executable = require_root_owned_executable(
        TRUSTED_GCLOUD_PATH,
        root=TRUSTED_DELIVERY_RUNTIME_ROOT,
    )
    version = _read_root_owned_runtime_file(
        TRUSTED_GCLOUD_VERSION_PATH,
        root=TRUSTED_DELIVERY_RUNTIME_ROOT,
        maximum_bytes=64,
    )
    try:
        version_text = version.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise TrustedDeliveryRuntimeError(
            "trusted gcloud version is invalid"
        ) from exc
    if version_text != TRUSTED_GCLOUD_VERSION:
        raise TrustedDeliveryRuntimeError(
            "trusted gcloud version does not match the reviewed archive"
        )
    # Runtime validation must work after the service has dropped to User=ab.
    # The root-only archive receipt remains part of the install-time asset
    # closure, while the executable closure is bound here to the independently
    # reproduced digest of the exact reviewed archive.
    _require_gcloud_tree_digest(TRUSTED_GCLOUD_TREE_SHA256)
    return executable


def _immutable_runtime_tree_sha256(root: Path, *, owner_uid: int = 0) -> str:
    """Validate and hash a symlink-free immutable Python runtime closure."""

    root = Path(root)
    try:
        root_metadata = root.lstat()
        if (
            root.is_symlink()
            or not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != owner_uid
            or stat.S_IMODE(root_metadata.st_mode) & 0o022
        ):
            raise TrustedDeliveryRuntimeError(
                "trusted Python runtime root metadata is unsafe"
            )
        digest = hashlib.sha256()
        entries = 0
        total_bytes = 0
        for current_raw, directories, files in os.walk(
            root, topdown=True, followlinks=False,
        ):
            current = Path(current_raw)
            directories.sort()
            files.sort()
            for name in directories:
                path = current / name
                metadata = path.lstat()
                relative = path.relative_to(root).as_posix()
                entries += 1
                if (
                    entries > 300_000
                    or stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != owner_uid
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                ):
                    raise TrustedDeliveryRuntimeError(
                        "trusted Python runtime directory is unsafe"
                    )
                digest.update(
                    f"D\0{relative}\0{stat.S_IMODE(metadata.st_mode):04o}\n".encode()
                )
            for name in files:
                path = current / name
                relative = path.relative_to(root).as_posix()
                if relative == "install-provenance.json":
                    continue
                metadata = path.lstat()
                entries += 1
                total_bytes += max(0, metadata.st_size)
                if (
                    entries > 300_000
                    or total_bytes > 4 * 1024 * 1024 * 1024
                    or stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != owner_uid
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                ):
                    raise TrustedDeliveryRuntimeError(
                        "trusted Python runtime file is unsafe"
                    )
                raw = _read_root_owned_runtime_file(
                    path,
                    root=root,
                    maximum_bytes=max(1, metadata.st_size),
                    allow_empty=True,
                    owner_uid=owner_uid,
                )
                digest.update(
                    f"F\0{relative}\0{stat.S_IMODE(metadata.st_mode):04o}"
                    f"\0{len(raw)}\0".encode()
                )
                digest.update(raw)
                digest.update(b"\n")
        return digest.hexdigest()
    except TrustedDeliveryRuntimeError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrustedDeliveryRuntimeError(
            "trusted Python runtime is unavailable"
        ) from exc


def require_trusted_python_base() -> tuple[Path, dict[str, str]]:
    """Validate the versioned hermetic CPython base used by both venvs."""

    executable = require_root_owned_executable(
        TRUSTED_PYTHON_PATH,
        root=TRUSTED_DELIVERY_RUNTIME_ROOT,
    )
    raw = _read_root_owned_runtime_file(
        TRUSTED_PYTHON_PROVENANCE_PATH,
        root=TRUSTED_DELIVERY_RUNTIME_ROOT,
        maximum_bytes=4096,
        exact_mode=0o400,
    )
    try:
        provenance = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustedDeliveryRuntimeError(
            "trusted Python base provenance is invalid"
        ) from exc
    if not isinstance(provenance, dict):
        raise TrustedDeliveryRuntimeError(
            "trusted Python base provenance is invalid"
        )
    tree_sha256 = provenance.get("tree_sha256")
    expected_static = {
        "archive_sha256": TRUSTED_PYTHON_ARCHIVE_SHA256,
        "archive_url": TRUSTED_PYTHON_ARCHIVE_URL,
        "build_tag": TRUSTED_PYTHON_BUILD_TAG,
        "python_version": TRUSTED_PYTHON_VERSION,
        "schema": "hermes-python-runtime/v1",
        "sqlite_version": TRUSTED_SQLITE_VERSION,
    }
    if (
        any(provenance.get(key) != value for key, value in expected_static.items())
        or set(provenance) != {
            *expected_static,
            "openssl_version",
            "sqlite_source_id",
            "tree_sha256",
        }
        or not isinstance(provenance.get("openssl_version"), str)
        or not provenance["openssl_version"].startswith(
            f"OpenSSL {TRUSTED_OPENSSL_VERSION} "
        )
        or not isinstance(provenance.get("sqlite_source_id"), str)
        or not provenance["sqlite_source_id"]
        or not isinstance(tree_sha256, str)
        or len(tree_sha256) != 64
        or any(character not in "0123456789abcdef" for character in tree_sha256)
    ):
        raise TrustedDeliveryRuntimeError(
            "trusted Python base provenance does not match the reviewed archive"
        )
    if not hmac.compare_digest(
        _immutable_runtime_tree_sha256(TRUSTED_PYTHON_ROOT), tree_sha256,
    ):
        raise TrustedDeliveryRuntimeError(
            "trusted Python base tree does not match provenance"
        )
    binding = {
        "python_archive_sha256": TRUSTED_PYTHON_ARCHIVE_SHA256,
        "python_archive_url": TRUSTED_PYTHON_ARCHIVE_URL,
        "python_openssl_version": str(provenance["openssl_version"]),
        "python_runtime_tree_sha256": tree_sha256,
        "python_version": TRUSTED_PYTHON_VERSION,
        "sqlite_source_id": str(provenance["sqlite_source_id"]),
        "sqlite_version": TRUSTED_SQLITE_VERSION,
    }
    return executable, binding


def _require_venv_uses_trusted_python(root: Path) -> None:
    raw = _read_root_owned_runtime_file(
        root / "venv" / "pyvenv.cfg",
        root=root,
        maximum_bytes=4096,
        exact_mode=0o644,
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TrustedDeliveryRuntimeError(
            "trusted Python venv metadata is invalid"
        ) from exc
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        key = key.strip().casefold()
        if not separator or not key or key in fields:
            raise TrustedDeliveryRuntimeError(
                "trusted Python venv metadata is invalid"
            )
        fields[key] = value.strip()
    if (
        fields.get("home") != str(TRUSTED_PYTHON_ROOT / "bin")
        or fields.get("executable") != str(TRUSTED_PYTHON_PATH)
        or fields.get("version") != TRUSTED_PYTHON_VERSION
        or fields.get("include-system-site-packages", "").casefold() != "false"
    ):
        raise TrustedDeliveryRuntimeError(
            "trusted Python venv does not use the reviewed hermetic base"
        )


def require_trusted_immutable_runtime(
    root: Path,
    *,
    executable: Path,
    schema: str,
) -> Path:
    """Require an installed runtime and its complete recorded byte closure."""

    root = Path(root)
    executable = require_root_owned_executable(Path(executable), root=root)
    raw = _read_root_owned_runtime_file(
        root / "install-provenance.json",
        root=root,
        maximum_bytes=4096,
        exact_mode=0o644,
    )
    try:
        provenance = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustedDeliveryRuntimeError(
            "trusted Python runtime provenance is invalid"
        ) from exc
    if not isinstance(provenance, dict):
        raise TrustedDeliveryRuntimeError(
            "trusted Python runtime provenance is invalid"
        )
    expected = provenance.get("tree_sha256")
    if (
        provenance.get("schema") != schema
        or not isinstance(expected, str)
        or len(expected) != 64
        or any(character not in "0123456789abcdef" for character in expected)
    ):
        raise TrustedDeliveryRuntimeError(
            "trusted Python runtime provenance does not match its contract"
        )
    if not hmac.compare_digest(
        _immutable_runtime_tree_sha256(root), expected,
    ):
        raise TrustedDeliveryRuntimeError(
            "trusted Python runtime tree does not match provenance"
        )
    _base, binding = require_trusted_python_base()
    if any(provenance.get(key) != value for key, value in binding.items()):
        raise TrustedDeliveryRuntimeError(
            "trusted Python runtime is not bound to the reviewed hermetic base"
        )
    _require_venv_uses_trusted_python(root)
    return executable


def require_trusted_gh() -> Path:
    return require_root_owned_executable(TRUSTED_GH_PATH, root=Path("/usr"))
