"""Discover, validate, and import public Version of Record PDFs for Zotero."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests

from . import (
    workflow_database,
    zotero_attachment,
    zotero_collections,
    zotero_http,
    zotero_local,
    zotero_runtime,
    zotero_translate,
    zotero_web_api,
)

CROSSREF_API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
PMC_IDCONV_API = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
PMC_S3_API = "https://pmc-oa-opendata.s3.amazonaws.com/"
USER_AGENT = "zotero-mcp-local PDF acquisition"
ZOTERO_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
SOURCE_TRUST_RANK = {
    "discovery_only": 1,
    "verified_repository": 2,
    "versioned_metadata": 3,
    "official_publisher": 4,
}
ROUTE_RANK = {
    "openalex": 1,
    "pmc": 2,
    "unpaywall": 3,
    "publisher": 4,
    "crossref": 5,
}
PREPRINT_DOMAINS = {
    "arxiv.org",
    "biorxiv.org",
    "medrxiv.org",
    "researchsquare.com",
}
REJECTED_VERSION_MARKERS = (
    "author manuscript",
    "accepted manuscript",
    "submitted manuscript",
    "journal pre-proof",
    "journal preproof",
    "uncorrected proof",
    "nih public access author manuscript",
)
MAX_REQUESTS_PER_ITEM = 16
MAX_REDIRECTS = 5
MAX_METADATA_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 100 * 1024 * 1024
MIN_PDF_BYTES = 1024
DOWNLOAD_TIMEOUT_SECONDS = 120.0
DOWNLOAD_ATTEMPTS = 2
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
TITLE_MATCH_THRESHOLD = 0.90
SUPPORTED_TEMPLATE_FIELDS = {"year", "publicationTitle", "title", "firstCreator"}
TEMPLATE_TOKEN_RE = re.compile(r"{{\s*([A-Za-z][A-Za-z0-9]*)(.*?)}}", re.DOTALL)
INVALID_FILENAME_RE = re.compile(r'[\\/?*:|"<>]')
ZERO_WIDTH_BIDI_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")


class PdfAcquisitionError(RuntimeError):
    """Expected PDF acquisition failure with actionable context."""


class PdfNetworkError(PdfAcquisitionError):
    """Transient network, rate-limit, or anti-bot failure."""


class PdfTransientNetworkError(PdfNetworkError):
    """Transport failure for which repeating a GET is safe."""


class PdfSyncPendingError(PdfNetworkError):
    """The cloud attachment exists but Zotero local sync has not exposed it yet."""


@dataclass(frozen=True)
class ZoteroPaper:
    item_key: str
    item_type: str
    title: str
    doi: str
    publication_title: str
    publisher: str
    url: str
    date: str
    creators: tuple[dict[str, Any], ...]

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> ZoteroPaper:
        data = item.get("data") or {}
        key = str(data.get("key") or item.get("key") or "").upper()
        if not ZOTERO_KEY_RE.fullmatch(key):
            raise PdfAcquisitionError(f"invalid Zotero item key: {key or '<missing>'}")
        creators = data.get("creators") or []
        return cls(
            item_key=key,
            item_type=str(data.get("itemType") or ""),
            title=str(data.get("title") or "").strip(),
            doi=normalize_doi(str(data.get("DOI") or "")),
            publication_title=str(data.get("publicationTitle") or "").strip(),
            publisher=str(data.get("publisher") or "").strip(),
            url=str(data.get("url") or "").strip(),
            date=str(data.get("date") or item.get("meta", {}).get("parsedDate") or ""),
            creators=tuple(row for row in creators if isinstance(row, dict)),
        )

    @property
    def is_preprint(self) -> bool:
        return self.item_type.casefold() in {"preprint", "manuscript"}


@dataclass(frozen=True)
class PdfCandidate:
    url: str
    route: str
    source_kind: str
    host_type: str
    source_trust: str
    version_kind: str
    access_kind: str
    license: str
    evidence: dict[str, Any]
    final_domain: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PdfCandidate:
        return cls(
            url=str(data.get("url") or ""),
            route=str(data.get("route") or ""),
            source_kind=str(data.get("source_kind") or ""),
            host_type=str(data.get("host_type") or ""),
            source_trust=str(data.get("source_trust") or ""),
            version_kind=str(data.get("version_kind") or "unknown"),
            access_kind=str(data.get("access_kind") or "unknown"),
            license=str(data.get("license") or ""),
            evidence=dict(data.get("evidence") or {}),
            final_domain=str(data.get("final_domain") or ""),
        )


@dataclass(frozen=True)
class PdfAcquisitionDecision:
    item_key: str
    title: str
    doi: str
    item_type: str
    publication_title: str
    publisher: str
    has_english_pdf: bool
    source_attachment_key: str
    state: str
    allowed: bool
    candidate: PdfCandidate | None
    rejected: tuple[dict[str, Any], ...]
    checked_at: str
    next_check_at: str
    last_error: str
    alternates: tuple[PdfCandidate, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidate"] = asdict(self.candidate) if self.candidate else None
        data["rejected"] = list(self.rejected)
        data["alternates"] = [asdict(row) for row in self.alternates]
        return data


@dataclass(frozen=True)
class VerifiedPdf:
    output: Path
    final_url: str
    final_domain: str
    size: int
    pages: int
    sha256: str
    verified_at: str


@dataclass
class RequestBudget:
    maximum: int = MAX_REQUESTS_PER_ITEM
    used: int = 0

    def consume(self, label: str) -> None:
        if self.used >= self.maximum:
            raise PdfNetworkError(
                f"request budget exhausted before {label}: {self.maximum} requests"
            )
        self.used += 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_doi(value: str) -> str:
    cleaned = value.strip().casefold()
    cleaned = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", cleaned)
    return cleaned.strip().rstrip(".")


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))


def title_similarity(left: str, right: str) -> float:
    left_normalized = normalize_title(left)
    right_normalized = normalize_title(right)
    if not left_normalized or not right_normalized:
        return 0.0
    return difflib.SequenceMatcher(None, left_normalized, right_normalized).ratio()


def canonical_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold()
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.casefold(), host + port, path, parsed.query, ""))


def domain_for_url(value: str) -> str:
    return (urlsplit(value).hostname or "").casefold()


def same_domain_family(left: str, right: str) -> bool:
    left = left.casefold().strip(".")
    right = right.casefold().strip(".")
    return bool(left and right) and (
        left == right or left.endswith(f".{right}") or right.endswith(f".{left}")
    )


def is_open_license(value: str) -> bool:
    lowered = " ".join(value.casefold().split())
    return (
        "creativecommons.org" in lowered
        or "creative commons" in lowered
        or re.search(r"\bcc(?:[- ]?(?:by|0))\b", lowered) is not None
        or lowered in {"open access", "open-access"}
    )


def _crossref_license_started(row: dict[str, Any]) -> bool:
    start = row.get("start")
    if not isinstance(start, dict) or not start:
        return True
    timestamp = start.get("timestamp")
    if isinstance(timestamp, (int, float)):
        return timestamp <= time.time() * 1000
    date_time = str(start.get("date-time") or "").strip()
    if date_time:
        try:
            parsed = datetime.fromisoformat(date_time)
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed <= datetime.now(UTC)
    date_parts = start.get("date-parts") or []
    if date_parts and isinstance(date_parts[0], list):
        try:
            values = [int(value) for value in date_parts[0][:3]]
            values.extend([1] * (3 - len(values)))
            parsed = datetime(*values, tzinfo=UTC)
        except (TypeError, ValueError):
            return False
        return parsed <= datetime.now(UTC)
    return False


def _crossref_open_licenses(message: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for row in message.get("license") or []:
        if not isinstance(row, dict) or not _crossref_license_started(row):
            continue
        version = normalize_version(row.get("content-version"))
        if version not in {"published", "unknown"}:
            continue
        url = str(row.get("URL") or "")
        if is_open_license(url):
            values.append(url)
    return values


def _pmc_https_url(value: str) -> str:
    prefix = "s3://pmc-oa-opendata/"
    if not value.startswith(prefix):
        return ""
    return PMC_S3_API + value.removeprefix(prefix)


def _json_ld_license_values(documents: list[str]) -> list[str]:
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.casefold() == "license":
                    if isinstance(child, str):
                        values.append(child)
                    elif isinstance(child, dict):
                        for field in ("url", "@id", "name"):
                            if isinstance(child.get(field), str):
                                values.append(child[field])
                    elif isinstance(child, list):
                        for item in child:
                            collect({"license": item})
                else:
                    collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for document in documents:
        try:
            collect(json.loads(document))
        except (TypeError, ValueError):
            continue
    return values


def normalize_version(value: Any) -> str:
    lowered = str(value or "").strip().casefold().replace("_", "")
    if lowered in {"vor", "publishedversion", "published", "versionofrecord"}:
        return "published"
    if lowered in {"am", "acceptedversion", "accepted", "acceptedmanuscript"}:
        return "accepted"
    if lowered in {"submittedversion", "submitted", "submittedmanuscript"}:
        return "submitted"
    if lowered in {"preprint", "ao", "authororiginal"}:
        return "preprint"
    return "unknown"


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(element: ET.Element) -> str:
    return " ".join(text.strip() for text in element.itertext() if text.strip())


class PublisherPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.license_links: list[str] = []
        self.json_ld: list[str] = []
        self._json_buffer: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.casefold(): value or "" for name, value in attrs}
        if tag.casefold() == "meta":
            key = (values.get("name") or values.get("property") or "").casefold()
            content = values.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag.casefold() == "link" and "license" in values.get("rel", "").casefold():
            href = values.get("href", "").strip()
            if href:
                self.license_links.append(href)
        elif (
            tag.casefold() == "script"
            and values.get("type", "").casefold() == "application/ld+json"
        ):
            self._json_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._json_buffer is not None:
            self.json_ld.append("".join(self._json_buffer))
            self._json_buffer = None

    def handle_data(self, data: str) -> None:
        if self._json_buffer is not None:
            self._json_buffer.append(data)


class PublicHttpClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        per_host_interval: float = 0.5,
        timeout: float = 20.0,
        sleeper: Any = time.sleep,
        clock: Any = time.monotonic,
    ) -> None:
        self.session = zotero_http.routed_session(
            zotero_http.RouteType.NORMAL,
            session,
        )
        self.session.max_redirects = MAX_REDIRECTS
        self.per_host_interval = max(0.0, per_host_interval)
        self.timeout = timeout
        self.sleeper = sleeper
        self.clock = clock
        self.last_request: dict[str, float] = {}

    def _wait(self, url: str) -> None:
        host = domain_for_url(url)
        previous = self.last_request.get(host)
        if previous is not None:
            delay = self.per_host_interval - (self.clock() - previous)
            if delay > 0:
                self.sleeper(delay)
        self.last_request[host] = self.clock()

    def request(
        self,
        method: str,
        url: str,
        *,
        budget: RequestBudget,
        label: str,
        stream: bool = False,
        timeout: float | tuple[float, float] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        canonical = canonical_url(url)
        if not canonical or urlsplit(canonical).scheme != "https":
            raise PdfAcquisitionError(f"{label} URL must use HTTPS: {url}")
        budget.consume(label)
        if hasattr(self.session, "cookies"):
            self.session.cookies.clear()
        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, application/xml, text/html, application/pdf",
        }
        if headers:
            request_headers.update(headers)
        current_method = method.upper()
        current_url = canonical
        current_params = params
        wiley_chain = (
            zotero_http.external_route(current_url)
            is zotero_http.RouteType.PROXY_REQUIRED
        )
        try:
            for redirect_count in range(MAX_REDIRECTS + 1):
                self._wait(current_url)
                route = (
                    zotero_http.RouteType.PROXY_REQUIRED
                    if wiley_chain
                    else zotero_http.external_route(current_url)
                )
                use_wiley_proxy = route is zotero_http.RouteType.PROXY_REQUIRED
                try:
                    response = zotero_http.session_request(
                        self.session,
                        current_method,
                        current_url,
                        route=route,
                        params=current_params,
                        headers=request_headers,
                        timeout=timeout or self.timeout,
                        allow_redirects=False,
                        stream=stream,
                    )
                except requests.RequestException as exc:
                    prefix = "Wiley proxy is unavailable" if use_wiley_proxy else label
                    raise PdfTransientNetworkError(
                        f"{prefix}: {type(exc).__name__}: {exc}"
                    ) from exc

                location = response.headers.get("Location")
                if response.status_code in REDIRECT_STATUS_CODES and location:
                    if redirect_count >= MAX_REDIRECTS:
                        response.close()
                        raise PdfNetworkError(
                            f"{label} exceeded {MAX_REDIRECTS} redirects"
                        )
                    next_url = canonical_url(urljoin(current_url, location))
                    response.close()
                    if not next_url or urlsplit(next_url).scheme != "https":
                        raise PdfAcquisitionError(
                            f"{label} redirected to a non-HTTPS URL"
                        )
                    wiley_chain = wiley_chain or (
                        zotero_http.external_route(next_url)
                        is zotero_http.RouteType.PROXY_REQUIRED
                    )
                    current_url = next_url
                    current_params = None
                    if response.status_code == 303 or (
                        response.status_code in {301, 302}
                        and current_method not in {"GET", "HEAD"}
                    ):
                        current_method = "GET"
                    continue

                if response.status_code in {401, 403, 429, 500, 502, 503, 504}:
                    response.close()
                    raise PdfNetworkError(
                        f"{label} returned HTTP {response.status_code}"
                    )
                if not response.ok:
                    response.close()
                    raise PdfAcquisitionError(
                        f"{label} returned HTTP {response.status_code}"
                    )
                return response
        finally:
            if hasattr(self.session, "cookies"):
                self.session.cookies.clear()

        raise PdfNetworkError(f"{label} exceeded {MAX_REDIRECTS} redirects")

    def json(
        self,
        url: str,
        *,
        budget: RequestBudget,
        label: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.request("GET", url, budget=budget, label=label, params=params)
        if len(response.content) > MAX_METADATA_BYTES:
            raise PdfAcquisitionError(f"{label} response is too large")
        try:
            data = response.json()
        except ValueError as exc:
            raise PdfAcquisitionError(f"{label} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise PdfAcquisitionError(f"{label} returned non-object JSON")
        return data

    def text(
        self,
        url: str,
        *,
        budget: RequestBudget,
        label: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        response = self.request("GET", url, budget=budget, label=label, params=params)
        if len(response.content) > MAX_METADATA_BYTES:
            raise PdfAcquisitionError(f"{label} response is too large")
        return response.text, str(response.url)


def _candidate(
    paper: ZoteroPaper,
    *,
    url: str,
    route: str,
    source_kind: str,
    host_type: str,
    source_trust: str,
    version_kind: str,
    access_kind: str,
    license_value: str,
    source_doi: str,
    source_title: str,
    evidence: dict[str, Any],
) -> PdfCandidate | None:
    normalized_url = canonical_url(url)
    if not normalized_url:
        return None
    source_doi = normalize_doi(source_doi)
    doi_match = bool(paper.doi and source_doi and paper.doi == source_doi)
    title_score = title_similarity(paper.title, source_title)
    details = dict(evidence)
    details.update(
        {
            "source_doi": source_doi,
            "source_title": source_title,
            "doi_match": doi_match,
            "title_score": round(title_score, 4),
            "routes": [route],
        }
    )
    return PdfCandidate(
        url=normalized_url,
        route=route,
        source_kind=source_kind,
        host_type=host_type or "unknown",
        source_trust=source_trust,
        version_kind=version_kind,
        access_kind=access_kind,
        license=license_value,
        evidence=details,
        final_domain=domain_for_url(normalized_url),
    )


def _merge_candidate(left: PdfCandidate, right: PdfCandidate) -> PdfCandidate:
    routes = list(
        dict.fromkeys(
            [*left.evidence.get("routes", []), *right.evidence.get("routes", [])]
        )
    )
    versions = {left.version_kind, right.version_kind} - {"unknown"}
    version_conflict = len(versions) > 1
    version_kind = next(iter(versions)) if len(versions) == 1 else "unknown"
    source = max(
        (left, right),
        key=lambda row: (
            SOURCE_TRUST_RANK.get(row.source_trust, 0),
            ROUTE_RANK.get(row.route, 0),
        ),
    )
    evidence = {
        **left.evidence,
        **right.evidence,
        "routes": routes,
        "version_conflict": version_conflict,
        "doi_match": bool(left.evidence.get("doi_match"))
        or bool(right.evidence.get("doi_match")),
        "title_score": max(
            float(left.evidence.get("title_score") or 0),
            float(right.evidence.get("title_score") or 0),
        ),
    }
    return PdfCandidate(
        url=left.url,
        route=source.route,
        source_kind=source.source_kind,
        host_type=(
            "publisher"
            if "publisher" in {left.host_type, right.host_type}
            else source.host_type
        ),
        source_trust=source.source_trust,
        version_kind=version_kind,
        access_kind=(
            "public_open"
            if "public_open" in {left.access_kind, right.access_kind}
            else source.access_kind
        ),
        license=left.license or right.license,
        evidence=evidence,
        final_domain=source.final_domain,
    )


def merge_candidates(candidates: list[PdfCandidate]) -> list[PdfCandidate]:
    merged: dict[str, PdfCandidate] = {}
    for candidate in candidates:
        existing = merged.get(candidate.url)
        merged[candidate.url] = (
            _merge_candidate(existing, candidate) if existing else candidate
        )
    return list(merged.values())


def candidate_rejection(paper: ZoteroPaper, candidate: PdfCandidate) -> str:
    if urlsplit(candidate.url).scheme != "https":
        return "candidate URL is not HTTPS"
    if paper.is_preprint:
        if candidate.version_kind != "preprint":
            return f"preprint item requires official preprint PDF, got {candidate.version_kind}"
        if not any(
            candidate.final_domain == domain
            or candidate.final_domain.endswith(f".{domain}")
            for domain in PREPRINT_DOMAINS
        ):
            return "preprint candidate is not on a supported official platform"
    elif candidate.version_kind != "published":
        return f"version is {candidate.version_kind}, not published"
    if candidate.access_kind != "public_open":
        return f"access is {candidate.access_kind}, not public_open"
    if paper.doi and not candidate.evidence.get("doi_match"):
        return "candidate DOI does not match Zotero DOI"
    source_title = str(candidate.evidence.get("source_title") or "")
    if not source_title:
        return "candidate source title is missing"
    if float(candidate.evidence.get("title_score") or 0) < TITLE_MATCH_THRESHOLD:
        return "candidate title does not match Zotero title"
    routes = set(candidate.evidence.get("routes") or [candidate.route])
    if candidate.route == "openalex" and routes == {"openalex"}:
        return "OpenAlex alone is discovery evidence, not VoR proof"
    if candidate.source_trust == "discovery_only":
        return "source is discovery-only evidence"
    if candidate.evidence.get("version_conflict"):
        return "sources disagree about candidate version"
    return ""


def candidate_sort_key(candidate: PdfCandidate) -> tuple[int, int, str]:
    return (
        -SOURCE_TRUST_RANK.get(candidate.source_trust, 0),
        -ROUTE_RANK.get(candidate.route, 0),
        candidate.url,
    )


def existing_source_attachment_key(item: dict[str, Any]) -> str:
    attachments = [
        row
        for row in zotero_local.pdf_attachments_for_item(item)
        if not zotero_local.has_translation_marker(row["title"], row["filename"])
    ]
    exact_pdf = next(
        (row for row in attachments if row["title"].strip().casefold() == "pdf"),
        None,
    )
    primary = next((row for row in attachments if row["primary"]), None)
    selected = exact_pdf or primary
    if selected:
        return str(selected["key"]).upper()
    try:
        selected = zotero_local.english_pdf_attachment_for_item(item)
    except SystemExit:
        return ""
    data = selected.get("data") or {}
    return str(
        selected.get("key") or data.get("key") or selected.get("attachment_key") or ""
    ).upper()


class PdfDiscoveryService:
    def __init__(
        self,
        http: PublicHttpClient | None = None,
        *,
        unpaywall_email: str | None = None,
    ) -> None:
        self.http = http or PublicHttpClient()
        self.unpaywall_email = (
            unpaywall_email
            or os.environ.get("UNPAYWALL_EMAIL", "").strip()
            or zotero_runtime.config_string("pdf_acquisition", "unpaywall_email")
            or ""
        )

    def _crossref(
        self, paper: ZoteroPaper, budget: RequestBudget
    ) -> tuple[list[PdfCandidate], list[str]]:
        if not paper.doi:
            return [], []
        data = self.http.json(
            f"{CROSSREF_API}/{quote(paper.doi, safe='')}",
            budget=budget,
            label="Crossref",
        )
        message = data.get("message") or {}
        if not isinstance(message, dict):
            raise PdfAcquisitionError("Crossref returned invalid work metadata")
        source_title = str((message.get("title") or [""])[0] or "")
        source_doi = str(message.get("DOI") or paper.doi)
        licenses = _crossref_open_licenses(message)
        open_license = next(iter(licenses), "")
        candidates: list[PdfCandidate] = []
        for link in message.get("link") or []:
            if not isinstance(link, dict):
                continue
            url = str(link.get("URL") or "")
            content_type = str(link.get("content-type") or "").casefold()
            if not url or (
                "pdf" not in content_type and not url.casefold().endswith(".pdf")
            ):
                continue
            row = _candidate(
                paper,
                url=url,
                route="crossref",
                source_kind="crossref_link",
                host_type="publisher",
                source_trust="versioned_metadata",
                version_kind=normalize_version(link.get("content-version")),
                access_kind="public_open" if open_license else "unknown",
                license_value=open_license,
                source_doi=source_doi,
                source_title=source_title,
                evidence={
                    "content_version": str(link.get("content-version") or ""),
                    "content_type": content_type,
                    "publisher": str(message.get("publisher") or ""),
                    "license_urls": licenses,
                },
            )
            if row:
                candidates.append(row)
        article_urls = [
            str(value)
            for value in (
                message.get("URL"),
                ((message.get("resource") or {}).get("primary") or {}).get("URL"),
            )
            if value
        ]
        return candidates, article_urls

    def _unpaywall(
        self, paper: ZoteroPaper, budget: RequestBudget
    ) -> list[PdfCandidate]:
        if not paper.doi or not self.unpaywall_email:
            return []
        data = self.http.json(
            f"{UNPAYWALL_API}/{quote(paper.doi, safe='')}",
            budget=budget,
            label="Unpaywall",
            params={"email": self.unpaywall_email},
        )
        source_title = str(data.get("title") or "")
        source_doi = str(data.get("doi") or paper.doi)
        locations = []
        best = data.get("best_oa_location")
        if isinstance(best, dict):
            locations.append(best)
        locations.extend(
            row for row in data.get("oa_locations") or [] if isinstance(row, dict)
        )
        candidates: list[PdfCandidate] = []
        seen: set[str] = set()
        for location in locations:
            url = str(location.get("url_for_pdf") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            license_value = str(location.get("license") or "")
            row = _candidate(
                paper,
                url=url,
                route="unpaywall",
                source_kind="unpaywall_location",
                host_type=str(location.get("host_type") or "unknown"),
                source_trust="versioned_metadata",
                version_kind=normalize_version(location.get("version")),
                access_kind="public_open" if data.get("is_oa") else "unknown",
                license_value=license_value,
                source_doi=source_doi,
                source_title=source_title,
                evidence={
                    "version": str(location.get("version") or ""),
                    "host_type": str(location.get("host_type") or ""),
                    "is_oa": bool(data.get("is_oa")),
                    "license": license_value,
                },
            )
            if row:
                candidates.append(row)
        return candidates

    def _openalex(
        self, paper: ZoteroPaper, budget: RequestBudget
    ) -> list[PdfCandidate]:
        if not paper.doi:
            return []
        data = self.http.json(
            f"{OPENALEX_API}/{quote('https://doi.org/' + paper.doi, safe='')}",
            budget=budget,
            label="OpenAlex",
        )
        source_title = str(data.get("title") or "")
        source_doi = str(data.get("doi") or paper.doi)
        candidates: list[PdfCandidate] = []
        for location in data.get("locations") or []:
            if not isinstance(location, dict):
                continue
            url = str(location.get("pdf_url") or "")
            if not url:
                continue
            source = location.get("source") or {}
            row = _candidate(
                paper,
                url=url,
                route="openalex",
                source_kind="openalex_location",
                host_type=str(
                    location.get("landing_page_url") and source.get("type") or "unknown"
                ),
                source_trust="discovery_only",
                version_kind=normalize_version(location.get("version")),
                access_kind="public_open" if location.get("is_oa") else "unknown",
                license_value=str(location.get("license") or ""),
                source_doi=source_doi,
                source_title=source_title,
                evidence={
                    "version": str(location.get("version") or ""),
                    "is_oa": bool(location.get("is_oa")),
                    "source_type": str(source.get("type") or ""),
                },
            )
            if row:
                candidates.append(row)
        return candidates

    def _pmc(self, paper: ZoteroPaper, budget: RequestBudget) -> list[PdfCandidate]:
        if not paper.doi:
            return []
        idconv = self.http.json(
            PMC_IDCONV_API,
            budget=budget,
            label="PMC ID Converter",
            params={
                "ids": paper.doi,
                "format": "json",
                "versions": "yes",
                "tool": "zotero-mcp-local",
            },
        )
        records = idconv.get("records") or []
        if not records or not isinstance(records[0], dict):
            return []
        record = records[0]
        pmcid = str(record.get("pmcid") or "").upper()
        if not re.fullmatch(r"PMC\d+", pmcid):
            return []
        versions_text, _ = self.http.text(
            PMC_S3_API,
            budget=budget,
            label="PMC versions",
            params={"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"},
        )
        try:
            versions_root = ET.fromstring(versions_text)
        except ET.ParseError as exc:
            raise PdfAcquisitionError("PMC S3 returned invalid version XML") from exc
        prefixes = {
            _xml_text(element)
            for element in versions_root.iter()
            if _xml_local_name(element.tag) == "Prefix"
            and re.fullmatch(rf"{pmcid}\.\d+/", _xml_text(element))
        }
        ordered = sorted(
            prefixes,
            key=lambda value: int(value.rstrip("/").rsplit(".", 1)[1]),
            reverse=True,
        )
        candidates: list[PdfCandidate] = []
        for prefix in ordered:
            stem = prefix.rstrip("/")
            metadata = self.http.json(
                f"{PMC_S3_API}metadata/{stem}.json",
                budget=budget,
                label="PMC metadata",
            )
            manuscript = metadata.get("is_manuscript")
            mid = str(metadata.get("mid") or "")
            if manuscript is True or mid:
                version_kind = "accepted"
            elif manuscript is False:
                version_kind = "published"
            else:
                version_kind = "unknown"
            license_value = str(metadata.get("license_code") or "")
            is_open = metadata.get("is_pmc_openaccess") is True and is_open_license(
                license_value
            )
            pdf_url = _pmc_https_url(str(metadata.get("pdf_url") or ""))
            if metadata.get("is_retracted") is True:
                pdf_url = ""
            row = _candidate(
                paper,
                url=pdf_url,
                route="pmc",
                source_kind="pmc_cloud_dataset",
                host_type="repository",
                source_trust="verified_repository",
                version_kind=version_kind,
                access_kind="public_open" if is_open and pdf_url else "unknown",
                license_value=license_value,
                source_doi=str(metadata.get("doi") or record.get("doi") or paper.doi),
                source_title=str(metadata.get("title") or ""),
                evidence={
                    "pmcid": pmcid,
                    "version": metadata.get("version"),
                    "mid": mid,
                    "is_manuscript": manuscript,
                    "is_pmc_openaccess": metadata.get("is_pmc_openaccess"),
                    "is_retracted": metadata.get("is_retracted"),
                    "license_code": license_value,
                },
            )
            if row:
                candidates.append(row)
        return candidates

    def _publisher(
        self,
        paper: ZoteroPaper,
        url: str,
        budget: RequestBudget,
        *,
        trusted_landing_page: bool,
    ) -> list[PdfCandidate]:
        html, final_url = self.http.text(
            url,
            budget=budget,
            label="publisher article page",
        )
        parser = PublisherPageParser()
        parser.feed(html)
        pdf_url = parser.meta.get("citation_pdf_url", "")
        if not pdf_url:
            return []
        pdf_url = urljoin(final_url, pdf_url)
        source_doi = parser.meta.get("citation_doi", "")
        source_title = parser.meta.get("citation_title", "")
        license_values = [
            parser.meta.get("dc.rights", ""),
            parser.meta.get("dc.rights.license", ""),
            parser.meta.get("citation_license", ""),
            *parser.license_links,
            *_json_ld_license_values(parser.json_ld),
        ]
        license_value = next(
            (value for value in license_values if is_open_license(value)), ""
        )
        page_domain = domain_for_url(final_url)
        is_preprint = paper.is_preprint and any(
            page_domain == domain or page_domain.endswith(f".{domain}")
            for domain in PREPRINT_DOMAINS
        )
        official_source = trusted_landing_page or is_preprint
        row = _candidate(
            paper,
            url=pdf_url,
            route="publisher",
            source_kind="publisher_page",
            host_type="publisher",
            source_trust=(
                "official_publisher" if official_source else "discovery_only"
            ),
            version_kind="preprint" if is_preprint else "published",
            access_kind="public_open" if license_value else "unknown",
            license_value=license_value,
            source_doi=source_doi,
            source_title=source_title,
            evidence={
                "article_url": final_url,
                "page_domain": page_domain,
                "open_license_evidence": license_value,
            },
        )
        return [row] if row else []

    def discover(
        self, paper: ZoteroPaper
    ) -> tuple[list[PdfCandidate], list[str], list[str]]:
        budget = RequestBudget()
        candidates: list[PdfCandidate] = []
        transient_errors: list[str] = []
        discovery_errors: list[str] = []
        article_urls: dict[str, bool] = {paper.url: False} if paper.url else {}
        adapters = []
        try:
            crossref_candidates, crossref_urls = self._crossref(paper, budget)
            candidates.extend(crossref_candidates)
            for url in crossref_urls:
                article_urls[url] = True
        except PdfNetworkError as exc:
            transient_errors.append(str(exc))
        except PdfAcquisitionError as exc:
            discovery_errors.append(str(exc))
        adapters.extend((self._unpaywall, self._openalex, self._pmc))
        for adapter in adapters:
            try:
                candidates.extend(adapter(paper, budget))
            except PdfNetworkError as exc:
                transient_errors.append(str(exc))
            except PdfAcquisitionError as exc:
                discovery_errors.append(str(exc))
        for article_url, trusted_landing_page in list(article_urls.items())[:3]:
            if not canonical_url(article_url):
                continue
            try:
                candidates.extend(
                    self._publisher(
                        paper,
                        article_url,
                        budget,
                        trusted_landing_page=trusted_landing_page,
                    )
                )
            except PdfNetworkError as exc:
                transient_errors.append(str(exc))
            except PdfAcquisitionError as exc:
                discovery_errors.append(str(exc))
        return merge_candidates(candidates), transient_errors, discovery_errors

    def plan_item(
        self,
        item: dict[str, Any],
        *,
        checked_at: str | None = None,
    ) -> PdfAcquisitionDecision:
        paper = ZoteroPaper.from_item(item)
        observed_at = checked_at or utc_now()
        source_key = existing_source_attachment_key(item)
        if source_key:
            return PdfAcquisitionDecision(
                item_key=paper.item_key,
                title=paper.title,
                doi=paper.doi,
                item_type=paper.item_type,
                publication_title=paper.publication_title,
                publisher=paper.publisher,
                has_english_pdf=True,
                source_attachment_key=source_key,
                state="not_needed",
                allowed=False,
                candidate=None,
                rejected=(),
                checked_at=observed_at,
                next_check_at="",
                last_error="",
            )
        candidates, network_errors, discovery_errors = self.discover(paper)
        accepted: list[PdfCandidate] = []
        rejected: list[dict[str, Any]] = []
        for candidate in candidates:
            reason = candidate_rejection(paper, candidate)
            if reason:
                rejected.append({**asdict(candidate), "rejection_reason": reason})
            else:
                accepted.append(candidate)
        accepted.sort(key=candidate_sort_key)
        selected = accepted[0] if accepted else None
        alternates = tuple(accepted[1:])
        next_check = ""
        if selected:
            if selected.version_kind == "preprint":
                state = "eligible_official_preprint"
            elif selected.route == "pmc":
                state = "eligible_pmc_vor"
            else:
                state = "eligible_publisher_vor"
        elif network_errors:
            state = "blocked"
            next_check = (datetime.now(UTC) + timedelta(days=1)).isoformat(
                timespec="seconds"
            )
        elif any(
            row.get("version_kind") in {"accepted", "submitted", "unknown"}
            for row in rejected
        ):
            state = "manual_version_unproven"
            next_check = (datetime.now(UTC) + timedelta(days=30)).isoformat(
                timespec="seconds"
            )
        else:
            state = "manual_no_vor_found"
            next_check = (datetime.now(UTC) + timedelta(days=30)).isoformat(
                timespec="seconds"
            )
        return PdfAcquisitionDecision(
            item_key=paper.item_key,
            title=paper.title,
            doi=paper.doi,
            item_type=paper.item_type,
            publication_title=paper.publication_title,
            publisher=paper.publisher,
            has_english_pdf=False,
            source_attachment_key="",
            state=state,
            allowed=selected is not None,
            candidate=selected,
            rejected=tuple(rejected),
            checked_at=observed_at,
            next_check_at=next_check,
            last_error="; ".join([*network_errors, *discovery_errors]),
            alternates=alternates,
        )

    def plan(self, items: list[dict[str, Any]]) -> list[PdfAcquisitionDecision]:
        return [self.plan_item(item) for item in items]


def plan_pdf_acquisition(
    items: list[dict[str, Any]],
    *,
    service: PdfDiscoveryService | None = None,
) -> list[PdfAcquisitionDecision]:
    if not isinstance(items, list) or not items:
        raise PdfAcquisitionError("items must be a non-empty list")
    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        paper = ZoteroPaper.from_item(item)
        unique.setdefault(paper.item_key, item)
    return (service or PdfDiscoveryService()).plan(list(unique.values()))


def _decision_database_record(decision: PdfAcquisitionDecision) -> dict[str, Any]:
    evidence = {
        "candidate": asdict(decision.candidate) if decision.candidate else None,
        "alternates": [asdict(row) for row in decision.alternates],
        "rejected": list(decision.rejected),
    }
    return {
        "item_key": decision.item_key,
        "state": decision.state,
        "candidate_url": decision.candidate.url if decision.candidate else None,
        "source_kind": decision.candidate.source_kind if decision.candidate else None,
        "version_kind": decision.candidate.version_kind if decision.candidate else None,
        "access_kind": decision.candidate.access_kind if decision.candidate else None,
        "evidence_json": json.dumps(
            evidence, ensure_ascii=False, separators=(",", ":")
        ),
        "checked_at": decision.checked_at,
        "next_check_at": decision.next_check_at or None,
        "downloaded_at": None,
        "last_error": decision.last_error,
    }


def save_decisions(
    decisions: list[PdfAcquisitionDecision], path: Path | None = None
) -> None:
    workflow_database.save_pdf_acquisition_records(
        [_decision_database_record(row) for row in decisions], path
    )


def _collection_item_keys(
    reference: str, *, recursive: bool, limit: int
) -> tuple[dict[str, Any], list[str]]:
    collections = zotero_local.fetch_all_collections()
    if ZOTERO_KEY_RE.fullmatch(reference.upper()):
        request = {"key": reference.upper()}
    elif ">" in reference:
        request = {"path": reference}
    else:
        request = {"name": reference}
    resolved = zotero_collections.resolve_collection(request, collections=collections)
    listing = zotero_local.list_collection_items(
        resolved["key"],
        recursive=recursive,
        limit=limit,
        collections=collections,
    )
    return resolved, list(dict.fromkeys(str(row["key"]) for row in listing["items"]))


def scan_collection(
    collection: str,
    *,
    recursive: bool = False,
    missing_only: bool = False,
    limit: int = 1000,
    database_path: Path | None = None,
    service: PdfDiscoveryService | None = None,
) -> dict[str, Any]:
    if limit < 1:
        raise PdfAcquisitionError("limit must be at least 1")
    resolved, keys = _collection_item_keys(collection, recursive=recursive, limit=limit)
    items = [zotero_local.get_item(key) for key in keys]
    decisions = plan_pdf_acquisition(items, service=service)
    save_decisions(decisions, database_path)
    visible = [row for row in decisions if not missing_only or not row.has_english_pdf]
    return {
        "collection": resolved,
        "recursive": recursive,
        "missing_only": missing_only,
        "scanned": len(decisions),
        "returned": len(visible),
        "states": {
            state: sum(row.state == state for row in decisions)
            for state in sorted({row.state for row in decisions})
        },
        "items": [row.to_dict() for row in visible],
    }


def acquisition_status(item_key: str, path: Path | None = None) -> dict[str, Any]:
    key = str(item_key or "").strip().upper()
    if not ZOTERO_KEY_RE.fullmatch(key):
        raise PdfAcquisitionError(f"invalid Zotero item key: {item_key}")
    record = workflow_database.pdf_acquisition_record(key, path)
    if record is None:
        raise PdfAcquisitionError(f"no PDF acquisition state for item: {key}")
    record["evidence"] = json.loads(record.pop("evidence_json"))
    return record


def _pdf_tool(name: str) -> str:
    configured = zotero_runtime.configured_command(
        "pdf_acquisition", f"{name}_command", name.upper(), name
    )
    if not configured:
        raise PdfAcquisitionError(f"required command not found: {name}")
    return configured


def _run_pdfinfo(path: Path) -> tuple[int, str]:
    result = subprocess.run(
        [_pdf_tool("pdfinfo"), str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise PdfAcquisitionError(
            f"pdfinfo failed with code {result.returncode}: {result.stderr.strip()[:300]}"
        )
    match = re.search(r"^Pages:\s*(\d+)\s*$", result.stdout, flags=re.MULTILINE)
    if not match or int(match.group(1)) < 1:
        raise PdfAcquisitionError("pdfinfo returned no positive page count")
    return int(match.group(1)), result.stdout


def _extract_pdf_text(path: Path, pages: int) -> str:
    result = subprocess.run(
        [
            _pdf_tool("pdftotext"),
            "-f",
            "1",
            "-l",
            str(min(pages, 5)),
            "-layout",
            str(path),
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise PdfAcquisitionError(
            f"pdftotext failed with code {result.returncode}: {result.stderr.strip()[:300]}"
        )
    return result.stdout


def validate_downloaded_pdf(path: Path, paper: ZoteroPaper) -> tuple[int, str]:
    size = path.stat().st_size if path.is_file() else 0
    if size < MIN_PDF_BYTES:
        raise PdfAcquisitionError(f"downloaded PDF is too small: {size} bytes")
    with path.open("rb") as handle:
        if b"%PDF-" not in handle.read(1024):
            raise PdfAcquisitionError("downloaded response is not a PDF")
    pages, _ = _run_pdfinfo(path)
    text = _extract_pdf_text(path, pages)
    lowered = text.casefold()
    marker = next((value for value in REJECTED_VERSION_MARKERS if value in lowered), "")
    if marker:
        raise PdfAcquisitionError(f"PDF contains rejected version marker: {marker}")
    doi_match = bool(paper.doi and paper.doi in re.sub(r"\s+", "", lowered))
    normalized_text = normalize_title(text[:20000])
    normalized_paper_title = normalize_title(paper.title)
    title_match = bool(
        normalized_paper_title and normalized_paper_title in normalized_text
    )
    if not doi_match and not title_match:
        raise PdfAcquisitionError("PDF DOI/title does not match Zotero item")
    return pages, text


def fetch_verified_pdf(
    decision: PdfAcquisitionDecision,
    destination: Path,
    *,
    http: PublicHttpClient | None = None,
) -> VerifiedPdf:
    if not decision.allowed or decision.candidate is None:
        raise PdfAcquisitionError(
            f"item is not eligible for automatic download: {decision.item_key}"
        )
    candidate = decision.candidate
    paper = ZoteroPaper(
        item_key=decision.item_key,
        item_type=decision.item_type,
        title=decision.title,
        doi=decision.doi,
        publication_title=decision.publication_title,
        publisher=decision.publisher,
        url="",
        date="",
        creators=(),
    )
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = http or PublicHttpClient(per_host_interval=0)
    budget = RequestBudget(maximum=DOWNLOAD_ATTEMPTS)
    last_error: PdfNetworkError | None = None
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            return _fetch_verified_pdf_once(
                candidate,
                paper,
                destination,
                client=client,
                budget=budget,
            )
        except PdfTransientNetworkError as exc:
            last_error = exc
            if attempt + 1 >= DOWNLOAD_ATTEMPTS:
                raise
    assert last_error is not None
    raise last_error


def _fetch_verified_pdf_once(
    candidate: PdfCandidate,
    paper: ZoteroPaper,
    destination: Path,
    *,
    client: PublicHttpClient,
    budget: RequestBudget,
) -> VerifiedPdf:
    temporary = destination.with_name(f".{destination.name}.part")
    started = time.monotonic()
    response: requests.Response | None = None
    try:
        response = client.request(
            "GET",
            candidate.url,
            budget=budget,
            label="PDF download",
            stream=True,
            timeout=(15.0, 60.0),
            headers={"Accept": "application/pdf"},
        )
        final_url = canonical_url(str(response.url))
        if not final_url or urlsplit(final_url).scheme != "https":
            raise PdfAcquisitionError("PDF download redirected to a non-HTTPS URL")
        final_domain = domain_for_url(final_url)
        if candidate.final_domain and not same_domain_family(
            candidate.final_domain, final_domain
        ):
            raise PdfAcquisitionError(
                "PDF download redirected outside the verified source domain: "
                f"{candidate.final_domain} -> {final_domain}"
            )
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared = int(content_length)
            except ValueError as exc:
                raise PdfAcquisitionError(
                    "PDF download returned invalid Content-Length"
                ) from exc
            if declared > MAX_PDF_BYTES:
                raise PdfAcquisitionError(f"PDF exceeds {MAX_PDF_BYTES} byte limit")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        raise PdfAcquisitionError(
                            f"PDF exceeds {MAX_PDF_BYTES} byte limit"
                        )
                    if time.monotonic() - started > DOWNLOAD_TIMEOUT_SECONDS:
                        raise PdfTransientNetworkError(
                            "PDF download exceeded total timeout"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except requests.RequestException as exc:
            raise PdfTransientNetworkError(
                f"PDF download stream failed: {type(exc).__name__}: {exc}"
            ) from exc
        pages, _ = validate_downloaded_pdf(temporary, paper)
        os.replace(temporary, destination)
        if os.name != "nt":
            os.chmod(destination, 0o600)
        return VerifiedPdf(
            output=destination,
            final_url=final_url,
            final_domain=final_domain,
            size=size,
            pages=pages,
            sha256=digest.hexdigest(),
            verified_at=utc_now(),
        )
    finally:
        if response is not None:
            response.close()
        temporary.unlink(missing_ok=True)


def _template_parts(template: str) -> list[tuple[str, dict[str, str]]]:
    parts: list[tuple[str, dict[str, str]]] = []
    position = 0
    for match in TEMPLATE_TOKEN_RE.finditer(template):
        if template[position : match.start()].strip():
            raise PdfAcquisitionError(
                "attachment rename template contains unsupported literal text"
            )
        field_name = match.group(1)
        if field_name not in SUPPORTED_TEMPLATE_FIELDS:
            raise PdfAcquisitionError(
                f"unsupported attachment rename field: {field_name}"
            )
        try:
            tokens = shlex.split(match.group(2).strip())
        except ValueError as exc:
            raise PdfAcquisitionError(
                f"invalid attachment rename template: {exc}"
            ) from exc
        options: dict[str, str] = {}
        for token in tokens:
            if "=" not in token:
                raise PdfAcquisitionError(
                    f"unsupported attachment rename token: {token}"
                )
            name, value = token.split("=", 1)
            if name in options:
                raise PdfAcquisitionError(f"duplicate attachment rename option: {name}")
            options[name] = value
        allowed = {"suffix"}
        if field_name == "title":
            allowed.add("truncate")
        unknown = set(options) - allowed
        if unknown:
            raise PdfAcquisitionError(
                f"unsupported {field_name} options: {', '.join(sorted(unknown))}"
            )
        if "truncate" in options:
            try:
                if int(options["truncate"]) < 1:
                    raise ValueError
            except ValueError as exc:
                raise PdfAcquisitionError(
                    "title truncate must be a positive integer"
                ) from exc
        parts.append((field_name, options))
        position = match.end()
    if template[position:].strip() or not parts:
        raise PdfAcquisitionError(
            "attachment rename template contains unsupported syntax"
        )
    return parts


def first_creator(paper: ZoteroPaper) -> str:
    creators = list(paper.creators)
    for creator_type in ("author", "editor", "director", "contributor"):
        selected = [
            row
            for row in creators
            if str(row.get("creatorType") or "").casefold() == creator_type
        ]
        if not selected:
            continue
        names = [
            str(row.get("lastName") or row.get("name") or "").strip()
            for row in selected
        ]
        names = [name for name in names if name]
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]}和{names[1]}"
        if names:
            return f"{names[0]} 等"
    return ""


def sanitize_zotero_filename(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = INVALID_FILENAME_RE.sub("", value)
    value = re.sub(r"[\r\n\t\u00a0\u2000-\u200a\u202f\u205f\u3000]+", " ", value)
    value = ZERO_WIDTH_BIDI_RE.sub("", value)
    value = "".join(character for character in value if _xml_character(character))
    value = re.sub(r" +", " ", value).strip().rstrip(". ")
    if not value or value in {".", ".."} or value.startswith("."):
        raise PdfAcquisitionError(
            "attachment rename template produced an invalid filename"
        )
    return value


def _xml_character(value: str) -> bool:
    codepoint = ord(value)
    return (
        codepoint in {0x9, 0xA, 0xD}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def render_attachment_filename(template: str, item: dict[str, Any]) -> str:
    paper = ZoteroPaper.from_item(item)
    fields = {
        "year": (re.search(r"(?:19|20)\d{2}", paper.date) or [""])[0],
        "publicationTitle": paper.publication_title,
        "title": paper.title,
        "firstCreator": first_creator(paper),
    }
    rendered: list[str] = []
    for field_name, options in _template_parts(template):
        value = fields[field_name]
        if field_name == "title" and value and "truncate" in options:
            value = value[: int(options["truncate"])].strip()
        if value:
            rendered.append(value + options.get("suffix", ""))
    filename = sanitize_zotero_filename("".join(rendered)) + ".pdf"
    return zotero_attachment.validate_filename(filename)


def read_attachment_rename_template(user_id: int, api: Any = zotero_web_api) -> str:
    data = api.web_api_request_json(
        "GET",
        f"users/{user_id}/settings/attachmentRenameTemplate",
        timeout=20.0,
    )
    if isinstance(data, dict):
        value = data.get("value")
        if isinstance(value, dict):
            value = value.get("template")
    else:
        value = None
    template = str(value or "").strip()
    if not template:
        raise PdfAcquisitionError("Zotero attachmentRenameTemplate is missing")
    _template_parts(template)
    return template


def _record_candidates(record: dict[str, Any]) -> tuple[PdfCandidate, ...]:
    evidence = json.loads(str(record.get("evidence_json") or "{}"))
    candidate = evidence.get("candidate")
    if not isinstance(candidate, dict):
        raise PdfAcquisitionError(
            f"eligible item has no stored candidate: {record['item_key']}"
        )
    alternates = evidence.get("alternates") or []
    if not isinstance(alternates, list) or any(
        not isinstance(row, dict) for row in alternates
    ):
        raise PdfAcquisitionError(
            f"eligible item has invalid stored alternates: {record['item_key']}"
        )
    return (
        PdfCandidate.from_dict(candidate),
        *(PdfCandidate.from_dict(row) for row in alternates),
    )


def _record_candidate(record: dict[str, Any]) -> PdfCandidate:
    return _record_candidates(record)[0]


def _matches_target_attachment(
    item: dict[str, Any], parent_key: str, filename: str
) -> bool:
    data = item.get("data") or {}
    return (
        str(data.get("parentItem") or "") == parent_key
        and str(data.get("linkMode") or "") == "imported_file"
        and str(data.get("contentType") or "") == "application/pdf"
        and str(data.get("title") or "").strip().casefold() == "pdf"
        and str(data.get("filename") or "") == filename
    )


def _wait_for_local_attachment(
    parent_key: str,
    attachment_key: str,
    filename: str,
    *,
    timeout: float = 30.0,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        for child in zotero_local.get_children(parent_key):
            data = child.get("data") or {}
            key = str(data.get("key") or child.get("key") or "").upper()
            if key == attachment_key and str(data.get("filename") or "") == filename:
                return
        if time.monotonic() >= deadline:
            raise PdfSyncPendingError(
                f"Zotero local sync did not expose attachment {attachment_key}"
            )
        time.sleep(1.0)


def apply_pdf_acquisition(
    item_keys: list[str],
    confirm: bool,
    *,
    dry_run: bool = False,
    database_path: Path | None = None,
    service: PdfDiscoveryService | None = None,
    attachment_client: zotero_attachment.ZoteroAttachmentClient | None = None,
) -> dict[str, Any]:
    if not dry_run and confirm is not True:
        raise PdfAcquisitionError("confirm=true is required for PDF acquisition")
    if not isinstance(item_keys, list) or not 1 <= len(item_keys) <= 50:
        raise PdfAcquisitionError("item_keys must contain between 1 and 50 Zotero keys")
    normalized = []
    for value in item_keys:
        key = str(value or "").strip().upper()
        if not ZOTERO_KEY_RE.fullmatch(key):
            raise PdfAcquisitionError(f"invalid Zotero item key: {value}")
        normalized.append(key)
    normalized = list(dict.fromkeys(normalized))
    client = attachment_client or zotero_attachment.ZoteroAttachmentClient(
        config_loader=zotero_translate.load_webdav_config
    )
    try:
        user_id = client.preflight(verify_write=not dry_run)
    except zotero_attachment.ZoteroAttachmentError as exc:
        raise PdfAcquisitionError(str(exc)) from exc
    template = read_attachment_rename_template(user_id)
    discovery = service or PdfDiscoveryService()
    results: list[dict[str, Any]] = []
    staging_parent = zotero_translate.state_dir() / "pdf_acquisition" / ".staging"
    staging_batch: tempfile.TemporaryDirectory[str] | None = None
    staging_root: Path | None = None
    if not dry_run:
        staging_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging_batch = tempfile.TemporaryDirectory(dir=staging_parent, prefix="batch-")
        staging_root = Path(staging_batch.name)
    try:
        for key in normalized:
            record = workflow_database.pdf_acquisition_record(key, database_path)
            if record is None:
                raise PdfAcquisitionError(f"no PDF acquisition plan for item: {key}")
            if not str(record["state"]).startswith("eligible_"):
                raise PdfAcquisitionError(
                    f"item is not eligible: {key} state={record['state']}"
                )
            stored_candidates = _record_candidates(record)
            item = zotero_local.get_item(key)
            fresh = discovery.plan_item(item)
            if not fresh.allowed or fresh.candidate is None:
                raise PdfAcquisitionError(
                    f"item is no longer eligible: {key} state={fresh.state}"
                )
            if fresh.candidate.url != stored_candidates[0].url:
                raise PdfAcquisitionError(
                    f"eligible candidate changed for {key}; review a new plan"
                )
            fresh_by_url = {
                row.url: row for row in (fresh.candidate, *fresh.alternates)
            }
            download_candidates = [
                fresh_by_url[row.url]
                for row in stored_candidates
                if row.url in fresh_by_url
            ]
            filename = render_attachment_filename(template, item)
            if dry_run:
                results.append(
                    {
                        "item_key": key,
                        "attachment_title": "PDF",
                        "filename": filename,
                        "source_url": fresh.candidate.url,
                        "state": "dry_run",
                    }
                )
                continue
            assert staging_root is not None
            try:
                with tempfile.TemporaryDirectory(
                    dir=staging_root, prefix=f"{key}-"
                ) as temp_dir:
                    local_pdf = Path(temp_dir) / "download.pdf"
                    verified = None
                    download_errors: list[PdfAcquisitionError] = []
                    for selected in download_candidates:
                        try:
                            verified = fetch_verified_pdf(
                                replace(fresh, candidate=selected), local_pdf
                            )
                        except PdfAcquisitionError as exc:
                            download_errors.append(type(exc)(f"{selected.url}: {exc}"))
                            continue
                        break
                    if verified is None:
                        message = "all reviewed PDF candidates failed: " + "; ".join(
                            str(error) for error in download_errors
                        )
                        if download_errors and all(
                            isinstance(error, PdfNetworkError)
                            for error in download_errors
                        ):
                            raise PdfNetworkError(message)
                        raise PdfAcquisitionError(message)
                    imported = client.import_pdf(
                        user_id,
                        key,
                        verified.output,
                        "PDF",
                        filename,
                        existing_match=lambda child, parent=key, target=filename: (
                            _matches_target_attachment(child, parent, target)
                        ),
                    )
                    attachment_key = str(imported.get("attachment_key") or "")
                    _wait_for_local_attachment(key, attachment_key, filename)
            except PdfNetworkError as exc:
                blocked = dict(record)
                blocked.update(
                    state="blocked",
                    checked_at=utc_now(),
                    next_check_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(
                        timespec="seconds"
                    ),
                    last_error=str(exc),
                )
                workflow_database.save_pdf_acquisition_records([blocked], database_path)
                raise
            except zotero_attachment.ZoteroAttachmentError as exc:
                workflow_database.mark_pdf_acquisition_failed(
                    key, str(exc), database_path
                )
                raise PdfAcquisitionError(str(exc)) from exc
            except PdfAcquisitionError as exc:
                workflow_database.mark_pdf_acquisition_failed(
                    key, str(exc), database_path
                )
                raise
            downloaded_at = utc_now()
            workflow_database.mark_pdf_acquisition_downloaded(
                key, downloaded_at, database_path
            )
            results.append(
                {
                    "item_key": key,
                    "attachment_key": attachment_key,
                    "attachment_title": "PDF",
                    "filename": filename,
                    "downloaded_at": downloaded_at,
                    "source_url": verified.final_url,
                    "pages": verified.pages,
                    "sha256": verified.sha256,
                    "already_present": bool(imported.get("already_present")),
                }
            )
    finally:
        if staging_batch is not None:
            staging_batch.cleanup()
            try:
                staging_parent.rmdir()
            except OSError:
                pass
    return {
        "applied": 0 if dry_run else len(results),
        "planned": len(results) if dry_run else 0,
        "dry_run": dry_run,
        "items": results,
    }


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path, default=workflow_database.default_database_path()
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="Discover public Version of Record PDFs")
    scan.add_argument("--collection", required=True)
    scan.add_argument("--recursive", action="store_true")
    scan.add_argument("--missing-only", action="store_true")
    scan.add_argument("--limit", type=positive_int, default=1000)
    scan.add_argument("--json", action="store_true")
    apply = subparsers.add_parser(
        "apply", help="Download and import exact eligible PDF plans"
    )
    apply.add_argument("--item", action="append", required=True)
    apply.add_argument("--confirm", action="store_true")
    apply.add_argument("--dry-run", action="store_true")
    status = subparsers.add_parser("status", help="Read one saved acquisition status")
    status.add_argument("--item", required=True)
    status.add_argument("--json", action="store_true")
    return parser


def _format_scan(data: dict[str, Any]) -> str:
    collection = data["collection"]
    lines = [
        f"collection: {collection['path']} ({collection['key']})",
        f"scanned: {data['scanned']}",
        f"returned: {data['returned']}",
    ]
    for row in data["items"]:
        lines.append(f"- {row['item_key']} state={row['state']} title={row['title']}")
        candidate = row.get("candidate")
        if candidate:
            lines.append(f"  candidate={candidate['url']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            data = scan_collection(
                args.collection,
                recursive=args.recursive,
                missing_only=args.missing_only,
                limit=args.limit,
                database_path=args.database,
            )
            print(
                json.dumps(data, ensure_ascii=False, indent=2)
                if args.json
                else _format_scan(data)
            )
            return 0
        if args.command == "apply":
            data = apply_pdf_acquisition(
                args.item,
                args.confirm,
                dry_run=args.dry_run,
                database_path=args.database,
            )
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        data = acquisition_status(args.item, args.database)
        print(
            json.dumps(data, ensure_ascii=False, indent=2)
            if args.json
            else json.dumps(data, ensure_ascii=False)
        )
        return 0
    except (
        PdfAcquisitionError,
        zotero_runtime.RuntimeConfigError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
