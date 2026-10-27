"""Regression tests for the optional worker, without ML packages or downloads."""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import modern_models


class FakeSeparator:
    instances = []

    def __init__(self, **options):
        self.options = options
        self.output_dir = options.get("output_dir")
        self.model_instance = None
        self.loaded = []
        self.downloaded = []
        self.processed = []
        self.__class__.instances.append(self)

    def load_model(self, model_filename):
        self.loaded.append(model_filename)
        self.model_instance = types.SimpleNamespace(output_dir=self.output_dir)

    def download_model_files(self, filename):
        self.downloaded.append(filename)

    def separate(self, source):
        source = Path(source)
        self.processed.append(str(source))
        # Real libraries sometimes print even when the caller wants JSON.
        print("backend diagnostic")
        if source.stem == "broken":
            raise RuntimeError("Unable to decode this input")
        if source.stem == "missing-output":
            return ["nonexistent.wav"]
        if source.stem == "cancel":
            raise KeyboardInterrupt()
        output = Path(self.model_instance.output_dir) / "vocals.wav"
        output.write_bytes(b"fake output")
        return [output.name]

    def list_supported_model_files(self):
        return {"MDXC": {"Example Roformer": {"filename": "example.ckpt"}}}


class ModernModelsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        FakeSeparator.instances = []
        self.addCleanup(self.temporary.cleanup)

    def args(self, *extra):
        return modern_models._parser().parse_args([
            "--model-dir", str(self.root / "models"),
            "--output-dir", str(self.root / "outputs"),
        ] + list(extra))

    def input(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"input fixture")
        return str(path)

    def run_job(self, args):
        events = []
        with contextlib.redirect_stdout(io.StringIO()):
            result = modern_models.run(args, lambda event, **data: events.append(dict(event=event, **data)), FakeSeparator)
        return result, events

    def main(self, args, backend=None):
        output, diagnostic = io.StringIO(), io.StringIO()
        backend = backend or FakeSeparator
        with mock.patch.object(modern_models, "_load_backend", return_value=(backend, modern_models.BACKEND_VERSION)):
            with mock.patch.object(modern_models.shutil, "which", return_value="/runtime/bin/ffmpeg"):
                with mock.patch.dict(os.environ):
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostic):
                        result = modern_models.main(args)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        return result, events, diagnostic.getvalue()

    def test_preset_listing_and_help_need_no_backend(self):
        worker = Path(modern_models.__file__).resolve()
        # -S ensures that optional site-packages are unavailable.
        completed = subprocess.run([sys.executable, "-S", str(worker), "--list-presets"], capture_output=True, text=True, check=True)
        presets = json.loads(completed.stdout)
        self.assertTrue(any("roformer" in preset["filename"].lower() for preset in presets))
        self.assertEqual(len(presets), len({preset["id"] for preset in presets}))
        help_result = subprocess.run([sys.executable, "-S", str(worker), "--help"], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--download-only", help_result.stdout)

    def test_same_basename_and_repeated_runs_do_not_overwrite(self):
        first = self.input("first/song.wav")
        second = self.input("second/song.wav")
        result, events = self.run_job(self.args(first, second))
        self.assertEqual(result, 0)
        outputs = [Path(event["path"]) for event in events if event["event"] == "output"]
        self.assertEqual(len(outputs), 2)
        self.assertNotEqual(outputs[0].parent, outputs[1].parent)
        self.assertTrue(all(path.is_file() for path in outputs))
        _, repeated = self.run_job(self.args(first))
        new_output = next(Path(event["path"]) for event in repeated if event["event"] == "output")
        self.assertNotIn(new_output, outputs)
        self.assertEqual(len(FakeSeparator.instances[0].loaded), 1)

    def test_batch_continues_after_missing_and_corrupt_inputs(self):
        result, events = self.run_job(self.args(str(self.root / "absent.wav"), self.input("broken.wav"), self.input("working.wav")))
        self.assertEqual(result, 1)
        self.assertEqual(len([event for event in events if event["event"] == "error"]), 2)
        self.assertEqual(events[-1], {"event": "complete", "successful": 1, "failed": 2, "cancelled": False})
        self.assertEqual(events[-2], {"event": "progress", "completed": 3, "total": 3})

    def test_invalid_inputs_do_not_download_weights(self):
        result, events = self.run_job(self.args(str(self.root / "absent.wav")))
        self.assertEqual(result, 1)
        self.assertEqual(FakeSeparator.instances, [])
        self.assertEqual(events[-1]["failed"], 1)

    def test_missing_output_is_a_failure(self):
        result, events = self.run_job(self.args(self.input("missing-output.wav")))
        self.assertEqual(result, 1)
        self.assertEqual(events[-1]["successful"], 0)
        self.assertFalse(any(event["event"] == "output" for event in events))

    def test_output_outside_job_directory_is_rejected(self):
        outside = Path(self.input("outside.wav"))
        folder = self.root / "outputs"
        folder.mkdir()
        with self.assertRaisesRegex(RuntimeError, "expected output"):
            modern_models._verified_output_paths([str(outside)], folder)

    def test_download_only_does_not_load_or_run_model(self):
        result, events = self.run_job(self.args("--preset", "bs-dereverb", "--download-only"))
        instance = FakeSeparator.instances[0]
        self.assertEqual(result, 0)
        self.assertEqual(instance.downloaded, ["deverb_bs_roformer_8_384dim_10depth.ckpt"])
        self.assertEqual(instance.loaded, [])
        self.assertEqual(instance.processed, [])
        self.assertTrue(instance.options["info_only"])
        self.assertEqual(events[-1]["failed"], 0)

    def test_model_settings_reach_backend(self):
        result, _ = self.run_job(self.args("--model", "model.ckpt", "--output-format", "flac", "--single-stem", "Vocals", "--overlap", "4", self.input("song.wav")))
        instance = FakeSeparator.instances[0]
        self.assertEqual(result, 0)
        self.assertEqual(instance.loaded, ["model.ckpt"])
        self.assertEqual(instance.options["output_format"], "FLAC")
        self.assertEqual(instance.options["output_single_stem"], "Vocals")
        self.assertEqual(instance.options["mdxc_params"]["overlap"], 4)

    def test_cpu_selection_skips_accelerator_discovery(self):
        fake_torch = types.SimpleNamespace(device=lambda name: name)
        with mock.patch.dict(sys.modules, {"torch": fake_torch}):
            result, _ = self.run_job(self.args("--device", "cpu", self.input("song.wav")))
        instance = FakeSeparator.instances[0]
        self.assertEqual(result, 0)
        self.assertTrue(instance.options["info_only"])
        self.assertEqual(instance.torch_device, "cpu")
        self.assertEqual(instance.onnx_execution_provider, ["CPUExecutionProvider"])

    def test_native_float16_is_opt_in_and_only_enabled_for_auto_device(self):
        source = self.input("song.wav")
        self.run_job(self.args(source))
        self.assertFalse(FakeSeparator.instances[-1].options["use_native_fp16"])
        self.run_job(self.args("--native-fp16", source))
        self.assertTrue(FakeSeparator.instances[-1].options["use_native_fp16"])
        fake_torch = types.SimpleNamespace(device=lambda name: name)
        with mock.patch.dict(sys.modules, {"torch": fake_torch}):
            self.run_job(self.args("--native-fp16", "--device", "cpu", source))
        self.assertFalse(FakeSeparator.instances[-1].options["use_native_fp16"])

    def test_stdout_contains_only_json_events(self):
        source = self.input("song.wav")
        result, events, diagnostic = self.main(["--output-dir", str(self.root / "out"), "--model-dir", str(self.root / "models"), "--", source])
        self.assertEqual(result, 0)
        self.assertEqual(events[-1]["event"], "complete")
        self.assertIn("backend diagnostic", diagnostic)

    def test_check_and_model_catalog_return_json_objects(self):
        result, items, _ = self.main(["--check"])
        self.assertEqual(result, 0)
        self.assertTrue(items[0]["available"])
        self.assertEqual(items[0]["version"], modern_models.BACKEND_VERSION)
        result, items, _ = self.main(["--list-models", "--model-dir", str(self.root / "models")])
        self.assertEqual(result, 0)
        self.assertEqual(items[0]["MDXC"]["Example Roformer"]["filename"], "example.ckpt")

    def test_invalid_options_fail_before_backend_import(self):
        for options in (["--model", "../model.ckpt", "input.wav"], ["--overlap", "1", "input.wav"], []):
            with self.subTest(options=options):
                with mock.patch.object(modern_models, "_load_backend") as loader:
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = modern_models.main(options)
                self.assertEqual(result, 1)
                loader.assert_not_called()

    def test_cancellation_is_reported(self):
        result, events, _ = self.main(["--model-dir", str(self.root / "models"), "--output-dir", str(self.root / "out"), self.input("cancel.wav")])
        self.assertEqual(result, 130)
        self.assertTrue(events[-1]["cancelled"])

    def test_import_path_does_not_shadow_backend_demucs(self):
        repo = str(Path(modern_models.__file__).resolve().parent)
        paths = [repo, "", str(self.root / "site-packages")]
        with mock.patch.object(sys, "path", paths):
            with mock.patch.object(Path, "cwd", return_value=Path(repo)):
                modern_models._isolate_import_path()
            self.assertNotIn(repo, sys.path)
            self.assertIn(str(self.root / "site-packages"), sys.path)

    def test_model_downloads_are_atomic_and_keep_existing_cache(self):
        calls = []

        class Downloader:
            def download_file_if_not_exists(self, url, output_path):
                calls.append(output_path)
                Path(output_path).write_bytes(b"complete model")

        downloader = modern_models._atomic_backend(Downloader)()
        target = self.root / "cache" / "model.ckpt"
        downloader.download_file_if_not_exists("https://example.test/model", str(target))
        self.assertEqual(target.read_bytes(), b"complete model")
        self.assertNotEqual(calls[0], str(target))
        downloader.download_file_if_not_exists("https://example.test/model", str(target))
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_cancelled_model_download_does_not_poison_cache(self):
        class InterruptedDownloader:
            def download_file_if_not_exists(self, url, output_path):
                Path(output_path).write_bytes(b"partial")
                raise KeyboardInterrupt()

        target = self.root / "cache" / "model.ckpt"
        downloader = modern_models._atomic_backend(InterruptedDownloader)()
        with self.assertRaises(KeyboardInterrupt):
            downloader.download_file_if_not_exists("https://example.test/model", str(target))
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])

    @unittest.skipIf(os.name == "nt" or sys.version_info < (3, 10), "POSIX worker needs Python 3.10 or newer")
    def test_sigterm_cleans_up_active_download_in_worker_process(self):
        cache = self.root / "cache"
        marker = self.root / "download-started"
        # Exercise the actual script entrypoint and signal handling, injecting a
        # small fake dependency so this requires neither ML packages nor network.
        launcher = """
import importlib.metadata
import pathlib
import runpy
import sys
import time
import types

worker, cache, marker = sys.argv[1:]

class Separator:
    def __init__(self, **options):
        self.cache = pathlib.Path(options['model_file_dir'])
    def download_model_files(self, filename):
        self.download_file_if_not_exists('https://example.test/model', self.cache / filename)
    def download_file_if_not_exists(self, url, output_path):
        pathlib.Path(output_path).write_bytes(b'partial weights')
        pathlib.Path(marker).touch()
        time.sleep(30)

package = types.ModuleType('audio_separator')
package.__path__ = []
module = types.ModuleType('audio_separator.separator')
module.Separator = Separator
sys.modules['audio_separator'] = package
sys.modules['audio_separator.separator'] = module
importlib.metadata.version = lambda name: '0.47.1'
sys.argv = [worker, '--download-only', '--model-dir', cache]
runpy.run_path(worker, run_name='__main__')
"""
        process = subprocess.Popen([
            sys.executable, "-c", launcher, str(Path(modern_models.__file__).resolve()), str(cache), str(marker),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "Worker did not reach the active download")
            process.send_signal(signal.SIGTERM)
            output, diagnostic = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 130, diagnostic)
        events = [json.loads(line) for line in output.splitlines()]
        self.assertTrue(events[-1]["cancelled"])
        self.assertEqual(list(cache.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
