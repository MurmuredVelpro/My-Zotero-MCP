import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from zotero_mcp import workflow_database, zotero_http, zotero_pdf

TEMPLATE = (
    '{{ year suffix="_" }}{{ publicationTitle suffix="_" }}'
    '{{ title truncate="100" suffix="_" }}{{ firstCreator }}'
)


def item(key="PAPER001", *, title="Example Article"):
    return {
        "key": key,
        "data": {
            "key": key,
            "itemType": "journalArticle",
            "title": title,
            "DOI": "10.1000/example",
            "publicationTitle": "Example Journal",
            "publisher": "Example Publisher",
            "date": "2025",
            "url": "https://publisher.example/article",
            "creators": [
                {"creatorType": "author", "firstName": "A", "lastName": "Xu"},
                {"creatorType": "author", "firstName": "B", "lastName": "Li"},
                {"creatorType": "author", "firstName": "C", "lastName": "Wu"},
            ],
        },
    }


def paper():
    return zotero_pdf.ZoteroPaper.from_item(item())


def candidate(**changes):
    values = {
        "url": "https://publisher.example/article.pdf",
        "route": "crossref",
        "source_kind": "crossref_link",
        "host_type": "publisher",
        "source_trust": "versioned_metadata",
        "version_kind": "published",
        "access_kind": "public_open",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "evidence": {
            "source_doi": "10.1000/example",
            "source_title": "Example Article",
            "doi_match": True,
            "title_score": 1.0,
            "routes": ["crossref"],
        },
        "final_domain": "publisher.example",
    }
    values.update(changes)
    return zotero_pdf.PdfCandidate(**values)


def decision(key="PAPER001", selected=None, alternates=()):
    selected = selected or candidate()
    return zotero_pdf.PdfAcquisitionDecision(
        item_key=key,
        title="Example Article",
        doi="10.1000/example",
        item_type="journalArticle",
        publication_title="Example Journal",
        publisher="Example Publisher",
        has_english_pdf=False,
        source_attachment_key="",
        state="eligible_publisher_vor",
        allowed=True,
        candidate=selected,
        rejected=(),
        checked_at="2026-08-26T00:00:00+00:00",
        next_check_at="",
        last_error="",
        alternates=tuple(alternates),
    )


class FakeHttp:
    def __init__(self, *, json_values=None, text_values=None):
        self.json_values = json_values or {}
        self.text_values = text_values or {}

    def json(self, url, *, label, **kwargs):
        value = self.json_values[label]
        if isinstance(value, Exception):
            raise value
        return value

    def text(self, url, *, label, **kwargs):
        value = self.text_values[label]
        if isinstance(value, Exception):
            raise value
        return value


def http_response(url, *, status=200, headers=None, content=b"{}"):
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.headers.update(headers or {})
    response._content = content
    response._content_consumed = True
    response.encoding = "utf-8"
    response.request = requests.Request("GET", url).prepare()
    response.raw = mock.Mock()
    return response


class RecordingSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = mock.Mock()
        self.max_redirects = None
        self.trust_env = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class PublicHttpClientTests(unittest.TestCase):
    NORMAL_PROXY_URL = "http://172.29.112.1:17892"
    REQUIRED_PROXY_URL = "http://172.29.112.1:17893"

    def test_normal_request_ignores_environment_and_uses_mihomo(self):
        session = RecordingSession(http_response("https://publisher.example/file"))
        with mock.patch.object(
            zotero_http,
            "wsl_gateway_ip",
            return_value="172.29.112.1",
        ):
            client = zotero_pdf.PublicHttpClient(session=session, per_host_interval=0)
            client.request(
                "GET",
                "https://publisher.example/file",
                budget=zotero_pdf.RequestBudget(),
                label="publisher",
            )

        self.assertFalse(session.trust_env)
        self.assertEqual(
            session.calls[0][2]["proxies"]["https"], self.NORMAL_PROXY_URL
        )

    def test_wiley_request_uses_explicit_wsl_proxy(self):
        session = RecordingSession(http_response("https://onlinelibrary.wiley.com/pdf"))
        with (
            mock.patch.object(
                zotero_http, "wsl_gateway_ip", return_value="172.29.112.1"
            ),
            mock.patch.object(
                zotero_http,
                "session_request",
                wraps=zotero_http.session_request,
            ) as routed_request,
        ):
            client = zotero_pdf.PublicHttpClient(session=session, per_host_interval=0)
            client.request(
                "GET",
                "https://onlinelibrary.wiley.com/pdf",
                budget=zotero_pdf.RequestBudget(),
                label="Wiley PDF",
            )

        self.assertEqual(
            session.calls[0][2]["proxies"],
            {
                "http": self.REQUIRED_PROXY_URL,
                "https": self.REQUIRED_PROXY_URL,
            },
        )
        self.assertIs(
            routed_request.call_args.kwargs["route"],
            zotero_http.RouteType.PROXY_REQUIRED,
        )

    def test_wiley_request_fails_closed_when_proxy_is_unavailable(self):
        session = RecordingSession()
        with mock.patch.object(
            zotero_http, "wsl_gateway_ip", return_value="172.29.112.1"
        ):
            client = zotero_pdf.PublicHttpClient(session=session, per_host_interval=0)

        with (
            mock.patch.object(
                zotero_http,
                "proxy_url",
                side_effect=zotero_http.RouteUnavailableError("offline"),
            ),
            self.assertRaisesRegex(
                zotero_pdf.PdfNetworkError, "Wiley proxy is unavailable"
            ),
        ):
            client.request(
                "GET",
                "https://www.wiley.com/article.pdf",
                budget=zotero_pdf.RequestBudget(),
                label="Wiley PDF",
            )

        self.assertEqual(session.calls, [])

    def test_redirect_to_wiley_switches_to_proxy_before_contacting_wiley(self):
        session = RecordingSession(
            http_response(
                "https://doi.example/article",
                status=302,
                headers={"Location": "https://onlinelibrary.wiley.com/doi/pdf/1"},
            ),
            http_response("https://onlinelibrary.wiley.com/doi/pdf/1"),
        )
        with (
            mock.patch.object(
                zotero_http, "wsl_gateway_ip", return_value="172.29.112.1"
            ),
            mock.patch.object(
                zotero_http,
                "session_request",
                wraps=zotero_http.session_request,
            ) as routed_request,
        ):
            client = zotero_pdf.PublicHttpClient(session=session, per_host_interval=0)
            client.request(
                "GET",
                "https://doi.example/article",
                budget=zotero_pdf.RequestBudget(),
                label="publisher",
            )

        self.assertEqual(
            [call.kwargs["route"] for call in routed_request.call_args_list],
            [zotero_http.RouteType.NORMAL, zotero_http.RouteType.PROXY_REQUIRED],
        )
        self.assertEqual(
            [call[2]["proxies"]["https"] for call in session.calls],
            [self.NORMAL_PROXY_URL, self.REQUIRED_PROXY_URL],
        )

    def test_redirect_after_wiley_stays_on_proxy_for_unknown_cdn(self):
        session = RecordingSession(
            http_response(
                "https://onlinelibrary.wiley.com/doi/pdf/1",
                status=302,
                headers={"Location": "https://cdn.example/paper.pdf"},
            ),
            http_response("https://cdn.example/paper.pdf"),
        )
        with (
            mock.patch.object(
                zotero_http, "wsl_gateway_ip", return_value="172.29.112.1"
            ),
            mock.patch.object(
                zotero_http,
                "session_request",
                wraps=zotero_http.session_request,
            ) as routed_request,
        ):
            client = zotero_pdf.PublicHttpClient(session=session, per_host_interval=0)
            client.request(
                "GET",
                "https://onlinelibrary.wiley.com/doi/pdf/1",
                budget=zotero_pdf.RequestBudget(),
                label="Wiley PDF",
            )

        self.assertEqual(
            [call.kwargs["route"] for call in routed_request.call_args_list],
            [zotero_http.RouteType.PROXY_REQUIRED] * 2,
        )
        self.assertEqual(
            [call[2]["proxies"]["https"] for call in session.calls],
            [self.REQUIRED_PROXY_URL] * 2,
        )


