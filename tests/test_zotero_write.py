import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from zotero_mcp import zotero_web_api, zotero_write

COLLECTIONS = {"LHRDK94L", "MKRHPXLP", "PTB3MGA6"}


def item(key, title, *, doi="", extra="", collections=None):
    return {
        "key": key,
        "data": {
            "key": key,
            "itemType": "journalArticle",
            "title": title,
            "DOI": doi,
            "extra": extra,
            "collections": list(collections or []),
        },
    }


def collection(key, name, parent=""):
    return {
        "key": key,
        "data": {
            "key": key,
            "name": name,
            "parentCollection": parent,
        },
    }


class IdentifierTests(unittest.TestCase):
    def test_identifier_normalization(self):
        self.assertEqual(
            zotero_write.normalize_doi("https://doi.org/10.1038/ABC.1"),
            "10.1038/abc.1",
        )
        self.assertEqual(
            zotero_write.normalize_doi("(https://doi.org/10.1038/ABC.1)"),
            "10.1038/abc.1",
        )
        self.assertEqual(
            zotero_write.normalize_doi("https://doi.org/10.1038/ABC.1)"),
            "10.1038/abc.1",
        )
        self.assertEqual(zotero_write.normalize_pmid("PMID: 12345"), "12345")
        self.assertEqual(zotero_write.normalize_pmcid("12345"), "PMC12345")
        self.assertEqual(
            zotero_write.normalize_arxiv("arXiv:2501.01234v2"), "2501.01234"
        )

    def test_item_identifiers_reads_extra_and_urls(self):
        value = item(
            "ABCDEFGH",
            "Paper",
            extra="PMID: 12345\nPMCID: PMC9876",
        )
        value["data"]["url"] = "https://arxiv.org/abs/2501.01234v3"
        self.assertEqual(
            zotero_write.item_identifiers(value),
            {"PMID": "12345", "PMCID": "PMC9876", "arXiv": "2501.01234"},
        )

    @unittest.skipIf(os.name == "nt", "Windows uses ACLs instead of POSIX mode bits")
    def test_web_api_key_file_must_be_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "key"
            key_file.write_text("secret\n", encoding="utf-8")
            key_file.chmod(0o644)
            with (
                mock.patch.dict(
                    os.environ,
                    {"ZOTERO_API_KEY_FILE": str(key_file)},
                    clear=True,
                ),
                self.assertRaisesRegex(zotero_write.ZoteroWriteError, "permission 600"),
            ):
                zotero_write.web_api_key()

    def test_web_api_key_uses_secret_filename_by_default(self):
        key_file = Path("/tmp/zotero_web_api_key.secret")
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                zotero_web_api.zotero_runtime,
                "configured_path",
                return_value=None,
            ),
            mock.patch.object(
                zotero_web_api.zotero_runtime,
                "default_secret_path",
                return_value=key_file,
            ) as default_path,
            mock.patch.object(Path, "is_file", return_value=False),
            self.assertRaisesRegex(
                zotero_write.ZoteroWriteError,
                "zotero_web_api_key\\.secret",
            ),
        ):
            zotero_write.web_api_key()
        default_path.assert_called_once_with("zotero_web_api_key.secret")


class FakeResponse:
    def __init__(self, status_code, data=None, text="", headers=None):
        self.status_code = status_code
        self._data = data
        self.text = text
        self.headers = dict(headers or {})

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._data is None:
            raise ValueError("no JSON")
        return self._data


