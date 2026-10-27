"""Regression tests for studio session/worker state without a Tk display."""

import json
from pathlib import Path
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gui_data import studio_view as studio
from gui_data.studio_widgets import StudioTimeline


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def transport():
    return SimpleNamespace(
        _source_path="/audio/old.wav", _active_track="source",
        _audio_path="/audio/old.wav", _audio_duration=60.0,
        _audio_position=12.0, _audio_playing=True, _closing=False,
        _audio_process=Mock(), _tracks=[],
        _analysis_cache={"/audio/old.wav": {"duration": 60}},
        time_var=Value("00:12.000"), duration_var=Value("/ 01:00.000"),
        session_var=Value("old"), metadata_var=Value("44.1 kHz"),
        track_count_var=Value("7 TRACK LANES"),
        status_var=Value("Ready"), gain_var=Value(0.8),
        play_button=Mock(), level_meter=Mock(), timeline=Mock(),
        _audio_command=Mock(), _append=Mock(),
    )


class SessionStateTests(unittest.TestCase):
    def test_removing_last_file_cannot_restart_removed_audio(self):
        view = transport()
        view.inputs = []
        view.file_list = Mock()
        view.library_count_var = Value("1 file")
        view._stop_audio = lambda: studio.StudioViewMixin._stop_audio(view)
        studio.StudioViewMixin._sync_library(view)
        studio.StudioViewMixin._toggle_audio(view)
        self.assertIsNone(view._source_path)
        self.assertIsNone(view._audio_path)
        self.assertEqual(view._audio_duration, 0)
        self.assertEqual(view.track_count_var.get(), "3 TRACK LANES")
        self.assertIn("00:00.000", view.duration_var.get())
        self.assertFalse(any(call.args[0] == "play" for call in view._audio_command.call_args_list))

    def test_preparing_same_track_recovers_after_preview_worker_exits(self):
        view = transport()
        view._audio_process.poll.return_value = 1
        studio.StudioViewMixin._prepare_audio(view, "/audio/old.wav")
        self.assertTrue(any(call.args[0] == "load" and call.kwargs.get("create")
                            for call in view._audio_command.call_args_list))

    def test_switching_stems_preserves_audition_time_and_play_state(self):
        view = transport()
        view._analysis_cache["/audio/vocals.wav"] = {"duration": 60}
        view._prepare_audio = Mock()
        view._seek_audio = Mock()
        studio.StudioViewMixin._track_selected(view, {"id": "vocals", "path": "/audio/vocals.wav"})
        self.assertEqual(view._active_track, "vocals")
        view._seek_audio.assert_called_once_with(12)
        self.assertTrue(any(call.args[0] == "play" for call in view._audio_command.call_args_list))

    def test_stale_worker_position_cannot_move_current_transport(self):
        view = transport()
        old_process = Mock()
        event = json.dumps({"event": "position", "position": 40,
                            "playing": False, "left": 0, "right": 0})
        studio.StudioViewMixin._studio_event(view, "audio", (old_process, event))
        self.assertEqual(view._audio_position, 12)
        self.assertTrue(view._audio_playing)
        view.timeline.set_playhead.assert_not_called()

    def test_stale_worker_exit_cannot_stop_replacement_worker(self):
        view = transport()
        current = view._audio_process
        studio.StudioViewMixin._studio_event(view, "audio_done", (Mock(), 1))
        self.assertIs(view._audio_process, current)
        self.assertTrue(view._audio_playing)
        view.level_meter.set_levels.assert_not_called()

    def test_analysis_failure_retains_the_backend_error_message(self):
        process = Mock(returncode=1)
        process.communicate.return_value = (
            json.dumps({"event": "error", "message": "Unsupported audio codec"}) + "\n", "")
        process.poll.return_value = 1
        view = SimpleNamespace(
            _analysis_pending=set(), _analysis_processes=set(), _closing=False,
            _analysis_lock=threading.Lock(), _events=queue.Queue(),
            _audio_worker_command=Mock(return_value=["python", "studio_audio.py"]),
            app_dir=Path.cwd(),
        )

        def inline_thread(target, daemon):
            return SimpleNamespace(start=target)

        with patch.object(studio.subprocess, "Popen", return_value=process), \
                patch.object(studio.threading, "Thread", side_effect=inline_thread):
            studio.StudioViewMixin._analyze(view, "/audio/broken.m4a")
        kind, (_, data, error) = view._events.get_nowait()
        self.assertEqual(kind, "analysis")
        self.assertIsNone(data)
        self.assertIn("Unsupported audio codec", error)


class TimelineInteractionTests(unittest.TestCase):
    def test_empty_and_loading_lanes_do_not_select_or_seek_another_track(self):
        for track in (
            {"id": "vocals", "name": "Vocals"},
            {"id": "vocals", "path": "/audio/vocals.wav", "peaks": []},
        ):
            with self.subTest(track=track):
                timeline = SimpleNamespace(
                    _rows=[(37, 137, track)], HEADER_WIDTH=142, duration=60,
                    set_active=Mock(), set_playhead=Mock(), focus_set=Mock(),
                    on_select=Mock(), on_seek=Mock(), _time=Mock(return_value=30),
                )
                StudioTimeline._press(timeline, SimpleNamespace(x=200, y=75))
                StudioTimeline._drag(timeline, SimpleNamespace(x=250, y=75))
                timeline.set_active.assert_not_called()
                timeline.on_select.assert_not_called()
                timeline.on_seek.assert_not_called()

    def test_loaded_lane_remains_selectable_and_seekable(self):
        track = {"id": "source", "path": "/audio/source.wav", "peaks": [0.1, 0.5]}
        timeline = SimpleNamespace(
            _rows=[(37, 137, track)], HEADER_WIDTH=142, duration=60,
            position=0, set_active=Mock(), on_select=Mock(), on_seek=Mock(), focus_set=Mock(),
            _time=Mock(return_value=30),
        )
        timeline.set_playhead = lambda value: setattr(timeline, "position", value)
        StudioTimeline._press(timeline, SimpleNamespace(x=200, y=75))
        timeline.on_select.assert_called_once_with(track)
        timeline.on_seek.assert_called_once_with(30)


if __name__ == "__main__":
    unittest.main()
