"""Isolated worker and CLI for current audio-separator models.

Run this file with the Python executable from the modern-model virtual
environment. Importing it to read MODEL_PRESETS never imports ML dependencies.
Worker stdout contains JSON lines; library logs and progress go to stderr.
"""

import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile


MODEL_PRESETS = [
    {
        "id": "melband-vocals",
        "name": "MelBand Roformer - Vocals (Kim FT2)",
        "filename": "mel_band_roformer_kim_ft2_unwa.ckpt",
        "description": "Separate vocals and instrumental with unwa's fine-tuned Kimberley Jensen model.",
    },
    {
        "id": "bs-vocals",
        "name": "BS-Roformer - Vocals (Viperx)",
        "filename": "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
        "description": "General vocal and instrumental separation with the Viperx BS-Roformer model.",
    },
    {
        "id": "melband-viperx",
        "name": "MelBand Roformer - Vocals (Viperx)",
        "filename": "model_mel_band_roformer_ep_3005_sdr_11.4360.ckpt",
        "description": "An alternative vocal and instrumental model for comparing separation results.",
    },
    {
        "id": "melband-instrumental",
        "name": "MelBand Roformer - Instrumental (becruily)",
        "filename": "mel_band_roformer_instrumental_becruily.ckpt",
        "description": "A model trained for extracting the instrumental backing track.",
    },
    {
        "id": "melband-karaoke",
        "name": "MelBand Roformer - Karaoke (aufr33 / Viperx)",
        "filename": "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt",
        "description": "Separate lead vocals while retaining backing vocals in the karaoke track.",
    },
    {
        "id": "melband-denoise",
        "name": "MelBand Roformer - Denoise (aufr33)",
        "filename": "denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt",
        "description": "Reduce noise in a vocal or speech recording.",
    },
    {
        "id": "bs-dereverb",
        "name": "BS-Roformer - De-reverb",
        "filename": "deverb_bs_roformer_8_384dim_10depth.ckpt",
        "description": "Separate reverberation from a recording, useful after extracting vocals.",
    },
    {
        "id": "bs-six-stem",
        "name": "BS-Roformer SW - Multiple stems (jarredou)",
        "filename": "BS-Roformer-SW.ckpt",
        "description": "Extract multiple instrument stems using jarredou's BS-Roformer SW model.",
    },
]

BACKEND_VERSION = "0.47.1"
DEFAULT_MODEL_DIR = Path(__file__).resolve().parent / "models" / "Modern_Models"


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list-presets", action="store_true", help="Print bundled presets as JSON, without loading ML packages.")
    actions.add_argument("--check", action="store_true", help="Check the selected Python environment and backend import.")
    actions.add_argument("--list-models", action="store_true", help="Print the backend's current model catalog as JSON.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--model", help="A filename from the audio-separator model catalog.")
    selection.add_argument("--preset", choices=[p["id"] for p in MODEL_PRESETS], help="A bundled model preset (default: melband-vocals).")
    parser.add_argument("--output-dir", default="modern_outputs", help="Output folder; each input gets a new subfolder to avoid overwrites.")
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR), help="Persistent cache for model weights and YAML configuration.")
    parser.add_argument("--output-format", type=str.upper, choices=("WAV", "FLAC", "MP3"), default="WAV")
    parser.add_argument("--device", choices=("auto", "cpu"), default="auto", help="Auto selects CUDA, Apple MPS, or CPU.")
    parser.add_argument("--native-fp16", action="store_true", help="Use faster float16 inference on supported Roformer GPU models; ignored for forced CPU.")
    parser.add_argument("--single-stem", help="Only save this model stem, e.g. Vocals or Instrumental; omit to save all stems.")
    parser.add_argument("--overlap", type=int, help="Roformer overlapping prediction windows (2-50); default uses the model's configuration.")
    parser.add_argument("--download-only", action="store_true", help="Download the model and its configuration without running inference.")
    parser.add_argument("inputs", nargs="*", help="Input audio files; use -- before filenames beginning with '-'.")
    return parser


def _model_filename(args):
    if args.model:
        filename = args.model
    else:
        preset_id = args.preset or MODEL_PRESETS[0]["id"]
        filename = next(p["filename"] for p in MODEL_PRESETS if p["id"] == preset_id)
    if (not filename or filename in (".", "..") or "/" in filename or "\\" in filename
            or any(ord(char) < 32 for char in filename)
            or Path(filename).suffix.lower() not in (".ckpt", ".pth", ".onnx", ".yaml", ".yml")):
        raise ValueError("Model must be a catalog filename ending in .ckpt, .pth, .onnx, or .yaml, without directory components.")
    return filename


