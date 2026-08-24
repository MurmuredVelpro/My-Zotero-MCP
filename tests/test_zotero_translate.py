from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import requests

from zotero_mcp import zotero_translate


def pdf2zh_prefs(
    *,
    service: str = "openailiked",
    model: str = "test-model",
    api_key: str = "secret-key",
    activate: bool = True,
    server_url: str = "http://localhost:8890",
    extra_data: dict | None = None,
) -> str:
    apis = json.dumps(
        [
            {
                "key": "CONFIG1",
                "service": service,
                "model": model,
                "apiKey": api_key,
                "apiUrl": "https://relay.example/v1",
                "activate": activate,
                "extraData": extra_data or {"reasoning_effort": "high"},
            }
        ]
    )
    values = {
        "engine": "pdf2zh_next",
        "new_serverip": server_url,
        "next_service": service,
        "sourceLang": "en",
        "targetLang": "zh-CN",
        "autoOcr": True,
        "llmApis": apis,
    }
    return "\n".join(
        f'user_pref("{zotero_translate.PREF_PREFIX}{key}", {json.dumps(value)});'
        for key, value in values.items()
    )


def settings() -> zotero_translate.PDF2ZHSettings:
    return zotero_translate.PDF2ZHSettings(
        prefs_path=Path("prefs.js"),
        server_url="http://localhost:8890",
        service="openailiked",
        model="test-model",
        llm_api={
            "service": "openailiked",
            "model": "test-model",
            "apiKey": "secret-key",
            "apiUrl": "https://relay.example/v1",
            "extraData": {},
        },
        request_options={"sourceLang": "en", "targetLang": "zh-CN"},
    )


def pending_row(
    output_pdf: str = "",
    *,
    parent_key: str = "ABCD1234",
    source_key: str = "EFGH5678",
    status: str = "pending",
    attempt_count: int = 0,
    downloaded_at: str = "",
    next_attempt_at: str = "",
) -> dict[str, str]:
    return {
        "paper_title": "Paper",
        "parent_item_key": parent_key,
        "source_attachment_key": source_key,
        "status": status,
        "output_pdf": output_pdf,
        "last_error": "",
        "attempt_count": str(attempt_count),
        "downloaded_at": downloaded_at,
        "next_attempt_at": next_attempt_at,
    }


def run_worker(
    worker,
    *,
    qps=7,
    pool_size=13,
    max_items=1,
    paper_concurrency=1,
    inter_item_delay=0,
    retry_delay=0,
    transient_retries=0,
    dry_run=False,
):
    return worker.run_batch(
        qps,
        pool_size,
        max_items,
        paper_concurrency=paper_concurrency,
        inter_item_delay=inter_item_delay,
        retry_delay=retry_delay,
        transient_retries=transient_retries,
        dry_run=dry_run,
    )


class FakeResponse:
    def __init__(self, status_code: int, data=None, content: bytes = b"") -> None:
        self.status_code = status_code
        self.data = data
        self.content = content
        self.text = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self.data

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int):
        yield self.content


class FakePDF2ZHSession:
    def __init__(self) -> None:
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/health"):
            return FakeResponse(200, {"status": "ok"})
        if "/translatedFile/" in url:
            return FakeResponse(200, content=b"%PDF-1.4 translated")
        raise AssertionError(url)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(
            200,
            {
                "status": "success",
                "fileList": ["paper.no_watermark.zh-CN.mono.pdf"],
            },
        )


class PDF2ZHPrefsTests(unittest.TestCase):
    def test_loads_active_gui_configuration_without_exposing_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.js"
            path.write_text(pdf2zh_prefs(), encoding="utf-8")
            value = zotero_translate.load_pdf2zh_settings(path)
        self.assertEqual(value.service, "openailiked")
        self.assertEqual(value.model, "test-model")
        self.assertEqual(value.llm_api["apiKey"], "secret-key")
        self.assertNotIn("secret-key", json.dumps(value.summary()))

    def test_rejects_insecure_remote_pdf2zh_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.js"
            path.write_text(
                pdf2zh_prefs(server_url="http://pdf2zh.example:8890"),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(
                    zotero_translate.TranslationError, "insecure remote HTTP"
                ),
            ):
                zotero_translate.load_pdf2zh_settings(path)

    def test_explicit_override_allows_insecure_remote_pdf2zh_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.js"
            path.write_text(
                pdf2zh_prefs(server_url="http://pdf2zh.example:8890"),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {zotero_translate.ALLOW_INSECURE_HTTP_ENV: "1"},
                clear=True,
            ):
                value = zotero_translate.load_pdf2zh_settings(path)
        self.assertEqual(value.server_url, "http://pdf2zh.example:8890")

    def test_redacts_nested_extra_data_secrets(self):
        value = settings()
        value.llm_api["extraData"] = {
            "headers": {"Authorization": "Bearer nested-secret"},
            "password": "nested-password",
        }
        redacted = value.redact(
            "Bearer nested-secret failed with nested-password at https://relay.example/v1"
        )
        self.assertEqual(redacted.count("[redacted]"), 3)
        self.assertNotIn("nested-secret", redacted)
        self.assertNotIn("nested-password", redacted)

    def test_uses_plugin_defaults_when_gui_values_are_not_user_overrides(self):
        text = "\n".join(
            line
            for line in pdf2zh_prefs().splitlines()
            if not any(
                f"{zotero_translate.PREF_PREFIX}{key}" in line
                for key in ("engine", "new_serverip")
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.js"
            path.write_text(text, encoding="utf-8")
            value = zotero_translate.load_pdf2zh_settings(path)
        self.assertEqual(value.server_url, "http://localhost:8890")

    def test_rejects_missing_active_configuration(self):
        text = pdf2zh_prefs(activate=False)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.js"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                zotero_translate.TranslationError, "no active PDF2zh configuration"
            ):
                zotero_translate.load_pdf2zh_settings(path)

    def test_payload_uses_gui_model_and_explicit_batch_limits(self):
        payload = zotero_translate.build_translation_payload(
            "paper.pdf", "base64", settings(), 7, 13
        )
        self.assertEqual(payload["next_service"], "openailiked")
        self.assertEqual(payload["llm_api"]["model"], "test-model")
        self.assertEqual(payload["qps"], 7)
        self.assertEqual(payload["poolSize"], 13)
        self.assertTrue(payload["mono"])
        self.assertFalse(payload["dual"])
        self.assertTrue(payload["noDual"])

    def test_explicit_prefs_path_does_not_fall_back_to_another_profile(self):
        explicit = Path("chosen-prefs.js")
        with mock.patch.object(
            zotero_translate.zotero_runtime,
            "configured_path",
            return_value=Path("other.js"),
        ):
            self.assertEqual(zotero_translate.prefs_candidates(explicit), [explicit])

    def test_default_profile_is_checked_before_other_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "profiles.ini").write_text(
                "[Profile0]\nPath=Profiles/other\nIsRelative=1\n"
                "[Profile1]\nPath=Profiles/default\nIsRelative=1\nDefault=1\n",
                encoding="utf-8",
            )
            paths = zotero_translate._profiles_from_root(root)
        self.assertEqual(paths[0], root / "Profiles" / "default" / "prefs.js")

    def test_wsl_converts_absolute_windows_profile_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "profiles.ini").write_text(
                "[Profile0]\nPath=C:\\\\Users\\\\alice\\\\ZoteroProfile\nIsRelative=0\nDefault=1\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                zotero_translate,
                "_host_path",
                return_value=Path("/mnt/c/Users/alice/ZoteroProfile"),
            ):
                paths = zotero_translate._profiles_from_root(root)
        self.assertEqual(paths[0], Path("/mnt/c/Users/alice/ZoteroProfile/prefs.js"))


