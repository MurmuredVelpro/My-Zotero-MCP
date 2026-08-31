# Troubleshooting

## Start with the setup report

```bash
zotero-mcp setup plan --profile full --json
```

Fix the first component whose status is `failed` or `manual_action_required`, then rerun the report.

## Zotero Local API is unreachable

1. Confirm Zotero is running.
2. Enable `Advanced > Allow other applications on this computer to communicate with Zotero`.
3. Test the same environment that launches Codex.
4. Inspect the `candidates` list in the JSON setup report.

## WSL cannot reach Windows Zotero

Try the auto-detected candidates first. If localhost and the Windows gateway on port `23119` both fail, a Windows port proxy may be required.

This changes Windows networking and requires an Administrator PowerShell. Codex must show the resolved WSL gateway address and obtain user approval before running it:

```powershell
netsh interface portproxy add v4tov4 listenaddress=<WSL_GATEWAY_IP> listenport=23120 connectaddress=127.0.0.1 connectport=23119
```

Allow inbound TCP `23120` only on the WSL virtual network, then configure:

```toml
[zotero]
local_api = "http://<WSL_GATEWAY_IP>:23120/api"
```

Do not expose this proxy on a public network interface.

## OA PDF download reports that the Wiley proxy is unavailable

Zotero MCP ignores shell `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` settings.
Local Zotero and PDF2zh requests use the `LOCAL` route. Ordinary Zotero,
repository, metadata, WebDAV, MinerU, and publisher requests use the `NORMAL`
route through the current Windows gateway on port `17892`. Wiley and every
later hop in a Wiley redirect chain use the dedicated `PROXY_REQUIRED` port
`17893`, whose Mihomo listener is pinned to the approved proxy group.

Confirm that Mihomo is running, the WSL-to-Windows `17892` and `17893`
forwarding paths are reachable, and the approved proxy uplink is present.
Neither external route falls back to WSL direct access. In particular, do not
work around a Wiley error by allowing it to fall back to the campus network.

## Attachment storage is missing

Local API metadata may work while PDF tools fail. Set `zotero.storage` to the actual local Zotero `storage` directory. For WSL, convert `D:\Zotero\storage` to `/mnt/d/Zotero/storage`.

WebDAV does not provide a local filesystem path by itself. Zotero must download the attachment to this computer before local PDF extraction can read it.

## Zotero Web API fails

Create a personal-library write key, save it with `zotero-mcp setup save-secret zotero`, then run `zotero_web_api_status`. A write workflow must stop if local and cloud item state disagree.

## MinerU fails

- Missing Token: follow [ACCOUNTS.md](ACCOUNTS.md#mineru).
- Interrupted upload: retain the batch ID stored in `zotero_workflow.sqlite3`; collect the existing batch instead of resubmitting.
- Stale result: the current Zotero PDF attachment key differs from the parsed key. Use `zotero-mineru` to replace it through the recoverable workflow.
- Missing artifacts: `zotero-mineru verify <batch-id>` reports the exact missing or invalid file.
- Existing result blocked as `untracked_existing`: do not resubmit it. Verify the current English attachment and run `zotero-mineru adopt-existing ITEMKEY --attachment-key ATTACHMENTKEY` as a read-only plan; add `--confirm` only after checking the result. The command writes only SQLite, resets `qmd_indexed` to false, and requires normal QMD indexing afterward.

## QMD fails

Confirm the executable and collection:

```bash
qmd status
qmd collection show zotero-mineru
```

Set `qmd.command` when QMD is not on `PATH`. Set `qmd.collection` when using a different collection name.

If MinerU is current but SQLite reports `pending_index`, the local `full.md` is not enough to pass the gate. Run the normal QMD update/embed/verification path. If SQLite says `missing`, the index marker exists but QMD cannot read the document; investigate the collection path before reviewing it.

## SciVerse tools are absent

Run `zotero-mcp setup save-secret sciverse` through hidden terminal input. Confirm the private Token file is owned by the current user with mode `0600`, confirm `sciverse-mcp-server` or `npx` is available, regenerate the Codex blocks, then restart Codex.

## PDF2zh preferences are not found

Start Zotero once after installing PDF2zh and save its settings. Run `zotero-translate doctor` in the same Windows or WSL user account. For a nonstandard Zotero profile, set `translation.prefs_file` in the Zotero MCP config or `ZOTERO_PDF2ZH_PREFS` to the exact `prefs.js` path.

## PDF2zh Server is unreachable

Confirm the official Server is running and its `/health` endpoint works. WSL automatically tries both the Server URL saved in Zotero and the Windows gateway when that URL uses localhost. Do not expose port `8890` on a public interface.

## Translation rejects insecure remote HTTP

Remote WebDAV and PDF2zh Server URLs require HTTPS because they carry credentials, PDFs, or provider configuration. Loopback HTTP remains supported for a same-computer PDF2zh Server. For a trusted private network that cannot provide HTTPS, set `ZOTERO_TRANSLATE_ALLOW_INSECURE_HTTP=1` in the Worker environment and keep the endpoint off public networks.

## Translation configuration is rejected

Open the PDF2zh settings in Zotero. Select `pdf2zh_next`, choose the intended service, and activate exactly one matching LLM entry. The Worker intentionally refuses ambiguous or inactive configurations.

## Translation succeeded but Zotero import failed

The queue retains the downloaded PDF and marks the row `failed`. Fix Zotero Web API or WebDAV, inspect the row, then run:

```bash
zotero-translate retry <ITEM_KEY>
zotero-translate run --qps <N> --pool-size <N> --max-items <N>
```

The second run reuses the downloaded PDF and does not pay for translation again.

## Codex still shows old tools

1. Confirm its Zotero MCP command points to the intended Python environment.
2. Regenerate blocks with `zotero-mcp setup print-codex-config`.
3. Preserve unrelated MCP entries while replacing the Zotero block.
4. Restart Codex so it reloads MCP definitions.
