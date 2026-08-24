"""Cross-platform setup guidance for Zotero MCP and optional integrations."""

from __future__ import annotations

import argparse
import getpass
import importlib.metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import (
    mineru_client,
    zotero_local,
    zotero_runtime,
    zotero_translate,
    zotero_write,
)

ZOTERO_KEY_URL = "https://www.zotero.org/settings/keys/new"
MINERU_TOKEN_URL = "https://mineru.net/apiManage/token"
SCIVERSE_URL = "https://sciverse.space/"
PAPER_LOOKUP_URL = (
    "https://github.com/K-Dense-AI/scientific-agent-skills/"
    "tree/main/skills/paper-lookup"
)
PDF2ZH_URL = "https://github.com/guaguastandup/zotero-pdf2zh"
QMD_PACKAGE = "@tobilu/qmd"
DEFAULT_QMD_COLLECTION = "zotero-mineru"
REQUIRED_DISTRIBUTIONS = ("mcp", "pydantic", "anyio", "requests")
SCIVERSE_SECRET_NAME = "sciverse_api_token.secret"


def component(
    status: str,
    summary: str,
    *,
    action: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "summary": summary}
    if action:
        result["action"] = action
    if details:
        result["details"] = details
    return result


def optional_or_manual(profile: str) -> str:
    return "manual_action_required" if profile == "full" else "optional"


def default_sciverse_token_path() -> Path:
    return zotero_runtime.default_secret_path(SCIVERSE_SECRET_NAME)


def private_secret_file_error(path: Path) -> str:
    try:
        if path.is_symlink():
            return "secret file must not be a symbolic link"
        metadata = path.stat()
    except OSError as exc:
        return f"secret file cannot be inspected: {type(exc).__name__}: {exc}"
    if not stat.S_ISREG(metadata.st_mode):
        return "secret path must be a regular file"
    if metadata.st_size < 1:
        return "secret file is empty"
    if os.name != "nt":
        if metadata.st_uid != os.getuid():
            return "secret file must be owned by the current user"
        if metadata.st_mode & 0o077:
            return "secret file permissions must not allow group or other access"
    return ""


def configured_command(section: str, key: str, env_name: str, fallback: str) -> str:
    return zotero_runtime.configured_command(section, key, env_name, fallback)


def dependency_status() -> dict[str, Any]:
    versions: dict[str, str | None] = {}
    for distribution in REQUIRED_DISTRIBUTIONS:
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = None
    missing = [name for name, version in versions.items() if version is None]
    if missing:
        return component(
            "failed",
            f"Missing Python packages: {', '.join(missing)}",
            action="Run python -m pip install . in the intended Python environment.",
            details={"versions": versions},
        )
    return component(
        "ready", "Python dependencies are installed.", details={"versions": versions}
    )


def local_api_status() -> dict[str, Any]:
    try:
        value = zotero_local.ping_status()
    except Exception as exc:  # noqa: BLE001 - setup report captures integration failures
        return component(
            "manual_action_required",
            "Zotero Local API is not reachable.",
            action=(
                "Start Zotero and enable Settings > Advanced > Allow other applications "
                "on this computer to communicate with Zotero, then rerun setup plan."
            ),
            details={
                "error": f"{type(exc).__name__}: {exc}",
                "candidates": zotero_local.local_api_candidates(),
            },
        )
    value.pop("sample_item", None)
    return component("ready", "Zotero Local API is reachable.", details=value)


def storage_status() -> dict[str, Any]:
    storage = zotero_local.storage_root()
    if storage.is_dir():
        return component(
            "ready",
            "Zotero attachment storage is locally accessible.",
            details={"path": str(storage)},
        )
    return component(
        "manual_action_required",
        "Zotero attachment storage is not locally accessible.",
        action=(
            "Download Zotero attachments to this computer or set zotero.storage in config.toml. "
            "Zotero Storage and WebDAV are both acceptable sync methods."
        ),
        details={"path": str(storage)},
    )


def web_api_status(profile: str) -> dict[str, Any]:
    try:
        value = zotero_write.web_api_status()
    except Exception as exc:  # noqa: BLE001 - setup report captures integration failures
        return component(
            optional_or_manual(profile),
            "Zotero Web API credentials are not ready.",
            action=(
                f"Create a personal-library write key at {ZOTERO_KEY_URL}, then run "
                "zotero-mcp setup save-secret zotero. Skip this in read-only use."
            ),
            details={"error": f"{type(exc).__name__}: {exc}"},
        )
    return component("ready", "Zotero Web API credentials are valid.", details=value)


