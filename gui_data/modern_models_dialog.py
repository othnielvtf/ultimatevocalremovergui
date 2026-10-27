"""Optional modern separation UI; importing this module needs only Python/Tk.

Run independently of the legacy UVR environment with::

    python -m gui_data.modern_models_dialog
"""

import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from gui_data.studio_view import StudioViewMixin

from gui_data.app_paths import (
    bundle_resources, bundled_python, default_model_dir, default_output_dir,
    inside_bundle, preferences_path, worker_environment,
)


APP_DIR = Path(__file__).resolve().parent.parent
SETTINGS_PATH = preferences_path()
AUDIO_EXTENSIONS = {
    ".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".aiff",
    ".aif", ".wma", ".mp4", ".mkv", ".webm", ".mka", ".ac3", ".ape",
}


def expand_inputs(paths):
    """Expand audio folders and deduplicate files while preserving order."""
    result = []
    seen = set()
    for value in paths:
        path = Path(value).expanduser()
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if path.is_dir() and candidate.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            resolved = str(candidate.resolve())
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
    return result


def load_settings(path=SETTINGS_PATH):
    try:
        with Path(path).open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            return {}
        return {key: item for key, item in value.items() if isinstance(item, str)}
    except (OSError, ValueError):
        return {}


def find_python(app_dir=APP_DIR, saved=""):
    """Prefer an explicit runtime or the isolated installation over legacy UVR."""
    override = os.environ.get("UVR_MODERN_PYTHON", "").strip()
    if override:
        return os.path.expanduser(override)
    packaged_python = bundled_python()
    if packaged_python is not None:
        # A relocated app must never fall back to a previous checkout's venv.
        return str(packaged_python)
    candidates = [saved] if saved else []
    candidates.extend([
        str(Path(app_dir) / ".venv-modern" / "bin" / "python"),
        str(Path(app_dir) / ".venv-modern" / "Scripts" / "python.exe"),
    ])
    if not getattr(sys, "frozen", False):
        candidates.append(sys.executable)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def terminate_process(process, force=False):
    """Stop the worker and its ffmpeg children, with a Windows fallback."""
    if process is None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif os.name == "nt":
            # Windowless Python and ffmpeg processes do not share a console for
            # graceful control events. Stop their tree before killing its root;
            # otherwise a surviving ffmpeg can retain stdout and hang the reader.
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, check=False,
            )
            if result.returncode and process.poll() is None:
                process.kill()
        elif process.poll() is not None:
            return
        elif force:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    except OSError:
        # A missing taskkill should still allow the worker itself to stop.
        if process.poll() is None:
            process.kill() if force or os.name == "nt" else process.terminate()