def _isolate_import_path():
    """Avoid shadowing the backend's Demucs with this app's vendored version.

    The worker has no further repository imports after this point. This also
    removes an empty sys.path entry when the app directory is the current cwd.
    """
    repo_dir = Path(__file__).resolve().parent
    sys.path[:] = [entry for entry in sys.path if Path(entry or os.curdir).resolve() != repo_dir]


def _load_backend():
    if sys.version_info < (3, 10):
        raise RuntimeError("Modern models require a separate Python 3.10 or newer environment. Run scripts/install_modern.py with a current Python.")
    _isolate_import_path()
    from importlib import metadata
    try:
        version = metadata.version("audio-separator")
    except metadata.PackageNotFoundError as error:
        raise RuntimeError("audio-separator is not installed in this Python environment. Run scripts/install_modern.py first.") from error
    if version != BACKEND_VERSION:
        raise RuntimeError("This worker requires audio-separator=={}; found {}. Run scripts/install_modern.py to update the isolated environment.".format(BACKEND_VERSION, version))
    from audio_separator.separator import Separator
    return _atomic_backend(Separator), version


def _atomic_backend(separator_class):
    """Publish downloaded weights only after the complete transfer succeeds.

    Upstream skips any existing final filename, including interrupted partial
    downloads. Keep partial files in a separate directory so cancelling and
    restarting the worker cannot turn an incomplete file into a cache hit.
    """
    class AtomicSeparator(separator_class):
        def download_file_if_not_exists(self, url, output_path):
            destination = Path(output_path)
            if destination.is_file():
                return
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".uvr-download-", dir=str(destination.parent)) as staging_dir:
                staging_file = Path(staging_dir) / destination.name
                super().download_file_if_not_exists(url, str(staging_file))
                if not staging_file.is_file() or staging_file.stat().st_size == 0:
                    raise RuntimeError("The model download returned an empty file: {}".format(destination.name))
                os.replace(str(staging_file), str(destination))

    return AtomicSeparator


def _unique_output_directory(parent, audio_path):
    """Reserve a directory atomically, including when files share a basename."""
    parent.mkdir(parents=True, exist_ok=True)
    # Truncate a long source name so suffixes fit typical filesystem limits.
    stem = audio_path.stem[:100].rstrip(" .") or "audio"
    candidate = parent / stem
    number = 2
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            candidate = parent / "{} ({})".format(stem, number)
            number += 1


def _verified_output_paths(files, output_dir):
    """Handle backend versions returning either absolute paths or basenames."""
    if not isinstance(files, (list, tuple)) or not files:
        raise RuntimeError("The model returned no output files. Check that the requested stem is supported by this model.")
    verified = []
    for value in files:
        path = Path(value)
        candidates = [path] if path.is_absolute() else [output_dir / path, Path.cwd() / path]
        found = None
        for candidate in candidates:
            candidate = candidate.resolve()
            try:
                candidate.relative_to(output_dir)
            except ValueError:
                continue
            if candidate.is_file():
                found = candidate
                break
        if found is None:
            raise RuntimeError("The backend did not create the expected output file in {}: {}".format(output_dir, value))
        verified.append(str(found))
    return verified


def _create_separator(separator_class, args):
    options = {
        "model_file_dir": str(Path(args.model_dir).expanduser().resolve()),
        "output_dir": str(Path(args.output_dir).expanduser().resolve()),
        "output_format": args.output_format,
        "output_single_stem": args.single_stem or None,
        "use_native_fp16": args.native_fp16 and args.device != "cpu",
        "mdxc_params": {
            "segment_size": 256,
            "override_model_segment_size": False,
            "batch_size": 1,
            "overlap": args.overlap,
            "pitch_shift": 0,
        },
        "info_only": args.download_only or args.device == "cpu",
    }
    separator = separator_class(**options)
    if args.device == "cpu" and not args.download_only:
        # The backend has no force-CPU constructor flag. Skip auto discovery
        # above, then supply the same attributes as its CPU fallback method.
        import torch
        separator.torch_device = torch.device("cpu")
        separator.torch_device_cpu = separator.torch_device
        separator.torch_device_mps = None
        separator.onnx_execution_provider = ["CPUExecutionProvider"]
    return separator