class WebApiTransportTests(unittest.TestCase):
    KEY_STATUS: ClassVar[dict] = {
        "userID": 12345678,
        "access": {
            "user": {"library": True, "files": True},
            "groups": {"all": {"library": True, "write": False}},
        },
    }

    def test_status_validates_user_and_personal_library_write_access(self):
        with (
            mock.patch.object(zotero_web_api, "web_api_key", return_value="secret"),
            mock.patch.object(
                zotero_web_api,
                "web_api_request_json",
                return_value=self.KEY_STATUS,
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            status = zotero_web_api.web_api_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["transport"], "zotero_web_api")
        self.assertEqual(status["user_id"], 12345678)
        self.assertTrue(status["personal_library_write"])
        self.assertNotIn("api_key", status)

        denied = {
            "userID": 12345678,
            "access": {"user": {"library": False, "files": False}},
        }
        with (
            mock.patch.object(zotero_web_api, "web_api_key", return_value="secret"),
            mock.patch.object(
                zotero_web_api,
                "web_api_request_json",
                return_value=denied,
            ),
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(zotero_web_api.ZoteroWriteError, "write access"),
        ):
            zotero_web_api.web_api_status()

        with (
            mock.patch.object(zotero_web_api, "web_api_key", return_value="secret"),
            mock.patch.object(
                zotero_web_api,
                "web_api_request_json",
                return_value={**self.KEY_STATUS, "userID": 1},
            ),
            mock.patch.dict(os.environ, {"ZOTERO_USER_ID": "12345678"}),
            self.assertRaisesRegex(
                zotero_web_api.ZoteroWriteError, "expected 12345678"
            ),
        ):
            zotero_web_api.web_api_status()

    def test_network_error_marks_writes_unknown_but_reads_failed(self):
        with (
            mock.patch.object(zotero_web_api, "web_api_key", return_value="secret"),
            mock.patch.object(
                zotero_web_api.requests,
                "request",
                side_effect=zotero_web_api.requests.Timeout("timed out"),
            ),
            self.assertRaisesRegex(
                zotero_web_api.ZoteroWriteError, "unknown write state"
            ),
        ):
            zotero_web_api.web_api_request("POST", "users/1/items", payload=[])

        with (
            mock.patch.object(zotero_web_api, "web_api_key", return_value="secret"),
            mock.patch.object(
                zotero_web_api.requests,
                "request",
                side_effect=zotero_web_api.requests.Timeout("timed out"),
            ),
            self.assertRaisesRegex(zotero_web_api.ZoteroWriteError, "read failed"),
        ):
            zotero_web_api.web_api_request("GET", "users/1/items")

    def test_read_retries_once_after_retry_after(self):
        responses = [
            FakeResponse(429, headers={"Retry-After": "2"}),
            FakeResponse(200),
        ]
        with (
            mock.patch.object(zotero_web_api, "web_api_key", return_value="secret"),
            mock.patch.object(
                zotero_web_api.requests,
                "request",
                side_effect=responses,
            ) as request,
            mock.patch.object(zotero_web_api.time, "monotonic", return_value=100.0),
            mock.patch.object(zotero_web_api.time, "sleep") as sleep,
            mock.patch.object(zotero_web_api, "_backoff_until", 0.0),
        ):
            response = zotero_web_api.web_api_request("GET", "users/1/items")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_write_is_not_retried_after_rate_limit(self):
        with (
            mock.patch.object(zotero_web_api, "web_api_key", return_value="secret"),
            mock.patch.object(
                zotero_web_api.requests,
                "request",
                return_value=FakeResponse(429, headers={"Retry-After": "3"}),
            ) as request,
            mock.patch.object(zotero_web_api.time, "monotonic", return_value=100.0),
            mock.patch.object(zotero_web_api, "_backoff_until", 0.0),
        ):
            response = zotero_web_api.web_api_request(
                "POST", "users/1/items", payload=[]
            )
        self.assertEqual(response.status_code, 429)
        request.assert_called_once()

    def test_backoff_header_delays_the_next_request(self):
        responses = [
            FakeResponse(200, headers={"Backoff": "4"}),
            FakeResponse(200),
        ]
        with (
            mock.patch.object(zotero_web_api, "web_api_key", return_value="secret"),
            mock.patch.object(
                zotero_web_api.requests,
                "request",
                side_effect=responses,
            ),
            mock.patch.object(zotero_web_api.time, "monotonic", return_value=100.0),
            mock.patch.object(zotero_web_api.time, "sleep") as sleep,
            mock.patch.object(zotero_web_api, "_backoff_until", 0.0),
        ):
            zotero_web_api.web_api_request("GET", "users/1/items")
            zotero_web_api.web_api_request("GET", "users/1/items")
        sleep.assert_called_once_with(4.0)

    def test_exact_identifier_search_fails_closed(self):
        with (
            mock.patch.object(
                zotero_write,
                "web_api_search_items",
                side_effect=zotero_write.ZoteroWriteError("API unavailable"),
            ),
            self.assertRaisesRegex(zotero_write.ZoteroWriteError, "API unavailable"),
        ):
            zotero_write.web_api_find_exact_items(12345678, {"DOI": "10.1000/test"})

    def test_collection_union_preserves_existing_memberships(self):
        before = item("ABCDEFGH", "Paper", collections=["LHRDK94L"])
        before["version"] = 4
        after = item("ABCDEFGH", "Paper", collections=["LHRDK94L", "MKRHPXLP"])
        after["version"] = 5
        with (
            mock.patch.object(
                zotero_write, "web_api_get_item", side_effect=[before, after]
            ),
            mock.patch.object(
                zotero_write,
                "web_api_request",
                return_value=FakeResponse(204),
            ) as request,
        ):
            result = zotero_write.web_api_add_to_collections(
                12345678, "ABCDEFGH", ["MKRHPXLP"]
            )
        self.assertEqual(result["status"], "collections_added")
        self.assertEqual(result["collections"], ["LHRDK94L", "MKRHPXLP"])
        self.assertEqual(
            request.call_args.kwargs["payload"]["collections"],
            ["LHRDK94L", "MKRHPXLP"],
        )
        self.assertEqual(
            request.call_args.kwargs["headers"]["If-Unmodified-Since-Version"],
            "4",
        )

    def test_collection_union_retries_version_conflict_and_recomputes(self):
        first = item("ABCDEFGH", "Paper", collections=["LHRDK94L"])
        first["version"] = 4
        concurrent = item("ABCDEFGH", "Paper", collections=["LHRDK94L", "PTB3MGA6"])
        concurrent["version"] = 5
        verified = item(
            "ABCDEFGH",
            "Paper",
            collections=["LHRDK94L", "PTB3MGA6", "MKRHPXLP"],
        )
        verified["version"] = 6
        with (
            mock.patch.object(
                zotero_write,
                "web_api_get_item",
                side_effect=[first, concurrent, verified],
            ),
            mock.patch.object(
                zotero_write,
                "web_api_request",
                side_effect=[FakeResponse(412), FakeResponse(204)],
            ) as request,
        ):
            result = zotero_write.web_api_add_to_collections(
                12345678, "ABCDEFGH", ["MKRHPXLP"]
            )
        self.assertEqual(result["collections"], ["LHRDK94L", "PTB3MGA6", "MKRHPXLP"])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[1].kwargs["payload"]["collections"],
            ["LHRDK94L", "PTB3MGA6", "MKRHPXLP"],
        )

    def test_collection_reconcile_only_patches_memberships(self):
        before = item(
            "SUZCN22Y",
            "BAGE",
            collections=["LHRDK94L", "MKRHPXLP"],
        )
        before["version"] = 4
        after = item(
            "SUZCN22Y",
            "BAGE",
            collections=["MKRHPXLP", "PTB3MGA6"],
        )
        after["version"] = 5
        with (
            mock.patch.object(
                zotero_write, "web_api_get_item", side_effect=[before, after]
            ),
            mock.patch.object(
                zotero_write,
                "web_api_request",
                return_value=FakeResponse(204),
            ) as request,
        ):
            result = zotero_write.web_api_reconcile_collections(
                12345678,
                "SUZCN22Y",
                add_collection_keys=["PTB3MGA6"],
                remove_collection_keys=["LHRDK94L"],
            )

        self.assertEqual(result["status"], "collections_reconciled")
        self.assertEqual(result["added"], ["PTB3MGA6"])
        self.assertEqual(result["removed"], ["LHRDK94L"])
        self.assertEqual(result["collections"], ["MKRHPXLP", "PTB3MGA6"])
        self.assertEqual(
            request.call_args.args[:2], ("PATCH", "users/12345678/items/SUZCN22Y")
        )
        self.assertEqual(
            request.call_args.kwargs["payload"],
            {"collections": ["MKRHPXLP", "PTB3MGA6"]},
        )

    def test_collection_reconcile_recomputes_after_version_conflict(self):
        first = item("SUZCN22Y", "BAGE", collections=["LHRDK94L"])
        first["version"] = 4
        concurrent = item(
            "SUZCN22Y",
            "BAGE",
            collections=["LHRDK94L", "MKRHPXLP"],
        )
        concurrent["version"] = 5
        verified = item(
            "SUZCN22Y",
            "BAGE",
            collections=["MKRHPXLP", "PTB3MGA6"],
        )
        verified["version"] = 6
        with (
            mock.patch.object(
                zotero_write,
                "web_api_get_item",
                side_effect=[first, concurrent, verified],
            ),
            mock.patch.object(
                zotero_write,
                "web_api_request",
                side_effect=[FakeResponse(412), FakeResponse(204)],
            ) as request,
        ):
            result = zotero_write.web_api_reconcile_collections(
                12345678,
                "SUZCN22Y",
                add_collection_keys=["PTB3MGA6"],
                remove_collection_keys=["LHRDK94L"],
            )

        self.assertEqual(result["collections"], ["MKRHPXLP", "PTB3MGA6"])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[1].kwargs["payload"],
            {"collections": ["MKRHPXLP", "PTB3MGA6"]},
        )

    def test_collection_reconcile_refuses_to_remove_last_membership(self):
        before = item("ABCDEFGH", "Paper", collections=["LHRDK94L"])
        before["version"] = 4
        with (
            mock.patch.object(zotero_write, "web_api_get_item", return_value=before),
            mock.patch.object(zotero_write, "web_api_request") as request,
            self.assertRaisesRegex(zotero_write.ZoteroWriteError, "no collections"),
        ):
            zotero_write.web_api_reconcile_collections(
                12345678,
                "ABCDEFGH",
                add_collection_keys=[],
                remove_collection_keys=["LHRDK94L"],
            )
        request.assert_not_called()

    def test_collection_reconcile_stops_when_local_and_cloud_memberships_differ(self):
        cloud = item("ABCDEFGH", "Paper", collections=["LHRDK94L"])
        cloud["version"] = 4
        with (
            mock.patch.object(zotero_write, "web_api_get_item", return_value=cloud),
            mock.patch.object(zotero_write, "web_api_request") as request,
            self.assertRaisesRegex(
                zotero_write.ZoteroNotSyncedError,
                "local/cloud collection memberships differ",
            ),
        ):
            zotero_write.web_api_reconcile_collections(
                12345678,
                "ABCDEFGH",
                add_collection_keys=["PTB3MGA6"],
                remove_collection_keys=[],
                expected_current_collections=["MKRHPXLP"],
            )
        request.assert_not_called()

    def test_collection_reconcile_allows_no_membership_only_when_explicit(self):
        before = item("ABCDEFGH", "Paper", collections=["LHRDK94L"])
        before["version"] = 4
        after = item("ABCDEFGH", "Paper", collections=[])
        after["version"] = 5
        with (
            mock.patch.object(
                zotero_write, "web_api_get_item", side_effect=[before, after]
            ),
            mock.patch.object(
                zotero_write,
                "web_api_request",
                return_value=FakeResponse(204),
            ) as request,
        ):
            result = zotero_write.web_api_reconcile_collections(
                12345678,
                "ABCDEFGH",
                add_collection_keys=[],
                remove_collection_keys=["LHRDK94L"],
                allow_no_collections=True,
            )
        self.assertEqual(result["collections"], [])
        self.assertEqual(request.call_args.kwargs["payload"], {"collections": []})

    def test_create_payload_has_no_attachment_fields_and_is_verified(self):
        resolved = {
            "key": "FORBID02",
            "version": 99,
            "itemType": "journalArticle",
            "title": "Paper",
            "DOI": "10.1000/test",
            "attachments": [{"title": "forbidden"}],
            "parentItem": "FORBID01",
        }
        verified = item(
            "ABCDEFGH", "Paper", doi="10.1000/test", collections=["LHRDK94L"]
        )
        with (
            mock.patch.object(
                zotero_write, "web_api_find_exact_items", return_value=[]
            ),
            mock.patch.object(
                zotero_write, "resolve_identifier_metadata", return_value=resolved
            ),
            mock.patch.object(
                zotero_write,
                "web_api_request_json",
                return_value={"success": {"0": "ABCDEFGH"}, "failed": {}},
            ) as request,
            mock.patch.object(zotero_write, "web_api_get_item", return_value=verified),
        ):
            result = zotero_write.web_api_import_identifier(
                12345678, {"DOI": "10.1000/test"}, ["LHRDK94L"]
            )
        payload = request.call_args.kwargs["payload"][0]
        self.assertNotIn("attachments", payload)
        self.assertNotIn("parentItem", payload)
        self.assertNotIn("key", payload)
        self.assertNotIn("version", payload)
        self.assertEqual(payload["collections"], ["LHRDK94L"])
        self.assertTrue(result["created"])
        self.assertEqual(result["item_key"], "ABCDEFGH")

    def test_cloud_preflight_reuses_existing_item(self):
        existing = item("ABCDEFGH", "Paper", doi="10.1000/test", collections=[])
        with (
            mock.patch.object(
                zotero_write,
                "web_api_find_exact_items",
                return_value=[existing],
            ),
            mock.patch.object(
                zotero_write,
                "web_api_add_to_collections",
                return_value={
                    "status": "collections_added",
                    "item_key": "ABCDEFGH",
                    "added": ["LHRDK94L"],
                    "collections": ["LHRDK94L"],
                },
            ) as add,
            mock.patch.object(zotero_write, "resolve_identifier_metadata") as resolve,
        ):
            result = zotero_write.web_api_import_identifier(
                12345678, {"DOI": "10.1000/test"}, ["LHRDK94L"]
            )
        self.assertFalse(result["created"])
        self.assertTrue(result["cloud_preflight_match"])
        add.assert_called_once_with(12345678, "ABCDEFGH", ["LHRDK94L"])
        resolve.assert_not_called()

    def test_cloud_preflight_rejects_multiple_exact_matches(self):
        matches = [
            item("ABCDEFGH", "First", doi="10.1000/test"),
            item("JKLMNPQR", "Second", doi="10.1000/test"),
        ]
        with (
            mock.patch.object(
                zotero_write, "web_api_find_exact_items", return_value=matches
            ),
            mock.patch.object(zotero_write, "resolve_identifier_metadata") as resolve,
            self.assertRaisesRegex(
                zotero_write.ZoteroCloudConflictError, "multiple Zotero cloud items"
            ),
        ):
            zotero_write.web_api_import_identifier(
                12345678, {"DOI": "10.1000/test"}, ["LHRDK94L"]
            )
        resolve.assert_not_called()

    def test_create_readback_requires_requested_collections(self):
        resolved = {
            "itemType": "journalArticle",
            "title": "Paper",
            "DOI": "10.1000/test",
        }
        verified = item("ABCDEFGH", "Paper", doi="10.1000/test", collections=[])
        with (
            mock.patch.object(
                zotero_write, "web_api_find_exact_items", return_value=[]
            ),
            mock.patch.object(
                zotero_write, "resolve_identifier_metadata", return_value=resolved
            ),
            mock.patch.object(
                zotero_write,
                "web_api_request_json",
                return_value={"success": {"0": "ABCDEFGH"}, "failed": {}},
            ),
            mock.patch.object(zotero_write, "web_api_get_item", return_value=verified),
            self.assertRaisesRegex(
                zotero_write.ZoteroWriteError, "verification failed"
            ),
        ):
            zotero_write.web_api_import_identifier(
                12345678, {"DOI": "10.1000/test"}, ["LHRDK94L"]
            )


