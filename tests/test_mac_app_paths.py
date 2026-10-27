"""The packaged application must stay independent of its original checkout."""

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gui_data import app_paths
from gui_data import modern_models_dialog as ui


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class PackagedPathsTests(unittest.TestCase):
    def resources(self, directory):
        resources = Path(directory) / "Moved UVR Modern.app" / "Contents" / "Resources"
        resources.mkdir(parents=True)
        return resources

    def test_source_launch_keeps_existing_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            source = Path(temporary) / "checkout"
            self.assertIsNone(app_paths.bundle_resources({}))
            self.assertEqual(app_paths.default_model_dir(source, home, {}), source / "models" / "Modern_Models")
            self.assertEqual(app_paths.default_output_dir(source, home, {}), source / "separated")
            self.assertEqual(app_paths.preferences_path(home, {}), home / ".uvr" / "modern_models.json")

    def test_packaged_data_and_preferences_are_outside_read_only_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            resources = self.resources(temporary)
            home = Path(temporary) / "home"
            environment = {"UVR_APP_RESOURCES": str(resources)}
            support = home / "Library" / "Application Support" / "UVRModern"
            self.assertEqual(app_paths.default_model_dir(resources / "app", home, environment), support / "models")
            self.assertEqual(app_paths.default_output_dir(resources / "app", home, environment), home / "Downloads" / "UVR Separated")
            self.assertEqual(app_paths.preferences_path(home, environment), support / "preferences.json")
            self.assertTrue(app_paths.inside_bundle(resources / "models", environment))
            self.assertFalse(app_paths.inside_bundle(support / "models", environment))

    def test_preferences_override_is_honored_without_creating_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            override = Path(temporary) / "chosen settings.json"
            self.assertEqual(app_paths.preferences_path(environ={"UVR_SETTINGS_PATH": str(override)}), override)
            self.assertFalse(override.exists())

    def test_unrelated_resources_directory_does_not_enable_packaged_mode(self):
        self.assertIsNone(app_paths.bundle_resources({"UVR_APP_RESOURCES": "/tmp/resources"}))

    def test_moved_app_uses_its_bundled_python_instead_of_old_saved_venv(self):
        with tempfile.TemporaryDirectory() as temporary:
            resources = self.resources(temporary)
            bundled_python = resources / "runtime" / "bin" / "python{}.{}".format(*sys.version_info[:2])
            bundled_python.parent.mkdir(parents=True)
            bundled_python.touch()
            old_python = Path(temporary) / "old checkout" / ".venv-modern" / "bin" / "python"
            old_python.parent.mkdir(parents=True)
            old_python.touch()
            environment = {"UVR_APP_RESOURCES": str(resources), "UVR_MODERN_PYTHON": ""}
            with patch.dict(os.environ, environment):
                self.assertEqual(ui.find_python(resources / "app", saved=str(old_python)), str(bundled_python.resolve()))
            with patch.dict(os.environ, dict(environment, UVR_MODERN_PYTHON=str(old_python))):
                self.assertEqual(ui.find_python(resources / "app"), str(old_python))

    def test_worker_strips_embedding_overrides_and_preserves_normal_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            resources = self.resources(temporary)
            environment = {
                "UVR_APP_RESOURCES": str(resources), "PATH": "/normal/path", "LANG": "en_US.UTF-8",
                "PYTHONHOME": "/other/python", "PYTHONPATH": "/injected/modules",
                "TCL_LIBRARY": "/embedding/tcl", "TK_LIBRARY": "/embedding/tk",
                "DYLD_LIBRARY_PATH": "/other/libraries", "__PYVENV_LAUNCHER__": "/old/python",
            }
            cleaned = app_paths.worker_environment(environment, home=Path(temporary) / "home")
            for key in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY", "DYLD_LIBRARY_PATH", "__PYVENV_LAUNCHER__"):
                self.assertNotIn(key, cleaned)
                self.assertIn(key, environment)
            self.assertEqual(cleaned["LANG"], environment["LANG"])
            self.assertEqual(cleaned["PATH"], environment["PATH"])
            self.assertIn("Library/Caches/UVRModern/numba", cleaned["NUMBA_CACHE_DIR"])

    def test_packaged_command_isolated_with_external_audio_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            resources = self.resources(temporary)
            dialog = SimpleNamespace(
                app_dir=resources / "app", python_var=Value(sys.executable),
                model_var=Value("model.ckpt"), output_var=Value(str(Path(temporary) / "output")),
                model_dir_var=Value(str(Path(temporary) / "models")), format_var=Value("WAV"),
                device_var=Value("auto"), stem_var=Value(""), overlap_var=Value("2"),
                native_fp16_var=Value(False), inputs=[str(Path(temporary) / "a track.wav")],
            )
            with patch.dict(os.environ, {"UVR_APP_RESOURCES": str(resources)}):
                command = ui.ModernModelsDialog._command(dialog, False)
                self.assertEqual(command[:4], [sys.executable, "-I", "-B", "-u"])
                self.assertEqual(command[4], str(resources / "app" / "modern_models.py"))
                self.assertEqual(command[command.index("--") + 1:], dialog.inputs)
                dialog.output_var = Value(str(resources / "outputs"))
                with self.assertRaisesRegex(ValueError, "outside the application"):
                    ui.ModernModelsDialog._command(dialog, False)


if __name__ == "__main__":
    unittest.main()
