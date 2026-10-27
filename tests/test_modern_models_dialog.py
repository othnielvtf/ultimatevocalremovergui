"""Headless regression tests for the modern-model window's file/process boundary."""

import io
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gui_data import modern_models_dialog as ui


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class InputAndPreferencesTests(unittest.TestCase):
    def test_folders_filter_audio_and_deduplicate_explicit_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            nested = folder / "album"
            nested.mkdir()
            first = folder / "first track.WAV"
            second = nested / "second.flac"
            first.touch()
            second.touch()
            (folder / "notes.txt").touch()
            self.assertEqual(ui.expand_inputs([first, folder, second, folder / "missing.wav"]),
                             [str(first.resolve()), str(second.resolve())])

    def test_missing_corrupt_and_invalid_preferences_are_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "preferences.json"
            self.assertEqual(ui.load_settings(path), {})
            for invalid in ["{", "[]", '"not settings"', '{"python": null, "overlap": []}']:
                path.write_text(invalid, encoding="utf-8")
                self.assertEqual(ui.load_settings(path), {})
            path.write_text(json.dumps({"python": "/runtime with spaces/python", "overlap": "4"}), encoding="utf-8")
            self.assertEqual(ui.load_settings(path)["overlap"], "4")

    def test_explicit_runtime_override_wins_over_saved_runtime(self):
        with patch.dict(os.environ, {"UVR_MODERN_PYTHON": "~/custom runtime/python"}):
            self.assertEqual(ui.find_python(saved=sys.executable),
                             os.path.expanduser("~/custom runtime/python"))

    def test_missing_saved_runtime_falls_back_to_isolated_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            interpreter = Path(temporary) / ".venv-modern" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
            with patch.dict(os.environ, {"UVR_MODERN_PYTHON": ""}):
                self.assertEqual(ui.find_python(temporary, saved="/missing/runtime"), str(interpreter))


class CommandTests(unittest.TestCase):
    def dialog(self, directory):
        # Exercise argument validation without creating a Tk root or a display.
        return SimpleNamespace(
            app_dir=Path(directory), python_var=Value(sys.executable),
            model_var=Value("model.ckpt"), output_var=Value(str(Path(directory) / "output folder")),
            model_dir_var=Value(str(Path(directory) / "model cache")),
            format_var=Value("FLAC"), device_var=Value("cpu"),
            stem_var=Value("Vocals"), overlap_var=Value("4"),
            native_fp16_var=Value(False),
            inputs=[str(Path(directory) / "track name; harmless.wav")],
        )

    def test_spaces_and_shell_characters_remain_individual_arguments(self):
        with tempfile.TemporaryDirectory(prefix="uvr command ") as temporary:
            dialog = self.dialog(temporary)
            command = ui.ModernModelsDialog._command(dialog, False)
            self.assertEqual(command[0], sys.executable)
            self.assertEqual(command[command.index("--output-dir") + 1], str(Path(dialog.output_var.get()).resolve()))
            self.assertEqual(command[command.index("--model-dir") + 1], str(Path(dialog.model_dir_var.get()).resolve()))
            self.assertEqual(command[command.index("--single-stem") + 1], "Vocals")
            self.assertEqual(command[command.index("--") + 1:], dialog.inputs)

    def test_download_does_not_require_or_pass_audio_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            dialog = self.dialog(temporary)
            dialog.inputs = []
            command = ui.ModernModelsDialog._command(dialog, True)
            self.assertIn("--download-only", command)
            self.assertNotIn("--", command)
            with self.assertRaisesRegex(ValueError, "audio file"):
                ui.ModernModelsDialog._command(dialog, False)

    def test_float16_option_only_reaches_worker_for_auto_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            dialog = self.dialog(temporary)
            dialog.native_fp16_var = Value(True)
            self.assertNotIn("--native-fp16", ui.ModernModelsDialog._command(dialog, False))
            dialog.device_var = Value("auto")
            self.assertIn("--native-fp16", ui.ModernModelsDialog._command(dialog, False))
            settings = ui.ModernModelsDialog._settings(dialog)
            self.assertEqual(settings["native_fp16"], "true")

    def test_bad_runtime_model_folders_and_overlap_fail_before_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            for attribute, value in [("python_var", "/missing/python"), ("model_var", ""),
                                     ("output_var", ""), ("model_dir_var", ""),
                                     ("overlap_var", "1"), ("overlap_var", "51"),
                                     ("overlap_var", "3.5")]:
                with self.subTest(attribute=attribute, value=value):
                    dialog = self.dialog(temporary)
                    setattr(dialog, attribute, Value(value))
                    with self.assertRaises(ValueError):
                        ui.ModernModelsDialog._command(dialog, False)