class PlanningTests(unittest.TestCase):
    def test_exact_doi_match_only_adds_missing_collections(self):
        existing = item(
            "ABCDEFGH",
            "Existing title",
            doi="10.1000/test",
            collections=["LHRDK94L"],
        )
        plan = zotero_write.plan_paper_import(
            [
                {
                    "source_id": "row-1",
                    "title": "Existing title",
                    "doi": "https://doi.org/10.1000/TEST",
                    "collection_keys": ["LHRDK94L", "MKRHPXLP"],
                }
            ],
            items=[existing],
            collection_keys=COLLECTIONS,
        )
        result = plan["results"][0]
        self.assertEqual(result["status"], "add_collections")
        self.assertEqual(result["matched_item_key"], "ABCDEFGH")
        self.assertEqual(result["missing_collection_keys"], ["MKRHPXLP"])

    def test_pmcid_is_exact_dedup_key_but_not_creation_key(self):
        existing = item("ABCDEFGH", "Paper", extra="PMCID: PMC12345")
        matched = zotero_write.plan_paper_import(
            [
                {
                    "title": "Paper changed title",
                    "pmcid": "PMC12345",
                    "collection_keys": ["LHRDK94L"],
                }
            ],
            items=[existing],
            collection_keys=COLLECTIONS,
        )["results"][0]
        self.assertEqual(matched["status"], "add_collections")
        self.assertEqual(matched["matched_by"], ["PMCID"])

        manual = zotero_write.plan_paper_import(
            [
                {
                    "title": "New paper",
                    "pmcid": "PMC54321",
                    "collection_keys": ["LHRDK94L"],
                }
            ],
            items=[],
            collection_keys=COLLECTIONS,
        )["results"][0]
        self.assertEqual(manual["status"], "manual")

    def test_title_match_is_ambiguous_even_with_new_identifier(self):
        existing = item("ABCDEFGH", "Same: Paper Title")
        result = zotero_write.plan_paper_import(
            [
                {
                    "title": "Same Paper Title",
                    "doi": "10.1000/new",
                    "collection_keys": ["LHRDK94L"],
                }
            ],
            items=[existing],
            collection_keys=COLLECTIONS,
        )["results"][0]
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["candidate_item_keys"], ["ABCDEFGH"])

    def test_conflicting_exact_identifiers_are_ambiguous(self):
        items = [
            item("ABCDEFGH", "First", doi="10.1000/first"),
            item("JKLMNPQR", "Second", extra="PMID: 222"),
        ]
        result = zotero_write.plan_paper_import(
            [
                {
                    "title": "Conflict",
                    "doi": "10.1000/first",
                    "pmid": "222",
                    "collection_keys": ["LHRDK94L"],
                }
            ],
            items=items,
            collection_keys=COLLECTIONS,
        )["results"][0]
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["candidate_item_keys"], ["ABCDEFGH", "JKLMNPQR"])

    def test_new_doi_is_ready_for_creation(self):
        result = zotero_write.plan_paper_import(
            [
                {
                    "title": "New paper",
                    "doi": "10.1000/new",
                    "collection_keys": ["LHRDK94L"],
                }
            ],
            items=[],
            collection_keys=COLLECTIONS,
        )["results"][0]
        self.assertEqual(result["status"], "create")
        self.assertEqual(result["import_identifier"], {"DOI": "10.1000/new"})


