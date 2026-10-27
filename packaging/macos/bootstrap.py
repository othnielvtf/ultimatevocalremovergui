"""Initialize paths and launch the packaged modern-model window."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback


RESOURCES = Path(__file__).resolve().parent
RUNTIME = RESOURCES / "runtime"
APP_SOURCE = RESOURCES / "app"
SUPPORT = Path.home() / "Library" / "Application Support" / "UVRModern"
CACHE = Path.home() / "Library" / "Caches" / "UVRModern"
LOGS = Path.home() / "Library" / "Logs" / "UVRModern"


def prepare_environment():
    """Keep all generated files outside the signed application bundle."""
    for path in (SUPPORT, CACHE, LOGS):
        path.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(APP_SOURCE))
    os.environ["UVR_APP_RESOURCES"] = str(RESOURCES)
    os.environ["UVR_SETTINGS_PATH"] = str(SUPPORT / "preferences.json")
    os.environ["PATH"] = str(RUNTIME / "bin") + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["NUMBA_CACHE_DIR"] = str(CACHE / "numba")
    os.environ["TORCH_HOME"] = str(CACHE / "torch")
    os.environ["XDG_CACHE_HOME"] = str(CACHE)
    # Tk's executable-relative search begins at Contents/MacOS in an embedded
    # app. Explicit script-library paths let it use the bundled Tcl/Tk runtime.
    for name, pattern in (("TCL_LIBRARY", "tcl[0-9]*"), ("TK_LIBRARY", "tk[0-9]*")):
        candidates = sorted(path for path in (RUNTIME / "lib").glob(pattern) if path.is_dir())
        if candidates:
            os.environ[name] = str(candidates[-1])


def self_test():
    """Check the embedded GUI and independent worker without repo dependencies."""
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.update_idletasks()
    tk_version = root.tk.call("info", "patchlevel")
    from gui_data.modern_models_dialog import ModernModelsDialog
    window = ModernModelsDialog(root)
    window.withdraw()
    root.update_idletasks()
    if not hasattr(window, "timeline") or not hasattr(window, "play_button"):
        raise RuntimeError("Studio controls did not initialize.")
    window.destroy()
    root.destroy()
    python = RUNTIME / "bin" / "python3.12"
    worker = APP_SOURCE / "modern_models.py"
    with tempfile.TemporaryDirectory(prefix="uvr-app-check-") as directory:
        result = subprocess.run(
            [str(python), "-I", "-B", str(worker), "--check"],
            cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=120,
        )
    try:
        backend = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise RuntimeError("Bundled engine check failed: " + result.stderr[-2000:]) from error
    if result.returncode or not backend.get("available"):
        raise RuntimeError("Bundled engine check failed: " + str(backend.get("error")))
    if Path(sys.prefix).resolve() != RUNTIME.resolve():
        raise RuntimeError("The app loaded Python from outside its bundled runtime.")
    if Path(backend["python"]).resolve().parent != (RUNTIME / "bin").resolve():
        raise RuntimeError("The worker loaded Python from outside its bundled runtime.")
    preview = subprocess.check_output(
        [str(python), "-I", "-B", "-c", "import sounddevice; print(sounddevice.__version__)"],
        text=True, timeout=30,
    ).strip()
    print(json.dumps({
        "audio_preview": preview,
        "available": True, "resources": str(RESOURCES),
        "python": sys.executable, "prefix": sys.prefix,
        "tk_version": tk_version, "backend": backend,
        "settings": str(SUPPORT / "preferences.json"),
    }), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    prepare_environment()
    if args.self_test:
        try:
            return self_test()
        except Exception as error:
            print(json.dumps({"available": False, "error": str(error)}), flush=True)
            traceback.print_exc(file=sys.stderr)
            return 1
    log = (LOGS / "app.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log
    try:
        from gui_data.modern_models_dialog import main as launch_window
        launch_window()
        return 0
    except Exception:
        traceback.print_exc()
        log.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
