#!/usr/bin/env python3
"""Run a bounded MinerU-to-QMD pipeline for one Zotero collection."""

from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import mineru_workflow, zotero_runtime

LOCK_PATH = Path(tempfile.gettempdir()) / "zotero-mineru-qmd-pipeline.lock"
DEFAULT_QMD_COLLECTION = "zotero-mineru"


def qmd_command() -> str:
    executable = zotero_runtime.configured_command(
        "qmd", "command", "QMD_COMMAND", "qmd"
    )
    if executable:
        return executable
    configured = (
        os.environ.get("QMD_COMMAND", "").strip()
        or zotero_runtime.config_string("qmd", "command")
        or "qmd"
    )
    raise FileNotFoundError(f"QMD command not found: {configured}")


def qmd_collection() -> str:
    return (
        os.environ.get("QMD_COLLECTION", "").strip()
        or zotero_runtime.config_string("qmd", "collection")
        or DEFAULT_QMD_COLLECTION
    )


def pipeline_states(state_dir: Path | None = None) -> list[dict[str, Any]]:
    state_dir = mineru_workflow.STATE_DIR if state_dir is None else state_dir
    states = []
    for path in sorted(state_dir.glob("*.json"), key=lambda item: item.stat().st_mtime):
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("batch_id") != path.stem:
            raise RuntimeError(f"Batch state does not match filename: {path}")
        states.append(state)
    return states


def find_active_batch(collection_key: str, state_dir: Path | None = None) -> str | None:
    active = [
        state
        for state in pipeline_states(state_dir)
        if state.get("collection_key") == collection_key
        and state.get("status") != "complete"
    ]
    if len(active) > 1:
        batch_ids = ", ".join(str(state["batch_id"]) for state in active)
        raise RuntimeError(
            f"Multiple unfinished MinerU batches for {collection_key}: {batch_ids}"
        )
    return str(active[0]["batch_id"]) if active else None


def unresolved_failed_keys(
    collection_key: str,
    ready_keys: set[str],
    state_dir: Path | None = None,
) -> list[str]:
    failed = set()
    for state in pipeline_states(state_dir):
        if state.get("collection_key") != collection_key:
            continue
        failed.update(set(state_failed_keys(state)) & ready_keys)
    return sorted(failed)


def select_pipeline_batch(
    plan: dict[str, Any],
    max_files: int,
    max_pages_per_batch: int,
    remaining_page_budget: int,
) -> list[dict[str, Any]]:
    page_limit = min(max_pages_per_batch, remaining_page_budget)
    selected, _ = mineru_workflow.select_batch(
        plan["ready"], max_files=max_files, max_pages=page_limit
    )
    return selected


def submit_batch(
    collection_key: str,
    selected: list[dict[str, Any]],
    deferred_count: int,
    max_pages: int,
) -> str:
    print(
        f"MINERU_SUBMIT_START collection={collection_key} "
        f"files={len(selected)} max_pages={max_pages}",
        flush=True,
    )
    state, failures = mineru_workflow.submit_selected_batch(
        collection_key,
        selected,
        deferred_count=deferred_count,
        max_pages=max_pages,
    )
    batch_id = str(state["batch_id"])
    if failures:
        raise RuntimeError(
            f"MinerU submit failed for batch {batch_id}; failed files: {failures}"
        )
    print(f"MINERU_SUBMIT_DONE batch_id={batch_id}", flush=True)
    return batch_id


def collect_once(batch_id: str) -> dict[str, Any]:
    print(f"MINERU_COLLECT_START batch_id={batch_id}", flush=True)
    try:
        state, failures = mineru_workflow.collect_batch(batch_id)
    except Exception as exc:
        state = mineru_workflow.load_state(batch_id)
        failed = state_failed_keys(state)
        failed_detail = f"; failed files: {failed}" if failed else ""
        raise RuntimeError(
            f"MinerU collect failed for batch {batch_id}{failed_detail}"
        ) from exc
    if failures:
        failed = state_failed_keys(state)
        raise RuntimeError(
            f"MinerU collect failed for batch {batch_id}; failed files: {failed}"
        )
    print(f"MINERU_COLLECT_DONE batch_id={batch_id}", flush=True)
    return state


def run_qmd_update() -> None:
    command = [qmd_command(), "update"]
    print(f"QMD_UPDATE_START command={' '.join(command)}", flush=True)
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"QMD_UPDATE failed with exit code {result.returncode}")
    print("QMD_UPDATE_DONE", flush=True)


