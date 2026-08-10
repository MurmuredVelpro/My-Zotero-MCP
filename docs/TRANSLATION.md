# Unattended PDF translation

This workflow removes per-paper right-click submission while keeping the official Zotero PDF2zh installation and settings as the source of truth.

## Responsibilities

- Zotero PDF2zh owns translation settings and the PDF translation engine.
- The user installs PDF2zh, chooses the provider, and enters credentials in Zotero.
- `zotero-translate` owns queueing, bounded batch execution, HTTP result download, Zotero attachment upload, recovery, and one-time scheduling.
- Zotero Web API creates the attachment record. WebDAV uploads the attachment file.

## One-time setup

1. Install [Zotero PDF2zh](https://github.com/guaguastandup/zotero-pdf2zh) and its official Server.
2. In Zotero, select `pdf2zh_next`, configure and activate the intended service, and verify the Server URL.
3. Translate one short PDF from Zotero to verify the official installation.
4. Configure WebDAV in Zotero and verify synchronization. Use the exact HTTPS attachment directory URL, normally the configured base URL plus `/zotero/`, when saving the Worker secret.
5. Create a Zotero Web API key with personal-library and file-write access. Save it with `zotero-mcp setup save-secret zotero`.
6. Save the WebDAV credentials through hidden terminal input with `zotero-mcp setup save-secret webdav`.
7. Run `zotero-translate doctor`. It checks PDF2zh Server health, Zotero Local API, Zotero Web API file-write permission, and WebDAV reachability without translating or uploading a file. The first real non-dry-run batch verifies WebDAV write access with a temporary zero-byte object and deletes it before translation starts.

Codex guides these steps and pauses at account, browser, and secret-entry gates. Provider credentials are never accepted through chat.

## Queue papers

Queue explicit Zotero item keys:

```bash
zotero-translate enqueue ITEMKEY1 ITEMKEY2
```

Queue a collection resolved by exact key, globally unique name, or full path:

```bash
zotero-translate enqueue --collection "Root > Topic" --recursive
```

The Worker reuses Zotero MCP's English-PDF selection rules and locks the selected source attachment key. Existing `CN` attachments are recorded as complete.

## Inspect and run

Every batch requires explicit limits:

```bash
zotero-translate run --qps 10 --pool-size 20 --max-items 3 --dry-run
zotero-translate run --qps 10 --pool-size 20 --max-items 3
```

`--max-items` limits how many pending papers this invocation may consume. It does not change the queue or create a recurring schedule.

The Worker requests one Chinese monolingual PDF without a watermark. It reads the returned `fileList`, downloads the selected file from `/translatedFile/<filename>`, validates the PDF, then imports an attachment titled `CN`. The attachment filename preserves the English source stem and appends `的全文翻译.pdf`.

## Repair names after manual translation

Manual translation from Zotero remains supported without changing PDF2zh source code. After PDF2zh creates the translated attachment:

1. Call `zotero_plan_manual_translation_rename` with exact parent or child item keys. It identifies one English source PDF and one PDF2zh translation, checks local/cloud agreement and filename conflicts, and performs no write.
2. Review the returned `parent_item_key`, `source_attachment_key`, and `translation_attachment_key`.
3. After explicit approval, call `zotero_apply_manual_translation_rename` with those exact keys and `confirm=true`.
4. Run Zotero sync. The attachment title becomes `CN`; the stored filename becomes the English source stem plus `的全文翻译.pdf`.

The apply step uses a versioned Web API PATCH and cloud readback. It does not change the PDF content, parent paper metadata, annotations, or attachment key. Ambiguous translated attachments, local/cloud disagreement, and existing target filenames block the write.

### Optional automatic mode

Automatic rename is disabled by default. Enable it in the user config only when the user wants future manual translations renamed without per-paper approval:

```toml
[translation]
auto_rename_manual = true
rename_poll_seconds = 30
```

Initialize the checkpoint without changing historical attachments:

```bash
zotero-translate rename-watch --once
```

Install and start the current-user background service:

```bash
zotero-translate rename-watch --install-service
zotero-translate rename-watch --service-status
```

The monitor only processes attachment versions observed after its first checkpoint. It retains blocked items and retries with bounded backoff when Zotero Local API, cloud sync, or the local file is not ready. Multiple translation candidates, a conflicting target filename, or inconsistent local/cloud metadata remain blocked rather than guessed.

To disable writes while leaving the service installed, set `auto_rename_manual = false`. To remove the background service:

```bash
zotero-translate rename-watch --remove-service
```

WSL/Linux uses a user-level systemd service. Windows uses a current-user Task Scheduler task triggered at logon.

Remote WebDAV and PDF2zh Server connections require HTTPS. Loopback HTTP remains allowed for a PDF2zh Server on the same computer. Set `ZOTERO_TRANSLATE_ALLOW_INSECURE_HTTP=1` only when a trusted private-network deployment cannot use HTTPS; this permits clear-text credentials and document traffic and should not be used on a public network.

## One-time scheduling

```bash
zotero-translate schedule \
  --at "2026-08-15 22:00" \
  --qps 10 \
  --pool-size 20 \
  --max-items 3
```

Windows uses a one-time Task Scheduler task that deletes itself after the final run. WSL and Linux use a transient `systemd --user` timer. No recurring, wake, or catch-up task is created.
The scheduler stores the resolved `prefs.js` path, not any provider or WebDAV credential.

## Recovery

Failed rows remain `failed` and are never retried indefinitely. After inspecting the error:

```bash
zotero-translate retry ITEMKEY1
```

If translation finished before attachment upload failed, the next run reuses the validated local PDF. Partial attachment creation triggers best-effort cleanup of the new WebDAV objects and Zotero attachment record.

## Paths and overrides

Default state:

- Windows: `%LOCALAPPDATA%\zotero-mcp`
- WSL/Linux: `~/.local/state/zotero-mcp`

The queue is `translation_queue.csv`; downloaded PDFs are stored under `translations`.

Useful overrides:

- `ZOTERO_PDF2ZH_PREFS`: exact Zotero `prefs.js`
- `ZOTERO_TRANSLATE_STATE`: queue and translated-PDF state directory
- `ZOTERO_WEBDAV_SECRET_FILE`: private WebDAV JSON file
- `ZOTERO_WEBDAV_URL`, `ZOTERO_WEBDAV_USERNAME`, `ZOTERO_WEBDAV_PASSWORD`: runtime credential overrides
- `ZOTERO_TRANSLATE_ALLOW_INSECURE_HTTP`: explicit opt-in for trusted remote HTTP endpoints

Neither the queue nor command output contains API keys or WebDAV passwords.
