"""Tk-only waveform lanes for the separation studio.

Waveforms always use supplied audio analysis. Empty and loading lanes contain
no synthetic audio, and the ruler is elapsed time rather than an assumed BPM.
"""

import math
import tkinter as tk
from tkinter import font as tkfont


COLORS = {
    "background": "#0b0e13", "panel": "#141922", "grid": "#242c38",
    "text": "#e6eaf2", "muted": "#8893a6", "teal": "#53d7b4",
    "violet": "#b19cff", "gold": "#f6be68",
}


def format_time(seconds, precise=False):
    """Format elapsed audio time without wrapping minutes at one hour."""
    seconds = max(0.0, float(seconds or 0))
    if not math.isfinite(seconds):
        seconds = 0
    if precise:
        total_ms = round(seconds * 1000)
        return "{:02d}:{:02d}.{:03d}".format(total_ms // 60000, (total_ms // 1000) % 60, total_ms % 1000)
    whole = int(seconds)
    return "{:02d}:{:02d}".format(whole // 60, whole % 60)


def display_peaks(peaks, bins):
    """Reduce a waveform by maximum amplitude, preserving short transients."""
    values = []
    for value in peaks:
        try:
            value = abs(float(value))
            values.append(min(1.0, value) if math.isfinite(value) else 0.0)
        except (TypeError, ValueError):
            values.append(0.0)
    if not values or bins < 1:
        return []
    bins = min(int(bins), len(values))
    return [max(values[index * len(values) // bins:(index + 1) * len(values) // bins])
            for index in range(bins)]


def ruler_step(duration, pixels):
    """Choose readable time marks with at least about 74 pixels per label."""
    needed = max(1.0, float(duration) * 74 / max(74, pixels))
    steps = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
    return next((step for step in steps if step >= needed), math.ceil(needed / 3600) * 3600)


def mix_color(first, second, amount):
    channels = []
    for offset in (1, 3, 5):
        a, b = int(first[offset:offset + 2], 16), int(second[offset:offset + 2], 16)
        channels.append(round(a * (1 - amount) + b * amount))
    return "#{:02x}{:02x}{:02x}".format(*channels)


class StudioTimeline(tk.Canvas):
    """Elapsed-time waveform lanes with selection, seeking and zoom.

    ``tracks`` are dictionaries with id, name, subtitle, color, peaks, duration,
    status and path. Selection calls on_select(track); seeking calls
    on_seek(seconds). Drawn amplitudes are normalized only to the fixed 0..1
    canvas scale, so quiet stems remain visibly quiet.
    """

    HEADER_WIDTH = 142
    RULER_HEIGHT = 37
    FOOTER_HEIGHT = 26

    def __init__(self, parent, on_select=None, on_seek=None, **kwargs):
        kwargs.setdefault("background", COLORS["background"])
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("borderwidth", 0)
        kwargs.setdefault("height", 365)
        kwargs.setdefault("width", 700)
        super().__init__(parent, **kwargs)
        self.on_select = on_select
        self.on_seek = on_seek
        self.tracks = []
        self.active_id = None
        self.position = 0.0
        self.duration = 0.0
        self.zoom = 1.0
        self.offset = 0.0
        self.playing = False
        self._redraw_id = None
        self._rows = []
        self._dragging = False
        self._track_scroll = 0.0
        self._max_track_scroll = 0.0
        self._lane_height = 90.0
        self._name_font = tkfont.Font(self, family="Helvetica Neue", size=10, weight="bold")
        self._small_font = tkfont.Font(self, family="Helvetica Neue", size=9)
        self._mono_font = tkfont.Font(self, family="Menlo", size=9)
        self.bind("<Configure>", self._schedule_draw)
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda event: setattr(self, "_dragging", False))
        self.bind("<Shift-MouseWheel>", self._scroll)
        self.bind("<MouseWheel>", self._vertical_scroll)
        self.bind("<Button-4>", self._vertical_scroll)
        self.bind("<Button-5>", self._vertical_scroll)
        self.bind("<Destroy>", self._destroyed, add="+")
        self.set_tracks([])

    def set_tracks(self, tracks):
        self.tracks = [dict(track) for track in tracks] or [
            {"id": "source", "name": "Original", "subtitle": "SOURCE AUDIO", "color": COLORS["teal"]},
            {"id": "vocals", "name": "Vocals", "subtitle": "SEPARATED STEM", "color": COLORS["violet"]},
            {"id": "instrumental", "name": "Instrumental", "subtitle": "SEPARATED STEM", "color": COLORS["gold"]},
        ]
        for index, track in enumerate(self.tracks):
            track.setdefault("id", str(index))
            track.setdefault("name", "Track {}".format(index + 1))
            track.setdefault("color", COLORS["teal"])
            track.setdefault("peaks", [])
            track.setdefault("duration", 0)
        self.duration = max((float(track.get("duration") or 0) for track in self.tracks), default=0.0)
        if self.active_id not in [track["id"] for track in self.tracks]:
            self.active_id = next((track["id"] for track in self.tracks if track.get("path")), self.tracks[0]["id"])
        self.position = min(self.position, self.duration)
        self.offset = min(self.offset, max(0, self.duration - self._visible_duration()))
        self._schedule_draw()

    def set_active(self, track_id):
        if track_id != self.active_id:
            self.active_id = track_id
            for index, track in enumerate(self.tracks):
                if track["id"] == track_id:
                    viewport = max(1, self.winfo_height() - self.RULER_HEIGHT - self.FOOTER_HEIGHT)
                    top, bottom = index * self._lane_height, (index + 1) * self._lane_height
                    if top < self._track_scroll:
                        self._track_scroll = top
                    elif bottom > self._track_scroll + viewport:
                        self._track_scroll = max(0, bottom - viewport)
                    break
            self._schedule_draw()

    def set_playhead(self, seconds):
        self.position = max(0, min(float(seconds), self.duration))
        visible = self._visible_duration()
        if self.playing and not self.offset <= self.position <= self.offset + visible:
            self.offset = max(0, min(self.position, max(0, self.duration - visible)))
            self._schedule_draw()
        else:
            self._draw_playhead()

    def set_playing(self, playing):
        self.playing = bool(playing)

    def set_zoom(self, zoom):
        self.zoom = max(1.0, min(4.0, float(zoom)))
        self.offset = max(0, min(self.position - self._visible_duration() * 0.2,
                                 max(0, self.duration - self._visible_duration())))
        self._schedule_draw()

    def _visible_duration(self):
        return (self.duration or 60.0) / self.zoom

    def _wave_width(self):
        return max(1, self.winfo_width() - self.HEADER_WIDTH - 16)

    def _x(self, seconds):
        return self.HEADER_WIDTH + 8 + (seconds - self.offset) / self._visible_duration() * self._wave_width()

    def _time(self, x):
        seconds = self.offset + (x - self.HEADER_WIDTH - 8) / self._wave_width() * self._visible_duration()
        return max(0, min(self.duration, seconds))

    def _schedule_draw(self, event=None):
        if self._redraw_id is None:
            self._redraw_id = self.after_idle(self._draw)

    def _draw(self):
        self._redraw_id = None
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 10 or height < 10:
            return
        visible = self._visible_duration()
        step = ruler_step(visible, self._wave_width())
        marker = math.ceil(self.offset / step) * step
        while marker <= self.offset + visible + 0.001:
            x = self._x(marker)
            self.create_line(x, self.RULER_HEIGHT - 8, x, height - self.FOOTER_HEIGHT,
                             fill=COLORS["grid"], dash=(1, 5))
            marker += step
        viewport = max(1, height - self.RULER_HEIGHT - self.FOOTER_HEIGHT)
        lane_height = max(44, viewport / len(self.tracks))
        self._lane_height = lane_height
        self._max_track_scroll = max(0, lane_height * len(self.tracks) - viewport)
        self._track_scroll = min(self._track_scroll, self._max_track_scroll)
        self._rows = []
        for index, track in enumerate(self.tracks):
            top = self.RULER_HEIGHT + index * lane_height - self._track_scroll
            bottom = top + lane_height
            if bottom <= self.RULER_HEIGHT or top >= height - self.FOOTER_HEIGHT:
                continue
            self._rows.append((max(self.RULER_HEIGHT, top), min(height - self.FOOTER_HEIGHT, bottom), track))
            compact = lane_height < 68
            selected = track["id"] == self.active_id
            color = track["color"]
            self.create_rectangle(0, top, self.HEADER_WIDTH, top + lane_height,
                                  fill="#191f2a" if selected else COLORS["panel"], outline="")
            self.create_rectangle(0, top + 12, 3, top + lane_height - 12, fill=color, outline="")
            self.create_text(14, top + (16 if compact else 22), text="{:02d}".format(index + 1), anchor="w", font=self._mono_font, fill=color)
            self.create_text(39, top + (16 if compact else 22), text=self._elide(track["name"], self.HEADER_WIDTH - 47),
                             anchor="w", font=self._name_font, fill=COLORS["text"])
            if not compact:
                self.create_text(14, top + 44, text=self._elide(track.get("subtitle", "AUDIO TRACK"), self.HEADER_WIDTH - 25),
                                 anchor="w", font=self._small_font, fill=COLORS["muted"])
            status = track.get("status") or ("READY" if track.get("peaks") else "EMPTY")
            self.create_text(14, top + lane_height - (11 if compact else 17), text=str(status).upper(), anchor="w",
                             font=self._small_font, fill=color if track.get("peaks") else COLORS["muted"])
            self._draw_clip(track, top + (4 if compact else 9), top + lane_height - (4 if compact else 9), selected)
            self.create_line(0, top + lane_height, width, top + lane_height, fill=COLORS["grid"])
        # The ruler and footer mask scrolled lane content so both remain fixed.
        self.create_rectangle(0, 0, width, self.RULER_HEIGHT, fill="#10151d", outline="")
        self.create_text(16, 18, text="TRACKS", anchor="w", font=self._small_font, fill=COLORS["muted"])
        marker = math.ceil(self.offset / step) * step
        while marker <= self.offset + visible + 0.001:
            x = self._x(marker)
            self.create_line(x, self.RULER_HEIGHT - 8, x, self.RULER_HEIGHT, fill=COLORS["grid"])
            if x < width - 46:
                self.create_text(x + 3, 14, text=format_time(marker), anchor="w", font=self._mono_font, fill=COLORS["muted"])
            marker += step
        footer_y = height - self.FOOTER_HEIGHT
        self.create_rectangle(0, footer_y, width, height, fill="#10151d", outline="")
        hint = ("SCROLL FOR MORE TRACKS  ·  CLICK WAVEFORM TO SEEK" if self._max_track_scroll else
                "CLICK A LANE TO AUDITION  ·  CLICK THE WAVEFORM TO SEEK")
        self.create_text(14, footer_y + 13, text=hint, anchor="w",
                         font=self._small_font, fill=COLORS["muted"])
        if self._max_track_scroll:
            thumb_height = max(18, viewport * viewport / (lane_height * len(self.tracks)))
            thumb_top = self.RULER_HEIGHT + (viewport - thumb_height) * self._track_scroll / self._max_track_scroll
            self.create_rectangle(width - 5, thumb_top, width - 2, thumb_top + thumb_height,
                                  fill=COLORS["muted"], outline="")
        self._draw_playhead()

    def _draw_clip(self, track, top, bottom, selected):
        left, right = self.HEADER_WIDTH + 8, self.winfo_width() - 8
        color = track["color"]
        peaks = track.get("peaks") or []
        loaded = bool(peaks)
        self.create_rectangle(left, top, right, bottom,
                              fill=mix_color("#10151d", color, 0.08 if loaded else 0.015),
                              outline=mix_color(COLORS["grid"], color, 0.65 if selected and loaded else 0.12),
                              dash=() if loaded else (4, 4))
        if not loaded:
            loading = str(track.get("status", "")).lower() in ("loading", "analyzing")
            message = "Analyzing audio…" if loading else ("Import an audio file to begin" if track["id"] in ("source", "original")
                                                           else "Ready for separation")
            self.create_text((left + right) / 2, (top + bottom) / 2, text=message,
                             font=self._small_font, fill=COLORS["muted"])
            return
        duration = float(track.get("duration") or self.duration or 1)
        first = max(0, int(len(peaks) * self.offset / duration))
        last = min(len(peaks), math.ceil(len(peaks) * (self.offset + self._visible_duration()) / duration))
        values = display_peaks(peaks[first:last], max(1, int((right - left) / 2)))
        if not values:
            return
        compact = bottom - top < 52
        center, amplitude = (top + bottom) / 2 + (0 if compact else 6), max(4, (bottom - top) * (0.40 if compact else 0.33))
        self.create_line(left + 1, center, right - 1, center, fill=mix_color(COLORS["grid"], color, 0.2))
        for index, peak in enumerate(values):
            x = left + 2 + index / max(1, len(values) - 1) * max(1, right - left - 4)
            self.create_line(x, center - peak * amplitude, x, center + peak * amplitude,
                             fill=color, width=1.3)
        if not compact:
            self.create_text(left + 10, top + 12, text=self._elide(track.get("filename") or track.get("name", "Audio"), right - left - 18),
                             anchor="w", font=self._small_font, fill=mix_color(COLORS["text"], color, 0.3))

    def _draw_playhead(self):
        self.delete("playhead")
        if not self.duration or not self.offset <= self.position <= self.offset + self._visible_duration():
            return
        x = self._x(self.position)
        self.create_line(x, 30, x, self.winfo_height() - self.FOOTER_HEIGHT,
                         fill="#f3f6fc", width=1, tags="playhead")
        self.create_polygon(x - 5, 24, x + 5, 24, x, 31, fill="#f3f6fc", outline="", tags="playhead")

    def _elide(self, value, pixels):
        value = str(value)
        if self._small_font.measure(value) <= pixels:
            return value
        while value and self._small_font.measure(value + "…") > pixels:
            value = value[:-1]
        return value + "…"

    def _press(self, event):
        self.focus_set()
        self._dragging = False
        for top, bottom, track in self._rows:
            if top <= event.y < bottom:
                if not track.get("path") or not track.get("peaks"):
                    return
                self.set_active(track["id"])
                if self.on_select:
                    self.on_select(track)
                break
        if event.x >= self.HEADER_WIDTH and self.duration:
            self._dragging = True
            self.set_playhead(self._time(event.x))
            if self.on_seek:
                self.on_seek(self.position)

    def _drag(self, event):
        if self._dragging and event.x >= self.HEADER_WIDTH and self.duration:
            self.set_playhead(self._time(event.x))
            if self.on_seek:
                self.on_seek(self.position)

    def _scroll(self, event):
        if self.zoom > 1:
            direction = -1 if event.delta > 0 else 1
            self.offset = max(0, min(self.offset + direction * self._visible_duration() * 0.1,
                                     max(0, self.duration - self._visible_duration())))
            self._schedule_draw()
        return "break"

    def _vertical_scroll(self, event):
        if self._max_track_scroll:
            delta = getattr(event, "delta", 0)
            direction = -1 if delta > 0 or getattr(event, "num", None) == 4 else 1
            units = max(1, abs(delta) // 120)
            self._track_scroll = max(0, min(self._track_scroll + direction * units * self._lane_height * 0.5,
                                            self._max_track_scroll))
            self._schedule_draw()
        return "break"

    def _destroyed(self, event):
        if event.widget is self and self._redraw_id is not None:
            self.after_cancel(self._redraw_id)
            self._redraw_id = None
