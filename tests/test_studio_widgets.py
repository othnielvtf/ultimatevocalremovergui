"""Audio-display math is checked without a display server or ML packages."""

import unittest

from gui_data.studio_widgets import display_peaks, format_time, ruler_step


class TimelineMathTests(unittest.TestCase):
    def test_elapsed_time_handles_long_tracks_and_millisecond_carry(self):
        self.assertEqual(format_time(748.356), "12:28")
        self.assertEqual(format_time(3601), "60:01")
        self.assertEqual(format_time(59.9999, precise=True), "01:00.000")
        self.assertEqual(format_time(-10), "00:00")

    def test_reduction_keeps_short_transients_and_quiet_audio_amplitude(self):
        self.assertEqual(display_peaks([0.01, 0.03, 0.95, 0.02, 0.04, 0.05], 3), [0.03, 0.95, 0.05])
        self.assertEqual(display_peaks([0.001] * 8, 2), [0.001, 0.001])
        self.assertEqual(display_peaks([], 100), [])

    def test_invalid_values_do_not_create_invalid_canvas_coordinates(self):
        self.assertEqual(display_peaks([float("nan"), float("inf"), None, -2], 4), [0.0, 0.0, 0.0, 1.0])
        self.assertEqual(format_time(float("nan")), "00:00")

    def test_ruler_marks_are_seconds_with_readable_spacing(self):
        self.assertEqual(ruler_step(2, 600), 1)
        self.assertEqual(ruler_step(748.356, 600), 120)
        self.assertGreaterEqual(ruler_step(10000, 100), 3600)

if __name__ == "__main__":
    unittest.main()
