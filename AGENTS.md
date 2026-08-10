# Zotero MCP Agent Instructions

## First-time setup

When the user asks to install, configure, diagnose, or share this project:

1. Read `docs/SETUP.md`. Read `docs/ACCOUNTS.md` when Zotero Web API, MinerU, SciVerse, or paper-lookup is requested. Read `docs/TROUBLESHOOTING.md` only after a check fails.
2. Detect the current Codex platform and the Zotero platform independently. Record one of: Windows Codex + Windows Zotero, WSL Codex + Windows Zotero, or same-host POSIX.
3. Use the intended Python 3.11+ environment. Install this repository there, then run `zotero-mcp setup plan --profile core` before changing configuration.
4. Create configuration with `zotero-mcp setup configure` only after resolving the actual Zotero storage path. Preserve existing configuration unless the user explicitly approves overwrite.
5. Generate MCP blocks with `zotero-mcp setup print-codex-config`. Merge only missing blocks into the existing Codex config; preserve unrelated MCP servers and settings.

Completion: `setup plan` reports every required component as `ready` or `optional`, and a restarted Codex can call `zotero_ping`. Report any optional component left unconfigured, including paper-lookup.

When the user asks to classify or reorganize a Zotero collection, read `docs/COLLECTION_REVIEW.md` and start from `templates/collection_review.md`. Finish QMD evidence review and human approval before calling any collection apply tool.

When the user asks for PDF full-text translation or scheduled translation, read `docs/TRANSLATION.md`. Treat the PDF2zh preferences saved by Zotero as the only LLM configuration source; expose only service/model status, never credential values.

## Manual gates

Account registration, browser settings, MFA, OAuth login, and secret entry belong to the user. At these gates:

- Give the official URL and one exact next command.
- Pause until the user confirms completion.
- Use `zotero-mcp setup save-secret zotero` or `zotero-mcp setup save-secret mineru` for hidden terminal input.
- Use `zotero-mcp setup save-secret webdav` when unattended translation needs attachment upload credentials.
- Never ask the user to paste secrets into chat. Never print, inspect, commit, or copy credential contents.

Installing packages, creating a QMD collection, uploading PDFs to MinerU, starting paid translation, scheduling translation, enabling automatic manual-translation renaming, and changing Windows networking also require explicit user approval. Report the exact command and impact before execution. Once `translation.auto_rename_manual = true` is explicitly approved, the background rename monitor may apply its narrow attachment title/filename change without per-item approval; all ambiguity and sync checks remain fail-closed.

Zotero Web API is required only for writes. MinerU and SciVerse are optional external services that require an account and Token and have usage quotas; do not hard-code quota numbers in guidance. QMD is local software and requires no account. paper-lookup is an optional Codex skill and complements SciVerse. WebDAV only syncs attachments and is not a Zotero MCP prerequisite.

## Safety

- Run read-only checks before configuration changes.
- Keep Zotero writes behind the existing plan/apply flow and explicit `confirm=true`.
- Do not infer collection keys when resolution is ambiguous.
- Do not replace the full Codex config file.
- Do not commit local config, tokens, ledgers, parsed papers, deletion backups, or private workflow modules.
- Do not create remote repositories or push unless the user explicitly requests it.
