"""DAW-style presentation and isolated audio preview for the modern window."""

import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from gui_data.app_paths import worker_environment
from gui_data.studio_widgets import StudioTimeline, format_time


BG = "#0c0f15"
PANEL = "#141923"
CARD = "#1b2230"
LINE = "#293243"
TEXT = "#edf1f9"
MUTED = "#8895aa"
ACCENT = "#ae95ff"
TEAL = "#57d8b4"
GOLD = "#f4bf70"
FONT = "Helvetica Neue" if sys.platform == "darwin" else "Segoe UI"
MONO = "Menlo" if sys.platform == "darwin" else "Consolas"


def clock_time(seconds):
    return format_time(seconds, precise=True)


class StudioButton(tk.Canvas):
    """Flat keyboard-accessible control, avoiding macOS native bevels."""

    def __init__(self, parent, text, command, primary=False, width=None):
        self._text = text
        self._command = command
        self._primary = primary
        self._disabled = False
        self._hover = False
        self._pressed = False
        self._font = (FONT, 11, "bold" if primary else "normal")
        from tkinter.font import Font
        measured = Font(parent, font=self._font).measure(text) + 28
        requested = max(34, width * 8 + 24 if width is not None else measured)
        super().__init__(parent, width=requested, height=34, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=True)
        self.bind("<Configure>", lambda event: self._draw_button())
        self.bind("<Enter>", lambda event: self._hover_button(True))
        self.bind("<Leave>", lambda event: self._hover_button(False))
        self.bind("<FocusIn>", lambda event: self._draw_button())
        self.bind("<FocusOut>", lambda event: self._draw_button())
        self.bind("<ButtonPress-1>", self._press_button)
        self.bind("<ButtonRelease-1>", self._release_button)
        self.bind("<space>", self._key_button)
        self.bind("<Return>", self._key_button)
        self._draw_button()

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs.update(cnf)
            cnf = None
        if "text" in kwargs:
            self._text = kwargs.pop("text")
        if "state" in kwargs:
            self._disabled = kwargs.pop("state") == "disabled"
            kwargs["takefocus"] = not self._disabled
        result = super().configure(cnf, **kwargs) if cnf is not None else super().configure(**kwargs)
        self._draw_button()
        return result

    config = configure

    def _hover_button(self, value):
        self._hover = value
        self._draw_button()

    def _press_button(self, event):
        if not self._disabled:
            self.focus_set()
            self._pressed = True
            self._draw_button()

    def _release_button(self, event):
        invoke = self._pressed and not self._disabled and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._pressed = False
        self._draw_button()
        if invoke:
            self._command()

    def _key_button(self, event):
        if not self._disabled:
            self._command()
        return "break"

    def _draw_button(self):
        self.delete("all")
        w, h = max(2, self.winfo_width()), max(2, self.winfo_height())
        fill = ACCENT if self._primary else "#242e40" if self._hover else CARD
        foreground = BG if self._primary else TEXT
        if self._disabled:
            fill, foreground = "#202635", "#576176"
        if self._pressed:
            fill = "#c4afff" if self._primary else "#343e53"
        outline = ACCENT if self.focus_get() is self and not self._disabled else fill if self._primary else LINE
        radius = 6
        points = [radius, 1, w-radius, 1, w-1, 1, w-1, radius,
                  w-1, h-radius, w-1, h-1, w-radius, h-1, radius, h-1,
                  1, h-1, 1, h-radius, 1, radius, 1, 1]
        self.create_polygon(points, smooth=True, splinesteps=16, fill=fill, outline=outline)
        self.create_text(w/2, h/2, text=self._text, fill=foreground, font=self._font)


class LevelMeter(tk.Canvas):
    """Stereo output peak display; levels always come from the audio worker."""

    def __init__(self, parent):
        super().__init__(parent, width=132, height=40, bg=PANEL, highlightthickness=0)
        self.levels = (0.0, 0.0)
        self.bind("<Configure>", lambda event: self.draw())

    def set_levels(self, left, right):
        self.levels = (left, right)
        self.draw()

    def draw(self):
        self.delete("all")
        for row, (label, amplitude) in enumerate(zip(("L", "R"), self.levels)):
            y = 11 + row * 17
            self.create_text(5, y, text=label, fill=MUTED, font=(MONO, 8))
            db = 20 * math.log10(max(float(amplitude), 1e-6))
            count = max(0, min(22, math.ceil((db + 60) / 60 * 22)))
            for index in range(22):
                color = TEAL if index < 16 else GOLD if index < 20 else "#f07886"
                self.create_rectangle(17 + index * 5, y - 4, 20 + index * 5, y + 4,
                                      fill=color if index < count else "#28303e", outline="")


