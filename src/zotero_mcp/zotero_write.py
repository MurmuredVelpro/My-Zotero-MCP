#!/usr/bin/env python3
"""Guarded Zotero write workflows for paper metadata and PDF cleanup.

The Zotero Local API remains the read path. Guarded writes use the official
Zotero Web API and sync back through Zotero. PDF attachments are never created,
downloaded, uploaded, or modified; deletion is a separate backup-first workflow.
"""

from __future__ import annotations

import csv
import hashlib
import html
import os
import re
import shutil
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from . import zotero_collections, zotero_local, zotero_web_api

USER_AGENT = zotero_web_api.USER_AGENT
ZOTERO_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
IMPORT_IDENTIFIER_PRIORITY = ("DOI", "PMID", "arXiv")
IDENTIFIER_KEYS = ("DOI", "PMID", "PMCID", "arXiv")
CROSSREF_TYPE_MAP = {
    "journal-article": "journalArticle",
    "book": "book",
    "book-chapter": "bookSection",
    "proceedings-article": "conferencePaper",
    "report": "report",
    "dissertation": "thesis",
    "posted-content": "preprint",
    "monograph": "book",
    "reference-entry": "encyclopediaArticle",
    "dataset": "document",
    "peer-review": "document",
    "edited-book": "book",
    "standard": "document",
}


ZoteroWriteError = zotero_web_api.ZoteroWriteError
ZoteroNotSyncedError = zotero_web_api.ZoteroNotSyncedError
ZoteroCloudConflictError = zotero_web_api.ZoteroCloudConflictError
ZoteroVersionConflictError = zotero_web_api.ZoteroVersionConflictError
web_api_key = zotero_web_api.web_api_key
web_api_request = zotero_web_api.web_api_request
web_api_error = zotero_web_api.web_api_error
web_api_request_json = zotero_web_api.web_api_request_json
web_api_status = zotero_web_api.web_api_status
web_api_get_item = zotero_web_api.web_api_get_item
web_api_search_items = zotero_web_api.web_api_search_items


def web_api_find_exact_items(
    user_id: int, identifiers: dict[str, str]
) -> list[dict[str, Any]]:
    matches: dict[str, dict[str, Any]] = {}
    for kind, value in identifiers.items():
        for item in web_api_search_items(user_id, value):
            if not is_regular_item(item):
                continue
            if item_identifiers(item).get(kind) != value:
                continue
            key = item_key(item).upper()
            if not ZOTERO_KEY_RE.fullmatch(key):
                raise ZoteroWriteError(
                    "Zotero Web API exact-identifier search returned an invalid item key"
                )
            matches[key] = item
    return [matches[key] for key in sorted(matches)]


def web_api_add_to_collections(
    user_id: int, item_key_value: str, collection_keys: list[str]
) -> dict[str, Any]:
    requested = list(dict.fromkeys(collection_keys))
    for _ in range(3):
        item = web_api_get_item(user_id, item_key_value)
        data = item.get("data") or {}
        current = list(data.get("collections") or [])
        missing = [key for key in requested if key not in current]
        if not missing:
            return {
                "status": "unchanged",
                "item_key": item_key_value,
                "added": [],
                "collections": current,
            }
        combined = current + missing
        version = item.get("version", data.get("version"))
        if not isinstance(version, int) or version < 1:
            raise ZoteroWriteError(
                f"Zotero Web API item {item_key_value} has no valid version"
            )
        response = web_api_request(
            "PATCH",
            f"users/{user_id}/items/{item_key_value}",
            payload={"collections": combined},
            headers={"If-Unmodified-Since-Version": str(version)},
            timeout=30.0,
        )
        if response.status_code in {409, 412}:
            continue
        if response.status_code != 204:
            raise web_api_error(response)
        verified = web_api_get_item(user_id, item_key_value)
        actual = list((verified.get("data") or {}).get("collections") or [])
        still_missing = [key for key in requested if key not in actual]
        if still_missing:
            raise ZoteroWriteError(
                f"Zotero Web API updated {item_key_value}, but collection verification failed for "
                f"{still_missing}; unknown write state. Rescan before retrying."
            )
        return {
            "status": "collections_added",
            "item_key": item_key_value,
            "added": missing,
            "collections": actual,
        }
    raise ZoteroWriteError(
        f"Zotero Web API collection update for {item_key_value} hit repeated 409/412 conflicts; "
        "rescan before retrying."
    )


def _collection_target(
    current: list[str], add: list[str], remove: list[str]
) -> list[str]:
    remove_set = set(remove)
    target = [key for key in current if key not in remove_set]
    for key in add:
        if key not in target:
            target.append(key)
    return target


def web_api_reconcile_collections(
    user_id: int,
    item_key_value: str,
    *,
    add_collection_keys: list[str],
    remove_collection_keys: list[str],
    allow_no_collections: bool = False,
    expected_current_collections: list[str] | None = None,
) -> dict[str, Any]:
    requested_add = list(dict.fromkeys(add_collection_keys))
    requested_remove = list(dict.fromkeys(remove_collection_keys))
    overlap = sorted(set(requested_add) & set(requested_remove))
    if overlap:
        raise ZoteroWriteError(
            f"collection keys cannot be both added and removed: {overlap}"
        )

    first_read = True
    for _ in range(3):
        item = web_api_get_item(user_id, item_key_value)
        if not is_regular_item(item):
            raise ZoteroWriteError(
                f"Zotero item {item_key_value} is not a top-level paper item"
            )
        data = item.get("data") or {}
        current = list(data.get("collections") or [])
        if (
            first_read
            and expected_current_collections is not None
            and set(current) != set(expected_current_collections)
        ):
            raise ZoteroNotSyncedError(
                f"local/cloud collection memberships differ for {item_key_value}; "
                "sync Zotero, rescan, and rerun the full batch"
            )
        first_read = False

        target = _collection_target(current, requested_add, requested_remove)
        if not target and not allow_no_collections:
            raise ZoteroWriteError(
                f"collection reconcile for {item_key_value} would leave the item with no collections; "
                "set allow_no_collections=true only after explicit review"
            )
        added = [key for key in requested_add if key not in current]
        removed = [key for key in current if key in set(requested_remove)]
        if not added and not removed:
            return {
                "status": "unchanged",
                "item_key": item_key_value,
                "added": [],
                "removed": [],
                "collections": current,
            }

        version = item.get("version", data.get("version"))
        if not isinstance(version, int) or version < 1:
            raise ZoteroWriteError(
                f"Zotero Web API item {item_key_value} has no valid version"
            )
        response = web_api_request(
            "PATCH",
            f"users/{user_id}/items/{item_key_value}",
            payload={"collections": target},
            headers={"If-Unmodified-Since-Version": str(version)},
            timeout=30.0,
        )
        if response.status_code in {409, 412}:
            continue
        if response.status_code != 204:
            raise web_api_error(response)

        verified = web_api_get_item(user_id, item_key_value)
        actual = list((verified.get("data") or {}).get("collections") or [])
        missing_added = [key for key in requested_add if key not in actual]
        present_removed = [key for key in requested_remove if key in actual]
        missing_preserved = [
            key
            for key in current
            if key not in set(requested_remove) and key not in actual
        ]
        if missing_added or present_removed or missing_preserved:
            raise ZoteroWriteError(
                f"Zotero Web API updated {item_key_value}, but collection verification failed; "
                f"missing_added={missing_added}, present_removed={present_removed}, "
                f"missing_preserved={missing_preserved}. Unknown write state. Rescan before retrying."
            )
        return {
            "status": "collections_reconciled",
            "item_key": item_key_value,
            "added": added,
            "removed": removed,
            "collections": actual,
        }

    raise ZoteroWriteError(
        f"Zotero Web API collection reconcile for {item_key_value} hit repeated 409/412 conflicts; "
        "rescan before retrying."
    )


