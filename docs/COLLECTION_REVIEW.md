# QMD-assisted collection review

Use this workflow after MinerU results are indexed in QMD. The user's existing Zotero collection tree is the classification standard; the Agent supplies evidence and proposed changes.

## 1. Lock the scope

Resolve the target collection by exact key, unique name, or full path. Record whether descendants are included, the batch size, and which destination collections are valid. Copy `templates/collection_review.md` for the review ledger.

Completion: every target item and permitted destination path is explicit.

## 2. Check the evidence gate

For each current English PDF attachment, confirm the MinerU result belongs to the same attachment key and QMD can locate and `get` the matching `full.md`. Search, query, and vsearch may locate evidence; final classification must use QMD get context.

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
