"""Waveform analysis and an output-only playback service for UVR Modern.

The GUI starts this file with its isolated audio runtime. Importing the module
does not import NumPy, libsndfile, PortAudio, or access an audio device.
"""

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time


def audio_libraries():
    import numpy as np
    import soundfile as sf
    return np, sf


def decibels(amplitude):
    """Use a finite silence floor so protocol JSON never contains infinity."""
    return round(20 * math.log10(max(float(amplitude), 0.000001)), 2)


class AudioSource:
    """A streamable audio file, with a temporary FFmpeg decode when needed."""

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise ValueError("Audio file does not exist: {}".format(self.path))
        self._temporary = None
        self.file = None
        _, sf = audio_libraries()
        try:
            self.file = sf.SoundFile(str(self.path))
        except (RuntimeError, OSError):
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("This audio format needs FFmpeg, which was not found in the selected runtime.")
            self._temporary = tempfile.TemporaryDirectory(prefix="uvr-listen-")
            decoded = Path(self._temporary.name) / "decoded.wav"
            try:
                result = subprocess.run(
                    [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(self.path),
                     "-vn", "-acodec", "pcm_f32le", str(decoded)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode:
                    raise RuntimeError("Could not decode audio: {}".format(result.stderr.strip()[-1200:]))
                self.file = sf.SoundFile(str(decoded))
            except BaseException:
                self.close()
                raise
        if self.file.frames <= 0 or self.file.samplerate <= 0 or self.file.channels <= 0:
            self.close()
            raise ValueError("The audio file contains no playable samples.")

    def metadata(self):
        return {
            "path": str(self.path), "name": self.path.name,
            "duration": self.file.frames / self.file.samplerate,
            "sample_rate": self.file.samplerate, "channels": self.file.channels,
            "frames": self.file.frames,
        }

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def analyze(path, bins=1100):
    """Compute real whole-track envelopes using a bounded audio read buffer."""
    bins = int(bins)
    if not 64 <= bins <= 4096:
        raise ValueError("Waveform bins must be between 64 and 4096.")
    np, _ = audio_libraries()
    with AudioSource(path) as source:
        result = source.metadata()
        frames, channels = result["frames"], result["channels"]
        peaks = np.zeros(bins, dtype="float64")
        energy = np.zeros(bins, dtype="float64")
        counts = np.zeros(bins, dtype="int64")
        offset = 0
        while True:
            block = source.file.read(65536, dtype="float32", always_2d=True)
            if not len(block):
                break
            if not np.isfinite(block).all():
                raise ValueError("Audio contains non-finite samples.")
            consumed = 0
            while consumed < len(block):
                frame = offset + consumed
                index = min(bins - 1, frame * bins // frames)
                boundary = ((index + 1) * frames + bins - 1) // bins
                count = min(len(block) - consumed, max(1, boundary - frame))
                portion = block[consumed:consumed + count]
                peaks[index] = max(peaks[index], float(np.max(np.abs(portion))))
                energy[index] += float(np.square(portion, dtype="float64").sum())
                counts[index] += count * channels
                consumed += count
            offset += len(block)
        if offset != frames:
            raise ValueError("The audio ended before its advertised duration.")
        rms = np.sqrt(np.divide(energy, counts, out=np.zeros(bins), where=counts > 0))
        result.update({
            "peaks": np.minimum(peaks, 1).round(6).tolist(),
            "rms": np.minimum(rms, 1).round(6).tolist(),
            "peak_db": decibels(float(peaks.max())),
            "rms_db": decibels(math.sqrt(float(energy.sum()) / (frames * channels))),
        })
        return result


class AudioPlayer:
    """Bounded, streaming output playback controlled by the JSON service.

    A blocking PortAudio output stream runs on a regular audio thread, rather
    than performing disk reads inside a realtime PortAudio callback. A lock
    protects the file and stream; commands wait at most one small audio block.
    """

    def __init__(self, emit, stream_factory=None, clock=time.monotonic):
        self.emit = emit
        self.stream_factory = stream_factory
        self.clock = clock
        self.lock = threading.RLock()
        self.source = None
        self.stream = None
        self.playing = False
        self.gain = 0.8
        self.frame = 0
        self.left = 0.0
        self.right = 0.0
        self._last_report = 0.0
        self._closing = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.blocksize = 2048

    def position(self):
        rate = self.source.file.samplerate if self.source else 1
        duration = self.source.file.frames / rate if self.source else 0.0
        return {"position": self.frame / rate, "duration": duration,
                "playing": self.playing, "left": self.left, "right": self.right}

    def _publish_position(self):
        self.emit("position", **self.position())
        self._last_report = self.clock()

    def _close_stream(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.abort()
            finally:
                stream.close()

    def _ensure_stream(self):
        if self.stream is not None:
            return
        factory = self.stream_factory
        if factory is None:
            import sounddevice as sd
            factory = sd.OutputStream
        self.stream = factory(samplerate=self.source.file.samplerate, channels=2,
                              dtype="float32", blocksize=self.blocksize, latency="low")

    def command(self, item):
        if not isinstance(item, dict):
            raise ValueError("Playback commands must be JSON objects.")
        action = item.get("command")
        with self.lock:
            if action == "load":
                # Open first: an invalid replacement should not destroy the
                # currently loaded track or leave its file handle half closed.
                replacement = AudioSource(item.get("path", ""))
                self.playing = False
                self._close_stream()
                if self.source is not None:
                    self.source.close()
                self.source = replacement
                self.frame = 0
                self.left = self.right = 0.0
                self.emit("loaded", **self.source.metadata())
                self._publish_position()
            elif action == "gain":
                value = float(item.get("value", 0.8))
                if not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError("Playback gain must be between 0 and 1.")
                self.gain = value
                if not self.playing or not value:
                    self.left = self.right = 0.0
                self._publish_position()
            elif action == "quit":
                self._closing.set()
                self._wake.set()
                return False
            elif action in ("play", "pause", "stop", "seek"):
                if self.source is None:
                    raise ValueError("Load an audio file before using the transport.")
                if action == "play":
                    if self.frame >= self.source.file.frames:
                        self.source.file.seek(0)
                        self.frame = 0
                    try:
                        self._ensure_stream()
                        if not self.playing:
                            self.stream.start()
                    except Exception:
                        # A disconnected or unavailable device can be retried
                        # after the user reconnects it; do not retain a broken stream.
                        self._close_stream()
                        raise
                    self.playing = True
                    self._wake.set()
                elif action == "pause":
                    self.playing = False
                    if self.stream is not None:
                        self.stream.abort()
                    self.left = self.right = 0.0
                elif action == "stop":
                    self.playing = False
                    if self.stream is not None:
                        self.stream.abort()
                    self.source.file.seek(0)
                    self.frame = 0
                    self.left = self.right = 0.0
                else:
                    seconds = float(item.get("position", 0))
                    if not math.isfinite(seconds):
                        raise ValueError("Seek position must be a finite number.")
                    target = max(0, min(self.source.file.frames,
                                        int(seconds * self.source.file.samplerate)))
                    if self.stream is not None:
                        self.stream.abort()
                    self.source.file.seek(target)
                    self.frame = target
                    self.left = self.right = 0.0
                    if self.playing and self.stream is not None:
                        self.stream.start()
                self._publish_position()
            else:
                raise ValueError("Unknown playback command: {}".format(action))
        return True

    def tick(self):
        """Write one small block. Tests inject a silent output sink."""
        with self.lock:
            if not self.playing or self.source is None:
                return
            np, _ = audio_libraries()
            block = self.source.file.read(self.blocksize, dtype="float32", always_2d=True)
            if len(block):
                if block.shape[1] == 1:
                    block = np.repeat(block, 2, axis=1)
                else:
                    block = block[:, :2].copy()
                np.nan_to_num(block, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
                block *= self.gain
                np.clip(block, -1, 1, out=block)
                self.stream.write(block)
                self.frame = self.source.file.tell()
                self.left = float(np.max(np.abs(block[:, 0])))
                self.right = float(np.max(np.abs(block[:, 1])))
            if self.frame >= self.source.file.frames or not len(block):
                self.playing = False
                self.stream.stop()
                self.left = self.right = 0.0
                self._publish_position()
                self.emit("ended", **self.position())
            elif self.clock() - self._last_report >= 0.05:
                self._publish_position()

    def _run(self):
        while not self._closing.is_set():
            if not self.playing:
                self._wake.wait(0.05)
                self._wake.clear()
                continue
            try:
                self.tick()
            except Exception as error:
                with self.lock:
                    self.playing = False
                    self.left = self.right = 0.0
                    try:
                        self._close_stream()
                    except Exception:
                        pass
                    self.emit("error", message=str(error))
                    self._publish_position()

    def start_thread(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="uvr-audio-output", daemon=True)
            self._thread.start()

    def close(self):
        self._closing.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        with self.lock:
            self.playing = False
            self._close_stream()
            if self.source is not None:
                self.source.close()
                self.source = None


def serve(input_stream=None, output_stream=None, player_factory=AudioPlayer):
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    emit_lock = threading.Lock()

    def emit(event, **fields):
        with emit_lock:
            output_stream.write(json.dumps(dict(event=event, **fields), allow_nan=False) + "\n")
            output_stream.flush()

    player = player_factory(emit)
    player.start_thread()
    emit("ready")
    try:
        for line in input_stream:
            try:
                item = json.loads(line)
                if not player.command(item):
                    break
            except Exception as error:
                emit("error", message=str(error))
    finally:
        player.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--analyze", metavar="FILE", help="Print real waveform envelopes and audio metadata as JSON.")
    actions.add_argument("--serve", action="store_true", help="Read JSON transport commands from stdin.")
    parser.add_argument("--bins", type=int, default=1100, help="Waveform envelope bins (64-4096).")
    args = parser.parse_args(argv)
    os.environ["PATH"] = str(Path(sys.executable).absolute().parent) + os.pathsep + os.environ.get("PATH", "")
    try:
        if args.analyze is not None:
            print(json.dumps(analyze(args.analyze, args.bins), allow_nan=False), flush=True)
            return 0
        return serve()
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(json.dumps({"event": "error", "message": str(error)}), flush=True)
        return 1


def _terminated(signum, frame):
    raise KeyboardInterrupt()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _terminated)
    raise SystemExit(main())
