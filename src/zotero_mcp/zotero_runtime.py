"""Cross-platform runtime configuration for the Zotero MCP server."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

CONFIG_FILE_ENV = "ZOTERO_MCP_CONFIG"
CONFIG_DIR_ENV = "ZOTERO_MCP_CONFIG_DIR"


class RuntimeConfigError(RuntimeError):
    """Raised when the optional user configuration is invalid."""


def is_windows() -> bool:
    return os.name == "nt" or platform.system().casefold() == "windows"


def is_wsl() -> bool:
    if is_windows():
        return False
    release = platform.release().casefold()
    return "microsoft" in release or "wsl" in release


def platform_name() -> str:
    if is_windows():
        return "windows"
    if is_wsl():
        return "wsl"
    return "posix"


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        return expand_path(override)
    if is_windows():
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            base = expand_path(appdata)
        else:
            profile = windows_user_profile()
            base = (
                Path(profile) / "AppData" / "Roaming"
                if profile
                else Path.cwd() / ".config"
            )
        return base / "zotero-mcp"
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = expand_path(xdg_config) if xdg_config else Path.home() / ".config"
    return base / "zotero-mcp"


def config_path() -> Path:
    override = os.environ.get(CONFIG_FILE_ENV, "").strip()
    return expand_path(override) if override else config_dir() / "config.toml"


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeConfigError(f"Invalid Zotero MCP config: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeConfigError(f"Zotero MCP config must contain a TOML table: {path}")
    return data


def config_value(section: str, key: str) -> Any:
    table = load_config().get(section, {})
    if not isinstance(table, dict):
        raise RuntimeConfigError(f"Config section [{section}] must be a TOML table")
    return table.get(key)


def config_string(section: str, key: str) -> str | None:
    value = config_value(section, key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeConfigError(
            f"Config value {section}.{key} must be a non-empty string"
        )
    return value.strip()


def config_positive_int(section: str, key: str) -> int | None:
    value = config_value(section, key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise RuntimeConfigError(
            f"Config value {section}.{key} must be a positive integer"
        )
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigError(
            f"Config value {section}.{key} must be a positive integer"
        ) from exc
    if number < 1:
        raise RuntimeConfigError(
            f"Config value {section}.{key} must be a positive integer"
        )
    return number


def config_bool(section: str, key: str, *, default: bool = False) -> bool:
    value = config_value(section, key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise RuntimeConfigError(f"Config value {section}.{key} must be a boolean")
    return value


def configured_path(env_name: str, section: str, key: str) -> Path | None:
    override = os.environ.get(env_name, "").strip()
    if override:
        return expand_path(override)
    value = config_string(section, key)
    return expand_path(value) if value else None


def configured_command(section: str, key: str, env_name: str, fallback: str) -> str:
    value = (
        os.environ.get(env_name, "").strip() or config_string(section, key) or fallback
    )
    executable = shutil.which(value)
    if executable:
        return executable
    path = expand_path(value)
    return str(path.resolve()) if path.is_file() else ""


def default_secret_path(name: str) -> Path:
    return config_dir() / name


@contextmanager
def exclusive_file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Serialize short cross-process updates through a one-byte lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(descriptor, mode, 1)
        else:
            import fcntl

            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            fcntl.flock(descriptor, operation)
        locked = True
        yield
    finally:
        try:
            if locked and os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            elif locked:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def windows_path_to_wsl_path(value: str) -> Path | None:
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", value.strip())
    if not match:
        return None
    drive = match.group(1).lower()
    rest = match.group(2).replace("\\", "/")
    return Path(f"/mnt/{drive}/{rest}")


def windows_user_profile() -> str | None:
    configured = os.environ.get("USERPROFILE", "").strip()
    if configured:
        return configured
    if is_windows():
        try:
            return str(Path.home())
        except RuntimeError:
            pass
    elif not is_wsl():
        return None
    command = shutil.which("cmd.exe")
    if not command:
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        root = windows_path_to_wsl_path(system_root) if is_wsl() else Path(system_root)
        candidate = root / "System32" / "cmd.exe" if root else None
        command = str(candidate) if candidate and candidate.is_file() else None
    if not command:
        return None
    try:
        result = subprocess.run(
            [command, "/d", "/u", "/s", "/c", "echo %USERPROFILE%"],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        value = result.stdout.decode("utf-16-le").lstrip("\ufeff").strip()
    except UnicodeDecodeError:
        return None
    return value if re.match(r"^[A-Za-z]:[\\/]", value) else None
