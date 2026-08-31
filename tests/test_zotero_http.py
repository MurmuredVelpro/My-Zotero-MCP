import unittest
from unittest import mock

from zotero_mcp import zotero_http


class RoutingTests(unittest.TestCase):
    def test_local_session_ignores_environment_proxy_settings(self):
        session = zotero_http.routed_session(zotero_http.RouteType.LOCAL)
        try:
            self.assertFalse(session.trust_env)
            self.assertEqual(session.proxies, {})
        finally:
            session.close()

    def test_normal_session_uses_the_rule_based_wsl_proxy(self):
        with mock.patch.object(
            zotero_http, "wsl_gateway_ip", return_value="172.29.112.1"
        ):
            session = zotero_http.routed_session(zotero_http.RouteType.NORMAL)
        try:
            self.assertFalse(session.trust_env)
            self.assertEqual(
                session.proxies,
                {
                    "http": "http://172.29.112.1:17892",
                    "https": "http://172.29.112.1:17892",
                },
            )
        finally:
            session.close()

    def test_proxy_required_session_uses_the_forced_wsl_proxy(self):
        with mock.patch.object(
            zotero_http, "wsl_gateway_ip", return_value="172.29.112.1"
        ):
            session = zotero_http.routed_session(
                zotero_http.RouteType.PROXY_REQUIRED
            )
        try:
            self.assertFalse(session.trust_env)
            self.assertEqual(
                session.proxies,
                {
                    "http": "http://172.29.112.1:17893",
                    "https": "http://172.29.112.1:17893",
                },
            )
        finally:
            session.close()

    def test_one_shot_local_get_disables_all_environment_proxy_schemes(self):
        with mock.patch.object(zotero_http.requests, "request") as request:
            zotero_http.get(
                "http://localhost:8890/health",
                route=zotero_http.RouteType.LOCAL,
            )

        self.assertEqual(
            request.call_args.kwargs["proxies"],
            {"http": "", "https": "", "all": ""},
        )

    def test_wiley_urls_are_proxy_required(self):
        self.assertEqual(
            zotero_http.external_route("https://onlinelibrary.wiley.com/doi/pdf/1"),
            zotero_http.RouteType.PROXY_REQUIRED,
        )
        self.assertEqual(
            zotero_http.external_route("https://pmc.ncbi.nlm.nih.gov/articles/1"),
            zotero_http.RouteType.NORMAL,
        )

    def test_external_routes_fail_when_wsl_gateway_is_unknown(self):
        with (
            mock.patch.object(zotero_http, "wsl_gateway_ip", return_value=None),
            self.assertRaises(zotero_http.RouteUnavailableError),
        ):
            zotero_http.route_proxies(zotero_http.RouteType.NORMAL)


if __name__ == "__main__":
    unittest.main()