class PDF2ZHClientTests(unittest.TestCase):
    def test_downloads_translated_pdf_over_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Dudnyk et al - 2024.pdf"
            source.write_bytes(b"%PDF-1.4 source")
            session = FakePDF2ZHSession()
            client = zotero_translate.PDF2ZHClient(
                session=session, output_dir=Path(tmp) / "output"
            )
            output = client.translate(settings(), "ABCD1234", "EFGH5678", source, 7, 13)
        self.assertEqual(output.name, "Dudnyk et al - 2024的全文翻译.pdf")
        self.assertEqual(output.parent.name, "ABCD1234_EFGH5678")
        self.assertTrue(any("/translatedFile/" in call[1] for call in session.calls))
        self.assertFalse(any("server/translated" in call[1] for call in session.calls))

    def test_custom_naming_controls_downloaded_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Dudnyk et al - 2024.pdf"
            source.write_bytes(b"%PDF-1.4 source")
            client = zotero_translate.PDF2ZHClient(
                session=FakePDF2ZHSession(),
                output_dir=Path(tmp) / "output",
                naming=zotero_translate.TranslationNaming(
                    attachment_title="Chinese",
                    filename_template="{source_stem} (Chinese).pdf",
                ),
            )
            output = client.translate(settings(), "ABCD1234", "EFGH5678", source, 7, 13)
        self.assertEqual(output.name, "Dudnyk et al - 2024 (Chinese).pdf")


class TranslationNamingTests(unittest.TestCase):
    def test_missing_config_uses_current_defaults(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(
                os.environ,
                {
                    zotero_translate.zotero_runtime.CONFIG_FILE_ENV: str(
                        Path(tmp) / "missing.toml"
                    )
                },
            ),
        ):
            naming = zotero_translate.load_translation_naming()
        self.assertEqual(naming.attachment_title, "CN")
        self.assertEqual(
            naming.filename_for("English paper"), "English paper的全文翻译.pdf"
        )

    def test_loads_custom_naming_from_translation_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                "[translation]\n"
                'attachment_title = "Chinese"\n'
                'filename_template = "{source_stem} (Chinese).pdf"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {zotero_translate.zotero_runtime.CONFIG_FILE_ENV: str(config)},
            ):
                naming = zotero_translate.load_translation_naming()
        self.assertEqual(naming.attachment_title, "Chinese")
        self.assertEqual(
            naming.filename_for("English paper"), "English paper (Chinese).pdf"
        )

    def test_rejects_invalid_titles_and_filename_templates(self):
        invalid = [
            ("", "{source_stem}.pdf"),
            ("CN", "translation.pdf"),
            ("CN", "{unknown}.pdf"),
            ("CN", "{source_stem!r}.pdf"),
            ("CN", "{source_stem:>20}.pdf"),
            ("CN", "../{source_stem}.pdf"),
            ("CN", "folder\\{source_stem}.pdf"),
            ("CN", "{source_stem}.txt"),
        ]
        for title, template in invalid:
            with (
                self.subTest(title=title, template=template),
                self.assertRaises(zotero_translate.TranslationError),
            ):
                zotero_translate.TranslationNaming(title, template)


class FakeRenameZotero:
    PARENT_KEY = "ABCD1234"
    SOURCE_KEY = "EFGH5678"
    TRANSLATION_KEY = "CNAT1234"

    def __init__(self, root: Path, *, duplicate=False, conflict=False) -> None:
        self.source = root / "English paper.pdf"
        self.translation = root / "English paper.no_watermark.zh-CN.mono.pdf"
        self.source.write_bytes(b"%PDF-1.4 source")
        self.translation.write_bytes(b"%PDF-1.4 translated")
        if conflict:
            (root / "English paper的全文翻译.pdf").write_bytes(b"%PDF-1.4 conflict")
        self.duplicate = duplicate

    def parent_item(self, key):
        return {
            "key": self.PARENT_KEY,
            "data": {
                "key": self.PARENT_KEY,
                "itemType": "journalArticle",
                "title": "Paper",
            },
        }

    def select_source_attachment(self, parent):
        return {
            "key": self.SOURCE_KEY,
            "filename": self.source.name,
            "path": self.source,
        }

    def children(self, parent_key):
        children = [
            {
                "key": self.TRANSLATION_KEY,
                "data": {
                    "key": self.TRANSLATION_KEY,
                    "itemType": "attachment",
                    "title": "openailiked-mono",
                    "parentItem": self.PARENT_KEY,
                    "contentType": "application/pdf",
                    "filename": self.translation.name,
                },
            }
        ]
        if self.duplicate:
            children.append(
                {
                    "key": "CNAT5678",
                    "data": {
                        "key": "CNAT5678",
                        "itemType": "attachment",
                        "title": "CN",
                        "parentItem": self.PARENT_KEY,
                        "contentType": "application/pdf",
                        "filename": "second.pdf",
                    },
                }
            )
        return children

    def attachment_pdf(self, attachment_key):
        return self.translation


class FakeRenameAPI:
    def __init__(self, zotero: FakeRenameZotero, *, conflict=False) -> None:
        self.zotero = zotero
        self.conflict = conflict
        self.title = "openailiked-mono"
        self.filename = zotero.translation.name
        self.requests = []

    def web_api_status(self):
        return {"user_id": 123, "files_write": True}

    def web_api_get_item(self, user_id, key):
        return {
            "key": key,
            "version": 9,
            "data": {
                "key": key,
                "itemType": "attachment",
                "parentItem": self.zotero.PARENT_KEY,
                "title": self.title,
                "filename": self.filename,
            },
        }

    def web_api_request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if self.conflict:
            return FakeResponse(412)
        self.title = kwargs["payload"]["title"]
        self.filename = kwargs["payload"]["filename"]
        return FakeResponse(204)