class CollectionReconcilePlanningTests(unittest.TestCase):
    def test_plan_remove_parent_preserves_existing_child_and_outside_membership(self):
        existing = item(
            "SUZCN22Y",
            "BAGE",
            collections=["LHRDK94L", "PTB3MGA6", "MKRHPXLP"],
        )
        plan = zotero_write.plan_collection_reconcile(
            [
                {
                    "item_key": "SUZCN22Y",
                    "add_collection_keys": [],
                    "remove_collection_keys": ["LHRDK94L"],
                }
            ],
            items=[existing],
            collection_keys=COLLECTIONS,
        )
        result = plan["results"][0]
        self.assertEqual(result["status"], "reconcile")
        self.assertEqual(
            result["target_collection_keys"],
            ["PTB3MGA6", "MKRHPXLP"],
        )
        self.assertEqual(result["remove_collection_keys"], ["LHRDK94L"])

    def test_plan_rejects_child_item_and_overlapping_delta(self):
        child = item("ABCDEFGH", "PDF", collections=["LHRDK94L"])
        child["data"]["itemType"] = "attachment"
        missing_parent = zotero_write.plan_collection_reconcile(
            [
                {
                    "item_key": "ABCDEFGH",
                    "add_collection_keys": ["PTB3MGA6"],
                    "remove_collection_keys": [],
                }
            ],
            items=[child],
            collection_keys=COLLECTIONS,
        )["results"][0]
        self.assertEqual(missing_parent["status"], "invalid")
        self.assertIn("top-level", missing_parent["reason"])

        overlap = zotero_write.plan_collection_reconcile(
            [
                {
                    "item_key": "SUZCN22Y",
                    "add_collection_keys": ["PTB3MGA6"],
                    "remove_collection_keys": ["PTB3MGA6"],
                }
            ],
            items=[item("SUZCN22Y", "BAGE", collections=["LHRDK94L"])],
            collection_keys=COLLECTIONS,
        )["results"][0]
        self.assertEqual(overlap["status"], "invalid")
        self.assertIn("both add and remove", overlap["reason"])

    def test_plan_allows_paper_to_remain_in_parent_collection(self):
        existing = item("ABCDEFGH", "Paper", collections=["LHRDK94L"])
        plan = zotero_write.plan_collection_reconcile(
            [
                {
                    "item_key": "ABCDEFGH",
                    "add_collection_keys": ["LHRDK94L"],
                    "remove_collection_keys": [],
                }
            ],
            items=[existing],
            collection_keys=COLLECTIONS,
        )["results"][0]
        self.assertEqual(plan["status"], "unchanged")
        self.assertEqual(plan["target_collection_keys"], ["LHRDK94L"])


