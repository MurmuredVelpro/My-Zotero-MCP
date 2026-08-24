import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zotero_mcp import zotero_setup


class SetupTests(unittest.TestCase):
    def test_render_user_config_has_no_secret_values(self):
        value = zotero_setup.render_user_config(
            Path(r"D:\Zotero\storage"),
            local_api="http://127.0.0.1:23119/api/",
            mineru_output=Path(r"D:\Zotero_MinerU"),
            qmd_command="qmd",
        )
        self.assertIn("[zotero]", value)
        self.assertIn('local_api = "http://127.0.0.1:23119/api"', value)
        self.assertIn("[mineru]", value)
        self.assertIn("[qmd]", value)
        self.assertIn("[translation]", value)
        self.assertIn('attachment_title = "CN"', value)
        self.assertIn('filename_template = "{source_stem}的全文翻译.pdf"', value)
        self.assertIn("auto_rename_manual = false", value)
        self.assertNotIn("token", value.casefold())
        self.assertNotIn("api_key", value.casefold())

    @unittest.skipIf(os.name == "nt", "Windows uses inherited user-profile ACLs")
    def test_write_private_file_uses_mode_600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            zotero_setup.write_private_file(path, "[zotero]\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_write_private_file_refuses_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("old", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "Refusing to overwrite"):
                zotero_setup.write_private_file(path, "new")

    @unittest.skipIf(os.name == "nt", "Windows symlink creation needs extra privileges")
    def test_write_private_file_refuses_symbolic_link_even_with_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_text("old", encoding="utf-8")
            path = Path(tmp) / "secret"
            path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                zotero_setup.write_private_file(path, "new", overwrite=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "old")

    def test_save_secret_uses_user_config_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp) / "mineru_api_token.secret"
            with mock.patch.object(
                zotero_setup.mineru_client,
                "default_token_path",
                return_value=expected,
            ) as default_path:
                path = zotero_setup.save_secret("mineru", "secret-value")
            self.assertEqual(path, expected)
            self.assertEqual(path.read_text(encoding="utf-8").strip(), "secret-value")
            default_path.assert_called_once_with()

    def test_save_zotero_secret_uses_secret_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp) / "zotero_web_api_key.secret"
            with mock.patch.object(
                zotero_setup.zotero_runtime,
                "default_secret_path",
                return_value=expected,
            ) as default_path:
                path = zotero_setup.save_secret("zotero", "secret-value")
            self.assertEqual(path, expected)
            default_path.assert_called_once_with("zotero_web_api_key.secret")

    def test_save_sciverse_secret_uses_private_secret_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp) / "sciverse_api_token.secret"
            with mock.patch.object(
                zotero_setup,
                "default_sciverse_token_path",
                return_value=expected,
            ) as default_path:
                path = zotero_setup.save_secret("sciverse", "secret-value")
            self.assertEqual(path, expected)
            self.assertEqual(path.read_text(encoding="utf-8").strip(), "secret-value")
            default_path.assert_called_once_with()
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_save_webdav_secret_is_private_and_structured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "translation_webdav.json"
            with mock.patch.object(
                zotero_setup.zotero_translate,
                "webdav_secret_path",
                return_value=path,
            ):
                saved = zotero_setup.save_webdav_secret(
                    "https://dav.example/zotero", "alice", "secret"
                )
            value = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(value["url"], "https://dav.example/zotero/")
        self.assertEqual(value["username"], "alice")
        self.assertEqual(value["password"], "secret")

    def test_save_webdav_secret_rejects_remote_http(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, "insecure remote HTTP"),
        ):
            zotero_setup.save_webdav_secret(
                "http://dav.example/zotero", "alice", "secret"
            )

    def test_codex_config_contains_three_independent_servers_without_secrets(self):
        with (
            mock.patch.object(
                zotero_setup,
                "configured_command",
                side_effect=lambda section, *_args: (
                    "qmd" if section == "qmd" else "/tools/sciverse-mcp-server"
                ),
            ),
        ):
            value = zotero_setup.codex_config_toml(
                ("literature", "review", "maintenance")
            )
        self.assertIn("[mcp_servers.zotero]", value)
        self.assertIn("[mcp_servers.qmd]", value)
        self.assertIn("[mcp_servers.sciverse]", value)
        self.assertIn("/tools/sciverse-mcp-server", value)
        self.assertIn('"-m", "zotero_mcp.zotero_mcp_server"', value)
        self.assertNotIn("cwd =", value)
        self.assertNotIn("SCIVERSE_API_TOKEN", value)
        self.assertNotIn("MINERU_API_TOKEN", value)

    def test_missing_sciverse_credentials_requires_manual_action(self):
        with (
            mock.patch.object(
                zotero_setup,
                "configured_command",
                return_value="sciverse-mcp-server",
            ),
            mock.patch.object(zotero_setup.shutil, "which", return_value="npx"),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(Path, "is_file", return_value=False),
        ):
            result = zotero_setup.sciverse_status("full")
        self.assertEqual(result["status"], "manual_action_required")
        self.assertIn(zotero_setup.SCIVERSE_URL, result["action"])

    def test_sciverse_accepts_configured_private_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("present", encoding="utf-8")
            if os.name != "nt":
                token_file.chmod(0o600)
            with (
                mock.patch.object(
                    zotero_setup,
                    "configured_command",
                    return_value="sciverse-mcp-server",
                ),
                mock.patch.object(
                    zotero_setup.zotero_runtime,
                    "configured_path",
                    return_value=token_file,
                ),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                result = zotero_setup.sciverse_status("full")
        self.assertEqual(result["status"], "ready")

    @unittest.skipIf(os.name == "nt", "Windows uses inherited user-profile ACLs")
    def test_sciverse_rejects_token_readable_by_group_or_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("present", encoding="utf-8")
            token_file.chmod(0o644)
            with (
                mock.patch.object(
                    zotero_setup,
                    "configured_command",
                    return_value="sciverse-mcp-server",
                ),
                mock.patch.object(
                    zotero_setup.zotero_runtime,
                    "configured_path",
                    return_value=token_file,
                ),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                result = zotero_setup.sciverse_status("full")
        self.assertEqual(result["status"], "manual_action_required")
        self.assertIn("not private", result["summary"])
        self.assertIn("group or other", result["details"]["error"])

    @unittest.skipIf(os.name == "nt", "Windows uses inherited user-profile ACLs")
    def test_sciverse_rejects_symbolic_link_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_text("present", encoding="utf-8")
            target.chmod(0o600)
            token_file = Path(tmp) / "token"
            token_file.symlink_to(target)
            with (
                mock.patch.object(
                    zotero_setup,
                    "configured_command",
                    return_value="sciverse-mcp-server",
                ),
                mock.patch.object(
                    zotero_setup.zotero_runtime,
                    "configured_path",
                    return_value=token_file,
                ),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                result = zotero_setup.sciverse_status("full")
        self.assertEqual(result["status"], "manual_action_required")
        self.assertIn("symbolic link", result["details"]["error"])

    def test_missing_paper_lookup_is_optional_and_links_upstream(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"CODEX_HOME": tmp}, clear=True),
        ):
            result = zotero_setup.paper_lookup_status()
        self.assertEqual(result["status"], "optional")
        self.assertIn(zotero_setup.PAPER_LOOKUP_URL, result["action"])

    def test_installed_paper_lookup_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_file = Path(tmp) / "skills" / "paper-lookup" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text("# paper-lookup\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": tmp}, clear=True):
                result = zotero_setup.paper_lookup_status()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["details"]["path"], str(skill_file))

    def test_core_profile_marks_external_account_as_optional(self):
        with mock.patch.object(
            zotero_setup.mineru_client,
            "load_token",
            side_effect=RuntimeError("missing"),
        ):
            result = zotero_setup.mineru_status("core")
        self.assertEqual(result["status"], "optional")

    def test_report_is_ready_when_all_components_are_ready_or_optional(self):
        ready = {"status": "ready", "summary": "ok"}
        optional = {"status": "optional", "summary": "optional"}
        with (
            mock.patch.object(zotero_setup, "dependency_status", return_value=ready),
            mock.patch.object(zotero_setup, "local_api_status", return_value=ready),
            mock.patch.object(zotero_setup, "storage_status", return_value=ready),
            mock.patch.object(zotero_setup, "web_api_status", return_value=ready),
            mock.patch.object(zotero_setup, "mineru_status", return_value=ready),
            mock.patch.object(zotero_setup, "qmd_status", return_value=ready),
            mock.patch.object(zotero_setup, "sciverse_status", return_value=optional),
            mock.patch.object(
                zotero_setup, "paper_lookup_status", return_value=optional
            ),
            mock.patch.object(zotero_setup, "translation_status", return_value=ready),
            mock.patch.object(zotero_setup, "codex_status", return_value=ready),
        ):
            report = zotero_setup.build_setup_report("full")
        self.assertTrue(report["ready"])
        self.assertEqual(report["summary"], {"ready": 8, "optional": 2})

    def test_core_report_skips_external_integrations(self):
        ready = {"status": "ready", "summary": "ok"}
        with (
            mock.patch.object(zotero_setup, "dependency_status", return_value=ready),
            mock.patch.object(zotero_setup, "local_api_status", return_value=ready),
            mock.patch.object(zotero_setup, "storage_status", return_value=ready),
            mock.patch.object(zotero_setup, "codex_status", return_value=ready),
            mock.patch.object(zotero_setup, "web_api_status") as web_api,
            mock.patch.object(zotero_setup, "mineru_status") as mineru,
            mock.patch.object(zotero_setup, "qmd_status") as qmd,
            mock.patch.object(zotero_setup, "sciverse_status") as sciverse,
            mock.patch.object(zotero_setup, "paper_lookup_status") as paper_lookup,
            mock.patch.object(zotero_setup, "translation_status") as translation,
        ):
            report = zotero_setup.build_setup_report("core")
        self.assertTrue(report["ready"])
        self.assertEqual(report["summary"], {"ready": 4})
        web_api.assert_not_called()
        mineru.assert_not_called()
        qmd.assert_not_called()
        sciverse.assert_not_called()
        paper_lookup.assert_not_called()
        translation.assert_not_called()

    def test_wsl_storage_on_mounted_drive_identifies_windows_zotero(self):
        local_api = {
            "status": "ready",
            "details": {"api_base": "http://172.29.112.1:23120/api"},
        }
        storage = {"status": "ready", "details": {"path": "/mnt/d/Zotero/storage"}}
        self.assertEqual(
            zotero_setup.infer_zotero_platform("wsl", local_api, storage),
            "windows",
        )

    def test_core_codex_status_requires_only_zotero(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(Path, "home", return_value=Path(tmp)),
        ):
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text(
                "[mcp_servers.zotero]\n"
                'command = "python"\n'
                'args = [ "zotero_mcp_server.py", "--toolsets", "literature" ]\n'
                "enabled = true\n",
                encoding="utf-8",
            )
            result = zotero_setup.codex_status("core")
        self.assertEqual(result["status"], "ready")

    def test_codex_status_rejects_disabled_or_incomplete_entries(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(Path, "home", return_value=Path(tmp)),
        ):
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text(
                "[mcp_servers.zotero]\n"
                'args = [ "--toolsets", "literature" ]\n'
                "enabled = false\n",
                encoding="utf-8",
            )
            result = zotero_setup.codex_status("core")
        self.assertEqual(result["status"], "manual_action_required")
        self.assertEqual(
            result["details"]["invalid"]["zotero"],
            ["entry is disabled", "command is missing"],
        )


if __name__ == "__main__":
    unittest.main()
