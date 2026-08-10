# Accounts and manual gates

These steps require user interaction. Codex should provide the link and command, then pause. Credentials must never be pasted into chat or committed to Git.

## Zotero Web API

Purpose: guarded Zotero writes. Read-only Local API use does not need this key.

1. Create a personal-library key at [Zotero API Keys](https://www.zotero.org/settings/keys/new).
2. Enable library write access only when write tools are needed.
3. Save the key through hidden terminal input:

```bash
zotero-mcp setup save-secret zotero
```

4. Verify:

```bash
zotero-mcp setup plan --profile full
```

## MinerU

Purpose: optional external PDF parsing. An account and Token are required, and usage quotas apply.

1. Register or sign in at [MinerU Token Management](https://mineru.net/apiManage/token).
2. Create a Token.
3. Save it through hidden terminal input:

```bash
zotero-mcp setup save-secret mineru
```

Authentication is verified on the first MinerU API request. Submission remains a separate user-approved action because it uploads PDFs to an external service.

## SciVerse

Purpose: external literature search through an independent MCP. An account and Token are required, and usage quotas apply.

1. Register at [SciVerse](https://sciverse.space/).
2. Install and authenticate in the environment that launches SciVerse MCP:

```bash
python -m pip install sciverse
sciverse auth login
```

The login command handles credentials outside this repository. After login, rerun:

```bash
zotero-mcp setup plan --profile full
```

SciVerse remains a separate MCP server. Zotero MCP does not proxy or store SciVerse credentials.

## paper-lookup

Purpose: complementary literature search across open academic APIs. Use it alongside SciVerse to broaden coverage and resolve identifiers or open-access sources.

Upstream project: [`K-Dense-AI/scientific-agent-skills`](https://github.com/K-Dense-AI/scientific-agent-skills/tree/main/skills/paper-lookup).

Ask Codex to use its built-in `skill-installer` to install repository `K-Dense-AI/scientific-agent-skills`, path `skills/paper-lookup`. Codex must obtain approval before installing it. This repository does not copy or vendor the third-party skill.

## PDF2zh and LLM provider

Purpose: optional PDF full-text translation.

1. Install Zotero PDF2zh from [the official project](https://github.com/guaguastandup/zotero-pdf2zh).
2. Start its official Server on the same computer as Zotero.
3. In Zotero settings, select `pdf2zh_next`, add the LLM provider or OpenAI-compatible relay, activate that entry, and set the Server URL.
4. Enter the provider API key only in the Zotero interface. Codex must not request, copy, display, or migrate it.
5. Submit one short PDF manually to verify the official plugin and Server before enabling unattended batches.

The unattended Worker reads these Zotero preferences in memory for each run. It does not maintain a second LLM configuration.

## QMD and WebDAV

QMD is local software and requires no account. WebDAV is not required for Local API reads or ordinary Web API writes, but unattended translation uses it to upload translated attachment files.

1. Configure a WebDAV account in Zotero and verify file sync there. Identify the exact attachment directory URL, normally the configured base URL plus `/zotero/`.
2. Run `zotero-mcp setup save-secret webdav`.
3. Enter the same storage URL, username, and password in the terminal prompts. The password prompt is hidden.
4. Run `zotero-translate doctor`.

The private file is stored under the Zotero MCP user configuration directory. Its contents must never be printed or committed. WebDAV uses HTTPS by default. `zotero-translate doctor` verifies the Zotero Web API file-write permission and a WebDAV `PROPFIND` request without uploading a file; the first real batch performs and removes a temporary WebDAV write probe.