class ManualTranslationRenameTests(unittest.TestCase):
    def test_plan_proposes_title_and_filename_without_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            zotero = FakeRenameZotero(Path(tmp))
            api = FakeRenameAPI(zotero)
            result = zotero_translate.plan_manual_translation_renames(
                [zotero.PARENT_KEY],
                zotero=zotero,
                api=api,
                naming=zotero_translate.DEFAULT_TRANSLATION_NAMING,
            )
        row = result["results"][0]
        self.assertEqual(result["rename"], 1)
        self.assertEqual(row["translation_attachment_key"], "CNAT1234")
        self.assertEqual(row["new_title"], "CN")
        self.assertEqual(row["new_filename"], "English paper的全文翻译.pdf")
        self.assertEqual(api.requests, [])

    def test_plan_blocks_ambiguous_translation_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            zotero = FakeRenameZotero(Path(tmp), duplicate=True)
            result = zotero_translate.plan_manual_translation_renames(
                [zotero.PARENT_KEY],
                zotero=zotero,
                api=FakeRenameAPI(zotero),
                naming=zotero_translate.DEFAULT_TRANSLATION_NAMING,
            )
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(
            result["results"][0]["blockers"],
            ["multiple_translation_attachments"],
        )

    def test_plan_blocks_existing_target_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            zotero = FakeRenameZotero(Path(tmp), conflict=True)
            result = zotero_translate.plan_manual_translation_renames(
                [zotero.PARENT_KEY],
                zotero=zotero,
                api=FakeRenameAPI(zotero),
                naming=zotero_translate.DEFAULT_TRANSLATION_NAMING,
            )
        self.assertEqual(result["results"][0]["blockers"], ["target_filename_exists"])

    def test_plan_uses_custom_title_and_filename(self):
        naming = zotero_translate.TranslationNaming(
            "Chinese", "{source_stem} (Chinese).pdf"
        )
        with tempfile.TemporaryDirectory() as tmp:
            zotero = FakeRenameZotero(Path(tmp))
            result = zotero_translate.plan_manual_translation_renames(
                [zotero.PARENT_KEY],
                zotero=zotero,
                api=FakeRenameAPI(zotero),
                naming=naming,
            )
        row = result["results"][0]
        self.assertEqual(row["new_title"], "Chinese")
        self.assertEqual(row["new_filename"], "English paper (Chinese).pdf")

    def test_apply_requires_exact_keys_and_versioned_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            zotero = FakeRenameZotero(Path(tmp))
            api = FakeRenameAPI(zotero)
            result = zotero_translate.apply_manual_translation_renames(
                [
                    {
                        "parent_item_key": zotero.PARENT_KEY,
                        "source_attachment_key": zotero.SOURCE_KEY,
                        "translation_attachment_key": zotero.TRANSLATION_KEY,
                        "new_title": "CN",
                        "new_filename": "English paper的全文翻译.pdf",
                    }
                ],
                True,
                zotero=zotero,
                api=api,
                naming=zotero_translate.DEFAULT_TRANSLATION_NAMING,
            )
        self.assertEqual(result["renamed"], 1)
        method, path, kwargs = api.requests[0]
        self.assertEqual((method, path), ("PATCH", "users/123/items/CNAT1234"))
        self.assertEqual(kwargs["headers"], {"If-Unmodified-Since-Version": "9"})
        self.assertEqual(
            kwargs["payload"],
            {"title": "CN", "filename": "English paper的全文翻译.pdf"},
        )

    def test_apply_rejects_version_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            zotero = FakeRenameZotero(Path(tmp))
            api = FakeRenameAPI(zotero, conflict=True)
            with self.assertRaisesRegex(
                zotero_translate.zotero_web_api.ZoteroVersionConflictError,
                "changed before rename",
            ):
                zotero_translate.apply_manual_translation_renames(
                    [
                        {
                            "parent_item_key": zotero.PARENT_KEY,
                            "source_attachment_key": zotero.SOURCE_KEY,
                            "translation_attachment_key": zotero.TRANSLATION_KEY,
                            "new_title": "CN",
                            "new_filename": "English paper的全文翻译.pdf",
                        }
                    ],
                    True,
                    zotero=zotero,
                    api=api,
                    naming=zotero_translate.DEFAULT_TRANSLATION_NAMING,
                )

    def test_apply_rejects_naming_changed_since_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            zotero = FakeRenameZotero(Path(tmp))
            with self.assertRaisesRegex(
                zotero_translate.TranslationError, "new_title changed.*run plan again"
            ):
                zotero_translate.apply_manual_translation_renames(
                    [
                        {
                            "parent_item_key": zotero.PARENT_KEY,
                            "source_attachment_key": zotero.SOURCE_KEY,
                            "translation_attachment_key": zotero.TRANSLATION_KEY,
                            "new_title": "CN",
                            "new_filename": "English paper的全文翻译.pdf",
                        }
                    ],
                    True,
                    zotero=zotero,
                    api=FakeRenameAPI(zotero),
                    naming=zotero_translate.TranslationNaming(
                        "Chinese", "{source_stem} (Chinese).pdf"
                    ),
                )


class AutoRenameWatchTests(unittest.TestCase):
    def test_attachment_versions_paginates_and_preserves_since(self):
        first = {f"A{index:07d}": index + 1 for index in range(100)}
        second = {"B0000000": 101}
        with mock.patch.object(
            zotero_translate.zotero_local,
            "zotero_get",
            side_effect=[first, second],
        ) as get:
            versions = zotero_translate._attachment_versions(50)
        self.assertEqual(len(versions), 101)
        self.assertEqual(get.call_args_list[0].args[1]["since"], 50)
        self.assertEqual(get.call_args_list[0].args[1]["start"], 0)
        self.assertEqual(get.call_args_list[1].args[1]["start"], 100)

    def test_first_scan_only_records_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "watch.json"
            rename_one = mock.Mock()
            result = zotero_translate.scan_manual_translation_renames(
                state_path,
                versions_reader=lambda since=None: {"OLDPDF01": 12},
                rename_one=rename_one,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(result["initialized"])
        self.assertEqual(state, {"last_version": 12, "pending": {}})
        rename_one.assert_not_called()

    def test_new_attachment_is_renamed_and_removed_from_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "watch.json"
            zotero_translate.scan_manual_translation_renames(
                state_path,
                versions_reader=lambda since=None: {"OLDPDF01": 12},
            )
            result = zotero_translate.scan_manual_translation_renames(
                state_path,
                now=100,
                versions_reader=lambda since=None: {"NEWPDF01": 14},
                rename_one=lambda key: {
                    "status": "renamed",
                    "attachment_key": key,
                },
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(result["renamed"], 1)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(state["last_version"], 14)
        self.assertEqual(state["pending"], {})

    def test_blocked_attachment_retries_after_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "watch.json"
            zotero_translate.scan_manual_translation_renames(
                state_path,
                versions_reader=lambda since=None: {"OLDPDF01": 12},
            )
            rename_one = mock.Mock(
                return_value={"status": "blocked", "blockers": ["not_synced"]}
            )
            first = zotero_translate.scan_manual_translation_renames(
                state_path,
                now=100,
                versions_reader=lambda since=None: {"NEWPDF01": 14},
                rename_one=rename_one,
            )
            early = zotero_translate.scan_manual_translation_renames(
                state_path,
                now=120,
                versions_reader=lambda since=None: {},
                rename_one=rename_one,
            )
            later = zotero_translate.scan_manual_translation_renames(
                state_path,
                now=131,
                versions_reader=lambda since=None: {},
                rename_one=rename_one,
            )
        self.assertEqual(first["blocked"], 1)
        self.assertEqual(early["results"], [])
        self.assertEqual(later["blocked"], 1)
        self.assertEqual(rename_one.call_count, 2)

    def test_watch_does_not_scan_when_disabled(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                zotero_translate.zotero_runtime,
                "config_bool",
                return_value=False,
            ),
            mock.patch.object(
                zotero_translate, "scan_manual_translation_renames"
            ) as scan,
        ):
            result = zotero_translate.watch_manual_translation_renames(
                once=True, state_path=Path(tmp) / "watch.json"
            )
        self.assertEqual(result, {"stopped": "disabled"})
        scan.assert_not_called()

    def test_watch_once_runs_one_scan_when_enabled(self):
        expected = {"initialized": True, "renamed": 0}
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                zotero_translate.zotero_runtime,
                "config_bool",
                return_value=True,
            ),
            mock.patch.object(
                zotero_translate,
                "scan_manual_translation_renames",
                return_value=expected,
            ) as scan,
        ):
            result = zotero_translate.watch_manual_translation_renames(
                once=True, state_path=Path(tmp) / "watch.json"
            )
        self.assertEqual(result, expected)
        scan.assert_called_once()


