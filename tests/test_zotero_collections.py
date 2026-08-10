import unittest
from unittest import mock

from zotero_mcp import zotero_collections


def collection(
    key: str,
    name: str,
    parent: str | None = None,
    *,
    deleted: bool = False,
) -> dict:
    data = {"key": key, "name": name, "parentCollection": parent or False}
    if deleted:
        data["deleted"] = True
    return {"key": key, "data": data}


class CollectionResolverTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            collection("ROOT0001", "Projects"),
            collection("GLIOMA01", "Glioma", "ROOT0001"),
            collection("AGING001", "Aging", "ROOT0001"),
        ]

    def test_key_name_and_path_return_canonical_collection(self):
        expected = {
            "key": "GLIOMA01",
            "name": "Glioma",
            "path": "Projects > Glioma",
            "parent_key": "ROOT0001",
        }
        for reference, match_kind in (
            ({"key": "GLIOMA01"}, "key"),
            ({"name": "Glioma"}, "name"),
            ({"path": "Projects > Glioma"}, "path"),
        ):
            with self.subTest(reference=reference):
                resolved = zotero_collections.resolve_collection(
                    reference,
                    collections=self.rows,
                )
                self.assertEqual(
                    resolved,
                    {**expected, "match_kind": match_kind},
                )

    def test_name_and_path_must_be_unique(self):
        rows = self.rows + [collection("GLIOMA02", "Glioma", "ROOT0001")]
        for reference in (
            {"name": "Glioma"},
            {"path": "Projects > Glioma"},
        ):
            with (
                self.subTest(reference=reference),
                self.assertRaisesRegex(
                    zotero_collections.CollectionResolutionError,
                    "not unique",
                ),
            ):
                zotero_collections.resolve_collection(
                    reference,
                    collections=rows,
                )

    def test_missing_name_only_reports_similar_candidates(self):
        with self.assertRaisesRegex(
            zotero_collections.CollectionResolutionError,
            "similar candidates=.*Glioma",
        ):
            zotero_collections.resolve_collection(
                {"name": "Gliom"},
                collections=self.rows,
            )

    def test_deleted_collection_fails_closed(self):
        rows = self.rows + [collection("DELETED1", "Old", deleted=True)]
        with self.assertRaisesRegex(
            zotero_collections.CollectionResolutionError,
            "deleted",
        ):
            zotero_collections.resolve_collection(
                {"key": "DELETED1"},
                collections=rows,
            )

    def test_reference_requires_exactly_one_supported_field(self):
        for reference in ({}, {"key": "GLIOMA01", "name": "Glioma"}):
            with (
                self.subTest(reference=reference),
                self.assertRaisesRegex(
                    zotero_collections.CollectionResolutionError,
                    "exactly one",
                ),
            ):
                zotero_collections.resolve_collection(
                    reference,
                    collections=self.rows,
                )

    def test_invalid_key_and_malformed_parent_fail_closed(self):
        with self.assertRaisesRegex(
            zotero_collections.CollectionResolutionError,
            "invalid Zotero collection key",
        ):
            zotero_collections.resolve_collection(
                {"key": "lowercase"},
                collections=self.rows,
            )

        rows = [collection("GLIOMA01", "Glioma", "MISSING1")]
        with self.assertRaisesRegex(
            zotero_collections.CollectionResolutionError,
            "parent key not found",
        ):
            zotero_collections.resolve_collection(
                {"key": "GLIOMA01"},
                collections=rows,
            )

    def test_live_resolution_fetches_collections_once(self):
        with mock.patch.object(
            zotero_collections.zotero_local,
            "fetch_all_collections",
            return_value=self.rows,
        ) as fetch:
            resolved = zotero_collections.resolve_collection({"name": "Glioma"})
        self.assertEqual(resolved["key"], "GLIOMA01")
        fetch.assert_called_once_with()

    def test_snapshot_builds_index_once_for_multiple_references(self):
        original = zotero_collections._collection_index
        with mock.patch.object(
            zotero_collections,
            "_collection_index",
            wraps=original,
        ) as build_index:
            resolver = zotero_collections.CollectionResolver(self.rows)
            resolver.resolve({"key": "GLIOMA01"})
            resolver.resolve({"name": "Glioma"})
            resolver.resolve({"path": "Projects > Glioma"})
        build_index.assert_called_once_with(self.rows)


if __name__ == "__main__":
    unittest.main()