class DiscoveryTests(unittest.TestCase):
    def test_plan_preserves_verified_alternate_candidates_for_fallback(self):
        primary = candidate(
            url="https://publisher.example/primary.pdf",
            source_trust="official_publisher",
        )
        alternate = candidate(
            url="https://pmc.example/fallback.pdf",
            route="pmc",
            source_trust="verified_repository",
            final_domain="pmc.example",
        )
        service = zotero_pdf.PdfDiscoveryService(http=FakeHttp())
        with (
            mock.patch.object(
                zotero_pdf, "existing_source_attachment_key", return_value=""
            ),
            mock.patch.object(
                service,
                "discover",
                return_value=([alternate, primary], [], []),
            ),
        ):
            result = service.plan_item(item())

        self.assertEqual(result.candidate, primary)
        self.assertEqual(result.alternates, (alternate,))

    def test_crossref_vor_with_open_license_is_eligible(self):
        http = FakeHttp(
            json_values={
                "Crossref": {
                    "message": {
                        "DOI": "10.1000/example",
                        "title": ["Example Article"],
                        "publisher": "Example Publisher",
                        "license": [
                            {
                                "URL": "https://creativecommons.org/licenses/by/4.0/",
                                "content-version": "vor",
                                "start": {"timestamp": 0},
                            }
                        ],
                        "link": [
                            {
                                "URL": "https://publisher.example/article.pdf",
                                "content-type": "application/pdf",
                                "content-version": "vor",
                            }
                        ],
                    }
                }
            }
        )
        service = zotero_pdf.PdfDiscoveryService(http=http, unpaywall_email="none")
        rows, _ = service._crossref(paper(), zotero_pdf.RequestBudget())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].version_kind, "published")
        self.assertEqual(rows[0].access_kind, "public_open")
        self.assertEqual(zotero_pdf.candidate_rejection(paper(), rows[0]), "")

    def test_crossref_future_license_does_not_mark_pdf_open(self):
        http = FakeHttp(
            json_values={
                "Crossref": {
                    "message": {
                        "DOI": "10.1000/example",
                        "title": ["Example Article"],
                        "license": [
                            {
                                "URL": "https://creativecommons.org/licenses/by/4.0/",
                                "content-version": "vor",
                                "start": {"timestamp": 9_999_999_999_999},
                            }
                        ],
                        "link": [
                            {
                                "URL": "https://publisher.example/article.pdf",
                                "content-type": "application/pdf",
                                "content-version": "vor",
                            }
                        ],
                    }
                }
            }
        )
        rows, _ = zotero_pdf.PdfDiscoveryService(
            http=http, unpaywall_email="none"
        )._crossref(paper(), zotero_pdf.RequestBudget())
        self.assertEqual(rows[0].access_kind, "unknown")
        self.assertIn(
            "not public_open", zotero_pdf.candidate_rejection(paper(), rows[0])
        )

    def test_crossref_accepted_manuscript_is_rejected(self):
        row = candidate(version_kind="accepted")
        self.assertEqual(
            zotero_pdf.candidate_rejection(paper(), row),
            "version is accepted, not published",
        )

    def test_unpaywall_prefers_published_location_over_best_accepted(self):
        http = FakeHttp(
            json_values={
                "Unpaywall": {
                    "doi": "10.1000/example",
                    "title": "Example Article",
                    "is_oa": True,
                    "best_oa_location": {
                        "url_for_pdf": "https://repo.example/accepted.pdf",
                        "version": "acceptedVersion",
                        "host_type": "repository",
                        "license": "cc-by",
                    },
                    "oa_locations": [
                        {
                            "url_for_pdf": "https://publisher.example/vor.pdf",
                            "version": "publishedVersion",
                            "host_type": "publisher",
                            "license": "cc-by",
                        }
                    ],
                }
            }
        )
        service = zotero_pdf.PdfDiscoveryService(
            http=http, unpaywall_email="reader@example.org"
        )
        rows = service._unpaywall(paper(), zotero_pdf.RequestBudget())
        allowed = [
            row for row in rows if not zotero_pdf.candidate_rejection(paper(), row)
        ]
        self.assertEqual(
            [row.url for row in allowed], ["https://publisher.example/vor.pdf"]
        )

    def test_openalex_alone_cannot_prove_vor(self):
        row = candidate(
            route="openalex",
            source_kind="openalex_location",
            source_trust="discovery_only",
            evidence={
                "source_doi": "10.1000/example",
                "source_title": "Example Article",
                "doi_match": True,
                "title_score": 1.0,
                "routes": ["openalex"],
            },
        )
        self.assertIn("OpenAlex alone", zotero_pdf.candidate_rejection(paper(), row))

    def test_pmc_mid_is_rejected_as_manuscript(self):
        versions_xml = (
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            "<CommonPrefixes><Prefix>PMC123.1/</Prefix></CommonPrefixes>"
            "</ListBucketResult>"
        )
        http = FakeHttp(
            json_values={
                "PMC ID Converter": {
                    "records": [
                        {
                            "doi": "10.1000/example",
                            "pmcid": "PMC123",
                            "versions": [{"pmcid": "PMC123.1", "mid": "NIHMS123"}],
                        }
                    ]
                },
                "PMC metadata": {
                    "pmcid": "PMC123",
                    "version": 1,
                    "doi": "10.1000/example",
                    "mid": "NIHMS123",
                    "title": "Example Article",
                    "is_pmc_openaccess": True,
                    "is_manuscript": True,
                    "is_retracted": False,
                    "license_code": "CC BY",
                    "pdf_url": "s3://pmc-oa-opendata/PMC123.1/PMC123.1.pdf",
                },
            },
            text_values={"PMC versions": (versions_xml, zotero_pdf.PMC_S3_API)},
        )
        service = zotero_pdf.PdfDiscoveryService(http=http, unpaywall_email="none")
        rows = service._pmc(paper(), zotero_pdf.RequestBudget())
        self.assertEqual(rows[0].version_kind, "accepted")
        self.assertIn("accepted", zotero_pdf.candidate_rejection(paper(), rows[0]))

    def test_pmc_cloud_published_open_access_pdf_is_eligible(self):
        versions_xml = (
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            "<CommonPrefixes><Prefix>PMC123.2/</Prefix></CommonPrefixes>"
            "</ListBucketResult>"
        )
        http = FakeHttp(
            json_values={
                "PMC ID Converter": {
                    "records": [{"doi": "10.1000/example", "pmcid": "PMC123"}]
                },
                "PMC metadata": {
                    "pmcid": "PMC123",
                    "version": 2,
                    "doi": "10.1000/example",
                    "mid": None,
                    "title": "Example Article",
                    "is_pmc_openaccess": True,
                    "is_manuscript": False,
                    "is_retracted": False,
                    "license_code": "CC BY",
                    "pdf_url": ("s3://pmc-oa-opendata/PMC123.2/PMC123.2.pdf?md5=abc"),
                },
            },
            text_values={"PMC versions": (versions_xml, zotero_pdf.PMC_S3_API)},
        )
        rows = zotero_pdf.PdfDiscoveryService(http=http, unpaywall_email="none")._pmc(
            paper(), zotero_pdf.RequestBudget()
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].version_kind, "published")
        self.assertEqual(
            rows[0].url,
            "https://pmc-oa-opendata.s3.amazonaws.com/PMC123.2/PMC123.2.pdf?md5=abc",
        )
        self.assertEqual(zotero_pdf.candidate_rejection(paper(), rows[0]), "")

    def test_pmc_missing_manuscript_flag_is_not_treated_as_published(self):
        versions_xml = (
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            "<CommonPrefixes><Prefix>PMC123.1/</Prefix></CommonPrefixes>"
            "</ListBucketResult>"
        )
        http = FakeHttp(
            json_values={
                "PMC ID Converter": {
                    "records": [{"doi": "10.1000/example", "pmcid": "PMC123"}]
                },
                "PMC metadata": {
                    "pmcid": "PMC123",
                    "version": 1,
                    "doi": "10.1000/example",
                    "title": "Example Article",
                    "is_pmc_openaccess": True,
                    "is_retracted": False,
                    "license_code": "CC BY",
                    "pdf_url": "s3://pmc-oa-opendata/PMC123.1/PMC123.1.pdf",
                },
            },
            text_values={"PMC versions": (versions_xml, zotero_pdf.PMC_S3_API)},
        )
        rows = zotero_pdf.PdfDiscoveryService(http=http, unpaywall_email="none")._pmc(
            paper(), zotero_pdf.RequestBudget()
        )
        self.assertEqual(rows[0].version_kind, "unknown")
        self.assertIn("not published", zotero_pdf.candidate_rejection(paper(), rows[0]))

    def test_json_ld_text_does_not_count_as_license(self):
        documents = [
            '{"@type":"ScholarlyArticle","abstract":"Open access methods study"}'
        ]
        self.assertEqual(zotero_pdf._json_ld_license_values(documents), [])

    def test_untrusted_zotero_url_is_not_promoted_to_publisher_source(self):
        html = (
            '<meta name="citation_pdf_url" content="https://repo.example/paper.pdf">'
            '<meta name="citation_doi" content="10.1000/example">'
            '<meta name="citation_title" content="Example Article">'
            '<meta name="dc.rights" content="CC BY">'
        )
        service = zotero_pdf.PdfDiscoveryService(
            http=FakeHttp(
                text_values={
                    "publisher article page": (html, "https://repo.example/item")
                }
            ),
            unpaywall_email="none",
        )
        rows = service._publisher(
            paper(),
            "https://repo.example/item",
            zotero_pdf.RequestBudget(),
            trusted_landing_page=False,
        )
        self.assertEqual(rows[0].source_trust, "discovery_only")
        self.assertIn(
            "discovery-only", zotero_pdf.candidate_rejection(paper(), rows[0])
        )

    def test_deterministic_discovery_error_is_not_marked_blocked(self):
        service = zotero_pdf.PdfDiscoveryService(
            http=FakeHttp(), unpaywall_email="none"
        )
        with (
            mock.patch.object(
                zotero_pdf, "existing_source_attachment_key", return_value=""
            ),
            mock.patch.object(
                service,
                "discover",
                return_value=([], [], ["Crossref returned invalid work metadata"]),
            ),
        ):
            result = service.plan_item(item())
        self.assertEqual(result.state, "manual_no_vor_found")
        self.assertIn("Crossref returned invalid", result.last_error)

    def test_existing_english_pdf_skips_all_discovery(self):
        service = zotero_pdf.PdfDiscoveryService(
            http=FakeHttp(), unpaywall_email="none"
        )
        with (
            mock.patch.object(
                zotero_pdf.zotero_local,
                "pdf_attachments_for_item",
                return_value=[
                    {
                        "key": "SOURCE01",
                        "title": "PDF",
                        "filename": "paper.pdf",
                        "primary": True,
                    }
                ],
            ),
            mock.patch.object(
                zotero_pdf.zotero_local, "english_pdf_attachment_for_item"
            ) as language_check,
            mock.patch.object(service, "discover") as discover,
        ):
            result = service.plan_item(item())
        self.assertEqual(result.state, "not_needed")
        language_check.assert_not_called()
        discover.assert_not_called()