class QueueTests(unittest.TestCase):
    def test_queue_schema_has_no_fixed_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = zotero_translate.QueueStore(Path(tmp) / "queue.csv")
            row = {
                "paper_title": "Paper",
                "parent_item_key": "ABCD1234",
                "source_attachment_key": "EFGH5678",
                "status": "pending",
                "output_pdf": "",
                "last_error": "",
                "attempt_count": "0",
                "downloaded_at": "",
                "next_attempt_at": "",
            }
            with store.lock():
                store.write([row])
                self.assertEqual(store.read(), [row])
        self.assertNotIn("model", zotero_translate.QUEUE_FIELDS)

    def test_second_queue_holder_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = zotero_translate.QueueStore(Path(tmp) / "queue.csv")
            with (
                store.lock(),
                self.assertRaisesRegex(
                    zotero_translate.TranslationError, "already locked"
                ),
                store.lock(),
            ):
                pass


class FakeZotero:
    def __init__(
        self, source: Path | dict[str, Path], cn_present: bool = False
    ) -> None:
        self.source = source
        self.cn_present = cn_present

    def source_pdf(self, parent_key, source_key):
        if isinstance(self.source, dict):
            return self.source[parent_key]
        return self.source

    def cn_attachment(self, parent_key):
        return {"key": "CNAT1234"} if self.cn_present else None


class FakeServer:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.translate_calls = 0
        self.health_calls = 0

    def health(self, value):
        self.health_calls += 1
        return "http://localhost:8890"

    def translate(self, value, parent_key, source_key, source_pdf, qps, pool_size):
        self.translate_calls += 1
        self.output.write_bytes(b"%PDF-1.4 translated")
        return self.output


class FakeAttachments:
    def __init__(self) -> None:
        self.imports = []
        self.preflight_calls = []

    def preflight(self, *, verify_write=False):
        self.preflight_calls.append(verify_write)
        return 123

    def import_pdf(self, user_id, parent_key, output):
        self.imports.append((user_id, parent_key, output))


class ConcurrentTranslationState:
    def __init__(
        self, root: Path, delays=None, failures=None, barrier_count: int | None = None
    ) -> None:
        self.root = root
        self.delays = delays or {}
        self.failures = dict(failures or {})
        self.barrier = threading.Barrier(barrier_count) if barrier_count else None
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.instances = 0
        self.calls = []
        self.started = {}
        self.finished = {}

    def server(self):
        with self.lock:
            self.instances += 1
        return ConcurrentFakeServer(self)


class ConcurrentFakeServer:
    def __init__(self, state: ConcurrentTranslationState) -> None:
        self.state = state
        self.base_url = ""

    def translate(self, value, parent_key, source_key, source_pdf, qps, pool_size):
        started = time.monotonic()
        with self.state.lock:
            self.state.active += 1
            self.state.max_active = max(self.state.max_active, self.state.active)
            self.state.calls.append((parent_key, qps, pool_size))
            self.state.started.setdefault(parent_key, []).append(started)
        try:
            if self.state.barrier is not None:
                self.state.barrier.wait(timeout=5)
            time.sleep(self.state.delays.get(parent_key, 0))
            with self.state.lock:
                remaining = self.state.failures.get(parent_key, 0)
                if remaining:
                    self.state.failures[parent_key] = remaining - 1
            if remaining:
                raise zotero_translate.TranslationError(
                    "Concurrency limit exceeded for account"
                )
            output = self.state.root / f"{parent_key}.pdf"
            output.write_bytes(b"%PDF-1.4 translated")
            return output
        finally:
            finished = time.monotonic()
            with self.state.lock:
                self.state.active -= 1
                self.state.finished.setdefault(parent_key, []).append(finished)


class SlowAttachments(FakeAttachments):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def import_pdf(self, user_id, parent_key, output):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            super().import_pdf(user_id, parent_key, output)
        finally:
            with self.lock:
                self.active -= 1


