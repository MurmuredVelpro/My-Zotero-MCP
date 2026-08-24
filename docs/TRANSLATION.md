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
7. Run `zotero-translate doctor`. It validates the attachment naming configuration and checks PDF2zh Server health, Zotero Local API, Zotero Web API file-write permission, and WebDAV reachability without translating or uploading a file. The first real non-dry-run batch verifies WebDAV write access with a temporary zero-byte object and deletes it before translation starts.

Codex guides these steps and pauses at account, browser, and secret-entry gates. Provider credentials are never accepted through chat.

## Attachment naming

Unattended imports and manual-translation renames share one naming configuration:

```toml
[translation]
attachment_title = "CN"
filename_template = "{source_stem}的全文翻译.pdf"
```

Change the values to match the user's filing convention. For example, `attachment_title = "Chinese"` and `filename_template = "{source_stem} (Chinese).pdf"` are valid. The filename template must contain and may only use `{source_stem}`; format specs, conversions, unknown variables, paths, and non-PDF results are rejected before any write. Missing keys use the defaults shown above. Attachments with the legacy title `CN` remain recognized after customization so a configuration change cannot create a duplicate translation attachment.

## Queue papers

Before queueing a paper or starting a batch, initialize or refresh the shared local workflow snapshot and inspect the exact target. On first initialization, repeat `--collection` for every collection to track; later no-argument sync reuses those roots. This catches an existing `CN` attachment, a queued row, or an active PDF2zh task created by another conversation:

```bash
python -m zotero_mcp.zotero_workflow sync \
  --collection Senescence \
  --collection "Journal Club" \
  --collection Glioma
python -m zotero_mcp.zotero_workflow sync
python -m zotero_mcp.zotero_workflow status --item ITEMKEY
```

The snapshot is coordination state only. It does not enqueue, translate, upload, rename, or modify Zotero.

Queue explicit Zotero item keys:

```bash
zotero-translate enqueue ITEMKEY1 ITEMKEY2
```

Queue a collection resolved by exact key, globally unique name, or full path:

```bash
zotero-translate enqueue --collection "Root > Topic" --recursive
```

The workflow database may track several collections, but each translation batch should name one collection explicitly; tracking scope and processing scope are separate.

The Worker reuses Zotero MCP's English-PDF selection rules and locks the selected source attachment key. Attachments using the configured title or the legacy title `CN` are recorded as complete.

## Inspect and run

Every batch requires explicit limits:

```bash
zotero-translate run \
  --paper-concurrency 3 \
  --qps 2 \
  --pool-size 4 \
  --inter-item-delay 420 \
  --retry-delay 420 \
  --transient-retries 1 \
  --max-items 8 \
  --dry-run

zotero-translate run \
  --paper-concurrency 3 \
  --qps 2 \
  --pool-size 4 \
  --inter-item-delay 420 \
  --retry-delay 420 \
  --transient-retries 1 \
  --max-items 8
```

`--paper-concurrency` controls how many papers may translate at once. `--qps` and `--pool-size` are passed unchanged to every paper; they are not divided across the paper workers. The command reports the nominal aggregate QPS and pool size for visibility but does not enforce a provider quota or price threshold.

Each paper slot cools down independently. After a translated PDF is downloaded and validated, that slot waits until `downloaded_at + inter_item_delay` before claiming another paper. Serial Zotero attachment import happens during that interval. An existing `CN` attachment or an already-downloaded local translation does not create a new provider request and does not start a new provider cooldown.

`--max-items` limits the unique papers selected by this invocation. A delayed retry of the same paper does not consume another item. The queue persists `attempt_count`, `downloaded_at`, and `next_attempt_at`, so a restarted Worker honors an existing retry or cooldown time.

The Worker requests one Chinese monolingual PDF without a watermark. It reads the returned `fileList`, downloads the selected file from `/translatedFile/<filename>`, validates the PDF, then applies the configured attachment title and filename template.

## Repair names after manual translation

Manual translation from Zotero remains supported without changing PDF2zh source code. After PDF2zh creates the translated attachment:

1. Call `zotero_plan_manual_translation_rename` with exact parent or child item keys. It identifies one English source PDF and one PDF2zh translation, checks local/cloud agreement and filename conflicts, and performs no write.
2. Review the returned `parent_item_key`, `source_attachment_key`, `translation_attachment_key`, `new_title`, and `new_filename`.
3. After explicit approval, call `zotero_apply_manual_translation_rename` with those exact reviewed values and `confirm=true`.
4. Run Zotero sync. The attachment title and stored filename become the configured values shown in the plan.

The apply step uses a versioned Web API PATCH and cloud readback. It does not change the PDF content, parent paper metadata, annotations, or attachment key. Ambiguous translated attachments, local/cloud disagreement, and existing target filenames block the write.

### Optional automatic mode

Automatic rename is disabled by default. Enable it in the user config only when the user wants future manual translations renamed without per-paper approval:

```toml
[translation]
attachment_title = "CN"
filename_template = "{source_stem}的全文翻译.pdf"
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
  --at "2030-01-15 22:00" \
  --paper-concurrency 3 \
  --qps 2 \
  --pool-size 4 \
  --inter-item-delay 420 \
  --retry-delay 420 \
  --transient-retries 1 \
  --max-items 8
```

Windows uses a one-time Task Scheduler task that deletes itself after the final run. WSL and Linux use a transient `systemd --user` timer. No recurring, wake, or catch-up task is created.
The scheduler stores the resolved `prefs.js` path, not any provider or WebDAV credential.

## Recovery

PDF2zh handles its own internal reconnect attempts. If the whole paper still fails with a recognized transient connection, rate-limit, or concurrency error, the Worker records `retry_wait`, waits until `next_attempt_at`, and retries in the same paper slot. After `--transient-retries` is exhausted, the row becomes `failed` and the slot continues with the next paper.

After inspecting a final failure, reset it explicitly:

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
