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

The command writes a private UTF-8 plain-text file named
`zotero_web_api_key.secret` under the user configuration directory. The file
contains only the key and is protected with mode `0600` on WSL/Linux; the
`.secret` suffix is a naming convention, not encryption. Do not put the value
in the repository or paste it into chat.

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

The command writes a private UTF-8 plain-text file named
`mineru_api_token.secret` under the independent MinerU configuration directory
(`~/.config/mineru` on WSL/Linux). The file contains only the Token and is
protected with mode `0600` on WSL/Linux; the `.secret` suffix is a naming
convention, not encryption. Do not put the value in the repository or paste it
into chat.

Authentication is verified on the first MinerU API request. Submission remains a separate user-approved action because it uploads PDFs to an external service.

## SciVerse

Purpose: external literature search through an independent MCP. An account and Token are required, and usage quotas apply.

1. Register at [SciVerse](https://sciverse.space/).
2. Save the Token through hidden terminal input:

```bash
zotero-mcp setup save-secret sciverse
```

The command writes `sciverse_api_token.secret` under the Zotero MCP user
configuration directory. On WSL/Linux it must be a regular file owned by the
current user with no group or other access; the setup check rejects symbolic
links and insecure permissions. The launcher supplies the Token through the
child process environment, not through Codex configuration or command-line
arguments.

3. Rerun:

```bash
zotero-mcp setup plan --profile full
```

SciVerse remains a separate MCP server. Zotero MCP only manages the local
private Token file and does not proxy SciVerse requests.

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
