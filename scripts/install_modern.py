#!/usr/bin/env python3
"""Install the optional modern separation engine without changing UVR's environment."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv


ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gpu", action="store_true",
        help="Install NVIDIA ONNX Runtime (Windows/Linux only). Apple MPS works without this flag.",
    )
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.error("Use Python 3.11 or newer; Python 3.12 is recommended for this runtime.")
    if args.gpu and sys.platform == "darwin":
        parser.error("Apple GPU acceleration uses MPS. Run without --gpu on macOS.")

    target = ROOT / ".venv-modern"
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    requirements = ROOT / ("requirements-modern-gpu.txt" if args.gpu else "requirements-modern.txt")
    try:
        if not (target / "pyvenv.cfg").is_file():
            print("Creating isolated runtime at {}".format(target), flush=True)
            venv.EnvBuilder(with_pip=True).create(str(target))
        subprocess.run(
            [str(python), "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"],
            check=True,
        )
        # ONNX CPU and GPU wheels provide the same import and must not coexist.
        conflicting_runtime = "onnxruntime" if args.gpu else "onnxruntime-gpu"
        subprocess.run(
            [str(python), "-m", "pip", "uninstall", "-y", conflicting_runtime], check=True,
        )
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(requirements)], check=True)
        # Give packages which invoke `ffmpeg` by name access to the bundled binary.
        # Copy on Windows because creating symlinks can require administrator rights.
        ffmpeg_source = subprocess.check_output(
            [str(python), "-c", "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
            text=True,
        ).strip()
        ffmpeg = python.parent / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not ffmpeg.exists():
            if os.name == "nt":
                shutil.copy2(ffmpeg_source, str(ffmpeg))
            else:
                ffmpeg.symlink_to(ffmpeg_source)
        runtime_env = dict(os.environ)
        runtime_env["PATH"] = str(python.parent) + os.pathsep + runtime_env.get("PATH", "")
        subprocess.run(
            [str(python), str(ROOT / "modern_models.py"), "--check"],
            env=runtime_env, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        print("Modern runtime setup failed: {}".format(error), file=sys.stderr)
        return 1

    print("\nModern models are ready. Open Settings > Modern Models in UVR.")
    print("Standalone window: {} -m gui_data.modern_models_dialog".format(python))
    return 0


if __name__ == "__main__":
    sys.exit(main())
