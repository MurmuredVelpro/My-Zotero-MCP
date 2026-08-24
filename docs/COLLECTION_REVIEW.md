# QMD-assisted collection review

Use this workflow after MinerU results are indexed in QMD. The user's existing Zotero collection tree is the classification standard; the Agent supplies evidence and proposed changes.

## 1. Lock the scope

Resolve the target collection by exact key, unique name, or full path. Record whether descendants are included, the batch size, and which destination collections are valid. Copy `templates/collection_review.md` for the review ledger.

Before reviewing evidence, initialize or refresh the shared local workflow snapshot. On first initialization, list every recursive collection to track; later no-argument sync reuses those roots:

```bash
python -m zotero_mcp.zotero_workflow sync \
  --collection Senescence \
  --collection "Journal Club" \
  --collection Glioma
python -m zotero_mcp.zotero_workflow sync
python -m zotero_mcp.zotero_workflow status
```

Tracking can cover several collections, but each review batch must select one explicit collection:

```bash
python -m zotero_mcp.zotero_workflow next-batch --collection Senescence --limit 5
```

Use the SQLite snapshot to identify the current English source attachment, MinerU state, QMD state, and known issues. It is a read-only coordination record; it does not replace the review ledger or perform collection writes.

Completion: every target item and permitted destination path is explicit.

## 2. Check the evidence gate

For each current English PDF attachment, confirm the MinerU result belongs to the same attachment key and QMD can locate and `get` the matching `full.md`. The SQLite fields `mineru_state=parsed_current` and `qmd_state=indexed_current` are preflight signals, not final evidence. Search, query, and vsearch may locate evidence; final classification must use QMD get context.

Mark unavailable or stale evidence as `no_qmd` with a reason. Do not classify that item from the raw PDF or an unindexed `full.md` as a silent fallback.

Completion: every reviewable item has current QMD evidence; every blocked item has a recorded reason.

## 3. Propose collection changes

Compare the full-text evidence with the user's written classification rules. Record current paths, proposed paths, decisive evidence, and reasoning. Preserve unrelated collection memberships and permit multi-label placement when the rules allow it.

Completion: every recommendation is reviewable without performing a Zotero write.

## 4. Human review

Present one bounded batch. The user may approve, reject, or edit each recommendation. Update the ledger; do not call an apply tool during this step.

Completion: the batch has explicit human decisions.

## 5. Plan, apply, and read back

Call `zotero_plan_collection_reconcile` using exact item and collection keys. Show the resulting changes. Only after a separate explicit approval call `zotero_apply_collection_reconcile` with `confirm=true`.

Read back local and cloud collection memberships for every item. A successful write response without matching readback is not completion evidence.

Completion: planned changes, applied changes, and readback all agree; blocked or rejected items remain unchanged.