class WorkerTests(unittest.TestCase):
    def test_default_clients_share_one_naming_snapshot(self):
        naming = zotero_translate.TranslationNaming(
            "Chinese", "{source_stem} (Chinese).pdf"
        )
        store = mock.Mock()
        with (
            mock.patch.object(
                zotero_translate, "load_translation_naming", return_value=naming
            ),
            mock.patch.object(zotero_translate, "ZoteroClient") as zotero_client,
            mock.patch.object(zotero_translate, "PDF2ZHClient") as pdf2zh_client,
            mock.patch.object(
                zotero_translate, "ZoteroAttachmentClient"
            ) as attachment_client,
        ):
            worker = zotero_translate.TranslationWorker(store)
        zotero_client.assert_called_once_with(naming=naming)
        pdf2zh_client.assert_called_once_with(naming=naming)
        attachment_client.assert_called_once_with(naming=naming)
        self.assertIs(worker.naming, naming)

    def test_translation_then_import_updates_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4 source")
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write([pending_row()])
            server = FakeServer(root / "translated.pdf")
            attachments = FakeAttachments()
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(source),
                server=server,
                attachments=attachments,
            )
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                result = run_worker(worker)
            rows = store.read()
        self.assertEqual(result["done"], 1)
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(server.translate_calls, 1)
        self.assertEqual(len(attachments.imports), 1)
        self.assertEqual(attachments.preflight_calls, [True])

    def test_existing_output_retries_import_without_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4 source")
            output = root / "translated.pdf"
            output.write_bytes(b"%PDF-1.4 translated")
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write([pending_row(str(output))])
            server = FakeServer(root / "unused.pdf")
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(source),
                server=server,
                attachments=FakeAttachments(),
            )
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                run_worker(worker)
        self.assertEqual(server.translate_calls, 0)

    def test_existing_output_is_renamed_to_current_template_before_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4 source")
            output = root / "old-name.pdf"
            output.write_bytes(b"%PDF-1.4 translated")
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write([pending_row(str(output))])
            attachments = FakeAttachments()
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(source),
                server=FakeServer(root / "unused.pdf"),
                attachments=attachments,
                naming=zotero_translate.TranslationNaming(
                    "Chinese", "{source_stem} (Chinese).pdf"
                ),
            )
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                run_worker(worker)
            row = store.read()[0]
        imported = attachments.imports[0][2]
        self.assertEqual(imported.name, "source (Chinese).pdf")
        self.assertEqual(Path(row["output_pdf"]).name, "source (Chinese).pdf")
        self.assertFalse(output.exists())

    def test_existing_cn_finishes_without_translation_or_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4 source")
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write([pending_row()])
            server = FakeServer(root / "unused.pdf")
            attachments = FakeAttachments()
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(source, cn_present=True),
                server=server,
                attachments=attachments,
            )
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                result = run_worker(worker)
        self.assertEqual(result["done"], 1)
        self.assertEqual(server.translate_calls, 0)
        self.assertEqual(attachments.imports, [])

    def test_failure_is_recorded_without_leaking_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4 source")
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write([pending_row()])
            server = FakeServer(root / "unused.pdf")
            server.translate = mock.Mock(side_effect=RuntimeError("secret-key failed"))
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(source),
                server=server,
                attachments=FakeAttachments(),
            )
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                result = run_worker(worker)
            row = store.read()[0]
        self.assertEqual(result["failed"], 1)
        self.assertEqual(row["status"], "failed")
        self.assertIn("[redacted] failed", row["last_error"])
        self.assertNotIn("secret-key", row["last_error"])

    def test_dry_run_does_not_mutate_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4 source")
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write([pending_row()])
            before = store.path.read_bytes()
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(source),
                server=FakeServer(root / "unused.pdf"),
                attachments=FakeAttachments(),
            )
            with (
                mock.patch.object(
                    zotero_translate, "load_pdf2zh_settings", return_value=settings()
                ),
                mock.patch.object(zotero_translate.time, "sleep") as sleep,
            ):
                result = run_worker(worker, dry_run=True)
            after = store.path.read_bytes()
        self.assertTrue(result["dry_run"])
        self.assertEqual(before, after)
        self.assertEqual(worker.attachments.preflight_calls, [False])
        sleep.assert_not_called()

    def test_three_papers_run_concurrently_with_full_per_paper_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keys = ["PAPER001", "PAPER002", "PAPER003"]
            sources = {}
            rows = []
            for position, key in enumerate(keys, start=1):
                source = root / f"source-{position}.pdf"
                source.write_bytes(b"%PDF-1.4 source")
                sources[key] = source
                rows.append(
                    pending_row(
                        parent_key=key,
                        source_key=f"SOURCE0{position}",
                    )
                )
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write(rows)
            state = ConcurrentTranslationState(
                root,
                delays={key: 0.05 for key in keys},
                barrier_count=3,
            )
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(sources),
                server=FakeServer(root / "unused.pdf"),
                server_factory=state.server,
                attachments=FakeAttachments(),
            )
            writer_threads = []
            real_write = store.write

            def tracked_write(current_rows):
                writer_threads.append(threading.get_ident())
                real_write(current_rows)

            store.write = tracked_write
            main_thread = threading.get_ident()
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                result = run_worker(
                    worker,
                    qps=2,
                    pool_size=4,
                    max_items=3,
                    paper_concurrency=3,
                )
        self.assertEqual(result["done"], 3)
        self.assertEqual(result["nominal_peak_qps"], 6)
        self.assertEqual(result["nominal_peak_pool_size"], 12)
        self.assertEqual(state.max_active, 3)
        self.assertEqual(state.instances, 3)
        self.assertEqual({call[1:] for call in state.calls}, {(2, 4)})
        self.assertEqual(set(writer_threads), {main_thread})

    def test_completed_downloads_import_in_completion_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keys = ["PAPER001", "PAPER002"]
            sources = {}
            for position, key in enumerate(keys, start=1):
                source = root / f"source-{position}.pdf"
                source.write_bytes(b"%PDF-1.4 source")
                sources[key] = source
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write(
                [
                    pending_row(parent_key="PAPER001", source_key="SOURCE01"),
                    pending_row(parent_key="PAPER002", source_key="SOURCE02"),
                ]
            )
            state = ConcurrentTranslationState(
                root, delays={"PAPER001": 0.08, "PAPER002": 0.01}
            )
            attachments = FakeAttachments()
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(sources),
                server=FakeServer(root / "unused.pdf"),
                server_factory=state.server,
                attachments=attachments,
            )
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                run_worker(worker, max_items=2, paper_concurrency=2)
        self.assertEqual(
            [parent_key for _, parent_key, _ in attachments.imports],
            ["PAPER002", "PAPER001"],
        )

    def test_each_slot_applies_its_own_inter_item_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keys = ["PAPER001", "PAPER002", "PAPER003", "PAPER004"]
            sources = {}
            rows = []
            for position, key in enumerate(keys, start=1):
                source = root / f"source-{position}.pdf"
                source.write_bytes(b"%PDF-1.4 source")
                sources[key] = source
                rows.append(
                    pending_row(
                        parent_key=key,
                        source_key=f"SOURCE0{position}",
                    )
                )
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write(rows)
            state = ConcurrentTranslationState(
                root,
                delays={"PAPER001": 0.01, "PAPER002": 0.08},
            )
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(sources),
                server=FakeServer(root / "unused.pdf"),
                server_factory=state.server,
                attachments=FakeAttachments(),
            )
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                run_worker(
                    worker,
                    max_items=4,
                    paper_concurrency=2,
                    inter_item_delay=0.12,
                )
        first_gap = state.started["PAPER003"][0] - state.finished["PAPER001"][0]
        second_gap = state.started["PAPER004"][0] - state.finished["PAPER002"][0]
        self.assertGreaterEqual(first_gap, 0.09)
        self.assertGreaterEqual(second_gap, 0.09)
        self.assertLess(state.started["PAPER003"][0], state.started["PAPER004"][0])

    def test_transient_failure_waits_then_retries_same_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4 source")
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write([pending_row()])
            state = ConcurrentTranslationState(root, failures={"ABCD1234": 1})
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(source),
                server=FakeServer(root / "unused.pdf"),
                server_factory=state.server,
                attachments=FakeAttachments(),
            )
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                result = run_worker(
                    worker,
                    retry_delay=0.05,
                    transient_retries=1,
                )
            row = store.read()[0]
        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["retried"], 1)
        self.assertEqual(result["done"], 1)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["attempt_count"], "2")
        self.assertGreaterEqual(
            state.started["ABCD1234"][1] - state.started["ABCD1234"][0],
            0.04,
        )

    def test_transient_retry_exhaustion_marks_final_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4 source")
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write([pending_row()])
            state = ConcurrentTranslationState(root, failures={"ABCD1234": 2})
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(source),
                server=FakeServer(root / "unused.pdf"),
                server_factory=state.server,
                attachments=FakeAttachments(),
            )
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                result = run_worker(worker, transient_retries=1)
            row = store.read()[0]
        self.assertEqual(result["retried"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempt_count"], "2")
        self.assertEqual(row["next_attempt_at"], "")

    def test_other_slot_continues_while_one_paper_waits_to_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keys = ["PAPER001", "PAPER002", "PAPER003"]
            sources = {}
            rows = []
            for position, key in enumerate(keys, start=1):
                source = root / f"source-{position}.pdf"
                source.write_bytes(b"%PDF-1.4 source")
                sources[key] = source
                rows.append(
                    pending_row(
                        parent_key=key,
                        source_key=f"SOURCE0{position}",
                    )
                )
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write(rows)
            state = ConcurrentTranslationState(
                root,
                failures={"PAPER001": 1},
                delays={"PAPER002": 0.01},
            )
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(sources),
                server=FakeServer(root / "unused.pdf"),
                server_factory=state.server,
                attachments=FakeAttachments(),
            )
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                result = run_worker(
                    worker,
                    max_items=3,
                    paper_concurrency=2,
                    retry_delay=0.1,
                    transient_retries=1,
                )
        self.assertEqual(result["done"], 3)
        self.assertLess(state.started["PAPER003"][0], state.started["PAPER001"][1])

    def test_transient_error_classifier_covers_provider_reconnect_failures(self):
        for message in (
            "HTTP 429",
            "Concurrency limit exceeded for account",
            "stream disconnected before completion",
            "HTTP 503",
        ):
            with self.subTest(message=message):
                self.assertTrue(
                    zotero_translate.is_transient_translation_error(
                        zotero_translate.TranslationError(message)
                    )
                )
        self.assertFalse(
            zotero_translate.is_transient_translation_error(
                zotero_translate.TranslationError("invalid PDF")
            )
        )

    def test_restart_honors_persisted_retry_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4 source")
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write(
                [
                    pending_row(
                        status="retry_wait",
                        attempt_count=1,
                        next_attempt_at=zotero_translate.utc_timestamp(
                            time.time() + 0.08
                        ),
                    )
                ]
            )
            state = ConcurrentTranslationState(root)
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(source),
                server=FakeServer(root / "unused.pdf"),
                server_factory=state.server,
                attachments=FakeAttachments(),
            )
            started = time.monotonic()
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                run_worker(worker, transient_retries=1)
            elapsed = time.monotonic() - started
            row = store.read()[0]
        self.assertGreaterEqual(elapsed, 0.06)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["attempt_count"], "2")

    def test_restart_honors_persisted_slot_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"%PDF-1.4 source")
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write(
                [
                    pending_row(
                        status="waiting",
                        next_attempt_at=zotero_translate.utc_timestamp(
                            time.time() + 0.08
                        ),
                    )
                ]
            )
            state = ConcurrentTranslationState(root)
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(source),
                server=FakeServer(root / "unused.pdf"),
                server_factory=state.server,
                attachments=FakeAttachments(),
            )
            started = time.monotonic()
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                run_worker(worker)
            elapsed = time.monotonic() - started
            row = store.read()[0]
        self.assertGreaterEqual(elapsed, 0.06)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["attempt_count"], "1")

    def test_existing_local_translation_does_not_start_provider_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = {}
            for position, key in enumerate(("PAPER001", "PAPER002"), start=1):
                source = root / f"source-{position}.pdf"
                source.write_bytes(b"%PDF-1.4 source")
                sources[key] = source
            existing = root / "existing.pdf"
            existing.write_bytes(b"%PDF-1.4 translated")
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write(
                [
                    pending_row(
                        str(existing),
                        parent_key="PAPER001",
                        source_key="SOURCE01",
                    ),
                    pending_row(parent_key="PAPER002", source_key="SOURCE02"),
                ]
            )
            server = FakeServer(root / "second.pdf")
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(sources),
                server=server,
                attachments=FakeAttachments(),
            )
            clock = [1000.0]
            sleeps = []

            def advance(seconds):
                sleeps.append(seconds)
                clock[0] += seconds

            with (
                mock.patch.object(
                    zotero_translate, "load_pdf2zh_settings", return_value=settings()
                ),
                mock.patch.object(
                    zotero_translate.time, "time", side_effect=lambda: clock[0]
                ),
                mock.patch.object(zotero_translate.time, "sleep", side_effect=advance),
            ):
                result = run_worker(
                    worker,
                    max_items=2,
                    inter_item_delay=60,
                )
        self.assertEqual(result["done"], 2)
        self.assertEqual(server.translate_calls, 1)
        self.assertEqual(sleeps, [])

    def test_retries_do_not_consume_additional_max_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = {}
            for position, key in enumerate(("PAPER001", "PAPER002"), start=1):
                source = root / f"source-{position}.pdf"
                source.write_bytes(b"%PDF-1.4 source")
                sources[key] = source
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write(
                [
                    pending_row(parent_key="PAPER001", source_key="SOURCE01"),
                    pending_row(parent_key="PAPER002", source_key="SOURCE02"),
                ]
            )
            state = ConcurrentTranslationState(root, failures={"PAPER001": 1})
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(sources),
                server=FakeServer(root / "unused.pdf"),
                server_factory=state.server,
                attachments=FakeAttachments(),
            )
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                result = run_worker(worker, transient_retries=1)
            rows = store.read()
        self.assertEqual(result["selected"], 1)
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(rows[0]["attempt_count"], "2")
        self.assertEqual(rows[1]["status"], "pending")

    def test_zotero_imports_remain_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = {}
            rows = []
            for position, key in enumerate(("PAPER001", "PAPER002"), start=1):
                source = root / f"source-{position}.pdf"
                source.write_bytes(b"%PDF-1.4 source")
                sources[key] = source
                rows.append(
                    pending_row(
                        parent_key=key,
                        source_key=f"SOURCE0{position}",
                    )
                )
            store = zotero_translate.QueueStore(root / "queue.csv")
            store.write(rows)
            state = ConcurrentTranslationState(
                root, delays={key: 0.02 for key in sources}
            )
            attachments = SlowAttachments()
            worker = zotero_translate.TranslationWorker(
                store,
                zotero=FakeZotero(sources),
                server=FakeServer(root / "unused.pdf"),
                server_factory=state.server,
                attachments=attachments,
            )
            with mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ):
                run_worker(worker, max_items=2, paper_concurrency=2)
        self.assertEqual(len(attachments.imports), 2)
        self.assertEqual(attachments.max_active, 1)


