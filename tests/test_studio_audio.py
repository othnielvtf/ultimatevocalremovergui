"""Audio analysis and transport tests; no real output device is opened."""

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import studio_audio as audio


HAS_AUDIO = all(importlib.util.find_spec(name) is not None for name in ("numpy", "soundfile"))


class ProtocolTests(unittest.TestCase):
    def test_help_does_not_import_or_open_an_audio_device(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(Path(audio.__file__)), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--analyze", result.stdout)
        self.assertIn("--serve", result.stdout)

    def test_silence_has_finite_decibel_value(self):
        self.assertEqual(audio.decibels(0), -120)
        self.assertEqual(audio.decibels(1), 0)

    def test_server_reports_invalid_commands_and_keeps_reading(self):
        commands = io.StringIO('invalid\n[]\n{"command":"play"}\n{"command":"quit"}\n')
        output = io.StringIO()
        audio.serve(commands, output)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[0]["event"], "ready")
        self.assertEqual(sum(item["event"] == "error" for item in events), 3)


@unittest.skipUnless(HAS_AUDIO, "NumPy and SoundFile are needed for real audio fixtures")
class AudioTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        import soundfile as sf
        self.np = np
        self.sf = sf
        self.temporary = tempfile.TemporaryDirectory(prefix="uvr audio ")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "sample track.wav"
        self.rate = 8000
        data = np.zeros((8000, 2), dtype="float32")
        data[:4000, 0] = 0.5
        data[4000:, 1] = -0.25
        sf.write(str(self.path), data, self.rate, subtype="FLOAT")

    def test_waveform_is_binned_from_real_samples_without_normalizing_silence(self):
        result = audio.analyze(self.path, 64)
        self.assertEqual(result["frames"], 8000)
        self.assertEqual(result["channels"], 2)
        self.assertEqual(result["sample_rate"], 8000)
        self.assertEqual(result["duration"], 1)
        self.assertEqual(result["peaks"][:32], [0.5] * 32)
        self.assertEqual(result["peaks"][32:], [0.25] * 32)
        self.assertAlmostEqual(result["rms"][0], (0.25 / 2) ** 0.5, places=6)
        self.assertAlmostEqual(result["peak_db"], -6.02, places=2)

    def test_short_and_silent_files_keep_requested_bin_count(self):
        self.sf.write(str(self.path), self.np.zeros(8), self.rate, subtype="FLOAT")
        result = audio.analyze(self.path, 1100)
        self.assertEqual(len(result["peaks"]), 1100)
        self.assertEqual(result["peaks"], [0] * 1100)
        self.assertEqual(result["rms_db"], -120)
        self.assertEqual(result["channels"], 1)

    def test_nonfinite_samples_are_not_rendered_as_a_plausible_waveform(self):
        self.sf.write(str(self.path), self.np.array([0, float("nan"), 0]), self.rate, subtype="FLOAT")
        with self.assertRaisesRegex(ValueError, "non-finite"):
            audio.analyze(self.path)

    def test_invalid_paths_and_bins_fail_cleanly(self):
        with self.assertRaises(ValueError):
            audio.analyze(self.path, 63)
        with self.assertRaises(ValueError):
            audio.analyze(self.path, 4097)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            audio.analyze(self.path.with_name("missing.wav"))

    def player(self):
        self.events = []
        self.outputs = []
        outputs = self.outputs
        class SilentStream:
            def __init__(self, **options):
                self.options = options
                self.started = False
                self.closed = False
                outputs.append(self)
            def start(self):
                self.started = True
            def abort(self):
                self.started = False
            def stop(self):
                self.started = False
            def close(self):
                self.closed = True
            def write(self, data):
                self.last = data.copy()
        player = audio.AudioPlayer(lambda event, **fields: self.events.append(dict(event=event, **fields)),
                                   stream_factory=SilentStream, clock=lambda: 1.0)
        self.addCleanup(player.close)
        return player

    def test_play_pause_seek_stop_and_metering_use_delivered_audio(self):
        player = self.player()
        player.command({"command": "load", "path": str(self.path)})
        self.assertEqual(self.outputs, [])  # Merely loading never touches a device.
        player.command({"command": "gain", "value": 0.5})
        player.command({"command": "play"})
        player.tick()
        self.assertAlmostEqual(player.position()["position"], 2048 / 8000)
        self.assertAlmostEqual(player.position()["left"], 0.25)
        self.assertEqual(player.position()["right"], 0)
        player.command({"command": "pause"})
        paused = player.frame
        player.tick()
        self.assertEqual(player.frame, paused)
        player.command({"command": "seek", "position": 0.75})
        self.assertEqual(player.frame, 6000)
        player.command({"command": "play"})
        player.tick()
        self.assertFalse(player.playing)
        self.assertEqual(self.events[-1]["event"], "ended")
        self.assertAlmostEqual(float(self.outputs[0].last[-1, 1]), -0.125)
        player.command({"command": "stop"})
        self.assertEqual(player.frame, 0)
        self.assertEqual(player.position()["left"], 0)

    def test_mono_repeats_to_stereo_and_seek_is_clamped(self):
        self.sf.write(str(self.path), self.np.ones(8000) * 0.4, self.rate, subtype="FLOAT")
        player = self.player()
        player.command({"command": "load", "path": str(self.path)})
        player.command({"command": "seek", "position": -2})
        self.assertEqual(player.frame, 0)
        player.command({"command": "play"})
        player.tick()
        delivered = self.outputs[0].last
        self.assertEqual(delivered.shape, (2048, 2))
        self.np.testing.assert_array_equal(delivered[:, 0], delivered[:, 1])
        player.command({"command": "seek", "position": 99})
        self.assertEqual(player.frame, 8000)
        player.command({"command": "play"})
        self.assertEqual(player.frame, 0)

    def test_invalid_replacement_keeps_loaded_track_and_closes_stream_on_new_track(self):
        player = self.player()
        player.command({"command": "load", "path": str(self.path)})
        player.command({"command": "play"})
        with self.assertRaises(ValueError):
            player.command({"command": "load", "path": str(self.path.with_name("missing.wav"))})
        self.assertTrue(player.playing)
        self.assertEqual(player.source.path, self.path.resolve())
        player.command({"command": "load", "path": str(self.path)})
        self.assertTrue(self.outputs[0].closed)
        self.assertFalse(player.playing)

    def test_gain_and_seek_reject_nonfinite_values(self):
        player = self.player()
        player.command({"command": "load", "path": str(self.path)})
        for gain in [-1, 2, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                player.command({"command": "gain", "value": gain})
        with self.assertRaises(ValueError):
            player.command({"command": "seek", "position": float("nan")})


if __name__ == "__main__":
    unittest.main()