class ExecutionTests(unittest.TestCase):
    def test_duplicate_input_is_created_once_then_collection_union(self):
        records = [
            {
                "source_id": "first",
                "title": "One paper",
                "doi": "10.1000/one",
                "collection_keys": ["LHRDK94L"],
            },
            {
                "source_id": "second",
                "title": "One paper",
                "doi": "10.1000/one",
                "collection_keys": ["MKRHPXLP"],
            },
        ]
        with (
            mock.patch.object(zotero_write, "fetch_library_items", return_value=[]),
            mock.patch.object(
                zotero_write, "fetch_collection_keys", return_value=COLLECTIONS
            ),
            mock.patch.object(
                zotero_write,
                "web_api_status",
                return_value={"ok": True, "user_id": 12345678},
            ),
            mock.patch.object(
                zotero_write,
                "web_api_import_identifier",
                return_value={
                    "status": "created",
                    "created": True,
                    "item_key": "ABCDEFGH",
                    "collections": ["LHRDK94L"],
                },
            ) as create,
            mock.patch.object(
                zotero_write,
                "web_api_add_to_collections",
                return_value={
                    "status": "collections_added",
                    "item_key": "ABCDEFGH",
                    "added": ["MKRHPXLP"],
                    "collections": ["LHRDK94L", "MKRHPXLP"],
                },
            ) as add,
            mock.patch.object(
                zotero_write,
                "build_library_index",
                wraps=zotero_write.build_library_index,
            ) as build_index,
        ):
            result = zotero_write.execute_paper_import(records, confirm=True)
        self.assertEqual(
            [row["status"] for row in result["results"]],
            ["created", "collections_added"],
        )
        create.assert_called_once()
        add.assert_called_once_with(12345678, "ABCDEFGH", ["MKRHPXLP"])
        build_index.assert_called_once_with([])

    def test_local_item_missing_from_cloud_stops_batch(self):
        existing = item("ABCDEFGH", "Paper", doi="10.1000/test")
        records = [
            {
                "title": "Paper",
                "doi": "10.1000/test",
                "collection_keys": ["LHRDK94L"],
            },
            {
                "title": "Second",
                "doi": "10.1000/second",
                "collection_keys": ["LHRDK94L"],
            },
        ]
        with (
            mock.patch.object(
                zotero_write, "fetch_library_items", return_value=[existing]
            ),
            mock.patch.object(
                zotero_write, "fetch_collection_keys", return_value=COLLECTIONS
            ),
            mock.patch.object(
                zotero_write,
                "web_api_status",
                return_value={"ok": True, "user_id": 12345678},
            ),
            mock.patch.object(
                zotero_write,
                "web_api_add_to_collections",
                side_effect=zotero_write.ZoteroNotSyncedError(
                    "local item ABCDEFGH is not synced to Zotero Web API"
                ),
            ),
        ):
            result = zotero_write.execute_paper_import(records, confirm=True)
        self.assertEqual(
            [row["status"] for row in result["results"]],
            ["not_synced_to_cloud", "not_attempted"],
        )

    def test_write_error_stops_remaining_items(self):
        records = [
            {
                "title": "First",
                "doi": "10.1000/first",
                "collection_keys": ["LHRDK94L"],
            },
            {
                "title": "Second",
                "doi": "10.1000/second",
                "collection_keys": ["LHRDK94L"],
            },
        ]
        with (
            mock.patch.object(zotero_write, "fetch_library_items", return_value=[]),
            mock.patch.object(
                zotero_write, "fetch_collection_keys", return_value=COLLECTIONS
            ),
            mock.patch.object(
                zotero_write,
                "web_api_status",
                return_value={"ok": True, "user_id": 12345678},
            ),
            mock.patch.object(
                zotero_write,
                "web_api_import_identifier",
                side_effect=zotero_write.ZoteroWriteError("unknown write state"),
            ),
        ):
            result = zotero_write.execute_paper_import(records, confirm=True)
        self.assertEqual(
            [row["status"] for row in result["results"]], ["unknown", "not_attempted"]
        )


