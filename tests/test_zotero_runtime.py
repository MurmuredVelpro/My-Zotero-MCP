import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zotero_mcp import zotero_runtime


class RuntimeConfigTests(unittest.TestCase):
    def test_toml_config_loads_platform_paths_and_user_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                """
[zotero]
storage = "/data/Zotero/storage"
user_id = 12345

[mineru]
output_dir = "/data/Zotero_MinerU"
""".strip(),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"ZOTERO_MCP_CONFIG": str(config)}, clear=True
            ):
                self.assertEqual(
                    zotero_runtime.configured_path(
                        "ZOTERO_STORAGE", "zotero", "storage"
                    ),
                    Path("/data/Zotero/storage"),
                )
                self.assertEqual(
                    zotero_runtime.config_positive_int("zotero", "user_id"),
                    12345,
                )

    def test_environment_path_overrides_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('[zotero]\nstorage = "/from/config"\n', encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "ZOTERO_MCP_CONFIG": str(config),
                    "ZOTERO_STORAGE": "/from/environment",
                },
                clear=True,
            ):
                self.assertEqual(
                    zotero_runtime.configured_path(
                        "ZOTERO_STORAGE", "zotero", "storage"
                    ),
                    Path("/from/environment"),
                )

    def test_windows_config_dir_uses_appdata(self):
        with (
            mock.patch.object(zotero_runtime, "is_windows", return_value=True),
            mock.patch.dict(
                os.environ,
                {"APPDATA": r"C:\Users\Senior\AppData\Roaming"},
                clear=True,
            ),
        ):
            self.assertEqual(
                zotero_runtime.config_dir(),
                Path(r"C:\Users\Senior\AppData\Roaming") / "zotero-mcp",
            )

    def test_windows_path_converts_to_wsl_mount(self):
        self.assertEqual(
            zotero_runtime.windows_path_to_wsl_path(r"D:\Zotero\storage"),
            Path("/mnt/d/Zotero/storage"),
        )

    def test_wsl_decodes_non_utf8_windows_user_profile(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=r"C:\Users\用户".encode("utf-16-le"),
            stderr=b"",
        )
        with (
            mock.patch.object(zotero_runtime, "is_windows", return_value=False),
            mock.patch.object(zotero_runtime, "is_wsl", return_value=True),
            mock.patch.object(zotero_runtime.shutil, "which", return_value=None),
            mock.patch.object(
                zotero_runtime,
                "windows_path_to_wsl_path",
                return_value=Path("/mnt/c/Windows"),
            ),
            mock.patch.object(zotero_runtime.Path, "is_file", return_value=True),
            mock.patch.object(
                zotero_runtime.subprocess,
                "run",
                return_value=completed,
            ) as run_mock,
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(
                zotero_runtime.windows_user_profile(),
                r"C:\Users\用户",
            )
            run_mock.assert_called_once_with(
                [
                    str(Path("/mnt/c/Windows") / "System32" / "cmd.exe"),
                    "/d",
                    "/u",
                    "/s",
                    "/c",
                    "echo %USERPROFILE%",
                ],
                check=True,
                capture_output=True,
                timeout=5,
            )

    def test_invalid_user_id_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('[zotero]\nuser_id = "none"\n', encoding="utf-8")
            with (
                mock.patch.dict(
                    os.environ, {"ZOTERO_MCP_CONFIG": str(config)}, clear=True
                ),
                self.assertRaisesRegex(
                    zotero_runtime.RuntimeConfigError, "positive integer"
                ),
            ):
                zotero_runtime.config_positive_int("zotero", "user_id")

    def test_boolean_config_requires_real_toml_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                "[translation]\nauto_rename_manual = true\n", encoding="utf-8"
            )
            with mock.patch.dict(
                os.environ, {"ZOTERO_MCP_CONFIG": str(config)}, clear=True
            ):
                self.assertTrue(
                    zotero_runtime.config_bool("translation", "auto_rename_manual")
                )

            config.write_text(
                '[translation]\nauto_rename_manual = "true"\n', encoding="utf-8"
            )
            with (
                mock.patch.dict(
                    os.environ, {"ZOTERO_MCP_CONFIG": str(config)}, clear=True
                ),
                self.assertRaisesRegex(
                    zotero_runtime.RuntimeConfigError, "must be a boolean"
                ),
            ):
                zotero_runtime.config_bool("translation", "auto_rename_manual")


if __name__ == "__main__":
    unittest.main()