class ProcessLifecycleTests(unittest.TestCase):
    def test_reader_reports_nonzero_exit_after_draining_output(self):
        events = queue.Queue()
        process = SimpleNamespace(stdout=io.StringIO("backend log\n{\"event\":\"error\",\"message\":\"failed\"}\n"),
                                  wait=Mock(return_value=1))
        ui.ModernModelsDialog._read_worker(SimpleNamespace(_events=events), process)
        self.assertEqual(events.get_nowait(), ("line", "backend log"))
        self.assertEqual(json.loads(events.get_nowait()[1])["event"], "error")
        self.assertEqual(events.get_nowait(), ("done", 1))
        self.assertTrue(process.stdout.closed)

    def test_repeated_cancel_only_signals_once_and_schedules_escalation(self):
        dialog = SimpleNamespace(is_running=True, _cancelled=False, status_var=Mock(),
                                 cancel_button=Mock(), _process=Mock(), after=Mock(return_value="timer"),
                                 _force_stop=Mock())
        with patch.object(ui, "terminate_process") as terminate:
            ui.ModernModelsDialog.cancel(dialog)
            ui.ModernModelsDialog.cancel(dialog)
        terminate.assert_called_once_with(dialog._process)
        self.assertTrue(dialog._cancelled)
        dialog.after.assert_called_once_with(3000, dialog._force_stop)

    def test_shutdown_escalates_even_when_worker_already_exited(self):
        process = Mock()
        process.wait.return_value = 0
        dialog = SimpleNamespace(_process=process)
        def inline_thread(target, daemon):
            self.assertFalse(daemon)  # Cleanup must survive interpreter shutdown.
            return SimpleNamespace(start=target)
        with patch.object(ui.threading, "Thread", side_effect=inline_thread), \
                patch.object(ui, "terminate_process") as terminate:
            ui.ModernModelsDialog.shutdown(dialog)
        self.assertEqual(terminate.call_args_list[-1].args, (process,))
        self.assertTrue(terminate.call_args_list[-1].kwargs["force"])

    def test_windows_cancellation_stops_descendants_without_using_shell(self):
        process = Mock(pid=4321)
        process.poll.return_value = None
        with patch.object(ui.os, "name", "nt"), \
                patch.object(ui.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch.object(ui.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            ui.terminate_process(process)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "4321", "/T", "/F"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_windows_missing_taskkill_still_stops_worker(self):
        process = Mock(pid=4321)
        process.poll.return_value = None
        with patch.object(ui.os, "name", "nt"), \
                patch.object(ui.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch.object(ui.subprocess, "run", side_effect=FileNotFoundError):
            ui.terminate_process(process, force=True)
        process.kill.assert_called_once()

    @unittest.skipUnless(os.name == "posix", "POSIX process-group regression")
    def test_force_cancel_releases_stdout_after_group_leader_has_exited(self):
        # The child ignores TERM and inherits the worker's stdout. This is the
        # case where checking only worker.poll() would leave cancellation stuck.
        child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
        parent_code = "import subprocess,sys; child=subprocess.Popen([sys.executable,'-c',sys.argv[1]]); print(child.pid,flush=True)"
        process = subprocess.Popen([sys.executable, "-u", "-c", parent_code, child_code],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            lines = [process.stdout.readline().strip(), process.stdout.readline().strip()]
            self.assertIn("ready", lines)
            self.assertEqual(process.wait(timeout=5), 0)
            ui.terminate_process(process, force=True)
            process.communicate(timeout=5)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