class CollectionReconcileExecutionTests(unittest.TestCase):
    RECORDS: ClassVar[list[dict]] = [
        {
            "item_key": "SUZCN22Y",
            "add_collection_keys": [],
            "remove_collection_keys": ["LHRDK94L"],
        },
        {
            "item_key": "ABCDEFGH",
            "add_collection_keys": ["PTB3MGA6"],
            "remove_collection_keys": [],
        },
    ]

    def test_execute_requires_confirmation(self):
        with self.assertRaisesRegex(zotero_write.ZoteroWriteError, "confirm=true"):
            zotero_write.execute_collection_reconcile(self.RECORDS, confirm=False)

    def test_local_item_missing_from_cloud_stops_reconcile_batch(self):
        items = [
            item("SUZCN22Y", "BAGE", collections=["LHRDK94L", "PTB3MGA6"]),
            item("ABCDEFGH", "Paper", collections=["LHRDK94L"]),
        ]
        with (
            mock.patch.object(zotero_write, "fetch_library_items", return_value=items),
            mock.patch.object(
                zotero_write, "fetch_collection_keys", return_value=COLLECTIONS
            ),
            mock.patch.object(
                zotero_write,
                "web_api_status",
                return_value={"ok": True, "user_id": 12345678},
            ),
            mock.patch.object(
                zotero_write,
                "web_api_reconcile_collections",
                side_effect=zotero_write.ZoteroNotSyncedError(
                    "local item SUZCN22Y is not synced to Zotero Web API"
                ),
            ),
        ):
            result = zotero_write.execute_collection_reconcile(
                self.RECORDS, confirm=True
            )
        self.assertEqual(
            [row["status"] for row in result["results"]],
            ["not_synced_to_cloud", "not_attempted"],
        )

    def test_unknown_write_state_stops_reconcile_batch(self):
        items = [
            item("SUZCN22Y", "BAGE", collections=["LHRDK94L", "PTB3MGA6"]),
            item("ABCDEFGH", "Paper", collections=["LHRDK94L"]),
        ]
        with (
            mock.patch.object(zotero_write, "fetch_library_items", return_value=items),
            mock.patch.object(
                zotero_write, "fetch_collection_keys", return_value=COLLECTIONS
            ),
            mock.patch.object(
                zotero_write,
                "web_api_status",
                return_value={"ok": True, "user_id": 12345678},
            ),
            mock.patch.object(
                zotero_write,
                "web_api_reconcile_collections",
                side_effect=zotero_write.ZoteroWriteError("unknown write state"),
            ),
        ):
            result = zotero_write.execute_collection_reconcile(
                self.RECORDS, confirm=True
            )
        self.assertEqual(
            [row["status"] for row in result["results"]],
            ["unknown", "not_attempted"],
        )