def mineru_status(profile: str) -> dict[str, Any]:
    try:
        mineru_client.load_token()
    except Exception as exc:  # noqa: BLE001 - setup report captures integration failures
        return component(
            optional_or_manual(profile),
            "MinerU is an external PDF parsing service that requires an account "
            "and Token; usage quotas apply.",
            action=(
                f"Register or sign in at {MINERU_TOKEN_URL}, create a Token, then run "
                "zotero-mcp setup save-secret mineru."
            ),
            details={
                "error": f"{type(exc).__name__}: {exc}",
                "output_dir": str(mineru_client.DEFAULT_OUTPUT_ROOT),
            },
        )
    return component(
        "ready",
        "MinerU account Token is available; the external service has usage quotas. "
        "Network authentication is checked on first API request.",
        details={"output_dir": str(mineru_client.DEFAULT_OUTPUT_ROOT)},
    )


def qmd_status(profile: str) -> dict[str, Any]:
    command = configured_command("qmd", "command", "QMD_COMMAND", "qmd")
    collection_name = (
        os.environ.get("QMD_COLLECTION", "").strip()
        or zotero_runtime.config_string("qmd", "collection")
        or DEFAULT_QMD_COLLECTION
    )
    if not command:
        return component(
            optional_or_manual(profile),
            "QMD is not installed.",
            action=(
                f"Install Node.js 18+ and run npm install -g {QMD_PACKAGE}. "
                "QMD is local software and requires no account."
            ),
        )
    try:
        result = subprocess.run(
            [command, "status"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return component(
            "failed",
            "QMD executable could not be started.",
            action="Set qmd.command in config.toml to the working QMD executable.",
            details={"command": command, "error": f"{type(exc).__name__}: {exc}"},
        )
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        return component(
            "failed",
            "QMD status check failed.",
            action="Run qmd status in a terminal and fix the first reported error.",
            details={"command": command, "returncode": result.returncode},
        )
    if collection_name not in output:
        return component(
            optional_or_manual(profile),
            f"QMD is installed but collection {collection_name!r} is missing.",
            action=(
                f"Run qmd collection add {mineru_client.DEFAULT_OUTPUT_ROOT} --name "
                f"{collection_name}, then qmd update and qmd embed -c {collection_name}."
            ),
            details={"command": command, "collection": collection_name},
        )
    return component(
        "ready",
        "QMD is installed and the MinerU collection is configured.",
        details={"command": command, "collection": collection_name},
    )


def sciverse_status(profile: str) -> dict[str, Any]:
    executable = configured_command(
        "sciverse", "command", "SCIVERSE_MCP_COMMAND", "sciverse-mcp-server"
    )
    npx = shutil.which("npx")
    try:
        home = Path.home()
    except RuntimeError:
        home = zotero_runtime.config_dir().parent
    credentials = home / ".sciverse" / "credentials.json"
    token_file = (
        zotero_runtime.configured_path(
            "SCIVERSE_API_TOKEN_FILE", "sciverse", "token_file"
        )
        or default_sciverse_token_path()
    )
    token_error = private_secret_file_error(token_file) if token_file.exists() else ""
    if token_error:
        return component(
            optional_or_manual(profile),
            "SciVerse Token file is not private.",
            action=(
                "Run zotero-mcp setup save-secret sciverse --overwrite to replace "
                "it with a private file."
            ),
            details={"credentials_file": str(token_file), "error": token_error},
        )
    token_available = (
        bool(os.environ.get("SCIVERSE_API_TOKEN", "").strip())
        or bool(token_file.is_file() and token_file.stat().st_size > 0)
        or (credentials.is_file() and credentials.stat().st_size > 0)
    )
    if not executable and not npx:
        return component(
            optional_or_manual(profile),
            "SciVerse MCP runtime is not available.",
            action=(
                "Install Node.js 18+. Codex will use npx -y sciverse-mcp-server; "
                "the SciVerse source is not bundled in this repository."
            ),
        )
    if not token_available:
        return component(
            optional_or_manual(profile),
            "SciVerse is an external literature service that requires an account "
            "and Token; usage quotas apply.",
            action=(
                f"Register at {SCIVERSE_URL}, create a Token, then run "
                "zotero-mcp setup save-secret sciverse."
            ),
            details={"runtime": executable or npx},
        )
    return component(
        "ready",
        "SciVerse runtime and account Token are available; the external service "
        "has usage quotas.",
        details={
            "runtime": executable or npx,
            "credentials_file": str(
                token_file if token_file.is_file() else credentials
            ),
        },
    )


def paper_lookup_status() -> dict[str, Any]:
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    skill_file = root / "skills" / "paper-lookup" / "SKILL.md"
    if skill_file.is_file():
        return component(
            "ready",
            "paper-lookup skill is installed for complementary open-API literature search.",
            details={"path": str(skill_file)},
        )
    return component(
        "optional",
        "paper-lookup is recommended alongside SciVerse for complementary open-API "
        "literature search.",
        action=(
            "Ask Codex to use its built-in skill-installer, after approval, to install "
            f"paper-lookup from {PAPER_LOOKUP_URL}."
        ),
        details={"expected_path": str(skill_file)},
    )


def translation_status(profile: str) -> dict[str, Any]:
    try:
        value = zotero_translate.doctor()
    except Exception as exc:  # noqa: BLE001 - report every integration failure as status
        return component(
            optional_or_manual(profile),
            f"PDF full-text translation is not ready: {exc}",
            action=(
                f"Install PDF2zh from {PDF2ZH_URL} if needed. In Zotero select "
                "pdf2zh_next, activate one service, verify its Server URL, configure "
                "WebDAV, run zotero-mcp setup save-secret webdav, then run "
                "zotero-translate doctor."
            ),
            details={"error": f"{type(exc).__name__}: {exc}"},
        )
    pdf2zh = value["pdf2zh"]
    return component(
        "ready",
        "PDF2zh unattended translation is configured.",
        details={
            "prefs_path": pdf2zh["prefs_path"],
            "server_url": pdf2zh["selected_server_url"],
            "service": pdf2zh["service"],
            "model": pdf2zh["model"],
            "webdav_source": value["webdav"]["source"],
            "attachment_title": value["naming"]["attachment_title"],
            "filename_template": value["naming"]["filename_template"],
        },
    )


def codex_status(profile: str) -> dict[str, Any]:
    path = Path.home() / ".codex" / "config.toml"
    if not path.is_file():
        return component(
            "manual_action_required",
            "Codex config.toml does not exist.",
            action="Run zotero-mcp setup print-codex-config and add the output to Codex config.",
            details={"path": str(path)},
        )
    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return component(
            "failed",
            "Codex config.toml is invalid.",
            action="Fix the TOML syntax before adding MCP servers.",
            details={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        )
    servers = config.get("mcp_servers") or {}
    required = ("zotero",) if profile == "core" else ("zotero", "qmd", "sciverse")
    if not isinstance(servers, dict):
        return component(
            "failed",
            "Codex mcp_servers must be a TOML table.",
            action="Fix the Codex MCP configuration before continuing.",
            details={"path": str(path)},
        )
    missing = [name for name in required if name not in servers]
    if missing:
        return component(
            "manual_action_required",
            f"Codex is missing MCP entries: {', '.join(missing)}.",
            action="Run zotero-mcp setup print-codex-config and merge the missing blocks.",
            details={"path": str(path), "configured": sorted(servers)},
        )
    invalid: dict[str, list[str]] = {}
    for name in required:
        entry = servers[name]
        problems = []
        if not isinstance(entry, dict):
            problems.append("entry is not a TOML table")
        else:
            if entry.get("enabled") is False:
                problems.append("entry is disabled")
            if (
                not isinstance(entry.get("command"), str)
                or not entry["command"].strip()
            ):
                problems.append("command is missing")
            args = entry.get("args", [])
            if not isinstance(args, list) or any(
                not isinstance(argument, str) for argument in args
            ):
                problems.append("args must be a string array")
            elif name == "qmd" and "mcp" not in args:
                problems.append("qmd mcp argument is missing")
        if problems:
            invalid[name] = problems
    if invalid:
        return component(
            "manual_action_required",
            "Codex contains invalid or disabled MCP entries.",
            action="Run zotero-mcp setup print-codex-config and repair the listed blocks.",
            details={"path": str(path), "invalid": invalid},
        )
    return component(
        "ready",
        "Codex contains the required MCP entries.",
        details={"path": str(path)},
    )


def infer_zotero_platform(
    codex_platform: str,
    local_api: dict[str, Any],
    storage: dict[str, Any],
) -> str:
    if codex_platform == "windows":
        return "windows"
    api_base = str(local_api.get("details", {}).get("api_base", ""))
    storage_path = str(storage.get("details", {}).get("path", "")).replace("\\", "/")
    if codex_platform == "wsl":
        if re.match(r"^/mnt/[a-zA-Z]/", storage_path):
            return "windows"
        if api_base and not re.match(r"^https?://(127\.0\.0\.1|localhost):", api_base):
            return "windows"
        return "wsl"
    return "posix"


def build_setup_report(profile: str = "full") -> dict[str, Any]:
    if profile not in {"core", "full"}:
        raise ValueError("profile must be core or full")
    local_api = local_api_status()
    storage = storage_status()
    components = {
        "python": dependency_status(),
        "zotero_local_api": local_api,
        "zotero_storage": storage,
        "codex": codex_status(profile),
    }
    if profile == "full":
        components.update(
            {
                "zotero_web_api": web_api_status(profile),
                "mineru": mineru_status(profile),
                "qmd": qmd_status(profile),
                "sciverse": sciverse_status(profile),
                "paper_lookup": paper_lookup_status(),
                "translation": translation_status(profile),
            }
        )
    counts: dict[str, int] = {}
    for value in components.values():
        status = str(value["status"])
        counts[status] = counts.get(status, 0) + 1
    codex_platform = zotero_runtime.platform_name()
    zotero_platform = infer_zotero_platform(codex_platform, local_api, storage)
    return {
        "ready": all(
            value["status"] in {"ready", "optional"} for value in components.values()
        ),
        "platform": codex_platform,
        "codex_platform": codex_platform,
        "zotero_platform": zotero_platform,
        "platform_pair": f"{codex_platform}-codex+{zotero_platform}-zotero",
        "profile": profile,
        "config_path": str(zotero_runtime.config_path()),
        "summary": counts,
        "components": components,
    }


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_user_config(
    storage: Path,
    *,
    local_api: str | None = None,
    mineru_output: Path | None = None,
    qmd_command: str | None = None,
    qmd_collection: str = DEFAULT_QMD_COLLECTION,
) -> str:
    lines = ["[zotero]"]
    if local_api:
        lines.append(f"local_api = {toml_string(local_api.rstrip('/'))}")
    lines.append(f"storage = {toml_string(str(storage))}")
    lines.extend(
        [
            "",
            "[mineru]",
            f"output_dir = {toml_string(str(mineru_output or mineru_client.DEFAULT_OUTPUT_ROOT))}",
        ]
    )
    lines.extend(
        [
            "",
            "[qmd]",
            f"command = {toml_string(qmd_command or 'qmd')}",
            f"collection = {toml_string(qmd_collection)}",
            "",
            "[translation]",
            f"attachment_title = {toml_string(zotero_translate.DEFAULT_TRANSLATION_ATTACHMENT_TITLE)}",
            f"filename_template = {toml_string(zotero_translate.DEFAULT_TRANSLATION_FILENAME_TEMPLATE)}",
            "auto_rename_manual = false",
            "rename_poll_seconds = 30",
            "",
        ]
    )
    return "\n".join(lines)


def write_private_file(path: Path, content: str, *, overwrite: bool = False) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise ValueError(f"Refusing to write secret through a symbolic link: {path}")
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def save_secret(kind: str, value: str, *, overwrite: bool = False) -> Path:
    if kind not in {"zotero", "mineru", "sciverse"}:
        raise ValueError("secret kind must be zotero, mineru, or sciverse")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("secret cannot be empty")
    if kind == "mineru":
        path = mineru_client.default_token_path()
    elif kind == "sciverse":
        path = default_sciverse_token_path()
    else:
        path = zotero_runtime.default_secret_path("zotero_web_api_key.secret")
    return write_private_file(
        path,
        cleaned,
        overwrite=overwrite,
    )


def save_webdav_secret(
    url: str,
    username: str,
    password: str,
    *,
    timeout: float = zotero_translate.DEFAULT_WEBDAV_TIMEOUT,
    overwrite: bool = False,
) -> Path:
    try:
        cleaned_url = (
            zotero_translate.validate_transport_url(url, purpose="WebDAV") + "/"
        )
    except zotero_translate.TranslationError as exc:
        raise ValueError(str(exc)) from exc
    cleaned_username = username.strip()
    if not cleaned_username or not password:
        raise ValueError("WebDAV username and password cannot be empty")
    if timeout <= 0:
        raise ValueError("WebDAV timeout must be positive")
    return write_private_file(
        zotero_translate.webdav_secret_path(),
        json.dumps(
            {
                "url": cleaned_url,
                "username": cleaned_username,
                "password": password,
                "timeout": timeout,
            },
            ensure_ascii=False,
            indent=2,
        ),
        overwrite=overwrite,
    )


def codex_config_toml(toolsets: tuple[str, ...]) -> str:
    python = Path(sys.executable).resolve()
    selected = ",".join(toolsets)
    qmd = configured_command("qmd", "command", "QMD_COMMAND", "qmd") or "qmd"
    sciverse = configured_command(
        "sciverse", "command", "SCIVERSE_MCP_COMMAND", "sciverse-mcp-server"
    )
    if sciverse:
        sciverse_command = sciverse
        sciverse_args = "args = []"
    else:
        sciverse_command = shutil.which("npx") or "npx"
        sciverse_args = 'args = [ "-y", "sciverse-mcp-server" ]'
    return "\n".join(
        [
            "[mcp_servers.zotero]",
            'type = "stdio"',
            f"command = {toml_string(str(python))}",
            (
                "args = [ "
                '"-m", "zotero_mcp.zotero_mcp_server", '
                f'"--toolsets", {toml_string(selected)} ]'
            ),
            "enabled = true",
            "",
            "[mcp_servers.qmd]",
            'type = "stdio"',
            f"command = {toml_string(qmd)}",
            'args = [ "mcp" ]',
            "enabled = true",
            "",
            "[mcp_servers.sciverse]",
            'type = "stdio"',
            f"command = {toml_string(sciverse_command)}",
            sciverse_args,
            "enabled = true",
        ]
    )


def print_human_report(report: dict[str, Any]) -> None:
    print(
        f"SETUP platform_pair={report['platform_pair']} profile={report['profile']} "
        f"ready={str(report['ready']).lower()}"
    )
    for name, value in report["components"].items():
        print(f"[{value['status']}] {name}: {value['summary']}")
        if value.get("action"):
            print(f"  action: {value['action']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Run read-only setup checks.")
    plan.add_argument("--profile", choices=("core", "full"), default="full")
    plan.add_argument("--json", action="store_true")

    configure = subparsers.add_parser(
        "configure", help="Create a new user config.toml."
    )
    configure.add_argument("--storage", type=Path, required=True)
    configure.add_argument("--local-api")
    configure.add_argument("--mineru-output", type=Path)
    configure.add_argument("--qmd-command")
    configure.add_argument("--qmd-collection", default=DEFAULT_QMD_COLLECTION)
    configure.add_argument("--overwrite", action="store_true")

    secret = subparsers.add_parser(
        "save-secret", help="Save a secret from hidden terminal input."
    )
    secret.add_argument("kind", choices=("zotero", "mineru", "sciverse", "webdav"))
    secret.add_argument("--overwrite", action="store_true")

    config = subparsers.add_parser(
        "print-codex-config", help="Print Zotero, QMD, and SciVerse Codex MCP blocks."
    )
    config.add_argument(
        "--toolsets",
        default="literature,review,maintenance",
        help="Comma-separated Zotero toolsets.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        report = build_setup_report(arguments.profile)
        if arguments.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_human_report(report)
        return
    if arguments.command == "configure":
        content = render_user_config(
            arguments.storage,
            local_api=arguments.local_api,
            mineru_output=arguments.mineru_output,
            qmd_command=arguments.qmd_command,
            qmd_collection=arguments.qmd_collection,
        )
        path = write_private_file(
            zotero_runtime.config_path(),
            content,
            overwrite=arguments.overwrite,
        )
        print(path)
        return
    if arguments.command == "save-secret":
        if arguments.kind == "webdav":
            url = input("WebDAV HTTPS Zotero directory URL (ending in /zotero/): ")
            username = input("WebDAV username: ")
            password = getpass.getpass("WebDAV password: ")
            print(
                save_webdav_secret(
                    url, username, password, overwrite=arguments.overwrite
                )
            )
        else:
            value = getpass.getpass(f"Enter {arguments.kind} secret: ")
            print(save_secret(arguments.kind, value, overwrite=arguments.overwrite))
        return
    toolsets = tuple(
        part.strip() for part in arguments.toolsets.split(",") if part.strip()
    )
    print(codex_config_toml(toolsets))


if __name__ == "__main__":
    main()
