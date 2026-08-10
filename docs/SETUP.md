# Setup

## 1. Identify the platform pair

Codex and Zotero may run in different environments:

| Codex | Zotero | Local API | Attachment storage example |
| --- | --- | --- | --- |
| Windows | Windows | `http://127.0.0.1:23119/api` | `D:\Zotero\storage` |
| WSL | Windows | auto-detected localhost or WSL gateway | `/mnt/d/Zotero/storage` |
| Linux | Linux | `http://127.0.0.1:23119/api` | `~/Zotero/storage` |

The setup command reports the detected platform, attempted Local API addresses, config path, and storage path.

## 2. Install in the intended Python environment

Python 3.11 or newer is required.

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
```

WSL/Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Run a read-only core check:

```bash
zotero-mcp setup plan --profile core
```

## 3. Enable Zotero Local API

Start Zotero. In Zotero settings, enable:

`Advanced > Allow other applications on this computer to communicate with Zotero`

Rerun the core check. If WSL still cannot reach Windows Zotero, use [TROUBLESHOOTING.md](TROUBLESHOOTING.md#wsl-cannot-reach-windows-zotero).

## 4. Configure paths

Windows example:

```powershell
zotero-mcp setup configure --storage "D:\Zotero\storage" --mineru-output "D:\Zotero_MinerU"
```

WSL example:

```bash
zotero-mcp setup configure --storage /mnt/d/Zotero/storage --mineru-output "$HOME/tools/Zotero_MinerU"
```

The command refuses to overwrite an existing config unless `--overwrite` is supplied. Review the existing file before approving overwrite.

Config locations:

- Windows: `%APPDATA%\zotero-mcp\config.toml`
- WSL/Linux: `~/.config/zotero-mcp/config.toml`

Example:

```toml
[zotero]
local_api = "http://127.0.0.1:23119/api"
storage = "D:\\Zotero\\storage"

[mineru]
output_dir = "D:\\Zotero_MinerU"
ledger = "D:\\Zotero_MinerU\\mineru_todo.csv"

[qmd]
command = "qmd"
collection = "zotero-mineru"

[translation]
auto_rename_manual = false
rename_poll_seconds = 30
```

Secrets are stored in separate private files, not in this TOML.

## 5. Configure optional integrations

For Zotero Web API, MinerU, SciVerse, paper-lookup, WebDAV, and PDF2zh setup, follow [ACCOUNTS.md](ACCOUNTS.md). MinerU and SciVerse are external services that require an account and Token and have usage quotas. QMD requires Node.js 18+ but no account. The following global install and collection creation change local state; Codex must obtain user approval before running them:

```bash
npm install -g @tobilu/qmd
qmd collection add <mineru-output-directory> --name zotero-mineru
qmd update
qmd embed -c zotero-mineru
```

WebDAV is optional for Zotero MCP, but unattended translation needs it to upload translated attachments. Configure the same WebDAV account in Zotero, then run `zotero-mcp setup save-secret webdav`; enter the exact HTTPS Zotero attachment directory URL, normally the configured base URL plus `/zotero/`. Secret entry remains a user action.

Install PDF2zh from its official project and configure its Server, `pdf2zh_next` engine, active LLM entry, and Server URL inside Zotero. Run one short manual translation to verify the official installation, then run:

```bash
zotero-translate doctor
```

The command reads the official Zotero preferences at runtime and reports only the selected service, model, Server URL, and readiness booleans. It checks WebDAV reachability without writing; a real non-dry-run batch performs a temporary write-and-delete probe before translation.

Users who want Zotero's manual translation command to produce consistently named attachments can opt into the background rename monitor described in [TRANSLATION.md](TRANSLATION.md#optional-automatic-mode). The setting is disabled by default and the first scan establishes a checkpoint without changing historical attachments.

## 6. Add MCP servers to Codex

Generate configuration from the same Python environment used above:

```bash
zotero-mcp setup print-codex-config --toolsets literature,review,maintenance
```

Merge the printed blocks into the existing Codex config:

- Windows: `%USERPROFILE%\.codex\config.toml`
- WSL/Linux: `~/.codex/config.toml`

The output contains three independent MCP servers: Zotero, QMD, and SciVerse. Remove or disable optional blocks that are not configured. Restart Codex after editing its config.

For literature discovery, use SciVerse together with the optional [`paper-lookup`](https://github.com/K-Dense-AI/scientific-agent-skills/tree/main/skills/paper-lookup) skill. `setup plan --profile full` detects whether the skill is installed. When it is missing, ask Codex to use its built-in `skill-installer` for repository `K-Dense-AI/scientific-agent-skills`, path `skills/paper-lookup`; installation requires user approval.

## 7. Verify

Run:

```bash
zotero-mcp setup plan --profile full
```

Then verify in Codex:

1. `zotero_ping` succeeds.
2. `qmd` tools are visible when QMD is enabled.
3. SciVerse tools are visible when SciVerse is enabled.
4. `paper_lookup` is `ready` or intentionally `optional`.
5. `zotero_web_api_status` succeeds before any write workflow.
6. `zotero-translate doctor` succeeds when full-text translation is enabled.

`manual_action_required` means the setup is intentionally paused for user action. `optional` means the core Zotero MCP can run without that component.

## Environment variables

- `ZOTERO_MCP_CONFIG`
- `ZOTERO_MCP_CONFIG_DIR`
- `ZOTERO_LOCAL_API`
- `ZOTERO_STORAGE`
- `ZOTERO_API_KEY`
- `ZOTERO_API_KEY_FILE`
- `ZOTERO_USER_ID`
- `ZOTERO_WEB_API_URL`
- `MINERU_API_TOKEN`
- `MINERU_API_TOKEN_FILE`
- `ZOTERO_MINERU_DIR`
- `ZOTERO_MINERU_LEDGER`
- `QMD_COMMAND`
- `QMD_COLLECTION`
- `SCIVERSE_MCP_COMMAND`
- `SCIVERSE_API_TOKEN_FILE`
- `PDFTOTEXT`
- `PDFTOPPM`
- `ZOTERO_PDF2ZH_PREFS`
- `ZOTERO_TRANSLATE_STATE`
- `ZOTERO_WEBDAV_SECRET_FILE`
- `ZOTERO_WEBDAV_URL`
- `ZOTERO_WEBDAV_USERNAME`
- `ZOTERO_WEBDAV_PASSWORD`
- `ZOTERO_WEBDAV_TIMEOUT`
- `ZOTERO_TRANSLATE_ALLOW_INSECURE_HTTP`