def verify_qmd_items(item_keys: list[str]) -> tuple[list[str], list[str]]:
    if not item_keys:
        return [], []
    collection = qmd_collection()
    references = {
        item_key: f"qmd://{collection}/{item_key}/full.md" for item_key in item_keys
    }
    result = subprocess.run(
        [
            qmd_command(),
            "multi-get",
            ",".join(references.values()),
            "--format",
            "json",
            "--max-bytes",
            "1",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"QMD multi-get failed with exit code {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    try:
        documents = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("QMD multi-get returned invalid JSON") from exc
    if not isinstance(documents, list):
        raise TypeError("QMD multi-get returned invalid data")
    found = {
        str(document.get("file"))
        for document in documents
        if isinstance(document, dict)
    }
    verified = [key for key in item_keys if references[key] in found]
    missing = [key for key in item_keys if references[key] not in found]
    return verified, missing


def start_qmd_embed(
    reason: str, item_keys: list[str] | None = None
) -> subprocess.Popen[bytes]:
    command = [qmd_command(), "embed", "-c", qmd_collection()]
    print(
        f"QMD_EMBED_START reason={reason} command={' '.join(command)}",
        flush=True,
    )
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    process.qmd_item_keys = (
        mineru_workflow.todo_keys_needing_qmd()
        if item_keys is None
        else list(item_keys)
    )
    return process


def wait_qmd_embed(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"QMD embedding failed with exit code {return_code}")
    item_keys = list(getattr(process, "qmd_item_keys", []))
    verified, missing = verify_qmd_items(item_keys)
    mineru_workflow.mark_todo_qmd_indexed(verified)
    if missing:
        raise RuntimeError(f"QMD could not read indexed MinerU documents: {missing}")
    print("QMD_EMBED_DONE", flush=True)


def stop_qmd_embed(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    print("QMD_EMBED_STOP", flush=True)
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        import signal

        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            import signal

            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def start_qmd_cycle(
    previous: subprocess.Popen[bytes] | None, reason: str
) -> subprocess.Popen[bytes] | None:
    wait_qmd_embed(previous)
    item_keys = mineru_workflow.todo_keys_needing_qmd()
    if not item_keys:
        print(f"QMD_CYCLE_SKIP reason={reason} pending=0", flush=True)
        return None
    run_qmd_update()
    return start_qmd_embed(reason, item_keys)


@contextmanager
def pipeline_lock(lock_path: Path = LOCK_PATH) -> Iterator[None]:
    try:
        with zotero_runtime.exclusive_file_lock(lock_path, blocking=False):
            yield
    except BlockingIOError as exc:
        raise RuntimeError("Another MinerU-QMD pipeline is already running") from exc
    except OSError as exc:
        if os.name == "nt" and exc.errno in {errno.EACCES, errno.EDEADLK}:
            raise RuntimeError(
                "Another MinerU-QMD pipeline is already running"
            ) from exc
        raise


def state_failed_keys(state: dict[str, Any]) -> list[str]:
    return sorted(
        str(record.get("data_id"))
        for record in state.get("files", [])
        if record.get("result_state") == "failed" or record.get("upload_error")
    )


def print_plan_summary(plan: dict[str, Any], remaining_page_budget: int) -> None:
    print(
        "PIPELINE_PLAN collection={} ready={} stale={} ready_pages={} existing={} "
        "missing={} blocked={} remaining_page_budget={}".format(
            plan["collection_key"],
            len(plan["ready"]),
            sum(record.get("plan_status") == "stale" for record in plan["ready"]),
            plan["ready_pages"],
            len(plan["existing"]),
            len(plan["missing"]),
            len(plan["blocked"]),
            remaining_page_budget,
        ),
        flush=True,
    )


def validate_arguments(arguments: argparse.Namespace) -> None:
    if not 1 <= arguments.max_files <= mineru_workflow.mineru_client.MAX_BATCH_FILES:
        raise ValueError(
            f"max_files must be 1-{mineru_workflow.mineru_client.MAX_BATCH_FILES}"
        )
    if arguments.page_budget < 1:
        raise ValueError("page_budget must be positive")
    if arguments.max_pages_per_batch < 1:
        raise ValueError("max_pages_per_batch must be positive")
    if arguments.max_batches < 0:
        raise ValueError("max_batches cannot be negative")
    if not 30 <= arguments.poll_seconds <= 60:
        raise ValueError("poll_seconds must be 30-60")
    if arguments.max_wait_minutes < 1:
        raise ValueError("max_wait_minutes must be positive")
    qmd_command()


def run_pipeline(arguments: argparse.Namespace) -> int:
    validate_arguments(arguments)
    submitted_batches = 0
    submitted_pages = 0
    qmd_process: subprocess.Popen[bytes] | None = None
    active_batch = None
    batch_wait_started = None

    with pipeline_lock():
        try:
            qmd_process = start_qmd_cycle(None, "startup-reconcile")
            active_batch = find_active_batch(arguments.collection_key)
            if active_batch:
                batch_wait_started = time.monotonic()
                print(f"MINERU_RESUME batch_id={active_batch}", flush=True)

            while True:
                if active_batch is not None:
                    wait_qmd_embed(qmd_process)
                    qmd_process = None
                    state = collect_once(active_batch)
                    failed = state_failed_keys(state)
                    if failed:
                        raise RuntimeError(
                            f"MinerU batch {active_batch} has failed files: {failed}"
                        )
                    if state.get("status") == "complete":
                        print(
                            f"MINERU_VERIFY_START batch_id={active_batch}", flush=True
                        )
                        mineru_workflow.verify_target(active_batch)
                        print(f"MINERU_VERIFY_DONE batch_id={active_batch}", flush=True)
                        qmd_process = start_qmd_cycle(qmd_process, active_batch)
                        active_batch = None
                        batch_wait_started = None
                        continue

                    assert batch_wait_started is not None
                    elapsed_minutes = (time.monotonic() - batch_wait_started) / 60
                    if elapsed_minutes >= arguments.max_wait_minutes:
                        raise TimeoutError(
                            f"MinerU batch {active_batch} exceeded "
                            f"{arguments.max_wait_minutes} minutes"
                        )
                    print(
                        f"MINERU_WAIT batch_id={active_batch} "
                        f"seconds={arguments.poll_seconds}",
                        flush=True,
                    )
                    time.sleep(arguments.poll_seconds)
                    continue

                remaining_page_budget = arguments.page_budget - submitted_pages
                plan = mineru_workflow.collection_plan(
                    arguments.collection_key, arguments.recursive
                )
                mineru_workflow.sync_todo_from_plan(plan)
                print_plan_summary(plan, remaining_page_budget)
                if not plan["ready"]:
                    wait_qmd_embed(qmd_process)
                    qmd_process = None
                    print(
                        f"PIPELINE_COMPLETE collection={arguments.collection_key} "
                        f"submitted_batches={submitted_batches} "
                        f"submitted_pages={submitted_pages}",
                        flush=True,
                    )
                    return 0

                ready_keys = {str(record["data_id"]) for record in plan["ready"]}
                unresolved = unresolved_failed_keys(
                    arguments.collection_key, ready_keys
                )
                if unresolved and not arguments.allow_retry_failed:
                    raise RuntimeError(
                        "Refusing to resubmit previously failed items without "
                        f"--allow-retry-failed: {unresolved}"
                    )

                if arguments.max_batches and submitted_batches >= arguments.max_batches:
                    wait_qmd_embed(qmd_process)
                    qmd_process = None
                    print("PIPELINE_LIMIT reason=max-batches", flush=True)
                    return 0
                if remaining_page_budget < 1:
                    wait_qmd_embed(qmd_process)
                    qmd_process = None
                    print("PIPELINE_LIMIT reason=page-budget", flush=True)
                    return 0

                selected = select_pipeline_batch(
                    plan,
                    max_files=arguments.max_files,
                    max_pages_per_batch=arguments.max_pages_per_batch,
                    remaining_page_budget=remaining_page_budget,
                )
                if not selected:
                    wait_qmd_embed(qmd_process)
                    qmd_process = None
                    print(
                        "PIPELINE_LIMIT reason=no-pdf-fits-remaining-page-budget",
                        flush=True,
                    )
                    return 0

                page_limit = min(arguments.max_pages_per_batch, remaining_page_budget)
                active_batch = submit_batch(
                    arguments.collection_key,
                    selected,
                    deferred_count=len(plan["ready"]) - len(selected),
                    max_pages=page_limit,
                )
                submitted_state = mineru_workflow.load_state(active_batch)
                submitted_batches += 1
                submitted_pages += int(submitted_state["selected_pages"])
                batch_wait_started = time.monotonic()

        except KeyboardInterrupt:
            print("PIPELINE_INTERRUPTED", file=sys.stderr, flush=True)
            return 130
        finally:
            stop_qmd_embed(qmd_process)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection_key")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--page-budget", type=int, required=True)
    parser.add_argument("--max-files", type=int, default=50)
    parser.add_argument("--max-pages-per-batch", type=int, default=1000)
    parser.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="Maximum new batches in this run; 0 means no batch-count cap",
    )
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--max-wait-minutes", type=int, default=180)
    parser.add_argument("--allow-retry-failed", action="store_true")
    return parser


def main() -> int:
    try:
        return run_pipeline(build_parser().parse_args())
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports unexpected failures
        print(f"ERROR {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