def normalize_doi(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 2 and (text[0], text[-1]) in {("(", ")"), ("[", "]"), ("<", ">")}:
        text = text[1:-1].strip()
    text = re.sub(r"^doi\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    text = text.split("?", 1)[0].split("#", 1)[0]
    text = text.strip().rstrip(".,;")
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1]
    while text.endswith("]") and text.count("]") > text.count("["):
        text = text[:-1]
    return text.lower()


def normalize_pmid(value: Any) -> str:
    text = re.sub(r"^pmid\s*:\s*", "", str(value or "").strip(), flags=re.IGNORECASE)
    return text if re.fullmatch(r"\d+", text) else ""


def normalize_pmcid(value: Any) -> str:
    text = re.sub(r"^pmcid\s*:\s*", "", str(value or "").strip(), flags=re.IGNORECASE)
    match = re.fullmatch(r"(?:PMC)?(\d+)", text, flags=re.IGNORECASE)
    return f"PMC{match.group(1)}" if match else ""


def normalize_arxiv(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^arxiv\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\.pdf$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"v\d+$", "", text, flags=re.IGNORECASE)
    match = re.fullmatch(
        r"(?:[a-z.-]+/\d{7}|\d{4}\.\d{4,5})", text, flags=re.IGNORECASE
    )
    return match.group(0).lower() if match else ""


NORMALIZERS = {
    "DOI": normalize_doi,
    "PMID": normalize_pmid,
    "PMCID": normalize_pmcid,
    "arXiv": normalize_arxiv,
}


def normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def record_identifiers(record: dict[str, Any]) -> dict[str, str]:
    values = {
        "DOI": record.get("doi") or record.get("DOI"),
        "PMID": record.get("pmid") or record.get("PMID"),
        "PMCID": record.get("pmcid") or record.get("PMCID"),
        "arXiv": record.get("arxiv") or record.get("arXiv"),
    }
    return {
        kind: normalized
        for kind, value in values.items()
        if (normalized := NORMALIZERS[kind](value))
    }


def item_identifiers(item: dict[str, Any]) -> dict[str, str]:
    data = item.get("data", {})
    identifiers: dict[str, str] = {}

    doi = normalize_doi(data.get("DOI"))
    if doi:
        identifiers["DOI"] = doi

    extra = str(data.get("extra") or "")
    url = str(data.get("url") or "")
    archive_id = str(data.get("archiveID") or "")
    sources = f"{extra}\n{url}\n{archive_id}"

    patterns = {
        "DOI": r"(?:^|\b)(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/\S+)",
        "PMID": r"(?:^|\b)(?:pmid\s*:\s*|pubmed\.ncbi\.nlm\.nih\.gov/)(\d+)",
        "PMCID": r"(?:^|\b)(?:pmcid\s*:\s*|ncbi\.nlm\.nih\.gov/pmc/articles/)(PMC\d+)",
        "arXiv": r"(?:^|\b)(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)((?:[a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?)",
    }
    for kind, pattern in patterns.items():
        if kind in identifiers:
            continue
        match = re.search(pattern, sources, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            normalized = NORMALIZERS[kind](match.group(1))
            if normalized:
                identifiers[kind] = normalized

    if "arXiv" not in identifiers and str(data.get("archive") or "").lower() == "arxiv":
        arxiv = normalize_arxiv(archive_id)
        if arxiv:
            identifiers["arXiv"] = arxiv
    return identifiers


def is_regular_item(item: dict[str, Any]) -> bool:
    return item.get("data", {}).get("itemType") not in {
        "attachment",
        "note",
        "annotation",
    }


def fetch_library_items() -> list[dict[str, Any]]:
    return [
        item
        for item in zotero_local.fetch_paginated("users/0/items/top")
        if is_regular_item(item)
    ]


def fetch_collection_keys() -> set[str]:
    return set(zotero_local.collection_index(zotero_local.fetch_all_collections()))


def resolve_unique_collection_path(
    path: str,
    *,
    collections: list[dict[str, Any]] | None = None,
) -> str:
    try:
        return zotero_collections.resolve_collection(
            {"path": path},
            collections=collections,
        )["key"]
    except zotero_collections.CollectionResolutionError as exc:
        raise ZoteroWriteError(str(exc)) from exc


def build_library_index(items: list[dict[str, Any]]) -> dict[str, Any]:
    index: dict[str, Any] = {
        "items": [],
        "identifiers": {},
        "titles": {},
    }
    for item in items:
        add_item_to_library_index(index, item)
    return index


def add_item_to_library_index(index: dict[str, Any], item: dict[str, Any]) -> None:
    index["items"].append(item)
    if not is_regular_item(item):
        return
    title = normalize_title(item.get("data", {}).get("title"))
    if title:
        index["titles"].setdefault(title, []).append(item)
    for kind, value in item_identifiers(item).items():
        index["identifiers"].setdefault((kind, value), []).append(item)


def _metadata_get(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> requests.Response:
    try:
        response = requests.get(
            url,
            params=params,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, application/xml",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ZoteroWriteError(
            f"metadata resolver request failed: {type(exc).__name__}: {exc}"
        ) from exc
    if not response.ok:
        raise ZoteroWriteError(
            f"metadata resolver returned HTTP {response.status_code} for {url}"
        )
    return response


def _clean_markup(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _set_if_supported(item: dict[str, Any], field: str, value: Any) -> None:
    if field in item and value not in (None, "", []):
        item[field] = value


def _append_extra(item: dict[str, Any], lines: list[str]) -> None:
    if "extra" not in item:
        return
    current = [line for line in str(item.get("extra") or "").splitlines() if line]
    for line in lines:
        if line and line not in current:
            current.append(line)
    item["extra"] = "\n".join(current)


def web_api_item_template(item_type: str) -> dict[str, Any]:
    template = web_api_request_json(
        "GET", "items/new", params={"itemType": item_type}, timeout=20.0
    )
    if not isinstance(template, dict) or template.get("itemType") != item_type:
        raise ZoteroWriteError(
            f"Zotero Web API returned no valid template for {item_type}"
        )
    return dict(template)


def _crossref_date(message: dict[str, Any]) -> str:
    for field in (
        "published-print",
        "published-online",
        "published",
        "issued",
        "created",
    ):
        date_parts = (message.get(field) or {}).get("date-parts") or []
        if date_parts and date_parts[0]:
            return "-".join(str(part) for part in date_parts[0])
    return ""


def resolve_doi_metadata(doi: str) -> dict[str, Any]:
    normalized = normalize_doi(doi)
    if not normalized:
        raise ZoteroWriteError(f"invalid DOI for metadata resolution: {doi}")
    params = None
    contact_email = os.environ.get("ZOTERO_MCP_CONTACT_EMAIL", "").strip()
    if contact_email:
        params = {"mailto": contact_email}
    response = _metadata_get(
        f"https://api.crossref.org/works/{quote(normalized, safe='')}",
        params=params,
    )
    try:
        message = response.json().get("message") or {}
    except ValueError as exc:
        raise ZoteroWriteError("CrossRef returned invalid JSON") from exc
    if not isinstance(message, dict):
        raise ZoteroWriteError("CrossRef returned invalid metadata")

    item_type = CROSSREF_TYPE_MAP.get(str(message.get("type") or ""), "document")
    item = web_api_item_template(item_type)
    titles = message.get("title") or []
    title = _clean_markup(titles[0] if titles else "")
    if not title:
        raise ZoteroWriteError(f"CrossRef returned no title for DOI {normalized}")
    _set_if_supported(item, "title", title)

    creators: list[dict[str, str]] = []
    for author in message.get("author") or []:
        family = str(author.get("family") or "").strip()
        given = str(author.get("given") or "").strip()
        name = str(author.get("name") or "").strip()
        if family:
            creators.append(
                {
                    "creatorType": "author",
                    "firstName": given,
                    "lastName": family,
                }
            )
        elif name:
            creators.append({"creatorType": "author", "name": name})
    if creators:
        item["creators"] = creators

    field_values = {
        "DOI": normalized,
        "url": message.get("URL"),
        "volume": message.get("volume"),
        "issue": message.get("issue"),
        "pages": message.get("page"),
        "publisher": message.get("publisher"),
        "date": _crossref_date(message),
        "language": message.get("language"),
        "abstractNote": _clean_markup(message.get("abstract")),
    }
    for field, value in field_values.items():
        _set_if_supported(item, field, value)

    container = _clean_markup((message.get("container-title") or [""])[0])
    for field in ("publicationTitle", "bookTitle", "conferenceName"):
        if field in item and container:
            item[field] = container
            break
    issn = (message.get("ISSN") or [""])[0]
    isbn = (message.get("ISBN") or [""])[0]
    _set_if_supported(item, "ISSN", issn)
    _set_if_supported(item, "ISBN", isbn)
    return item


def _pubmed_article_ids(article: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
        kind = str(node.attrib.get("IdType") or "").lower()
        value = _element_text(node)
        if kind and value:
            result[kind] = value
    for node in article.findall(".//MedlineCitation/Article/ELocationID"):
        if str(node.attrib.get("EIdType") or "").lower() == "doi":
            value = _element_text(node)
            if value:
                result.setdefault("doi", value)
    return result


def resolve_pmid_metadata(pmid: str) -> dict[str, Any]:
    normalized = normalize_pmid(pmid)
    if not normalized:
        raise ZoteroWriteError(f"invalid PMID for metadata resolution: {pmid}")
    response = _metadata_get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={"db": "pubmed", "id": normalized, "retmode": "xml"},
    )
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise ZoteroWriteError("PubMed returned invalid XML") from exc
    article = root.find(".//PubmedArticle")
    if article is None:
        raise ZoteroWriteError(f"PubMed returned no article for PMID {normalized}")
    identifiers = _pubmed_article_ids(article)
    doi = normalize_doi(identifiers.get("doi"))
    pmcid = normalize_pmcid(identifiers.get("pmc"))
    if doi:
        try:
            item = resolve_doi_metadata(doi)
            _append_extra(
                item,
                [f"PMID: {normalized}", f"PMCID: {pmcid}" if pmcid else ""],
            )
            return item
        except ZoteroWriteError:
            pass

    item = web_api_item_template("journalArticle")
    title = _element_text(article.find(".//MedlineCitation/Article/ArticleTitle"))
    if not title:
        raise ZoteroWriteError(f"PubMed returned no title for PMID {normalized}")
    _set_if_supported(item, "title", title)

    creators: list[dict[str, str]] = []
    for author in article.findall(".//MedlineCitation/Article/AuthorList/Author"):
        collective = _element_text(author.find("CollectiveName"))
        family = _element_text(author.find("LastName"))
        given = _element_text(author.find("ForeName"))
        if collective:
            creators.append({"creatorType": "author", "name": collective})
        elif family:
            creators.append(
                {
                    "creatorType": "author",
                    "firstName": given,
                    "lastName": family,
                }
            )
    if creators:
        item["creators"] = creators

    journal_issue = article.find(".//MedlineCitation/Article/Journal/JournalIssue")
    pub_date = journal_issue.find("PubDate") if journal_issue is not None else None
    date = ""
    if pub_date is not None:
        date = _element_text(pub_date.find("MedlineDate"))
        if not date:
            date = "-".join(
                value
                for value in (
                    _element_text(pub_date.find("Year")),
                    _element_text(pub_date.find("Month")),
                    _element_text(pub_date.find("Day")),
                )
                if value
            )
    abstracts = [
        _element_text(node)
        for node in article.findall(".//MedlineCitation/Article/Abstract/AbstractText")
    ]
    field_values = {
        "publicationTitle": _element_text(
            article.find(".//MedlineCitation/Article/Journal/Title")
        ),
        "journalAbbreviation": _element_text(
            article.find(".//MedlineCitation/MedlineJournalInfo/MedlineTA")
        ),
        "volume": _element_text(journal_issue.find("Volume"))
        if journal_issue is not None
        else "",
        "issue": _element_text(journal_issue.find("Issue"))
        if journal_issue is not None
        else "",
        "pages": _element_text(
            article.find(".//MedlineCitation/Article/Pagination/MedlinePgn")
        ),
        "date": date,
        "abstractNote": "\n".join(value for value in abstracts if value),
        "language": _element_text(article.find(".//MedlineCitation/Article/Language")),
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{normalized}/",
        "DOI": doi,
    }
    for field, value in field_values.items():
        _set_if_supported(item, field, value)
    _append_extra(
        item,
        [f"PMID: {normalized}", f"PMCID: {pmcid}" if pmcid else ""],
    )
    return item


def _arxiv_crossref_fallback(arxiv_id: str, reason: str) -> dict[str, Any]:
    try:
        item = resolve_doi_metadata(f"10.48550/arxiv.{arxiv_id}")
    except ZoteroWriteError as exc:
        raise ZoteroWriteError(
            f"arXiv metadata resolution failed ({reason}); CrossRef fallback failed: {exc}"
        ) from exc
    _set_if_supported(item, "url", f"https://arxiv.org/abs/{arxiv_id}")
    _append_extra(item, [f"arXiv: {arxiv_id}"])
    return item


def resolve_arxiv_metadata(arxiv_id: str) -> dict[str, Any]:
    normalized = normalize_arxiv(arxiv_id)
    if not normalized:
        raise ZoteroWriteError(f"invalid arXiv ID for metadata resolution: {arxiv_id}")
    try:
        response = _metadata_get(
            "https://export.arxiv.org/api/query",
            params={"id_list": normalized},
            timeout=25.0,
        )
        root = ET.fromstring(response.content)
    except (ZoteroWriteError, ET.ParseError) as exc:
        return _arxiv_crossref_fallback(normalized, str(exc))

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    entry = root.find("atom:entry", ns)
    if entry is None or "api/errors" in _element_text(entry.find("atom:id", ns)):
        return _arxiv_crossref_fallback(normalized, "no arXiv entry")
    title = _element_text(entry.find("atom:title", ns))
    if not title:
        return _arxiv_crossref_fallback(normalized, "missing title")

    item = web_api_item_template("preprint")
    _set_if_supported(item, "title", title)
    creators: list[dict[str, str]] = []
    for author in entry.findall("atom:author", ns):
        name = _element_text(author.find("atom:name", ns))
        parts = name.rsplit(" ", 1)
        if len(parts) == 2:
            creators.append(
                {
                    "creatorType": "author",
                    "firstName": parts[0],
                    "lastName": parts[1],
                }
            )
        elif name:
            creators.append({"creatorType": "author", "name": name})
    if creators:
        item["creators"] = creators
    doi = normalize_doi(_element_text(entry.find("arxiv:doi", ns)))
    field_values = {
        "abstractNote": _element_text(entry.find("atom:summary", ns)),
        "date": _element_text(entry.find("atom:published", ns))[:10],
        "url": f"https://arxiv.org/abs/{normalized}",
        "DOI": doi,
        "archive": "arXiv",
        "archiveID": normalized,
    }
    for field, value in field_values.items():
        _set_if_supported(item, field, value)
    _append_extra(item, [f"arXiv: {normalized}"])
    return item


def resolve_identifier_metadata(identifier: dict[str, str]) -> dict[str, Any]:
    if len(identifier) != 1:
        raise ZoteroWriteError("one import identifier is required")
    kind, value = next(iter(identifier.items()))
    if kind == "DOI":
        return resolve_doi_metadata(value)
    if kind == "PMID":
        return resolve_pmid_metadata(value)
    if kind == "arXiv":
        return resolve_arxiv_metadata(value)
    raise ZoteroWriteError(f"unsupported import identifier: {kind}")


FORBIDDEN_CREATE_FIELDS = {
    "key",
    "version",
    "dateAdded",
    "dateModified",
    "library",
    "links",
    "meta",
    "children",
    "attachments",
    "attachment",
    "parentItem",
    "linkMode",
    "contentType",
    "charset",
    "filename",
    "md5",
    "mtime",
}


def sanitize_create_payload(
    item: dict[str, Any], collection_keys: list[str]
) -> dict[str, Any]:
    payload = dict(item)
    for field in FORBIDDEN_CREATE_FIELDS:
        payload.pop(field, None)
    if not payload.get("itemType") or not str(payload.get("title") or "").strip():
        raise ZoteroWriteError(
            "metadata resolver returned no writable item type or title"
        )
    payload["collections"] = list(dict.fromkeys(collection_keys))
    payload.setdefault("tags", [])
    payload.setdefault("relations", {})
    return payload


def _created_item_key(response: dict[str, Any]) -> str:
    entry: Any = (response.get("success") or {}).get("0")
    if entry is None:
        entry = (response.get("successful") or {}).get("0")
    if isinstance(entry, dict):
        entry = entry.get("key") or (entry.get("data") or {}).get("key")
    key = str(entry or "").upper()
    if not ZOTERO_KEY_RE.fullmatch(key):
        failure = (response.get("failed") or {}).get("0")
        if failure:
            raise ZoteroWriteError(f"Zotero Web API rejected item creation: {failure}")
        raise ZoteroWriteError(
            "Zotero Web API creation returned no valid item key; unknown write state. "
            "Rescan before retrying."
        )
    return key


def web_api_import_identifier(
    user_id: int,
    identifier: dict[str, str],
    collection_keys: list[str],
    *,
    exact_identifiers: dict[str, str] | None = None,
) -> dict[str, Any]:
    preflight_ids = exact_identifiers or identifier
    cloud_matches = web_api_find_exact_items(user_id, preflight_ids)
    if len(cloud_matches) > 1:
        keys = sorted(item_key(item).upper() for item in cloud_matches)
        raise ZoteroCloudConflictError(
            "exact identifiers resolve to multiple Zotero cloud items", keys
        )
    if cloud_matches:
        key = item_key(cloud_matches[0]).upper()
        result = web_api_add_to_collections(user_id, key, collection_keys)
        result.update({"created": False, "cloud_preflight_match": True})
        return result

    item = sanitize_create_payload(
        resolve_identifier_metadata(identifier), collection_keys
    )
    response = web_api_request_json(
        "POST",
        f"users/{user_id}/items",
        payload=[item],
        headers={"Zotero-Write-Token": uuid.uuid4().hex},
        timeout=45.0,
    )
    if not isinstance(response, dict):
        raise ZoteroWriteError(
            "Zotero Web API creation returned invalid data; unknown write state. "
            "Rescan before retrying."
        )
    key = _created_item_key(response)
    try:
        verified = web_api_get_item(user_id, key)
    except ZoteroNotSyncedError as exc:
        raise ZoteroWriteError(
            f"Zotero Web API created {key}, but read-back failed; unknown write state. "
            "Rescan before retrying."
        ) from exc
    actual_key = item_key(verified).upper()
    actual_collections = list((verified.get("data") or {}).get("collections") or [])
    missing = [key for key in collection_keys if key not in actual_collections]
    if actual_key != key or missing:
        raise ZoteroWriteError(
            f"Zotero Web API created {key}, but write verification failed; "
            f"missing collections={missing}. Unknown write state. Rescan before retrying."
        )
    return {
        "status": "created",
        "created": True,
        "item_key": key,
        "collections": actual_collections,
        "cloud_preflight_match": False,
    }


def validate_record(
    record: dict[str, Any], valid_collection_keys: set[str]
) -> tuple[dict[str, Any] | None, str | None]:
    title = str(record.get("title") or "").strip()
    source_id = str(record.get("source_id") or title or "untitled").strip()
    collection_keys = record.get("collection_keys")
    if not title:
        return None, "title is required"
    if not isinstance(collection_keys, list) or not collection_keys:
        return None, "collection_keys must be a non-empty list"
    if len(collection_keys) > 20:
        return None, "one paper may target at most 20 collections"

    normalized_keys: list[str] = []
    for key in collection_keys:
        key = str(key or "").strip().upper()
        if not ZOTERO_KEY_RE.fullmatch(key):
            return None, f"invalid Zotero collection key: {key or '<empty>'}"
        if key not in valid_collection_keys:
            return None, f"unknown Zotero collection key: {key}"
        if key not in normalized_keys:
            normalized_keys.append(key)

    return {
        "source_id": source_id,
        "title": title,
        "identifiers": record_identifiers(record),
        "collection_keys": normalized_keys,
    }, None


def item_key(item: dict[str, Any]) -> str:
    data = item.get("data", {})
    return str(data.get("key") or item.get("key") or "")


def plan_one(
    record: dict[str, Any],
    index: dict[str, Any],
    valid_collection_keys: set[str],
) -> dict[str, Any]:
    prepared, error = validate_record(record, valid_collection_keys)
    if error:
        return {
            "source_id": str(
                record.get("source_id") or record.get("title") or "untitled"
            ),
            "title": str(record.get("title") or ""),
            "status": "invalid",
            "reason": error,
        }
    assert prepared is not None

    result: dict[str, Any] = {
        "source_id": prepared["source_id"],
        "title": prepared["title"],
        "identifiers": prepared["identifiers"],
        "requested_collection_keys": prepared["collection_keys"],
    }
    matched: dict[str, dict[str, Any]] = {}
    matched_by: dict[str, list[str]] = {}
    for kind, value in prepared["identifiers"].items():
        for item in index["identifiers"].get((kind, value), []):
            key = item_key(item)
            matched[key] = item
            matched_by.setdefault(key, []).append(kind)

    if len(matched) > 1:
        result.update(
            {
                "status": "ambiguous",
                "reason": "exact identifiers resolve to different Zotero items",
                "candidate_item_keys": sorted(matched),
                "matched_by": matched_by,
            }
        )
        return result

    if len(matched) == 1:
        key, item = next(iter(matched.items()))
        current = list(item.get("data", {}).get("collections") or [])
        missing = [key for key in prepared["collection_keys"] if key not in current]
        result.update(
            {
                "status": "add_collections" if missing else "unchanged",
                "matched_item_key": key,
                "matched_by": matched_by[key],
                "missing_collection_keys": missing,
            }
        )
        existing_title = str(item.get("data", {}).get("title") or "")
        if normalize_title(existing_title) != normalize_title(prepared["title"]):
            result["warning"] = "exact identifier matched, but titles differ"
            result["existing_title"] = existing_title
        return result

    title_matches = index["titles"].get(normalize_title(prepared["title"]), [])
    if title_matches:
        result.update(
            {
                "status": "ambiguous",
                "reason": "normalized title matches existing item; title-only matches are never auto-deduplicated",
                "candidate_item_keys": sorted(item_key(item) for item in title_matches),
            }
        )
        return result

    import_identifier = next(
        (
            {kind: prepared["identifiers"][kind]}
            for kind in IMPORT_IDENTIFIER_PRIORITY
            if kind in prepared["identifiers"]
        ),
        None,
    )
    if import_identifier is None:
        result.update(
            {
                "status": "manual",
                "reason": (
                    "no DOI, PMID, or arXiv identifier is available for reliable Zotero translation"
                ),
            }
        )
        return result

    result.update({"status": "create", "import_identifier": import_identifier})
    return result


def summarize_results(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(result.get("status") or "unknown") for result in results)
    return {key: counts[key] for key in sorted(counts)}


def plan_paper_import(
    records: list[dict[str, Any]],
    *,
    items: list[dict[str, Any]] | None = None,
    collection_keys: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(records, list) or not 1 <= len(records) <= 50:
        raise ZoteroWriteError("items must contain between 1 and 50 papers")
    library_items = fetch_library_items() if items is None else items
    valid_keys = fetch_collection_keys() if collection_keys is None else collection_keys
    index = build_library_index(library_items)
    results = [plan_one(record, index, valid_keys) for record in records]
    return {
        "total": len(results),
        "summary": summarize_results(results),
        "results": results,
        "write_performed": False,
    }


def prepare_fixed_collection_import(
    records: list[dict[str, Any]], collection_key: str
) -> list[dict[str, Any]]:
    if not isinstance(records, list) or not 1 <= len(records) <= 50:
        raise ZoteroWriteError("items must contain between 1 and 50 papers")
    normalized_key = str(collection_key or "").strip().upper()
    if not ZOTERO_KEY_RE.fullmatch(normalized_key):
        raise ZoteroWriteError(
            f"invalid Zotero collection key: {normalized_key or '<empty>'}"
        )

    prepared: list[dict[str, Any]] = []
    seen_source_ids: set[str] = set()
    for position, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ZoteroWriteError(f"item {position} must be an object")
        if "collection_keys" in record:
            raise ZoteroWriteError(
                "collection_keys is not accepted by a fixed-collection import workflow"
            )
        source_id = str(record.get("source_id") or "").strip()
        if not source_id:
            raise ZoteroWriteError(f"item {position} requires source_id")
        if source_id in seen_source_ids:
            raise ZoteroWriteError(f"duplicate source_id: {source_id}")
        seen_source_ids.add(source_id)
        prepared_record = dict(record)
        prepared_record["source_id"] = source_id
        prepared_record["collection_keys"] = [normalized_key]
        prepared.append(prepared_record)
    return prepared


def compact_paper_import_result(
    result: dict[str, Any], collection_key: str, collection_path: str
) -> dict[str, Any]:
    result_fields = (
        "source_id",
        "status",
        "reason",
        "error",
        "item_key",
        "matched_item_key",
        "matched_by",
        "candidate_item_keys",
        "warning",
        "cloud_preflight_match",
    )
    compact_results = [
        {field: row[field] for field in result_fields if field in row}
        for row in result.get("results", [])
    ]
    return {
        "target_collection_path": collection_path,
        "target_collection_key": collection_key,
        "total": result.get("total", len(compact_results)),
        "summary": result.get("summary", {}),
        "results": compact_results,
        "write_performed": bool(result.get("write_performed", False)),
    }


def _validate_reconcile_keys(
    value: Any,
    field: str,
    valid_collection_keys: set[str],
) -> tuple[list[str] | None, str | None]:
    if not isinstance(value, list):
        return None, f"{field} must be a list"
    if len(value) > 20:
        return None, f"{field} may contain at most 20 collections"
    normalized: list[str] = []
    for raw_key in value:
        key = str(raw_key or "").strip().upper()
        if not ZOTERO_KEY_RE.fullmatch(key):
            return None, f"invalid Zotero collection key in {field}: {key or '<empty>'}"
        if key not in valid_collection_keys:
            return None, f"unknown Zotero collection key in {field}: {key}"
        if key not in normalized:
            normalized.append(key)
    return normalized, None


def validate_collection_reconcile_record(
    record: Any,
    valid_collection_keys: set[str],
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(record, dict):
        return None, "each reconcile item must be an object"
    key = str(record.get("item_key") or "").strip().upper()
    if not ZOTERO_KEY_RE.fullmatch(key):
        return None, f"invalid Zotero item key: {key or '<empty>'}"

    add, error = _validate_reconcile_keys(
        record.get("add_collection_keys"),
        "add_collection_keys",
        valid_collection_keys,
    )
    if error:
        return None, error
    remove, error = _validate_reconcile_keys(
        record.get("remove_collection_keys"),
        "remove_collection_keys",
        valid_collection_keys,
    )
    if error:
        return None, error
    assert add is not None and remove is not None
    if not add and not remove:
        return None, "at least one collection key must be added or removed"
    overlap = sorted(set(add) & set(remove))
    if overlap:
        return None, f"collection keys cannot be both add and remove targets: {overlap}"
    return {
        "item_key": key,
        "add_collection_keys": add,
        "remove_collection_keys": remove,
    }, None


def plan_collection_reconcile(
    records: list[dict[str, Any]],
    *,
    allow_no_collections: bool = False,
    items: list[dict[str, Any]] | None = None,
    collection_keys: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(records, list) or not 1 <= len(records) <= 50:
        raise ZoteroWriteError("items must contain between 1 and 50 papers")
    library_items = fetch_library_items() if items is None else items
    valid_keys = fetch_collection_keys() if collection_keys is None else collection_keys
    by_key = {
        item_key(item).upper(): item
        for item in library_items
        if is_regular_item(item) and ZOTERO_KEY_RE.fullmatch(item_key(item).upper())
    }

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        prepared, error = validate_collection_reconcile_record(record, valid_keys)
        raw_key = (
            str(record.get("item_key") or "").strip().upper()
            if isinstance(record, dict)
            else ""
        )
        if error:
            results.append({"item_key": raw_key, "status": "invalid", "reason": error})
            continue
        assert prepared is not None
        key = prepared["item_key"]
        if key in seen:
            results.append(
                {
                    "item_key": key,
                    "status": "invalid",
                    "reason": "duplicate item_key in reconcile batch",
                }
            )
            continue
        seen.add(key)

        item = by_key.get(key)
        if item is None:
            results.append(
                {
                    "item_key": key,
                    "status": "invalid",
                    "reason": "item_key was not found among top-level regular Zotero items",
                }
            )
            continue
        data = item.get("data") or {}
        current = list(data.get("collections") or [])
        target = _collection_target(
            current,
            prepared["add_collection_keys"],
            prepared["remove_collection_keys"],
        )
        if not target and not allow_no_collections:
            results.append(
                {
                    "item_key": key,
                    "title": str(data.get("title") or ""),
                    "status": "invalid",
                    "reason": (
                        "reconcile would leave the item with no collections; "
                        "allow_no_collections is false"
                    ),
                    "current_collection_keys": current,
                    "target_collection_keys": target,
                }
            )
            continue

        added = [
            collection_key
            for collection_key in prepared["add_collection_keys"]
            if collection_key not in current
        ]
        remove_set = set(prepared["remove_collection_keys"])
        removed = [
            collection_key for collection_key in current if collection_key in remove_set
        ]
        results.append(
            {
                "item_key": key,
                "title": str(data.get("title") or ""),
                "status": "reconcile" if added or removed else "unchanged",
                "current_collection_keys": current,
                "requested_add_collection_keys": prepared["add_collection_keys"],
                "requested_remove_collection_keys": prepared["remove_collection_keys"],
                "add_collection_keys": added,
                "remove_collection_keys": removed,
                "target_collection_keys": target,
            }
        )

    return {
        "total": len(results),
        "summary": summarize_results(results),
        "results": results,
        "allow_no_collections": allow_no_collections,
        "write_performed": False,
    }


def synthetic_item_from_created(
    record: dict[str, Any], created_key: str, collection_keys: list[str]
) -> dict[str, Any]:
    identifiers = record_identifiers(record)
    data: dict[str, Any] = {
        "key": created_key,
        "itemType": "journalArticle",
        "title": str(record.get("title") or ""),
        "collections": collection_keys,
    }
    if identifiers.get("DOI"):
        data["DOI"] = identifiers["DOI"]
    extra_lines = [
        f"{kind}: {identifiers[kind]}"
        for kind in ("PMID", "PMCID", "arXiv")
        if kind in identifiers
    ]
    if extra_lines:
        data["extra"] = "\n".join(extra_lines)
    return {"key": created_key, "data": data}


def not_attempted_results(
    records: list[dict[str, Any]], start: int, reason: str
) -> list[dict[str, Any]]:
    return [
        {
            "source_id": str(
                record.get("source_id") or record.get("title") or "untitled"
            ),
            "title": str(record.get("title") or ""),
            "status": "not_attempted",
            "reason": reason,
        }
        for record in records[start:]
    ]


def _web_api_user_id() -> int:
    user_id = web_api_status().get("user_id")
    if not isinstance(user_id, int) or user_id < 1:
        raise ZoteroWriteError("Zotero Web API status returned no valid user_id")
    return user_id


def _merge_index_collections(
    index: dict[str, Any], identifiers: dict[str, str], collection_keys: list[str]
) -> None:
    matched_items = index["identifiers"]
    for kind, value in identifiers.items():
        for item in matched_items.get((kind, value), []):
            current = item.setdefault("data", {}).setdefault("collections", [])
            for key in collection_keys:
                if key not in current:
                    current.append(key)


def _apply_paper_import_plan(
    record: dict[str, Any],
    plan: dict[str, Any],
    index: dict[str, Any],
    user_id: int,
) -> None:
    if plan["status"] == "add_collections":
        response = web_api_add_to_collections(
            user_id,
            plan["matched_item_key"],
            plan["missing_collection_keys"],
        )
        plan.update(
            {
                "status": response["status"],
                "item_key": response["item_key"],
                "collection_keys": response["collections"],
                "web_api_result": response,
            }
        )
        _merge_index_collections(index, plan["identifiers"], response["collections"])
        return

    response = web_api_import_identifier(
        user_id,
        plan["import_identifier"],
        plan["requested_collection_keys"],
        exact_identifiers=plan["identifiers"],
    )
    created_key = str(response.get("item_key") or "").upper()
    if not ZOTERO_KEY_RE.fullmatch(created_key):
        raise ZoteroWriteError(
            "Zotero Web API import returned no valid item_key; rescan before retrying."
        )
    created = synthetic_item_from_created(
        record, created_key, list(response.get("collections") or [])
    )
    add_item_to_library_index(index, created)
    plan.update(
        {
            "status": response["status"],
            "item_key": created_key,
            "collection_keys": response["collections"],
            "cloud_preflight_match": bool(response.get("cloud_preflight_match")),
        }
    )


def execute_paper_import(
    records: list[dict[str, Any]], confirm: bool
) -> dict[str, Any]:
    if confirm is not True:
        raise ZoteroWriteError("confirm=true is required for Zotero writes")
    if not isinstance(records, list) or not 1 <= len(records) <= 50:
        raise ZoteroWriteError("items must contain between 1 and 50 papers")

    library_items = fetch_library_items()
    valid_keys = fetch_collection_keys()
    index = build_library_index(library_items)
    user_id = _web_api_user_id()

    results: list[dict[str, Any]] = []
    for position, record in enumerate(records):
        plan = plan_one(record, index, valid_keys)
        if plan["status"] in {"invalid", "ambiguous", "manual", "unchanged"}:
            results.append(plan)
            continue

        try:
            _apply_paper_import_plan(record, plan, index, user_id)
            results.append(plan)
        except ZoteroCloudConflictError as exc:
            plan.update(
                {
                    "status": "ambiguous",
                    "reason": str(exc),
                    "candidate_item_keys": exc.item_keys,
                }
            )
            results.append(plan)
        except ZoteroNotSyncedError as exc:
            plan.update({"status": "not_synced_to_cloud", "error": str(exc)})
            results.append(plan)
            results.extend(
                not_attempted_results(
                    records,
                    position + 1,
                    "stopped because a local Zotero item is not synced to the cloud; "
                    "sync, rescan, and rerun the full batch",
                )
            )
            break
        except ZoteroWriteError as exc:
            plan.update({"status": "unknown", "error": str(exc)})
            results.append(plan)
            results.extend(
                not_attempted_results(
                    records,
                    position + 1,
                    "stopped after a Web API error; rescan and rerun the full batch",
                )
            )
            break

    return {
        "total": len(results),
        "summary": summarize_results(results),
        "results": results,
        "write_performed": any(
            result.get("status") in {"created", "collections_added", "unknown"}
            for result in results
        ),
    }


def _not_attempted_reconcile_results(
    records: list[dict[str, Any]], start: int, reason: str
) -> list[dict[str, Any]]:
    return [
        {
            "item_key": str(record.get("item_key") or "").strip().upper(),
            "status": "not_attempted",
            "reason": reason,
        }
        for record in records[start:]
    ]


def execute_collection_reconcile(
    records: list[dict[str, Any]],
    confirm: bool,
    *,
    allow_no_collections: bool = False,
) -> dict[str, Any]:
    if confirm is not True:
        raise ZoteroWriteError("confirm=true is required for Zotero writes")
    if not isinstance(records, list) or not 1 <= len(records) <= 50:
        raise ZoteroWriteError("items must contain between 1 and 50 papers")

    library_items = fetch_library_items()
    valid_keys = fetch_collection_keys()
    plan = plan_collection_reconcile(
        records,
        allow_no_collections=allow_no_collections,
        items=library_items,
        collection_keys=valid_keys,
    )
    user_id = _web_api_user_id()

    results: list[dict[str, Any]] = []
    for position, planned in enumerate(plan["results"]):
        if planned["status"] in {"invalid", "unchanged"}:
            results.append(planned)
            continue

        try:
            response = web_api_reconcile_collections(
                user_id,
                planned["item_key"],
                add_collection_keys=planned["requested_add_collection_keys"],
                remove_collection_keys=planned["requested_remove_collection_keys"],
                allow_no_collections=allow_no_collections,
                expected_current_collections=planned["current_collection_keys"],
            )
            planned["status"] = response["status"]
            planned["collection_keys"] = response["collections"]
            planned["web_api_result"] = response
            results.append(planned)
        except ZoteroNotSyncedError as exc:
            planned.update({"status": "not_synced_to_cloud", "error": str(exc)})
            results.append(planned)
            results.extend(
                _not_attempted_reconcile_results(
                    records,
                    position + 1,
                    "stopped because local and cloud Zotero state are not synchronized; "
                    "sync, rescan, and rerun the full batch",
                )
            )
            break
        except ZoteroWriteError as exc:
            planned.update({"status": "unknown", "error": str(exc)})
            results.append(planned)
            results.extend(
                _not_attempted_reconcile_results(
                    records,
                    position + 1,
                    "stopped after a Web API error; rescan and rerun the full batch",
                )
            )
            break

    return {
        "total": len(results),
        "summary": summarize_results(results),
        "results": results,
        "allow_no_collections": allow_no_collections,
        "write_performed": any(
            result.get("status") in {"collections_reconciled", "unknown"}
            for result in results
        ),
    }


MANAGED_PDF_LINK_MODES = {"imported_file", "imported_url"}


def _valid_zotero_key(value: Any, label: str) -> str:
    key = str(value or "").strip().upper()
    if not ZOTERO_KEY_RE.fullmatch(key):
        raise ZoteroWriteError(f"invalid Zotero {label}: {key or '<empty>'}")
    return key


def _is_pdf_attachment(item: dict[str, Any]) -> bool:
    data = item.get("data") or {}
    filename = str(data.get("filename") or "")
    return data.get("itemType") == "attachment" and (
        data.get("contentType") == "application/pdf"
        or filename.lower().endswith(".pdf")
    )


def plan_pdf_attachment_delete(
    collection_key: str,
    *,
    recursive: bool = False,
    limit: int = 1000,
    offset: int = 0,
    page_size: int = 50,
    collections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    key = _valid_zotero_key(collection_key, "collection key")
    if not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ZoteroWriteError("limit must be between 1 and 1000")
    if not isinstance(offset, int) or offset < 0:
        raise ZoteroWriteError("offset must be a non-negative integer")
    if not isinstance(page_size, int) or not 1 <= page_size <= 1000:
        raise ZoteroWriteError("page_size must be between 1 and 1000")

    collection_rows = (
        zotero_local.fetch_all_collections() if collections is None else collections
    )
    collections_by_key = zotero_local.collection_index(collection_rows)
    if key not in collections_by_key:
        raise ZoteroWriteError(f"unknown Zotero collection key: {key}")
    scoped_keys = set(
        zotero_local.descendant_collection_keys(key, collections_by_key)
        if recursive
        else [key]
    )
    parents, _ = zotero_local.collect_collection_items(
        key, collections_by_key, recursive, limit
    )

    results: list[dict[str, Any]] = []
    for parent in parents:
        parent_data = parent.get("data") or {}
        parent_key = item_key(parent).upper()
        if not ZOTERO_KEY_RE.fullmatch(parent_key):
            continue
        parent_collections = list(parent_data.get("collections") or [])
        shared_collections = [
            collection
            for collection in parent_collections
            if collection not in scoped_keys
        ]
        children = (
            zotero_local.zotero_get(f"users/0/items/{quote(parent_key)}/children") or []
        )
        for attachment in children:
            if not _is_pdf_attachment(attachment):
                continue
            attachment_data = attachment.get("data") or {}
            attachment_key = item_key(attachment).upper()
            if not ZOTERO_KEY_RE.fullmatch(attachment_key):
                continue
            local_files = zotero_local.find_pdf_for_attachment(attachment_key)
            grandchildren = (
                zotero_local.zotero_get(
                    f"users/0/items/{quote(attachment_key)}/children"
                )
                or []
            )
            annotation_count = sum(
                (child.get("data") or {}).get("itemType") == "annotation"
                for child in grandchildren
            )
            child_note_count = sum(
                (child.get("data") or {}).get("itemType") == "note"
                for child in grandchildren
            )
            attachment_note_chars = len(str(attachment_data.get("note") or "").strip())
            link_mode = str(attachment_data.get("linkMode") or "")
            blockers: list[str] = []
            if link_mode not in MANAGED_PDF_LINK_MODES:
                blockers.append("linked_or_unsupported_file")
            if len(local_files) != 1:
                blockers.append("local_pdf_file_count_not_one")
            if shared_collections:
                blockers.append("parent_in_other_collections")
            if annotation_count or child_note_count or attachment_note_chars:
                blockers.append("attachment_has_annotations_or_notes")
            results.append(
                {
                    "status": "ready" if not blockers else "blocked",
                    "blockers": blockers,
                    "parent_item_key": parent_key,
                    "parent_title": str(parent_data.get("title") or ""),
                    "parent_collection_keys": parent_collections,
                    "parent_collection_paths": [
                        zotero_local.collection_path(collection, collections_by_key)
                        for collection in parent_collections
                    ],
                    "shared_collection_keys": shared_collections,
                    "attachment_key": attachment_key,
                    "attachment_title": str(attachment_data.get("title") or ""),
                    "filename": str(attachment_data.get("filename") or ""),
                    "link_mode": link_mode,
                    "local_files": [str(path) for path in local_files],
                    "size_bytes": sum(path.stat().st_size for path in local_files),
                    "annotation_count": annotation_count,
                    "child_note_count": child_note_count,
                    "attachment_note_chars": attachment_note_chars,
                }
            )

    total = len(results)
    page = results[offset : offset + page_size]
    return {
        "collection_key": key,
        "collection_path": zotero_local.collection_path(key, collections_by_key),
        "recursive": recursive,
        "parent_items_scanned": len(parents),
        "total": total,
        "summary": summarize_results(results),
        "total_size_bytes": sum(result["size_bytes"] for result in results),
        "ready_size_bytes": sum(
            result["size_bytes"] for result in results if result["status"] == "ready"
        ),
        "offset": offset,
        "page_size": page_size,
        "has_more": offset + len(page) < total,
        "results": page,
        "write_performed": False,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_pdf_attachments(
    planned: list[dict[str, Any]], backup_dir: str
) -> tuple[Path, list[dict[str, Any]]]:
    raw_base = Path(str(backup_dir or "")).expanduser()
    if not raw_base.is_absolute():
        raise ZoteroWriteError("backup_dir must be an absolute path")
    base = raw_base.resolve()
    storage = zotero_local.storage_root().resolve()
    if base == storage or storage in base.parents:
        raise ZoteroWriteError(
            "backup_dir must be outside the Zotero storage directory"
        )
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    batch_dir = base / f"zotero_pdf_delete_{stamp}_{uuid.uuid4().hex[:8]}"
    batch_dir.mkdir(mode=0o700)

    backups: list[dict[str, Any]] = []
    for row in planned:
        source = Path(row["local_files"][0])
        destination_dir = batch_dir / row["attachment_key"]
        destination_dir.mkdir(mode=0o700)
        destination = destination_dir / source.name
        source_hash = _sha256(source)
        shutil.copy2(source, destination)
        backup_hash = _sha256(destination)
        if (
            source_hash != backup_hash
            or source.stat().st_size != destination.stat().st_size
        ):
            raise ZoteroWriteError(
                f"backup verification failed for attachment {row['attachment_key']}"
            )
        backups.append(
            {
                "parent_item_key": row["parent_item_key"],
                "attachment_key": row["attachment_key"],
                "filename": source.name,
                "size_bytes": source.stat().st_size,
                "sha256": source_hash,
                "source_path": str(source),
                "backup_path": str(destination),
            }
        )

    index_path = batch_dir / "backup_index.csv"
    with index_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(backups[0]))
        writer.writeheader()
        writer.writerows(backups)
    index_path.chmod(0o600)
    return batch_dir, backups


def web_api_delete_pdf_attachment(
    user_id: int,
    parent_item_key: str,
    attachment_key: str,
    attachment_version: int,
) -> dict[str, Any]:
    response = web_api_request(
        "DELETE",
        f"users/{user_id}/items/{attachment_key}",
        headers={"If-Unmodified-Since-Version": str(attachment_version)},
        timeout=30.0,
    )
    if response.status_code in {409, 412}:
        raise ZoteroVersionConflictError(
            f"attachment {attachment_key} changed before deletion; no deletion was performed"
        )
    if response.status_code != 204:
        raise web_api_error(response)

    verification = web_api_request(
        "GET", f"users/{user_id}/items/{attachment_key}", timeout=20.0
    )
    if verification.status_code != 404:
        raise ZoteroWriteError(
            f"attachment {attachment_key} DELETE returned 204 but read-back was not 404; "
            "unknown write state. Rescan before retrying."
        )
    web_api_get_item(user_id, parent_item_key)
    return {
        "status": "attachment_deleted",
        "parent_item_key": parent_item_key,
        "attachment_key": attachment_key,
    }


def _normalize_pdf_delete_records(
    records: list[dict[str, Any]],
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ZoteroWriteError("each PDF deletion item must be an object")
        parent_key = _valid_zotero_key(record.get("parent_item_key"), "parent item key")
        attachment_key = _valid_zotero_key(
            record.get("attachment_key"), "attachment key"
        )
        if attachment_key in seen:
            raise ZoteroWriteError(
                f"duplicate attachment_key in deletion batch: {attachment_key}"
            )
        seen.add(attachment_key)
        normalized.append(
            {"parent_item_key": parent_key, "attachment_key": attachment_key}
        )
    return normalized


def _select_pdf_delete_rows(
    normalized: list[dict[str, str]],
    plan: dict[str, Any],
    *,
    allow_shared_parents: bool,
    allow_annotations: bool,
) -> list[dict[str, Any]]:
    by_attachment = {row["attachment_key"]: row for row in plan["results"]}
    planned: list[dict[str, Any]] = []
    for record in normalized:
        row = by_attachment.get(record["attachment_key"])
        if row is None or row["parent_item_key"] != record["parent_item_key"]:
            raise ZoteroWriteError(
                f"attachment {record['attachment_key']} is not a PDF child of parent "
                f"{record['parent_item_key']} in collection {plan['collection_path']}"
            )
        blockers = set(row["blockers"])
        if allow_shared_parents:
            blockers.discard("parent_in_other_collections")
        if allow_annotations:
            blockers.discard("attachment_has_annotations_or_notes")
        if blockers:
            raise ZoteroWriteError(
                f"attachment {row['attachment_key']} is blocked: {sorted(blockers)}"
            )
        planned.append(dict(row))
    return planned


def _verify_pdf_delete_cloud_state(
    planned: list[dict[str, Any]], user_id: int
) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for row in planned:
        cloud_parent = web_api_get_item(user_id, row["parent_item_key"])
        cloud_parent_collections = list(
            (cloud_parent.get("data") or {}).get("collections") or []
        )
        if set(cloud_parent_collections) != set(row["parent_collection_keys"]):
            raise ZoteroNotSyncedError(
                f"local/cloud collection memberships differ for parent {row['parent_item_key']}; "
                "sync Zotero, rescan, and rerun the full batch"
            )
        cloud_attachment = web_api_get_item(user_id, row["attachment_key"])
        cloud_data = cloud_attachment.get("data") or {}
        if (
            not _is_pdf_attachment(cloud_attachment)
            or str(cloud_data.get("parentItem") or "").upper() != row["parent_item_key"]
            or str(cloud_data.get("linkMode") or "") != row["link_mode"]
        ):
            raise ZoteroNotSyncedError(
                f"local/cloud PDF attachment metadata differ for {row['attachment_key']}; "
                "sync Zotero, rescan, and rerun the full batch"
            )
        version = cloud_attachment.get("version", cloud_data.get("version"))
        if not isinstance(version, int) or version < 1:
            raise ZoteroWriteError(
                f"Zotero Web API attachment {row['attachment_key']} has no valid version"
            )
        verified.append({**row, "cloud_version": version})
    return verified


def _pending_pdf_delete_results(
    planned: list[dict[str, Any]], start: int, reason: str
) -> list[dict[str, Any]]:
    return [
        {
            "parent_item_key": pending["parent_item_key"],
            "attachment_key": pending["attachment_key"],
            "status": "not_attempted",
            "reason": reason,
        }
        for pending in planned[start:]
    ]


def _execute_pdf_deletions(
    planned: list[dict[str, Any]],
    backups: list[dict[str, Any]],
    user_id: int,
) -> list[dict[str, Any]]:
    backups_by_key = {backup["attachment_key"]: backup for backup in backups}
    planned_keys = {row["attachment_key"] for row in planned}
    if set(backups_by_key) != planned_keys:
        raise ZoteroWriteError(
            "PDF backup records do not match the planned deletion batch"
        )

    results: list[dict[str, Any]] = []
    for position, row in enumerate(planned):
        backup = backups_by_key[row["attachment_key"]]
        result = {
            "parent_item_key": row["parent_item_key"],
            "attachment_key": row["attachment_key"],
            "filename": row["filename"],
            "size_bytes": row["size_bytes"],
            "backup_path": backup["backup_path"],
            "sha256": backup["sha256"],
        }
        try:
            result.update(
                web_api_delete_pdf_attachment(
                    user_id,
                    row["parent_item_key"],
                    row["attachment_key"],
                    row["cloud_version"],
                )
            )
            results.append(result)
        except ZoteroVersionConflictError as exc:
            result.update({"status": "conflict", "error": str(exc)})
            results.append(result)
            results.extend(
                _pending_pdf_delete_results(
                    planned,
                    position + 1,
                    "stopped after a version conflict; rescan before retrying",
                )
            )
            break
        except ZoteroWriteError as exc:
            result.update({"status": "unknown", "error": str(exc)})
            results.append(result)
            results.extend(
                _pending_pdf_delete_results(
                    planned,
                    position + 1,
                    "stopped after an unknown deletion state; rescan before retrying",
                )
            )
            break
    return results


def execute_pdf_attachment_delete(
    records: list[dict[str, Any]],
    collection_key: str,
    backup_dir: str,
    confirm: bool,
    *,
    recursive: bool = False,
    allow_shared_parents: bool = False,
    allow_annotations: bool = False,
) -> dict[str, Any]:
    if confirm is not True:
        raise ZoteroWriteError("confirm=true is required for PDF attachment deletion")
    if not isinstance(records, list) or not 1 <= len(records) <= 50:
        raise ZoteroWriteError("items must contain between 1 and 50 PDF attachments")

    normalized = _normalize_pdf_delete_records(records)

    plan = plan_pdf_attachment_delete(
        collection_key,
        recursive=recursive,
        limit=1000,
        offset=0,
        page_size=1000,
    )
    planned = _select_pdf_delete_rows(
        normalized,
        plan,
        allow_shared_parents=allow_shared_parents,
        allow_annotations=allow_annotations,
    )

    user_id = _web_api_user_id()
    planned = _verify_pdf_delete_cloud_state(planned, user_id)

    backup_path, backups = _backup_pdf_attachments(planned, backup_dir)
    results = _execute_pdf_deletions(planned, backups, user_id)

    return {
        "collection_key": plan["collection_key"],
        "collection_path": plan["collection_path"],
        "backup_dir": str(backup_path),
        "backup_index": str(backup_path / "backup_index.csv"),
        "total": len(results),
        "summary": summarize_results(results),
        "results": results,
        "write_performed": any(
            row.get("status") in {"attachment_deleted", "unknown"} for row in results
        ),
        "local_sync_pending": any(
            row.get("status") == "attachment_deleted" for row in results
        ),
    }