class FilenameTests(unittest.TestCase):
    def test_current_zotero_template_matches_golden_filename(self):
        source = item(
            "3QVFFHRS",
            title="Biomedical data and AI",
        )
        source["data"]["publicationTitle"] = "Science China. LIFE Sciences"
        self.assertEqual(
            zotero_pdf.render_attachment_filename(TEMPLATE, source),
            "2025_Science China. LIFE Sciences_Biomedical data and AI_Xu 等.pdf",
        )

    def test_unsupported_template_field_fails_closed(self):
        with self.assertRaisesRegex(
            zotero_pdf.PdfAcquisitionError, "unsupported attachment rename field"
        ):
            zotero_pdf.render_attachment_filename("{{ volume }}", item())

    def test_title_and_filename_remain_separate(self):
        self.assertNotEqual(
            "PDF", zotero_pdf.render_attachment_filename(TEMPLATE, item())
        )


class DatabaseTests(unittest.TestCase):
    def make_database(self, root, keys=("PAPER001",)):
        path = Path(root) / "workflow.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE items(item_key TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO items(item_key) VALUES(?)", [(key,) for key in keys]
            )
        return path

    def test_schema_is_idempotent_and_blocked_does_not_erase_eligible_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            row = zotero_pdf._decision_database_record(decision())
            workflow_database.save_pdf_acquisition_records([row], database)
            blocked = dict(row)
            blocked.update(
                state="blocked",
                candidate_url=None,
                evidence_json="{}",
                last_error="HTTP 429",
            )
            workflow_database.save_pdf_acquisition_records([blocked], database)
            workflow_database.save_pdf_acquisition_records([blocked], database)
            stored = workflow_database.pdf_acquisition_record("PAPER001", database)
            with sqlite3.connect(database) as connection:
                columns = connection.execute(
                    "PRAGMA table_info(pdf_acquisition)"
                ).fetchall()
        self.assertEqual(stored["state"], "eligible_publisher_vor")
        self.assertEqual(stored["candidate_url"], candidate().url)
        self.assertEqual(stored["last_error"], "HTTP 429")
        self.assertEqual(sum(row[1] == "candidate_url" for row in columns), 1)

    def test_not_needed_does_not_erase_downloaded_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            row = zotero_pdf._decision_database_record(decision())
            workflow_database.save_pdf_acquisition_records([row], database)
            workflow_database.mark_pdf_acquisition_downloaded(
                "PAPER001", "2026-08-26T00:01:00+00:00", database
            )
            not_needed = dict(row)
            not_needed.update(state="not_needed", candidate_url=None)
            workflow_database.save_pdf_acquisition_records([not_needed], database)
            stored = workflow_database.pdf_acquisition_record("PAPER001", database)
        self.assertEqual(stored["state"], "downloaded")
        self.assertEqual(stored["downloaded_at"], "2026-08-26T00:01:00+00:00")