class ModernModelsDialog(StudioViewMixin, tk.Toplevel):
    def __init__(self, parent, inputs=(), output_dir="", app_dir=APP_DIR,
                 busy_callback=None, on_close=None):
        super().__init__(parent)
        self.app_dir = Path(app_dir).resolve()
        self.packaged = bundle_resources() is not None
        self.busy_callback = busy_callback or (lambda: False)
        self.on_close = on_close
        self._process = None
        self._events = queue.Queue()
        self._closing = False
        self._cancelled = False
        self._poll_id = None
        self._kill_id = None
        self._complete_event = None
        self._download_only = False
        self.inputs = []
        self._editable = []
        self.title("UVR Modern" if self.packaged else "Modern Models — Ultimate Vocal Remover")
        self.geometry("1280x790")
        self.minsize(1080, 710)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Destroy>", self._on_destroy, add="+")

        from modern_models import MODEL_PRESETS
        self.presets = list(MODEL_PRESETS)
        saved = load_settings()
        first = self.presets[0] if self.presets else {"filename": "", "name": "Custom model"}
        self.python_var = tk.StringVar(value=find_python(self.app_dir, saved.get("python", "")))
        self.model_var = tk.StringVar(value=saved.get("model", first["filename"]))
        self.preset_var = tk.StringVar(value="Custom model")
        self.description_var = tk.StringVar(value="Enter a filename from the audio-separator model catalog.")
        output_default = str(default_output_dir(self.app_dir))
        model_default = str(default_model_dir(self.app_dir))
        output_value = output_dir or saved.get("output_dir", output_default)
        model_value = saved.get("model_dir", model_default)
        self.output_var = tk.StringVar(value=output_default if inside_bundle(output_value) else output_value)
        self.model_dir_var = tk.StringVar(value=model_default if inside_bundle(model_value) else model_value)
        self.format_var = tk.StringVar(value=saved.get("output_format", "WAV"))
        self.device_var = tk.StringVar(value=saved.get("device", "auto"))
        self.stem_var = tk.StringVar(value=saved.get("single_stem", ""))
        self.overlap_var = tk.StringVar(value=str(saved.get("overlap", "")))
        self.native_fp16_var = tk.BooleanVar(value=saved.get("native_fp16", "false") == "true")
        self.status_var = tk.StringVar(value="Ready. Models download automatically on first use.")
        self.progress_var = tk.DoubleVar(value=0)
        for preset in self.presets:
            if preset["filename"] == self.model_var.get():
                self.preset_var.set(preset["name"])
                self.description_var.set(preset.get("description", ""))
                break
        self._build()
        self.add_inputs(inputs)
        self._poll_id = self.after(100, self._poll)

    @property
    def is_running(self):
        # Keep controls and legacy processing locked until the worker is reaped.
        return self._process is not None

    def _select_preset(self, event=None):
        for preset in self.presets:
            if preset["name"] == self.preset_var.get():
                self.model_var.set(preset["filename"])
                self.description_var.set(preset.get("description", ""))
                return
        self.description_var.set("Enter a filename from the audio-separator model catalog.")

    def _browse_python(self):
        path = filedialog.askopenfilename(parent=self, title="Select the modern environment's Python executable")
        if path:
            self.python_var.set(path)

    def _browse_dir(self, variable):
        path = filedialog.askdirectory(parent=self, initialdir=variable.get() or str(self.app_dir))
        if path:
            variable.set(path)

    def _add_files(self):
        paths = filedialog.askopenfilenames(parent=self, title="Select audio files", filetypes=[
            ("Audio files", " ".join("*" + suffix for suffix in sorted(AUDIO_EXTENSIONS))),
            ("All files", "*"),
        ])
        self.add_inputs(paths)

    def _add_folder(self):
        path = filedialog.askdirectory(parent=self, title="Add audio folder and subfolders")
        if path:
            self.add_inputs([path])

    def add_inputs(self, paths):
        if self.is_running:
            return
        known = set(self.inputs)
        for path in expand_inputs(paths):
            if path not in known:
                self.inputs.append(path)
                known.add(path)
        self._sync_library()

    def _remove_files(self):
        for index in reversed(self.file_list.curselection()):
            self.file_list.delete(index)
            del self.inputs[index]
        self._sync_library()

    def _settings(self):
        return {
            "python": self.python_var.get(), "model": self.model_var.get(),
            "output_dir": self.output_var.get(), "model_dir": self.model_dir_var.get(),
            "output_format": self.format_var.get(), "device": self.device_var.get(),
            "single_stem": self.stem_var.get(), "overlap": self.overlap_var.get(),
            "native_fp16": "true" if self.native_fp16_var.get() else "false",
        }

    def _save_settings(self):
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = SETTINGS_PATH.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._settings(), indent=2) + "\n", encoding="utf-8")
            temporary.replace(SETTINGS_PATH)
        except OSError as exc:
            self._append("Could not save preferences: {}".format(exc))

    def _command(self, download_only):
        python = self.python_var.get().strip()
        executable = shutil.which(python) or (str(Path(python).expanduser()) if python else "")
        if not executable or not Path(executable).is_file():
            raise ValueError("Select a Python interpreter. Run the modern-engine setup instructions in README.md first.")
        filename = self.model_var.get().strip()
        if not filename:
            raise ValueError("Choose a model preset or enter its catalog filename.")
        if not download_only and not self.inputs:
            raise ValueError("Add at least one audio file or folder.")
        if not self.output_var.get().strip() or not self.model_dir_var.get().strip():
            raise ValueError("Choose an output folder and a model cache folder.")
        if inside_bundle(self.output_var.get()) or inside_bundle(self.model_dir_var.get()):
            raise ValueError("Choose output and model folders outside the application bundle.")
        command = [executable]
        if bundle_resources() is not None:
            command.extend(["-I", "-B"])
        command.extend(["-u", str(self.app_dir / "modern_models.py"),
                   "--model", filename,
                   "--output-dir", str(Path(self.output_var.get()).expanduser().resolve()),
                   "--model-dir", str(Path(self.model_dir_var.get()).expanduser().resolve()),
                   "--output-format", self.format_var.get(), "--device", self.device_var.get()])
        if self.native_fp16_var.get() and self.device_var.get() == "auto":
            command.append("--native-fp16")
        if self.stem_var.get().strip():
            command.extend(["--single-stem", self.stem_var.get().strip()])
        if self.overlap_var.get().strip():
            try:
                overlap = int(self.overlap_var.get())
            except ValueError:
                raise ValueError("Overlap must be a whole number from 2 to 50, or blank.")
            if not 2 <= overlap <= 50:
                raise ValueError("Overlap must be a whole number from 2 to 50, or blank.")
            command.extend(["--overlap", str(overlap)])
        if download_only:
            command.append("--download-only")
        else:
            command.append("--")
            command.extend(self.inputs)
        return command

    def start(self, download_only=False):
        if self.is_running:
            return
        if self.busy_callback():
            messagebox.showinfo("Processing in progress", "Wait for the current UVR job to finish before starting a modern model.", parent=self)
            return
        try:
            command = self._command(download_only)
        except ValueError as exc:
            messagebox.showerror("Modern models", str(exc), parent=self)
            return
        self._save_settings()
        self._stop_audio()
        if self._advanced_window is not None and self._advanced_window.winfo_exists():
            self._advanced_window.destroy()
        self._cancelled = False
        self._complete_event = None
        self._download_only = download_only
        self.progress_var.set(0)
        self._append("\n{}: {}\n".format("Downloading model" if download_only else "Starting separation", self.model_var.get()))
        try:
            kwargs = {"cwd": str(self.app_dir), "stdout": subprocess.PIPE,
                      "stderr": subprocess.STDOUT, "text": True, "encoding": "utf-8",
                      "errors": "replace", "bufsize": 1, "env": worker_environment()}
            if os.name == "posix":
                kwargs["start_new_session"] = True
            elif os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self._process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            self.status_var.set("Could not start the modern engine.")
            self._append(str(exc))
            return
        for widget, state in self._editable:
            if widget.winfo_exists():
                widget.configure(state="disabled")
        self.file_list.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_var.set("Loading separation model…")
        self.engine_state_var.set("PROCESSING")
        if not download_only:
            for path in self.inputs:
                self._outputs_by_input.pop(path, None)
            self._refresh_tracks()
        self.progress.configure(mode="indeterminate")
        self.progress.start(15)
        threading.Thread(target=self._read_worker, args=(self._process,), daemon=True).start()

    def _read_worker(self, process):
        try:
            for line in process.stdout:
                self._events.put(("line", line.rstrip("\r\n")))
        finally:
            process.stdout.close()
            self._events.put(("done", process.wait()))

    def _append(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", str(message) + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 700:
            self.log.delete("1.0", "100.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _handle_line(self, line):
        try:
            event = json.loads(line)
        except ValueError:
            self._append(line)
            return
        if not isinstance(event, dict) or "event" not in event:
            self._append(line)
            return
        kind = event["event"]
        if kind == "progress":
            self.progress.stop()
            self.progress.configure(mode="determinate")
            completed, total = event.get("completed", 0), event.get("total", 0)
            if total:
                self.progress_var.set(100 * completed / total)
                self.status_var.set("Processed {} of {} files.".format(completed, total))
        elif kind == "output":
            self._append("Saved: {}".format(event.get("path", "")))
            self._studio_output(event)
        elif kind == "complete":
            self._complete_event = event
        else:
            message = event.get("message", line)
            self._append(message)
            if kind in ("status", "error"):
                self.status_var.set(message)

    def _poll(self):
        self._poll_id = None
        # Bound each tick so verbose model logs cannot starve Tk events.
        for _ in range(200):
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "line":
                self._handle_line(value)
            elif kind == "done":
                self._finish(value)
                if self._closing:
                    return
            else:
                self._studio_event(kind, value)
        self._poll_id = self.after(100, self._poll)

    def _finish(self, returncode):
        self._process = None
        self.engine_state_var.set("ENGINE IDLE")
        if self._kill_id is not None:
            self.after_cancel(self._kill_id)
            self._kill_id = None
        self.progress.stop()
        self.progress.configure(mode="determinate")
        if self._cancelled:
            self.status_var.set("Cancelled.")
        elif returncode == 0:
            self.progress_var.set(100)
            self.status_var.set("Model downloaded. Ready to separate audio." if self._download_only
                                else "Complete. Output is in the selected folder.")
        elif self._complete_event is not None:
            self.status_var.set("Finished: {} succeeded, {} failed. See the log above.".format(
                self._complete_event.get("successful", 0), self._complete_event.get("failed", 0)))
        else:
            self.status_var.set("The modern engine failed (exit {}). See the log above.".format(returncode))
        self._append(self.status_var.get())
        for widget, state in self._editable:
            if widget.winfo_exists():
                widget.configure(state=state)
        self.file_list.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        if self._closing:
            self._destroy()

    def cancel(self):
        if not self.is_running or self._cancelled:
            return
        self._cancelled = True
        self.status_var.set("Cancelling…")
        self.cancel_button.configure(state="disabled")
        terminate_process(self._process)
        self._kill_id = self.after(3000, self._force_stop)

    def _force_stop(self):
        self._kill_id = None
        terminate_process(self._process, force=True)

    def close(self):
        self._save_settings()
        if self.is_running:
            self._closing = True
            self.cancel()
        else:
            self._destroy()

    def _destroy(self):
        self.destroy()
        if self.on_close is not None:
            self.on_close()

    def shutdown(self):
        """Cancel even if the parent Tk window is already being destroyed."""
        process = self._process
        if process is None:
            return
        terminate_process(process)

        def reap():
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            # A worker may have exited while a child still owns its stdout.
            # Stop the whole group even when waiting for the leader succeeded.
            terminate_process(process, force=True)
            process.wait()

        # Keep the interpreter alive briefly so closing Tk cannot orphan a worker.
        threading.Thread(target=reap, daemon=False).start()

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
        if self._kill_id is not None:
            self.after_cancel(self._kill_id)
        self._studio_shutdown()
        self.shutdown()


def open_modern_models(parent, inputs=(), output_dir="", app_dir=APP_DIR,
                       busy_callback=None):
    """Open or focus the parent's single modern-model window."""
    existing = getattr(parent, "_modern_models_dialog", None)
    if existing is not None and existing.winfo_exists():
        existing.add_inputs(inputs)
        existing.deiconify()
        existing.lift()
        existing.focus_force()
        return existing
    dialog = ModernModelsDialog(parent, inputs=inputs, output_dir=output_dir,
                                app_dir=app_dir, busy_callback=busy_callback)
    parent._modern_models_dialog = dialog
    return dialog


def main(inputs=()):
    root = tk.Tk()
    root.withdraw()
    dialog = ModernModelsDialog(root, inputs=inputs, on_close=root.destroy)
    root.protocol("WM_DELETE_WINDOW", dialog.close)
    if sys.platform == "darwin":
        root.tk.call("tk", "appname", "UVR Modern")
        root.createcommand("tk::mac::Quit", dialog.close)
        menubar = tk.Menu(root)
        application_menu = tk.Menu(menubar, name="apple", tearoff=False)
        application_menu.add_command(label="About UVR Modern", command=lambda: messagebox.showinfo(
            "UVR Modern", "UVR Modern\nLocal audio separation with BS-Roformer and Mel-Band Roformer.", parent=dialog))
        menubar.add_cascade(label="UVR Modern", menu=application_menu)
        dialog.configure(menu=menubar)
        def quit_application(event=None):
            dialog.close()
            return "break"
        root.bind_all("<Command-q>", quit_application)
    dialog.deiconify()
    dialog.lift()
    root.mainloop()


if __name__ == "__main__":
    main()