def run(args, emit, separator_class):
    """Run a job using an injected backend class for focused regression tests."""
    model_filename = _model_filename(args)
    input_paths = [Path(source).expanduser().resolve() for source in args.inputs]
    if not args.download_only and not any(path.is_file() for path in input_paths):
        # Do not fetch large model weights for a job with no usable inputs.
        total = len(input_paths)
        for index, input_path in enumerate(input_paths, start=1):
            emit("error", message="Input is not a file: {}".format(input_path), input=str(input_path))
            emit("progress", completed=index, total=total)
        emit("complete", successful=0, failed=total, cancelled=False)
        return 1
    model_dir = Path(args.model_dir).expanduser().resolve()
    model_dir.mkdir(parents=True, exist_ok=True)
    emit("status", message="Preparing {}".format(model_filename))
    separator = _create_separator(separator_class, args)
    if args.download_only:
        separator.download_model_files(model_filename)
        emit("status", message="Model and configuration are cached in {}".format(model_dir))
        emit("complete", successful=1, failed=0, cancelled=False)
        return 0

    output_root = Path(args.output_dir).expanduser().resolve()
    separator.load_model(model_filename=model_filename)
    successful = 0
    failed = 0
    total = len(input_paths)
    emit("progress", completed=0, total=total)
    for index, input_path in enumerate(input_paths, start=1):
        try:
            if not input_path.is_file():
                raise ValueError("Input is not a file: {}".format(input_path))
            output_dir = _unique_output_directory(output_root, input_path)
            # Keep both the public facade and the loaded architecture in sync.
            separator.output_dir = str(output_dir)
            if getattr(separator, "model_instance", None) is not None:
                separator.model_instance.output_dir = str(output_dir)
            emit("status", message="Processing {} ({}/{})".format(input_path.name, index, total))
            files = separator.separate(str(input_path))
            for path in _verified_output_paths(files, output_dir):
                emit("output", path=path, input=str(input_path))
            successful += 1
        except Exception as error:
            failed += 1
            emit("error", message=str(error), input=str(input_path))
        emit("progress", completed=index, total=total)
    emit("complete", successful=successful, failed=failed, cancelled=False)
    return 1 if failed else 0


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.list_presets:
        print(json.dumps(MODEL_PRESETS))
        return 0

    # The installer puts FFmpeg next to the isolated Python executable. This
    # also makes the standalone CLI work without manually activating the venv.
    runtime_bin = str(Path(sys.executable).absolute().parent)
    os.environ["PATH"] = runtime_bin + os.pathsep + os.environ.get("PATH", "")

    protocol_stream = sys.stdout

    def emit(event, **fields):
        protocol_stream.write(json.dumps(dict(event=event, **fields)) + "\n")
        protocol_stream.flush()

    try:
        if args.overlap is not None and not 2 <= args.overlap <= 50:
            raise ValueError("Overlap must be between 2 and 50 prediction windows.")
        if not (args.check or args.list_models or args.download_only) and not args.inputs:
            raise ValueError("Choose at least one input audio file, or use --download-only.")
        if not (args.check or args.list_models):
            _model_filename(args)
        # Keep incidental prints, model download bars, and library logs out of
        # the JSON protocol even during imports and environment checks.
        with contextlib.redirect_stdout(sys.stderr):
            separator_class, version = _load_backend()
            if args.check:
                ffmpeg = shutil.which("ffmpeg")
                if ffmpeg is None:
                    raise RuntimeError("FFmpeg was not found. Run scripts/install_modern.py to install the modern model runtime.")
                protocol_stream.write(json.dumps({"available": True, "version": version, "python": sys.executable, "ffmpeg": ffmpeg, "error": None}) + "\n")
                return 0
            if args.list_models:
                model_dir = Path(args.model_dir).expanduser().resolve()
                model_dir.mkdir(parents=True, exist_ok=True)
                separator = separator_class(model_file_dir=str(model_dir), info_only=True)
                protocol_stream.write(json.dumps(separator.list_supported_model_files()) + "\n")
                return 0
            if not args.download_only and shutil.which("ffmpeg") is None:
                raise RuntimeError("FFmpeg was not found on PATH. Install FFmpeg before separating audio.")
            return run(args, emit, separator_class)
    except KeyboardInterrupt:
        emit("status", message="Processing cancelled.")
        emit("complete", successful=0, failed=0, cancelled=True)
        return 130
    except Exception as error:
        if args.check:
            protocol_stream.write(json.dumps({"available": False, "version": None, "python": sys.executable, "error": str(error)}) + "\n")
        else:
            emit("error", message=str(error))
            emit("complete", successful=0, failed=1, cancelled=False)
        return 1


def _cancel_on_termination(signum, frame):
    # A GUI cancellation sends SIGTERM. Unwind active download contexts before
    # exiting so their temporary directories do not retain large partial files.
    raise KeyboardInterrupt()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _cancel_on_termination)
    sys.exit(main())