class FakeWebDAVSession:
    def __init__(
        self,
        prop_status: int = 201,
        probe_status: int = 201,
        probe_delete_status: int = 204,
    ) -> None:
        self.auth = None
        self.prop_status = prop_status
        self.probe_status = probe_status
        self.probe_delete_status = probe_delete_status
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, b""))
        return FakeResponse(207)

    def put(self, url, data, **kwargs):
        payload = data.read() if hasattr(data, "read") else bytes(data)
        self.calls.append(("PUT", url, payload))
        if "zotero-mcp-write-test-" in url:
            return FakeResponse(self.probe_status)
        return FakeResponse(self.prop_status if url.endswith(".prop") else 201)

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, b""))
        if "zotero-mcp-write-test-" in url:
            return FakeResponse(self.probe_delete_status)
        return FakeResponse(204)


class FakeWebAPI:
    USER_ID = 123
    PARENT_KEY = "ABCD1234"
    ATTACHMENT_KEY = "CNAT1234"

    def __init__(self, children=None) -> None:
        self.children = list(children or [])
        self.payload = None
        self.deleted = []
        self.attachment_exists = False

    def web_api_status(self):
        return {"user_id": self.USER_ID, "files_write": True}

    def web_api_get_item(self, user_id, key):
        if key == self.PARENT_KEY:
            return {
                "key": key,
                "version": 1,
                "data": {"key": key, "itemType": "journalArticle"},
            }
        if key == self.ATTACHMENT_KEY and self.attachment_exists:
            return {
                "key": key,
                "version": 2,
                "data": {"key": key, **self.payload},
            }
        raise RuntimeError(f"missing item {key}")

    def web_api_request_json(self, method, path, **kwargs):
        if method == "GET" and path.endswith("/children"):
            return list(self.children)
        self.payload = dict(kwargs["payload"][0])
        self.attachment_exists = True
        return {
            "success": {"0": self.ATTACHMENT_KEY},
            "successVersions": {"0": 2},
        }

    def web_api_request(self, method, path, **kwargs):
        if method == "DELETE" and path.endswith(f"/{self.ATTACHMENT_KEY}"):
            self.deleted.append(self.ATTACHMENT_KEY)
            self.attachment_exists = False
        return FakeResponse(204)


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.pdf = Path(self.temp_dir.name) / "translated.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 translated")
        self.environ = {
            "ZOTERO_WEBDAV_URL": "https://dav.example/zotero",
            "ZOTERO_WEBDAV_USERNAME": "alice",
            "ZOTERO_WEBDAV_PASSWORD": "secret",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_client(self, *, api=None, session=None, environ=None, naming=None):
        return zotero_translate.ZoteroAttachmentClient(
            session=session or FakeWebDAVSession(),
            api=api or FakeWebAPI(),
            environ=self.environ if environ is None else environ,
            naming=naming,
        )

    def test_preflight_checks_web_api_and_webdav(self):
        session = FakeWebDAVSession()
        client = self.make_client(session=session)
        self.assertEqual(client.preflight(), FakeWebAPI.USER_ID)
        self.assertEqual(session.auth, ("alice", "secret"))
        self.assertEqual(session.calls[0][0], "PROPFIND")
        self.assertEqual(len(session.calls), 1)

    def test_preflight_write_probe_is_created_and_deleted(self):
        session = FakeWebDAVSession()
        client = self.make_client(session=session)
        self.assertEqual(
            client.preflight(verify_write=True),
            FakeWebAPI.USER_ID,
        )
        self.assertEqual(
            [call[0] for call in session.calls], ["PROPFIND", "PUT", "DELETE"]
        )
        self.assertEqual(session.calls[1][1], session.calls[2][1])

    def test_preflight_rejects_webdav_without_write_access(self):
        session = FakeWebDAVSession(probe_status=403, probe_delete_status=404)
        with self.assertRaisesRegex(
            zotero_translate.TranslationError, "write check failed with HTTP 403"
        ):
            self.make_client(session=session).preflight(verify_write=True)
        self.assertEqual(
            [call[0] for call in session.calls], ["PROPFIND", "PUT", "DELETE"]
        )

    def test_import_creates_cn_attachment_then_uploads_zip_and_prop(self):
        api = FakeWebAPI()
        session = FakeWebDAVSession()
        result = self.make_client(api=api, session=session).import_pdf(
            api.USER_ID, api.PARENT_KEY, self.pdf
        )
        expected_md5 = hashlib.md5(self.pdf.read_bytes()).hexdigest()
        self.assertEqual(api.payload["title"], "CN")
        self.assertEqual(result["attachment_key"], "CNAT1234")
        puts = [call for call in session.calls if call[0] == "PUT"]
        self.assertEqual([call[1].rsplit(".", 1)[-1] for call in puts], ["zip", "prop"])
        with zipfile.ZipFile(io.BytesIO(puts[0][2])) as archive:
            self.assertEqual(archive.namelist(), [self.pdf.name])
            self.assertEqual(archive.read(self.pdf.name), self.pdf.read_bytes())
        self.assertIn(f"<hash>{expected_md5}</hash>", puts[1][2].decode("utf-8"))

    def test_existing_cn_attachment_is_preserved(self):
        existing = {
            "key": "OLDAT123",
            "data": {
                "key": "OLDAT123",
                "itemType": "attachment",
                "title": "CN",
                "contentType": "application/pdf",
                "filename": "old.pdf",
            },
        }
        api = FakeWebAPI(children=[existing])
        session = FakeWebDAVSession()
        result = self.make_client(api=api, session=session).import_pdf(
            api.USER_ID, api.PARENT_KEY, self.pdf
        )
        self.assertTrue(result["already_present"])
        self.assertIsNone(api.payload)
        self.assertEqual(session.calls, [])

    def test_custom_naming_still_preserves_legacy_cn_attachment(self):
        existing = {
            "key": "OLDAT123",
            "data": {
                "key": "OLDAT123",
                "itemType": "attachment",
                "title": "CN",
                "contentType": "application/pdf",
                "filename": "old.pdf",
            },
        }
        api = FakeWebAPI(children=[existing])
        result = self.make_client(
            api=api,
            naming=zotero_translate.TranslationNaming(
                "Chinese", "{source_stem} (Chinese).pdf"
            ),
        ).import_pdf(api.USER_ID, api.PARENT_KEY, self.pdf)
        self.assertTrue(result["already_present"])
        self.assertIsNone(api.payload)

    def test_multiple_recognized_translation_attachments_block_import(self):
        children = [
            {
                "key": "OLDAT123",
                "data": {
                    "key": "OLDAT123",
                    "itemType": "attachment",
                    "title": "CN",
                    "contentType": "application/pdf",
                    "filename": "old.pdf",
                },
            },
            {
                "key": "NEWAT123",
                "data": {
                    "key": "NEWAT123",
                    "itemType": "attachment",
                    "title": "Chinese",
                    "contentType": "application/pdf",
                    "filename": "new.pdf",
                },
            },
        ]
        api = FakeWebAPI(children=children)
        with self.assertRaisesRegex(
            zotero_translate.TranslationError,
            "multiple existing translation attachments",
        ):
            self.make_client(
                api=api,
                naming=zotero_translate.TranslationNaming(
                    "Chinese", "{source_stem} (Chinese).pdf"
                ),
            ).import_pdf(api.USER_ID, api.PARENT_KEY, self.pdf)
        self.assertIsNone(api.payload)

    def test_import_uses_custom_attachment_title(self):
        api = FakeWebAPI()
        result = self.make_client(
            api=api,
            naming=zotero_translate.TranslationNaming(
                "Chinese", "{source_stem} (Chinese).pdf"
            ),
        ).import_pdf(api.USER_ID, api.PARENT_KEY, self.pdf)
        self.assertEqual(api.payload["title"], "Chinese")
        self.assertEqual(result["title"], "Chinese")

    def test_matching_cn_attachment_refreshes_webdav_after_interruption(self):
        md5_hex = hashlib.md5(self.pdf.read_bytes()).hexdigest()
        mtime_ms = int(self.pdf.stat().st_mtime * 1000)
        existing = {
            "key": "OLDAT123",
            "data": {
                "key": "OLDAT123",
                "itemType": "attachment",
                "title": "CN",
                "parentItem": FakeWebAPI.PARENT_KEY,
                "linkMode": "imported_file",
                "contentType": "application/pdf",
                "filename": self.pdf.name,
                "md5": md5_hex,
                "mtime": mtime_ms,
            },
        }
        api = FakeWebAPI(children=[existing])
        session = FakeWebDAVSession()
        result = self.make_client(api=api, session=session).import_pdf(
            api.USER_ID, api.PARENT_KEY, self.pdf
        )
        self.assertTrue(result["webdav_refreshed"])
        self.assertEqual(
            [call[1].rsplit(".", 1)[-1] for call in session.calls], ["zip", "prop"]
        )

    def test_prop_failure_rolls_back_webdav_and_attachment(self):
        api = FakeWebAPI()
        session = FakeWebDAVSession(prop_status=500)
        with self.assertRaisesRegex(zotero_translate.TranslationError, "HTTP 500"):
            self.make_client(api=api, session=session).import_pdf(
                api.USER_ID, api.PARENT_KEY, self.pdf
            )
        self.assertEqual(api.deleted, [api.ATTACHMENT_KEY])
        deletes = [call[1] for call in session.calls if call[0] == "DELETE"]
        self.assertEqual([url.rsplit(".", 1)[-1] for url in deletes], ["prop", "zip"])


class DoctorTests(unittest.TestCase):
    def test_doctor_verifies_web_api_and_webdav_without_exposing_api_key(self):
        server = mock.Mock()
        server.health.return_value = "http://localhost:8890"
        attachments = mock.Mock()
        naming = zotero_translate.TranslationNaming(
            "Chinese", "{source_stem} (Chinese).pdf"
        )
        attachments.configuration_status.return_value = {
            "configured": True,
            "source": "private.json",
            "timeout": 30,
        }
        with (
            mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ),
            mock.patch.object(
                zotero_translate, "load_translation_naming", return_value=naming
            ),
            mock.patch.object(zotero_translate, "PDF2ZHClient", return_value=server),
            mock.patch.object(
                zotero_translate, "ZoteroAttachmentClient", return_value=attachments
            ),
            mock.patch.object(
                zotero_translate.zotero_local,
                "ping_status",
                return_value={"ok": True, "sample_item": {"secret": "data"}},
            ),
        ):
            result = zotero_translate.doctor()
        attachments.preflight.assert_called_once_with(verify_write=False)
        self.assertTrue(result["webdav"]["reachable"])
        self.assertFalse(result["webdav"]["write_verified"])
        self.assertEqual(
            result["naming"],
            {
                "attachment_title": "Chinese",
                "filename_template": "{source_stem} (Chinese).pdf",
            },
        )
        rendered = json.dumps(result)
        self.assertNotIn("secret-key", rendered)
        self.assertNotIn("sample_item", rendered)