class PdfAttachmentDeleteTests(unittest.TestCase):
    COLLECTION_KEY = "COLLECT1"
    PARENT_KEY = "PARENT01"
    ATTACHMENT_KEY = "ATTACH01"

    @staticmethod
    def collection(key="COLLECT1", name="Archive"):
        return {
            "key": key,
            "data": {"key": key, "name": name, "parentCollection": False},
            "meta": {},
        }

    @classmethod
    def parent(cls, collections=None):
        return item(
            cls.PARENT_KEY,
            "Memorial paper",
            collections=collections or [cls.COLLECTION_KEY],
        )

    @classmethod
    def attachment(cls):
        return {
            "key": cls.ATTACHMENT_KEY,
            "data": {
                "key": cls.ATTACHMENT_KEY,
                "itemType": "attachment",
                "parentItem": cls.PARENT_KEY,
                "title": "Full Text PDF",
                "contentType": "application/pdf",
                "filename": "paper.pdf",
                "linkMode": "imported_file",
                "note": "",
            },
        }

    @classmethod
    def planned_row(cls, parent_key=None, attachment_key=None):
        return {
            "parent_item_key": parent_key or cls.PARENT_KEY,
            "attachment_key": attachment_key or cls.ATTACHMENT_KEY,
            "filename": "paper.pdf",
            "size_bytes": 8,
            "link_mode": "imported_file",
            "parent_collection_keys": [cls.COLLECTION_KEY],
            "blockers": [],
        }

    @classmethod
    def cloud_attachment(cls, parent_key=None, attachment_key=None, version=17):
        attachment = cls.attachment()
        attachment["key"] = attachment_key or cls.ATTACHMENT_KEY
        attachment["data"]["key"] = attachment_key or cls.ATTACHMENT_KEY
        attachment["data"]["parentItem"] = parent_key or cls.PARENT_KEY
        attachment["version"] = version
        return attachment

    def test_plan_reports_ready_managed_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"pdf-data")
            parent = self.parent()
            attachment = self.attachment()
            with (
                mock.patch.object(
                    zotero_write.zotero_local,
                    "fetch_all_collections",
                    return_value=[self.collection()],
                ),
                mock.patch.object(
                    zotero_write.zotero_local,
                    "collect_collection_items",
                    return_value=([parent], {self.PARENT_KEY: [self.COLLECTION_KEY]}),
                ),
                mock.patch.object(
                    zotero_write.zotero_local,
                    "zotero_get",
                    side_effect=[[attachment], []],
                ),
                mock.patch.object(
                    zotero_write.zotero_local,
                    "find_pdf_for_attachment",
                    return_value=[pdf],
                ),
            ):
                plan = zotero_write.plan_pdf_attachment_delete(self.COLLECTION_KEY)

        self.assertEqual(plan["summary"], {"ready": 1})
        self.assertEqual(plan["ready_size_bytes"], 8)
        self.assertEqual(plan["results"][0]["attachment_key"], self.ATTACHMENT_KEY)
        self.assertEqual(plan["results"][0]["blockers"], [])

    def test_plan_reuses_provided_collection_snapshot(self):
        collections = [self.collection()]
        with (
            mock.patch.object(
                zotero_write.zotero_local,
                "fetch_all_collections",
            ) as fetch,
            mock.patch.object(
                zotero_write.zotero_local,
                "collect_collection_items",
                return_value=([], {}),
            ),
        ):
            plan = zotero_write.plan_pdf_attachment_delete(
                self.COLLECTION_KEY,
                collections=collections,
            )
        self.assertEqual(plan["total"], 0)
        self.assertFalse(fetch.called)

    def test_plan_blocks_shared_parent_and_annotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"pdf-data")
            parent = self.parent([self.COLLECTION_KEY, "OTHER001"])
            attachment = self.attachment()
            annotation = {"data": {"itemType": "annotation"}}
            collections = [self.collection(), self.collection("OTHER001", "Other")]
            with (
                mock.patch.object(
                    zotero_write.zotero_local,
                    "fetch_all_collections",
                    return_value=collections,
                ),
                mock.patch.object(
                    zotero_write.zotero_local,
                    "collect_collection_items",
                    return_value=([parent], {self.PARENT_KEY: [self.COLLECTION_KEY]}),
                ),
                mock.patch.object(
                    zotero_write.zotero_local,
                    "zotero_get",
                    side_effect=[[attachment], [annotation]],
                ),
                mock.patch.object(
                    zotero_write.zotero_local,
                    "find_pdf_for_attachment",
                    return_value=[pdf],
                ),
            ):
                plan = zotero_write.plan_pdf_attachment_delete(self.COLLECTION_KEY)

        blockers = plan["results"][0]["blockers"]
        self.assertIn("parent_in_other_collections", blockers)
        self.assertIn("attachment_has_annotations_or_notes", blockers)

    def test_backup_is_copied_and_sha256_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / "storage"
            storage.mkdir()
            source = storage / "paper.pdf"
            source.write_bytes(b"backup-me")
            planned = [
                {
                    "parent_item_key": self.PARENT_KEY,
                    "attachment_key": self.ATTACHMENT_KEY,
                    "local_files": [str(source)],
                }
            ]
            with mock.patch.object(
                zotero_write.zotero_local, "storage_root", return_value=storage
            ):
                batch_dir, backups = zotero_write._backup_pdf_attachments(
                    planned, str(root / "backups")
                )

            backup = Path(backups[0]["backup_path"])
            self.assertEqual(backup.read_bytes(), b"backup-me")
            self.assertEqual(backups[0]["sha256"], zotero_write._sha256(source))
            self.assertTrue((batch_dir / "backup_index.csv").is_file())

    def test_web_api_delete_is_versioned_and_parent_survives(self):
        with (
            mock.patch.object(
                zotero_write,
                "web_api_request",
                side_effect=[FakeResponse(204), FakeResponse(404)],
            ) as request,
            mock.patch.object(
                zotero_write,
                "web_api_get_item",
                return_value=self.parent(),
            ) as get_item,
        ):
            result = zotero_write.web_api_delete_pdf_attachment(
                12345678, self.PARENT_KEY, self.ATTACHMENT_KEY, 17
            )

        self.assertEqual(result["status"], "attachment_deleted")
        self.assertEqual(
            request.call_args_list[0].kwargs["headers"],
            {"If-Unmodified-Since-Version": "17"},
        )
        get_item.assert_called_once_with(12345678, self.PARENT_KEY)

    def test_web_api_delete_conflict_is_known_not_deleted(self):
        with (
            mock.patch.object(
                zotero_write,
                "web_api_request",
                return_value=FakeResponse(412),
            ) as request,
            self.assertRaises(zotero_write.ZoteroVersionConflictError),
        ):
            zotero_write.web_api_delete_pdf_attachment(
                12345678, self.PARENT_KEY, self.ATTACHMENT_KEY, 17
            )
        request.assert_called_once()

    def test_execute_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(zotero_write.ZoteroWriteError, "confirm=true"):
            zotero_write.execute_pdf_attachment_delete(
                [
                    {
                        "parent_item_key": self.PARENT_KEY,
                        "attachment_key": self.ATTACHMENT_KEY,
                    }
                ],
                self.COLLECTION_KEY,
                "/tmp/backup",
                False,
            )

    def test_execute_runs_verified_backup_and_versioned_delete(self):
        row = self.planned_row()
        plan = {
            "collection_key": self.COLLECTION_KEY,
            "collection_path": "Archive",
            "results": [row],
        }
        backup = {
            "attachment_key": self.ATTACHMENT_KEY,
            "backup_path": "/tmp/batch/ATTACH01/paper.pdf",
            "sha256": "abc123",
        }
        with (
            mock.patch.object(
                zotero_write, "plan_pdf_attachment_delete", return_value=plan
            ),
            mock.patch.object(
                zotero_write, "web_api_status", return_value={"user_id": 12345678}
            ),
            mock.patch.object(
                zotero_write,
                "web_api_get_item",
                side_effect=[self.parent(), self.cloud_attachment()],
            ),
            mock.patch.object(
                zotero_write,
                "_backup_pdf_attachments",
                return_value=(Path("/tmp/batch"), [backup]),
            ) as backup_files,
            mock.patch.object(
                zotero_write,
                "web_api_delete_pdf_attachment",
                return_value={
                    "status": "attachment_deleted",
                    "parent_item_key": self.PARENT_KEY,
                    "attachment_key": self.ATTACHMENT_KEY,
                },
            ) as delete,
        ):
            result = zotero_write.execute_pdf_attachment_delete(
                [
                    {
                        "parent_item_key": self.PARENT_KEY,
                        "attachment_key": self.ATTACHMENT_KEY,
                    }
                ],
                self.COLLECTION_KEY,
                "/tmp/backups",
                True,
            )

        backup_files.assert_called_once()
        delete.assert_called_once_with(
            12345678, self.PARENT_KEY, self.ATTACHMENT_KEY, 17
        )
        self.assertEqual(result["summary"], {"attachment_deleted": 1})
        self.assertEqual(result["results"][0]["backup_path"], backup["backup_path"])

    def test_execute_conflict_stops_batch_and_matches_backups_by_key(self):
        second_parent = "PARENT02"
        second_attachment = "ATTACH02"
        first_row = self.planned_row()
        second_row = self.planned_row(second_parent, second_attachment)
        plan = {
            "collection_key": self.COLLECTION_KEY,
            "collection_path": "Archive",
            "results": [first_row, second_row],
        }
        backups = [
            {
                "attachment_key": second_attachment,
                "backup_path": "/tmp/batch/ATTACH02/paper.pdf",
                "sha256": "second",
            },
            {
                "attachment_key": self.ATTACHMENT_KEY,
                "backup_path": "/tmp/batch/ATTACH01/paper.pdf",
                "sha256": "first",
            },
        ]
        second_parent_item = item(
            second_parent, "Second paper", collections=[self.COLLECTION_KEY]
        )
        with (
            mock.patch.object(
                zotero_write, "plan_pdf_attachment_delete", return_value=plan
            ),
            mock.patch.object(
                zotero_write, "web_api_status", return_value={"user_id": 12345678}
            ),
            mock.patch.object(
                zotero_write,
                "web_api_get_item",
                side_effect=[
                    self.parent(),
                    self.cloud_attachment(),
                    second_parent_item,
                    self.cloud_attachment(second_parent, second_attachment, 18),
                ],
            ),
            mock.patch.object(
                zotero_write,
                "_backup_pdf_attachments",
                return_value=(Path("/tmp/batch"), backups),
            ),
            mock.patch.object(
                zotero_write,
                "web_api_delete_pdf_attachment",
                side_effect=zotero_write.ZoteroVersionConflictError("conflict"),
            ) as delete,
        ):
            result = zotero_write.execute_pdf_attachment_delete(
                [
                    {
                        "parent_item_key": self.PARENT_KEY,
                        "attachment_key": self.ATTACHMENT_KEY,
                    },
                    {
                        "parent_item_key": second_parent,
                        "attachment_key": second_attachment,
                    },
                ],
                self.COLLECTION_KEY,
                "/tmp/backups",
                True,
            )

        delete.assert_called_once_with(
            12345678, self.PARENT_KEY, self.ATTACHMENT_KEY, 17
        )
        self.assertEqual(
            [row["status"] for row in result["results"]],
            ["conflict", "not_attempted"],
        )
        self.assertEqual(
            result["results"][0]["backup_path"],
            "/tmp/batch/ATTACH01/paper.pdf",
        )


if __name__ == "__main__":
    unittest.main()
