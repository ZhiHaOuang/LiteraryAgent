from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.config import ConfigError
from lg_cli.native import native_binary, writing_args

from tests.helpers import make_config


class NativeTests(unittest.TestCase):
    def test_rejects_unbuilt_stale_and_modified_runtime(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binary = root / "literary"
            binary.write_bytes(b"test binary")
            data = {
                "schema_version": 1,
                "kind": "lg-native-tui",
                "status": "built",
                "commit": "a" * 40,
                "binary": str(binary),
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            }
            manifest = root / "native-runtime.json"
            with (
                patch("lg_cli.native._bundled_core_pin", return_value="a" * 40),
                patch("lg_cli.native.product_root", return_value=root),
            ):
                manifest.write_text(json.dumps(data))
                self.assertEqual(native_binary(manifest), binary)
                for field, value in (
                    ("status", "prepared"),
                    ("commit", "b" * 40),
                    ("binary_sha256", "bad"),
                ):
                    with self.subTest(field=field):
                        manifest.write_text(json.dumps({**data, field: value}))
                        with self.assertRaises(ConfigError):
                            native_binary(manifest)

    def test_writing_surface_has_no_shell_and_only_explicit_tools(self):
        with tempfile.TemporaryDirectory() as raw:
            args = writing_args(make_config(Path(raw)))
            settings = dict(item.split("=", 1) for item in args if item != "-c")
            self.assertEqual(settings["features.shell_tool"], "false")
            self.assertEqual(settings["skills.bundled.enabled"], "false")
            self.assertEqual(settings["include_apps_instructions"], "false")
            self.assertEqual(settings["include_collaboration_mode_instructions"], "false")
            self.assertEqual(settings["mcp_servers.lg_writing.required"], "true")
            self.assertEqual(
                len(json.loads(settings["mcp_servers.lg_writing.enabled_tools"])), 9
            )
            instructions = Path(json.loads(settings["model_instructions_file"]))
            self.assertIn("LiteraryGiant", instructions.read_text())

    def test_lock_repair_only_allows_workspace_versions(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "build_native_ui.py"
        spec = importlib.util.spec_from_file_location("lg_native_builder", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        old = {
            "package": [
                {"name": "lg", "version": "0.0.0"},
                {"name": "dependency", "version": "1", "source": "registry"},
            ]
        }
        new = copy.deepcopy(old)
        new["package"][0]["version"] = "0.154.0"
        module.verify_workspace_lock(old, new, "0.154.0")
        new["package"][1]["version"] = "2"
        with self.assertRaises(ValueError):
            module.verify_workspace_lock(old, new, "0.154.0")
