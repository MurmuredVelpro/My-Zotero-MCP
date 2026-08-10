"""Fail-closed Zotero collection reference resolution."""

from __future__ import annotations

import re
from difflib import get_close_matches
from typing import Any

from . import zotero_local

ZOTERO_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
REFERENCE_FIELDS = ("key", "name", "path")


class CollectionResolutionError(RuntimeError):
    """Raised when a collection reference cannot be resolved exactly."""


def _collection_data(collection: dict[str, Any]) -> dict[str, Any]:
    data = collection.get("data")
    return data if isinstance(data, dict) else {}


def _collection_key(collection: dict[str, Any]) -> str:
    data = _collection_data(collection)
    return str(data.get("key") or collection.get("key") or "").strip()


def _collection_name(collection: dict[str, Any]) -> str:
    return str(_collection_data(collection).get("name") or "").strip()


def _parent_key(collection: dict[str, Any]) -> str | None:
    value = str(_collection_data(collection).get("parentCollection") or "").strip()
    return value or None


def _is_deleted(collection: dict[str, Any]) -> bool:
    return bool(
        collection.get("deleted") or _collection_data(collection).get("deleted")
    )


def _collection_index(collections: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for position, collection in enumerate(collections, start=1):
        if not isinstance(collection, dict):
            raise CollectionResolutionError(
                f"Zotero collection row {position} is not an object"
            )
        key = _collection_key(collection)
        if not ZOTERO_KEY_RE.fullmatch(key):
            raise CollectionResolutionError(
                f"Zotero collection row {position} has invalid key: {key or '<empty>'}"
            )
        if key in by_key:
            raise CollectionResolutionError(f"duplicate Zotero collection key: {key}")
        by_key[key] = collection
    return by_key


def _parse_reference(reference: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(reference, dict):
        raise CollectionResolutionError("collection reference must be an object")
    unknown = sorted(set(reference) - set(REFERENCE_FIELDS))
    if unknown:
        raise CollectionResolutionError(
            f"unknown collection reference fields: {unknown}"
        )
    provided = [field for field in REFERENCE_FIELDS if field in reference]
    if len(provided) != 1:
        raise CollectionResolutionError(
            "collection reference must contain exactly one of: key, name, path"
        )
    field = provided[0]
    value = reference[field]
    if not isinstance(value, str) or not value.strip():
        raise CollectionResolutionError(
            f"collection reference {field} must be a non-empty string"
        )
    value = value.strip()
    if field == "key" and not ZOTERO_KEY_RE.fullmatch(value):
        raise CollectionResolutionError(f"invalid Zotero collection key: {value}")
    return field, value


class CollectionResolver:
    """Resolve many references against one immutable collection snapshot."""

    def __init__(self, collections: list[dict[str, Any]]):
        self.collections = list(collections)
        self._by_key = _collection_index(self.collections)
        self._canonical_cache: dict[str, dict[str, Any]] = {}
        self._path_index: dict[str, list[str]] | None = None
        self._name_index: dict[str, list[str]] = {}
        for key, collection in self._by_key.items():
            name = _collection_name(collection)
            if name:
                self._name_index.setdefault(name, []).append(key)

    @classmethod
    def from_zotero(cls) -> CollectionResolver:
        return cls(zotero_local.fetch_all_collections())

    @property
    def collection_keys(self) -> set[str]:
        return set(self._by_key)

    def _canonical_base(self, key: str) -> dict[str, Any]:
        cached = self._canonical_cache.get(key)
        if cached is not None:
            return cached

        chain: list[tuple[str, dict[str, Any], str]] = []
        current_key: str | None = key
        seen: set[str] = set()
        parent_path = ""
        while current_key:
            if current_key in seen:
                raise CollectionResolutionError(
                    f"Zotero collection parent cycle detected at key: {current_key}"
                )
            seen.add(current_key)
            cached = self._canonical_cache.get(current_key)
            if cached is not None:
                parent_path = cached["path"]
                break
            collection = self._by_key.get(current_key)
            if collection is None:
                raise CollectionResolutionError(
                    f"Zotero collection parent key not found: {current_key}"
                )
            if _is_deleted(collection):
                raise CollectionResolutionError(
                    f"Zotero collection is deleted: {current_key}"
                )
            name = _collection_name(collection)
            if not name:
                raise CollectionResolutionError(
                    f"Zotero collection has no name: {current_key}"
                )
            chain.append((current_key, collection, name))
            current_key = _parent_key(collection)

        for current_key, collection, name in reversed(chain):
            path = f"{parent_path} > {name}" if parent_path else name
            self._canonical_cache[current_key] = {
                "key": current_key,
                "name": name,
                "path": path,
                "parent_key": _parent_key(collection),
            }
            parent_path = path
        return self._canonical_cache[key]

    def _path_matches(self, path: str) -> list[str]:
        if self._path_index is None:
            self._path_index = {}
            for key in sorted(self._by_key):
                try:
                    canonical = self._canonical_base(key)
                except CollectionResolutionError:
                    continue
                self._path_index.setdefault(canonical["path"], []).append(key)
        return list(self._path_index.get(path, []))

    def _similar_candidates(self, value: str, field: str) -> list[dict[str, Any]]:
        candidates: dict[str, list[dict[str, Any]]] = {}
        for key in sorted(self._by_key):
            try:
                canonical = self._canonical_base(key)
            except CollectionResolutionError:
                continue
            candidates.setdefault(canonical[field], []).append(canonical)

        close = get_close_matches(value, sorted(candidates), n=5, cutoff=0.55)
        results: list[dict[str, Any]] = []
        for label in close:
            for canonical in candidates[label]:
                results.append({**canonical, "match_kind": field})
                if len(results) == 5:
                    return results
        return results

    def resolve(self, reference: dict[str, Any]) -> dict[str, Any]:
        field, value = _parse_reference(reference)
        if field == "key":
            matches = [value] if value in self._by_key else []
        elif field == "name":
            matches = sorted(self._name_index.get(value, []))
        else:
            matches = self._path_matches(value)

        if not matches:
            candidates = (
                self._similar_candidates(value, field) if field != "key" else []
            )
            suffix = f"; similar candidates={candidates}" if candidates else ""
            raise CollectionResolutionError(
                f"Zotero collection {field} not found: {value}{suffix}"
            )
        if len(matches) > 1:
            raise CollectionResolutionError(
                f"Zotero collection {field} is not unique: {value}; keys={matches}"
            )
        return {**self._canonical_base(matches[0]), "match_kind": field}


def resolve_collection(
    reference: dict[str, Any],
    *,
    collections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve one exact key, globally unique name, or full collection path."""
    resolver = (
        CollectionResolver.from_zotero()
        if collections is None
        else CollectionResolver(collections)
    )
    return resolver.resolve(reference)