class SchedulingTests(unittest.TestCase):
    def test_posix_rename_watch_installs_user_service(self):
        completed = mock.Mock(returncode=0, stdout="ok", stderr="")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                zotero_translate.zotero_runtime, "is_windows", return_value=False
            ),
            mock.patch.object(
                zotero_translate.shutil, "which", return_value="/usr/bin/systemctl"
            ),
            mock.patch.object(
                zotero_translate.subprocess, "run", return_value=completed
            ) as run,
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}, clear=True),
        ):
            result = zotero_translate.configure_rename_watch_service("install")
            unit = Path(result["unit"]).read_text(encoding="utf-8")
        self.assertIn("rename-watch", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertEqual(run.call_count, 2)
        self.assertIn("enable", run.call_args_list[1].args[0])

    def test_windows_rename_watch_installs_and_runs_logon_task(self):
        completed = mock.Mock(returncode=0, stdout="ok", stderr="")
        with (
            mock.patch.object(
                zotero_translate.zotero_runtime, "is_windows", return_value=True
            ),
            mock.patch.object(
                zotero_translate.shutil,
                "which",
                return_value=r"C:\\Windows\\System32\\schtasks.exe",
            ),
            mock.patch.object(
                zotero_translate.subprocess, "run", return_value=completed
            ) as run,
        ):
            result = zotero_translate.configure_rename_watch_service("install")
        self.assertEqual(result["scheduler"], "windows-task-scheduler")
        self.assertEqual(run.call_count, 2)
        self.assertIn("ONLOGON", run.call_args_list[0].args[0])
        self.assertIn("/Run", run.call_args_list[1].args[0])

    def test_posix_schedule_uses_one_shot_systemd_timer(self):
        run_at = datetime.now(UTC).astimezone().replace(tzinfo=None) + timedelta(
            hours=1
        )
        completed = mock.Mock(returncode=0, stdout="ok", stderr="")
        with (
            mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ),
            mock.patch.object(
                zotero_translate.zotero_runtime, "is_windows", return_value=False
            ),
            mock.patch.object(
                zotero_translate.shutil, "which", return_value="/usr/bin/systemd-run"
            ),
            mock.patch.object(
                zotero_translate.subprocess, "run", return_value=completed
            ) as run,
        ):
            result = zotero_translate.schedule_translation(
                run_at,
                Path("queue.csv"),
                qps=7,
                pool_size=13,
                paper_concurrency=3,
                inter_item_delay=420,
                retry_delay=300,
                transient_retries=1,
                max_items=2,
            )
        command = run.call_args.args[0]
        self.assertIn("--user", command)
        self.assertIn("--collect", command)
        self.assertTrue(any(value.startswith("--on-calendar=") for value in command))
        self.assertIn("--prefs", command)
        self.assertIn("--paper-concurrency", command)
        self.assertIn("--inter-item-delay", command)
        self.assertIn("--retry-delay", command)
        self.assertIn("--transient-retries", command)
        self.assertEqual(result["scheduler"], "systemd-user-timer")

    def test_windows_schedule_uses_delete_after_run_task(self):
        run_at = datetime.now(UTC).astimezone().replace(tzinfo=None) + timedelta(
            hours=1
        )
        completed = mock.Mock(returncode=0, stdout="ok", stderr="")
        with (
            mock.patch.object(
                zotero_translate, "load_pdf2zh_settings", return_value=settings()
            ),
            mock.patch.object(
                zotero_translate.zotero_runtime, "is_windows", return_value=True
            ),
            mock.patch.object(
                zotero_translate.shutil, "which", return_value="schtasks.exe"
            ),
            mock.patch.object(
                zotero_translate.subprocess, "run", return_value=completed
            ) as run,
        ):
            result = zotero_translate.schedule_translation(
                run_at,
                Path("queue.csv"),
                qps=7,
                pool_size=13,
                paper_concurrency=3,
                inter_item_delay=420,
                retry_delay=300,
                transient_retries=1,
                max_items=2,
            )
        command = run.call_args.args[0]
        self.assertIn("/Z", command)
        self.assertIn("ONCE", command)
        self.assertEqual(result["scheduler"], "windows-task-scheduler")


if __name__ == "__main__":
    unittest.main()