class ValidationTests(unittest.TestCase):
    def test_pdf_identity_passes_with_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.pdf"
            path.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
            with (
                mock.patch.object(zotero_pdf, "_run_pdfinfo", return_value=(4, "")),
                mock.patch.object(
                    zotero_pdf,
                    "_extract_pdf_text",
                    return_value="Example Article\nExample Publisher",
                ),
            ):
                pages, _ = zotero_pdf.validate_downloaded_pdf(path, paper())
        self.assertEqual(pages, 4)

    def test_author_manuscript_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.pdf"
            path.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
            with (
                mock.patch.object(zotero_pdf, "_run_pdfinfo", return_value=(4, "")),
                mock.patch.object(
                    zotero_pdf,
                    "_extract_pdf_text",
                    return_value="Author Manuscript\nExample Article",
                ),
                self.assertRaisesRegex(
                    zotero_pdf.PdfAcquisitionError, "author manuscript"
                ),
            ):
                zotero_pdf.validate_downloaded_pdf(path, paper())


class DownloadTests(unittest.TestCase):
    def test_transient_get_failure_is_retried_once(self):
        content = b"%PDF-1.4\n" + b"x" * 2048
        session = RecordingSession(
            requests.ConnectTimeout("temporary timeout"),
            http_response(
                "https://publisher.example/article.pdf",
                content=content,
                headers={"Content-Length": str(len(content))},
            ),
        )
        client = zotero_pdf.PublicHttpClient(session=session, per_host_interval=0)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "paper.pdf"
            with mock.patch.object(
                zotero_pdf, "validate_downloaded_pdf", return_value=(4, "")
            ):
                result = zotero_pdf.fetch_verified_pdf(decision(), output, http=client)

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(result.size, len(content))

    def test_http_block_is_not_retried_as_a_transport_failure(self):
        session = RecordingSession(
            http_response("https://publisher.example/article.pdf", status=403)
        )
        client = zotero_pdf.PublicHttpClient(session=session, per_host_interval=0)

        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(zotero_pdf.PdfNetworkError, "HTTP 403"),
        ):
            zotero_pdf.fetch_verified_pdf(
                decision(), Path(tmp) / "paper.pdf", http=client
            )

        self.assertEqual(len(session.calls), 1)