def stop_preview_process(process, force=False):
    if process is None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        elif process.poll() is None:
            process.kill() if force else process.terminate()
    except (ProcessLookupError, OSError):
        pass


class StudioViewMixin:
    def _frame(self, parent, color=PANEL, **kwargs):
        return tk.Frame(parent, bg=color, **kwargs)

    def _label(self, parent, text=None, variable=None, size=11, color=TEXT, bold=False, **kwargs):
        options = {"bg": parent.cget("bg"), "fg": color,
                   "font": (FONT, size, "bold" if bold else "normal")}
        if variable is not None:
            options["textvariable"] = variable
        else:
            options["text"] = text
        options.update(kwargs)
        return tk.Label(parent, **options)

    def _studio_button(self, parent, text, command, primary=False, editable=True, width=None):
        button = StudioButton(parent, text=text, command=command, primary=primary, width=width)
        if editable:
            self._editable.append((button, "normal"))
        return button

    def _studio_entry(self, parent, variable, width=None):
        entry = tk.Entry(parent, textvariable=variable, relief="flat", bd=0, bg=CARD,
                         fg=TEXT, insertbackground=ACCENT, selectbackground="#514273",
                         selectforeground=TEXT, font=(FONT, 11), highlightthickness=1,
                         highlightbackground=LINE, highlightcolor=ACCENT)
        if width is not None:
            entry.configure(width=width)
        self._editable.append((entry, "normal"))
        return entry

    def _studio_combo(self, parent, variable, values):
        combo = ttk.Combobox(parent, textvariable=variable, values=values,
                             state="readonly", style="Studio.TCombobox", font=(FONT, 10),
                             width=8 if tuple(values) == ("auto", "cpu") else 20)
        self._editable.append((combo, "readonly"))
        return combo

    def _section(self, parent, label):
        section = self._frame(parent)
        section.pack(fill="x", pady=(0, 18))
        self._label(section, label, size=9, color=MUTED, bold=True).pack(anchor="w", pady=(0, 9))
        return section

    def _build(self):
        self.configure(bg=BG)
        self._analysis_cache = {}
        self._analysis_pending = set()
        self._analysis_processes = set()
        self._analysis_lock = threading.Lock()
        self._source_path = None
        self._active_track = "source"
        self._tracks = []
        self._outputs_by_input = {}
        self._audio_process = None
        self._audio_path = None
        self._audio_playing = False
        self._audio_position = 0.0
        self._audio_duration = 0.0
        self._zoom = 1
        self._advanced_window = None
        self.time_var = tk.StringVar(value="00:00.000")
        self.duration_var = tk.StringVar(value="/ 00:00.000")
        self.session_var = tk.StringVar(value="Untitled session")
        self.metadata_var = tk.StringVar(value="Load audio to begin")
        self.engine_state_var = tk.StringVar(value="ENGINE IDLE")
        self.track_count_var = tk.StringVar(value="3 TRACK LANES")
        self.library_count_var = tk.StringVar(value="0 files")
        self.gain_var = tk.DoubleVar(value=0.8)
        self.zoom_var = tk.StringVar(value="100%")
        self.device_label_var = tk.StringVar(value="AUTO DEVICE")
        self.model_title_var = tk.StringVar(value="MelBand Roformer")
        self.engine_detail_var = tk.StringVar(value="Vocals / instrumental")
        self.output_label_var = tk.StringVar()
        self._style_studio()

        shell = self._frame(self, BG)
        shell.pack(fill="both", expand=True)
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(2, weight=1)

        header = self._frame(shell, BG)
        header.grid(row=0, sticky="ew", padx=22, pady=(14, 10))
        logo = tk.Canvas(header, width=36, height=36, bg=BG, highlightthickness=0)
        logo.pack(side="left", padx=(0, 12))
        for x, height in ((5, 10), (12, 25), (19, 35), (26, 19)):
            logo.create_rectangle(x, (36 - height) / 2, x + 4, (36 + height) / 2, fill=ACCENT, outline="")
        brand = self._frame(header, BG)
        brand.pack(side="left")
        self._label(brand, "UVR MODERN", size=17, bold=True).pack(anchor="w")
        self._label(brand, "SEPARATION STUDIO", size=8, color=MUTED).pack(anchor="w")
        self._studio_button(header, "+  Add audio", self._add_files).pack(side="right", padx=(10, 0))
        self._studio_button(header, "Reveal exports  ↗", self._reveal_output, editable=False).pack(side="right")
        self._label(header, "●  LOCAL ENGINE", size=9, color=TEAL).pack(side="right", padx=20)

        transport = self._frame(shell)
        transport.grid(row=1, sticky="ew", padx=22, pady=(0, 14))
        title = self._frame(transport)
        title.pack(side="left", padx=16, pady=12)
        self._label(title, variable=self.session_var, size=12, bold=True).pack(anchor="w")
        self._label(title, variable=self.metadata_var, size=9, color=MUTED).pack(anchor="w", pady=(3, 0))
        controls = self._frame(transport)
        controls.pack(side="left", padx=(18, 12))
        self._studio_button(controls, "↤", lambda: self._seek_audio(0), editable=False).pack(side="left", padx=3)
        self.play_button = self._studio_button(controls, "▶", self._toggle_audio, primary=True, editable=False, width=2)
        self.play_button.pack(side="left", padx=3)
        self.play_button.configure(state="disabled")
        self._studio_button(controls, "■", self._stop_audio, editable=False).pack(side="left", padx=3)
        clock = self._frame(transport)
        clock.pack(side="left", padx=7)
        self._label(clock, variable=self.time_var, size=19, color=TEXT, font=(MONO, 19)).pack(anchor="w")
        self._label(clock, variable=self.duration_var, size=9, color=MUTED, font=(MONO, 9)).pack(anchor="w")
        self.level_meter = LevelMeter(transport)
        self.level_meter.pack(side="right", padx=(8, 16), pady=12)
        monitor = self._frame(transport)
        monitor.pack(side="right", padx=10)
        self._label(monitor, "MONITOR", size=8, color=MUTED).pack(anchor="w")
        tk.Scale(monitor, from_=0, to=1, resolution=0.01, variable=self.gain_var,
                 orient="horizontal", showvalue=False, length=86, width=4,
                 sliderlength=12, bg=PANEL, fg=TEXT, troughcolor=LINE, bd=0,
                 highlightthickness=0, activebackground=ACCENT,
                 command=lambda value: self._audio_command("gain", value=float(value))).pack()

        work = self._frame(shell, BG)
        work.grid(row=2, sticky="nsew", padx=22)
        work.grid_columnconfigure(1, weight=1)
        work.grid_rowconfigure(0, weight=1)
        self._build_library(work)
        self._build_arrangement(work)
        self._build_inspector(work)

        activity = self._frame(shell)
        activity.grid(row=3, sticky="ew", padx=22, pady=(14, 10))
        activity.grid_columnconfigure(0, weight=1)
        top = self._frame(activity)
        top.grid(row=0, sticky="ew", padx=14, pady=(10, 7))
        self._label(top, "ACTIVITY", size=8, color=MUTED, bold=True).pack(side="left")
        self._label(top, variable=self.engine_state_var, size=8, color=ACCENT).pack(side="right")
        self.log = tk.Text(activity, height=3, bg=PANEL, fg=MUTED, insertbackground=TEXT,
                           relief="flat", font=(MONO, 9), wrap="word", state="disabled",
                           padx=14, pady=2, highlightthickness=0)
        self.log.grid(row=1, sticky="ew")
        self.progress = ttk.Progressbar(activity, variable=self.progress_var, maximum=100,
                                       style="Studio.Horizontal.TProgressbar")
        self.progress.grid(row=2, sticky="ew", pady=(8, 0))
        footer = self._frame(shell, BG)
        footer.grid(row=4, sticky="ew", padx=23, pady=(0, 12))
        self._label(footer, variable=self.status_var, size=9, color=MUTED, anchor="w").pack(side="left", fill="x", expand=True)
        self._label(footer, "Space  Play / pause    ·    Click timeline to seek", size=9, color=MUTED).pack(side="right")
        self.status_var.set("Ready. Add audio to start a session.")
        self.output_var.trace_add("write", lambda *_: self._output_caption())
        self.preset_var.trace_add("write", lambda *_: self._model_caption())
        self.device_var.trace_add("write", lambda *_: self.device_label_var.set("AUTO DEVICE" if self.device_var.get() == "auto" else "CPU"))
        self._output_caption()
        self._model_caption()
        self.bind("<space>", self._space_transport, add="+")
        self.timeline.set_tracks([])

    def _style_studio(self):
        style = ttk.Style(self)
        # Standalone launches have a hidden root. Keep the legacy app's active
        # theme when this window is opened from its Settings panel.
        if not self.master.winfo_viewable():
            style.theme_use("clam")
        style.configure("Studio.TCombobox", fieldbackground=CARD, background=CARD,
                        foreground=TEXT, arrowcolor=MUTED, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=7)
        style.map("Studio.TCombobox", fieldbackground=[("readonly", CARD)],
                  foreground=[("readonly", TEXT), ("disabled", MUTED)],
                  selectbackground=[("readonly", CARD)], selectforeground=[("readonly", TEXT)])
        style.configure("Studio.Horizontal.TProgressbar", troughcolor=PANEL,
                        background=ACCENT, bordercolor=PANEL, lightcolor=ACCENT,
                        darkcolor=ACCENT, thickness=3)
        style.configure("Studio.Vertical.TScrollbar", background=LINE, troughcolor=PANEL,
                        bordercolor=PANEL, arrowcolor=MUTED, lightcolor=PANEL, darkcolor=PANEL, width=7, arrowsize=8)
        style.configure("Studio.TCheckbutton", background=PANEL, foreground=TEXT, font=(FONT, 10),
                        indicatorbackground=CARD, indicatorforeground=ACCENT)
        style.map("Studio.TCheckbutton", background=[("active", PANEL)], foreground=[("disabled", MUTED)])
        self.option_add("*TCombobox*Listbox.background", CARD)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", "#493e65")

    def _build_library(self, parent):
        library = self._frame(parent, width=202)
        library.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        library.grid_propagate(False)
        library.grid_columnconfigure(0, weight=1)
        library.grid_rowconfigure(2, weight=1)
        heading = self._frame(library)
        heading.grid(row=0, sticky="ew", padx=14, pady=(15, 4))
        self._label(heading, "SESSION FILES", size=9, bold=True).pack(side="left")
        self._label(library, variable=self.library_count_var, size=9, color=MUTED).grid(row=1, sticky="w", padx=14, pady=(0, 12))
        self.file_list = tk.Listbox(library, selectmode="extended", exportselection=False,
                                   bg=PANEL, fg=TEXT, selectbackground="#332c48",
                                   selectforeground=TEXT, font=(FONT, 10), bd=0,
                                   highlightthickness=0, activestyle="none")
        self.file_list.grid(row=2, sticky="nsew", padx=10)
        self.file_list.bind("<<ListboxSelect>>", self._file_selected)
        actions = self._frame(library)
        actions.grid(row=3, sticky="ew", padx=12, pady=(12, 8))
        self._studio_button(actions, "+ Files", self._add_files).pack(side="left")
        self._studio_button(actions, "Folder", self._add_folder).pack(side="right")
        self._studio_button(library, "Remove selected", self._remove_files).grid(row=4, sticky="ew", padx=12, pady=(0, 16))
        self._label(library, "BATCH READY", size=8, color=TEAL).grid(row=5, sticky="w", padx=14, pady=(0, 4))
        self._label(library, "Add several files or a folder.\nEach file gets its own export.",
                    size=9, color=MUTED, justify="left").grid(row=6, sticky="w", padx=14, pady=(0, 16))

    def _build_arrangement(self, parent):
        arrangement = self._frame(parent, BG)
        arrangement.grid(row=0, column=1, sticky="nsew", padx=(0, 12))
        arrangement.grid_rowconfigure(1, weight=1)
        arrangement.grid_columnconfigure(0, weight=1)
        toolbar = self._frame(arrangement, BG)
        toolbar.grid(row=0, sticky="ew", pady=(1, 13))
        self._label(toolbar, "Arrangement", size=14, bold=True).pack(side="left")
        self._studio_button(toolbar, "Import stems", self._import_stems).pack(side="left", padx=12)
        self._studio_button(toolbar, "+", lambda: self._change_zoom(1), editable=False).pack(side="right", padx=(3, 0))
        self._label(toolbar, variable=self.zoom_var, size=9, color=MUTED).pack(side="right", padx=7)
        self._studio_button(toolbar, "−", lambda: self._change_zoom(-1), editable=False).pack(side="right")
        self._studio_button(toolbar, "Fit", lambda: self._change_zoom(0), editable=False).pack(side="right", padx=5)
        self.timeline = StudioTimeline(arrangement, on_select=self._track_selected, on_seek=self._seek_audio)
        self.timeline.grid(row=1, sticky="nsew")
        info = self._frame(arrangement, BG)
        info.grid(row=2, sticky="ew", pady=(10, 0))
        self._label(info, variable=self.track_count_var, size=8, color=MUTED).pack(side="left")
        self._label(info, "● ORIGINAL     ● VOCALS     ● INSTRUMENTAL", size=8, color=MUTED).pack(side="right")

    def _build_inspector(self, parent):
        inspector = self._frame(parent, width=264)
        inspector.grid(row=0, column=2, sticky="nsew")
        inspector.grid_propagate(False)
        inspector.grid_rowconfigure(0, weight=1)
        inspector.grid_columnconfigure(0, weight=1)
        viewport = tk.Canvas(inspector, bg=PANEL, highlightthickness=0, height=220, width=244)
        viewport.grid(row=0, column=0, sticky="nsew", padx=(14, 4), pady=(14, 0))
        scrollbar = ttk.Scrollbar(inspector, orient="vertical", command=viewport.yview,
                                  style="Studio.Vertical.TScrollbar")
        scrollbar.grid(row=0, column=1, sticky="ns", pady=(14, 0))
        viewport.configure(yscrollcommand=scrollbar.set)
        body = self._frame(viewport)
        body_id = viewport.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda event: viewport.configure(scrollregion=viewport.bbox("all")))
        viewport.bind("<Configure>", lambda event: viewport.itemconfigure(body_id, width=event.width))
        engine = self._section(body, "SEPARATION ENGINE")
        self._label(engine, variable=self.model_title_var, size=15, bold=True).pack(anchor="w", pady=(0, 8))
        presets = self._studio_combo(engine, self.preset_var, [p["name"] for p in self.presets] + ["Custom model"])
        presets.pack(fill="x")
        presets.bind("<<ComboboxSelected>>", self._select_preset)
        self._label(engine, variable=self.engine_detail_var, size=10, color=MUTED,
                    wraplength=230, justify="left").pack(anchor="w", pady=(10, 0))

        processing = self._section(body, "PROCESSING")
        row = self._frame(processing)
        row.pack(fill="x")
        device = self._frame(row)
        device.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._label(device, "Device", size=9, color=MUTED).pack(anchor="w", pady=(0, 4))
        self._studio_combo(device, self.device_var, ("auto", "cpu")).pack(fill="x")
        overlap = self._frame(row)
        overlap.pack(side="right")
        self._label(overlap, "Overlap", size=9, color=MUTED).pack(anchor="w", pady=(0, 4))
        self._studio_entry(overlap, self.overlap_var, width=5).pack(ipady=7)
        precision = ttk.Checkbutton(processing, text="GPU float16", variable=self.native_fp16_var,
                                    style="Studio.TCheckbutton")
        precision.pack(anchor="w", pady=(9, 0))
        self._editable.append((precision, "normal"))
        self._label(processing, "Blank overlap uses the model default.", size=8, color=MUTED).pack(anchor="w", pady=(3, 0))

        export = self._section(body, "EXPORT")
        row = self._frame(export)
        row.pack(fill="x", pady=(0, 10))
        self._format_buttons = {}
        for value in ("WAV", "FLAC", "MP3"):
            button = self._studio_button(row, value, lambda choice=value: self.format_var.set(choice))
            button.pack(side="left", fill="x", expand=True, padx=(0, 3))
            self._format_buttons[value] = button
        def format_selected(*args):
            for choice, button in self._format_buttons.items():
                button._primary = choice == self.format_var.get()
                button._draw_button()
        self.format_var.trace_add("write", format_selected)
        format_selected()
        self._label(export, "Only stem (optional)", size=9, color=MUTED).pack(anchor="w", pady=(0, 5))
        self._studio_entry(export, self.stem_var).pack(fill="x", ipady=6)
        self._studio_button(export, "Choose output folder…", lambda: self._browse_dir(self.output_var)).pack(fill="x", pady=(10, 4))
        self._label(export, variable=self.output_label_var, size=8, color=MUTED,
                    wraplength=230, justify="left").pack(anchor="w")

        run = self._frame(inspector)
        run.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14, pady=(10, 14))
        self.run_button = self._studio_button(run, "Separate audio  →", self.start, primary=True)
        self.run_button.pack(fill="x")
        controls = self._frame(run)
        controls.pack(fill="x", pady=(7, 0))
        self._studio_button(controls, "Download model", lambda: self.start(download_only=True)).pack(side="left")
        self.cancel_button = self._studio_button(controls, "Cancel", self.cancel, editable=False)
        self.cancel_button.pack(side="right")
        self.cancel_button.configure(state="disabled")
        self._studio_button(body, "Advanced settings…", self._open_advanced).pack(fill="x", pady=(0, 10))
        def wheel(event):
            viewport.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        def bind_scroll(widget):
            widget.bind("<MouseWheel>", wheel, add="+")
            for child in widget.winfo_children():
                bind_scroll(child)
        bind_scroll(body)
        viewport.bind("<MouseWheel>", wheel)

    def _model_caption(self):
        name = self.preset_var.get()
        self.model_title_var.set(name.split(" - ")[0] if " - " in name else "Custom model")
        self.engine_detail_var.set(name.split(" - ", 1)[-1] if " - " in name else "Choose a supported model filename in Advanced settings.")

    def _output_caption(self):
        value = self.output_var.get()
        home = str(Path.home())
        self.output_label_var.set(value.replace(home, "~", 1) if value.startswith(home) else value)

    def _open_advanced(self):
        if self._advanced_window is not None and self._advanced_window.winfo_exists():
            self._advanced_window.lift()
            return
        window = tk.Toplevel(self, bg=PANEL)
        self._advanced_window = window
        window.title("Engine settings")
        window.geometry("580x310" if self.packaged else "580x380")
        window.transient(self)
        body = self._frame(window)
        body.pack(fill="both", expand=True, padx=22, pady=20)
        fields = [("Model filename", self.model_var), ("Model cache", self.model_dir_var)]
        if not self.packaged:
            fields.append(("Python interpreter", self.python_var))
        for title, variable in fields:
            self._label(body, title, color=MUTED).pack(anchor="w", pady=(7, 5))
            self._studio_entry(body, variable).pack(fill="x", ipady=7)
        self._studio_button(body, "Browse model catalog  ↗", lambda: __import__("webbrowser").open(
            "https://github.com/nomadkaraoke/python-audio-separator/blob/main/audio_separator/models.json"
        )).pack(anchor="w", pady=15)
        self._studio_button(body, "Done", window.destroy, primary=True, editable=False).pack(anchor="e")

    def _sync_library(self):
        selected = self._source_path
        self.file_list.delete(0, "end")
        for path in self.inputs:
            self.file_list.insert("end", "  " + Path(path).name)
        self.library_count_var.set("{} file{}".format(len(self.inputs), "" if len(self.inputs) == 1 else "s"))
        if self.inputs:
            if selected not in self.inputs:
                selected = self.inputs[0]
            index = self.inputs.index(selected)
            self.file_list.selection_set(index)
            self._select_source(selected)
        else:
            self._source_path = None
            self._stop_audio()
            self._audio_path = None
            self._audio_duration = 0.0
            self._active_track = "source"
            self.duration_var.set("/ " + clock_time(0))
            self._tracks = []
            self.timeline.set_tracks([])
            self.session_var.set("Untitled session")
            self.metadata_var.set("Load audio to begin")
            self.track_count_var.set("3 TRACK LANES")
            self.play_button.configure(state="disabled")

    def _file_selected(self, event=None):
        indices = self.file_list.curselection()
        if indices and indices[0] < len(self.inputs):
            self._select_source(self.inputs[indices[0]])

    def _select_source(self, path):
        if self._source_path != path:
            self._stop_audio()
            self._source_path = path
            self._active_track = "source"
            self._audio_path = None
            name = Path(path).stem
            self.session_var.set(name[:27] + "…" if len(name) > 28 else name)
            self.metadata_var.set("Reading audio…")
        self._refresh_tracks()

    def _refresh_tracks(self):
        if self._source_path is None:
            return
        tracks = [{"id": "source", "name": "Original", "path": self._source_path,
                   "color": TEAL, "subtitle": "SOURCE", "filename": Path(self._source_path).name}]
        outputs = sorted(self._outputs_by_input.get(self._source_path, []),
                         key=lambda path: (0 if "vocal" in Path(path).name.lower() else
                                           1 if any(name in Path(path).name.lower() for name in ("other", "instrumental")) else 2,
                                           Path(path).name))
        for index, output in enumerate(outputs):
            stem = Path(output).stem
            if "_(" in stem:
                label = stem.split("_(", 1)[-1].split(")", 1)[0]
            else:
                label = next((name for name in ("Instrumental", "Vocals", "Drums", "Bass", "Guitar", "Piano")
                              if name.lower() in stem.lower()), "Stem {}".format(index + 1))
            label = "Instrumental" if label.lower() == "instrumental" or (label.lower() == "other" and len(outputs) <= 2) else label.title()
            tracks.append({"id": output, "name": label, "path": output,
                           "color": {"Vocals": ACCENT, "Instrumental": GOLD, "Drums": "#7ba9f9",
                                     "Bass": "#f187a3", "Guitar": GOLD, "Piano": "#73d5d0", "Other": "#a3afc5"}.get(label, GOLD),
                           "subtitle": "SEPARATED", "filename": Path(output).name})
        if not outputs:
            tracks.extend([{"id": "vocals", "name": "Vocals", "color": ACCENT, "subtitle": "OUTPUT", "status": "AWAITING EXPORT"},
                           {"id": "instrumental", "name": "Instrumental", "color": GOLD, "subtitle": "OUTPUT", "status": "AWAITING EXPORT"}])
        for track in tracks:
            path = track.get("path")
            if path in self._analysis_cache:
                data = self._analysis_cache[path]
                track.update(peaks=data["peaks"], duration=data["duration"], status="READY")
            elif path:
                track["status"] = "Analyzing"
                self._analyze(path)
        self._tracks = tracks
        self.timeline.set_tracks(tracks)
        self.timeline.set_active(self._active_track)
        self.track_count_var.set("{} TRACK LANES".format(len(tracks)))
        source = self._analysis_cache.get(self._source_path)
        if source:
            channels = "MONO" if source["channels"] == 1 else "STEREO" if source["channels"] == 2 else "{} CH".format(source["channels"])
            self.metadata_var.set("{:.1f} kHz  ·  {}  ·  {}".format(source["sample_rate"] / 1000, channels, clock_time(source["duration"]).split(".")[0]))
        active = next((track for track in tracks if track["id"] == self._active_track), tracks[0])
        if active.get("path") in self._analysis_cache:
            self._prepare_audio(active["path"])

    def _import_stems(self):
        if self._source_path is None:
            messagebox.showinfo("Import stems", "Add an original audio file first, then attach its separated stems.", parent=self)
            return
        if self.is_running:
            return
        paths = filedialog.askopenfilenames(parent=self, title="Import separated stems",
                                           initialdir=self.output_var.get() or str(Path.home()),
                                           filetypes=[("Audio files", "*.wav *.flac *.mp3 *.m4a *.ogg"), ("All files", "*")])
        if paths:
            self._outputs_by_input[self._source_path] = [str(Path(path).resolve()) for path in paths]
            self._refresh_tracks()

    def _studio_output(self, event):
        source = event.get("input")
        if source is None or not event.get("path"):
            return
        source = str(Path(source).resolve())
        paths = self._outputs_by_input.setdefault(source, [])
        if event["path"] not in paths:
            paths.append(event["path"])
        if source == self._source_path:
            self._refresh_tracks()

    def _audio_worker_command(self, *args):
        return [self.python_var.get().strip(), "-I", "-B", "-u", str(self.app_dir / "studio_audio.py")] + list(args)

    def _analyze(self, path):
        if path in self._analysis_pending:
            return
        self._analysis_pending.add(path)
        command = self._audio_worker_command("--analyze=" + path, "--bins", "1100")
        def analyze():
            process = None
            try:
                process = subprocess.Popen(command, cwd=str(self.app_dir), env=worker_environment(),
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                           start_new_session=os.name == "posix")
                with self._analysis_lock:
                    self._analysis_processes.add(process)
                    if self._closing:
                        stop_preview_process(process)
                stdout, stderr = process.communicate(timeout=90)
                data = json.loads(stdout.strip().splitlines()[-1])
                if process.returncode or data.get("event") == "error" or "error" in data:
                    raise RuntimeError(data.get("message") or data.get("error") or stderr[-400:] or "Audio analysis failed")
                self._events.put(("analysis", (path, data, None)))
            except Exception as error:
                if process is not None and process.poll() is None:
                    stop_preview_process(process, force=True)
                    process.communicate()
                self._events.put(("analysis", (path, None, str(error))))
            finally:
                if process is not None:
                    with self._analysis_lock:
                        self._analysis_processes.discard(process)
        threading.Thread(target=analyze, daemon=True).start()

    def _track_selected(self, track):
        if not track.get("path") or track.get("path") not in self._analysis_cache:
            return
        if self._active_track == track["id"]:
            return
        position, playing = self._audio_position, self._audio_playing
        self._active_track = track["id"]
        self.timeline.set_active(track["id"])
        self._prepare_audio(track["path"])
        self._seek_audio(position)
        if playing:
            self._audio_command("gain", value=self.gain_var.get())
            self._audio_command("play")

    def _prepare_audio(self, path):
        data = self._analysis_cache[path]
        self._audio_duration = data["duration"]
        self.duration_var.set("/ " + clock_time(self._audio_duration))
        self.play_button.configure(state="normal")
        if self._audio_path != path or self._audio_process is None or self._audio_process.poll() is not None:
            self._audio_path = path
            self._audio_position = 0
            self._audio_playing = False
            self.time_var.set(clock_time(0))
            self.timeline.set_playhead(0)
            self.timeline.set_playing(False)
            self.play_button.configure(text="▶")
            self._audio_command("load", path=path, create=True)

    def _audio_command(self, command, create=False, **fields):
        if self._audio_process is None or self._audio_process.poll() is not None:
            if not create:
                return
            try:
                self._audio_process = subprocess.Popen(self._audio_worker_command("--serve"),
                    cwd=str(self.app_dir), env=worker_environment(), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                    start_new_session=os.name == "posix")
                process = self._audio_process
                def read_audio():
                    for line in process.stdout:
                        self._events.put(("audio", (process, line)))
                    process.stdout.close()
                    self._events.put(("audio_done", (process, process.wait())))
                threading.Thread(target=read_audio, daemon=True).start()
            except OSError as error:
                self.status_var.set("Audio preview unavailable. Check the selected runtime.")
                self._append(str(error))
                return
        try:
            self._audio_process.stdin.write(json.dumps(dict(command=command, **fields)) + "\n")
            self._audio_process.stdin.flush()
        except (OSError, ValueError):
            self._audio_playing = False

    def _toggle_audio(self):
        if not self._audio_path:
            return
        if self._audio_process is None or self._audio_process.poll() is not None:
            self._audio_command("load", path=self._audio_path, create=True)
        self._audio_command("gain", value=self.gain_var.get())
        self._audio_command("pause" if self._audio_playing else "play")

    def _stop_audio(self):
        self._audio_command("stop")
        self._audio_position = 0
        self._audio_playing = False
        self.time_var.set(clock_time(0))
        self.play_button.configure(text="▶")
        self.level_meter.set_levels(0, 0)
        self.timeline.set_playhead(0)
        self.timeline.set_playing(False)

    def _seek_audio(self, seconds):
        if not self._audio_path:
            return
        seconds = min(max(0, seconds), self._audio_duration)
        self._audio_command("seek", position=seconds)
        self._audio_position = seconds
        self.time_var.set(clock_time(seconds))
        self.timeline.set_playhead(seconds)

    def _space_transport(self, event):
        if isinstance(self.focus_get(), (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text, tk.Button, ttk.Checkbutton, tk.Checkbutton, tk.Scale)):
            return
        self._toggle_audio()
        return "break"

    def _change_zoom(self, direction):
        self._zoom = 1 if direction == 0 else max(1, min(4, self._zoom + direction))
        self.timeline.set_zoom(self._zoom)
        self.zoom_var.set("{}%".format(self._zoom * 100))

    def _reveal_output(self):
        path = Path(self.output_var.get()).expanduser()
        if not path.is_dir():
            messagebox.showinfo("Export folder", "The output folder will be created when audio is exported.", parent=self)
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _studio_event(self, kind, value):
        if kind == "analysis":
            path, data, error = value
            self._analysis_pending.discard(path)
            if error:
                self._append("Waveform: {}".format(error))
                if path == self._source_path:
                    self.metadata_var.set("Waveform unavailable")
                return
            self._analysis_cache[path] = data
            self._refresh_tracks()
        elif kind == "audio":
            process, line = value
            if process is not self._audio_process:
                return
            try:
                data = json.loads(line)
            except ValueError:
                self._append(line.strip())
                return
            event = data.get("event")
            if event in ("position", "ended"):
                self._audio_position = data.get("position", self._audio_position)
                self._audio_playing = data.get("playing", False)
                self.time_var.set(clock_time(self._audio_position))
                self.timeline.set_playhead(self._audio_position)
                self.timeline.set_playing(self._audio_playing)
                self.play_button.configure(text="Ⅱ" if self._audio_playing else "▶")
                self.level_meter.set_levels(data.get("left", 0), data.get("right", 0))
            elif event == "error":
                self.status_var.set("Audio preview: " + data.get("message", "unavailable"))
                self._append(self.status_var.get())
                self._audio_playing = False
                self.play_button.configure(text="▶")
        elif kind == "audio_done" and not self._closing:
            process, returncode = value
            if process is not self._audio_process:
                return
            self._audio_process = None
            self._audio_playing = False
            self.play_button.configure(text="▶")
            self.level_meter.set_levels(0, 0)

    def _studio_shutdown(self):
        self._closing = True
        self._audio_command("quit")
        process = self._audio_process
        if process is not None:
            def reap():
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    stop_preview_process(process, force=True)
                    process.wait()
            threading.Thread(target=reap, daemon=False).start()
        with self._analysis_lock:
            pending = list(self._analysis_processes)
        for process in pending:
            stop_preview_process(process)
            def reap_analysis(worker=process):
                try:
                    worker.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                stop_preview_process(worker, force=True)
            threading.Thread(target=reap_analysis, daemon=False).start()