class ApplyTests(DatabaseTests):
    def test_antibot_response_uses_next_stored_and_revalidated_candidate(self):
        primary = candidate(url="https://publisher.example/blocked.pdf")
        alternate = candidate(
            url="https://pmc.example/paper.pdf",
            route="pmc",
            source_trust="verified_repository",
            final_domain="pmc.example",
        )
        reviewed = decision(selected=primary, alternates=(alternate,))
        fetched = []

        class Discovery:
            def plan_item(self, value):
                return reviewed

        class Attachments:
            def preflight(self, verify_write=False):
                return 123

            def import_pdf(self, *args, **kwargs):
                return {"attachment_key": "ATTACH01", "already_present": False}

        def fetch(selected, destination):
            fetched.append(selected.candidate.url)
            if selected.candidate.url == primary.url:
                raise zotero_pdf.PdfAcquisitionError("downloaded response is not a PDF")
            destination.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
            return zotero_pdf.VerifiedPdf(
                destination,
                alternate.url,
                alternate.final_domain,
                destination.stat().st_size,
                4,
                "a" * 64,
                "2026-08-26T00:01:00+00:00",
            )

        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            workflow_database.save_pdf_acquisition_records(
                [zotero_pdf._decision_database_record(reviewed)], database
            )
            with (
                mock.patch.object(
                    zotero_pdf.zotero_local, "get_item", return_value=item()
                ),
                mock.patch.object(
                    zotero_pdf, "read_attachment_rename_template", return_value=TEMPLATE
                ),
                mock.patch.object(zotero_pdf, "fetch_verified_pdf", side_effect=fetch),
                mock.patch.object(zotero_pdf, "_wait_for_local_attachment"),
                mock.patch.object(
                    zotero_pdf.zotero_translate, "state_dir", return_value=Path(tmp)
                ),
            ):
                result = zotero_pdf.apply_pdf_acquisition(
                    ["PAPER001"],
                    True,
                    database_path=database,
                    service=Discovery(),
                    attachment_client=Attachments(),
                )

        self.assertEqual(result["applied"], 1)
        self.assertEqual(fetched, [primary.url, alternate.url])

    def test_dry_run_does_not_download_import_or_mark_downloaded(self):
        source_item = item()
        selected = decision()

        class Discovery:
            def plan_item(self, value):
                return selected

        class Attachments:
            def preflight(self, verify_write=False):
                self.verify_write = verify_write
                return 123

            def import_pdf(self, *args, **kwargs):
                raise AssertionError("dry-run must not import")

        attachments = Attachments()
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            workflow_database.save_pdf_acquisition_records(
                [zotero_pdf._decision_database_record(selected)], database
            )
            with (
                mock.patch.object(
                    zotero_pdf.zotero_local, "get_item", return_value=source_item
                ),
                mock.patch.object(
                    zotero_pdf, "read_attachment_rename_template", return_value=TEMPLATE
                ),
                mock.patch.object(zotero_pdf, "fetch_verified_pdf") as fetch,
            ):
                result = zotero_pdf.apply_pdf_acquisition(
                    ["PAPER001"],
                    False,
                    dry_run=True,
                    database_path=database,
                    service=Discovery(),
                    attachment_client=attachments,
                )
            state = workflow_database.pdf_acquisition_record("PAPER001", database)[
                "state"
            ]

        self.assertEqual(result["planned"], 1)
        self.assertEqual(result["applied"], 0)
        self.assertFalse(attachments.verify_write)
        fetch.assert_not_called()
        self.assertEqual(state, "eligible_publisher_vor")

    def test_batch_reads_template_once_and_imports_pdf_title_with_rendered_filename(
        self,
    ):
        keys = ("PAPER001", "PAPER002")
        items = {key: item(key) for key in keys}
        decisions = {key: decision(key) for key in keys}

        class Discovery:
            def plan_item(self, value):
                return decisions[value["data"]["key"]]

        class Attachments:
            def __init__(self):
                self.imports = []

            def preflight(self, verify_write=False):
                self.verify_write = verify_write
                return 123

            def import_pdf(
                self,
                user_id,
                parent_key,
                file_path,
                attachment_title,
                filename,
                *,
                existing_match=None,
            ):
                self.imports.append(
                    (parent_key, attachment_title, filename, existing_match)
                )
                return {
                    "attachment_key": f"AT{parent_key[-6:]}",
                    "already_present": False,
                }

        def fetch(selected, destination):
            destination.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
            return zotero_pdf.VerifiedPdf(
                destination,
                selected.candidate.url,
                selected.candidate.final_domain,
                destination.stat().st_size,
                4,
                "a" * 64,
                "2026-08-26T00:01:00+00:00",
            )

        attachments = Attachments()
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp, keys)
            workflow_database.save_pdf_acquisition_records(
                [zotero_pdf._decision_database_record(decisions[key]) for key in keys],
                database,
            )
            with (
                mock.patch.object(
                    zotero_pdf.zotero_local,
                    "get_item",
                    side_effect=lambda key: items[key],
                ),
                mock.patch.object(
                    zotero_pdf, "read_attachment_rename_template", return_value=TEMPLATE
                ) as read_template,
                mock.patch.object(zotero_pdf, "fetch_verified_pdf", side_effect=fetch),
                mock.patch.object(zotero_pdf, "_wait_for_local_attachment"),
                mock.patch.object(
                    zotero_pdf.zotero_translate, "state_dir", return_value=Path(tmp)
                ),
            ):
                result = zotero_pdf.apply_pdf_acquisition(
                    list(keys),
                    True,
                    database_path=database,
                    service=Discovery(),
                    attachment_client=attachments,
                )
            states = [
                workflow_database.pdf_acquisition_record(key, database)["state"]
                for key in keys
            ]

        self.assertEqual(result["applied"], 2)
        read_template.assert_called_once_with(123)
        self.assertTrue(attachments.verify_write)
        self.assertEqual([row[1] for row in attachments.imports], ["PDF", "PDF"])
        self.assertTrue(
            all(row[2].endswith("_Xu 等.pdf") for row in attachments.imports)
        )
        self.assertTrue(all(callable(row[3]) for row in attachments.imports))
        self.assertEqual(states, ["downloaded", "downloaded"])

    def test_changed_candidate_does_not_replace_reviewed_plan(self):
        reviewed = decision()
        changed = decision(
            selected=candidate(url="https://publisher.example/changed.pdf")
        )

        class Discovery:
            def plan_item(self, value):
                return changed

        class Attachments:
            def preflight(self, verify_write=False):
                return 123

        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            workflow_database.save_pdf_acquisition_records(
                [zotero_pdf._decision_database_record(reviewed)], database
            )
            with (
                mock.patch.object(
                    zotero_pdf.zotero_local, "get_item", return_value=item()
                ),
                mock.patch.object(
                    zotero_pdf, "read_attachment_rename_template", return_value=TEMPLATE
                ),
                self.assertRaisesRegex(
                    zotero_pdf.PdfAcquisitionError, "candidate changed"
                ),
            ):
                zotero_pdf.apply_pdf_acquisition(
                    ["PAPER001"],
                    False,
                    dry_run=True,
                    database_path=database,
                    service=Discovery(),
                    attachment_client=Attachments(),
                )
            stored = workflow_database.pdf_acquisition_record("PAPER001", database)
        self.assertEqual(stored["candidate_url"], reviewed.candidate.url)


if __name__ == "__main__":
    unittest.main()
