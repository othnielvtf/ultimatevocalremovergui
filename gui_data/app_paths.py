"""Paths shared by source launches and the self-contained macOS application."""

import os
from pathlib import Path
import sys


def bundle_resources(environ=None):
    environ = os.environ if environ is None else environ
    value = environ.get("UVR_APP_RESOURCES", "").strip()
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    # The native bootstrap sets this explicitly, rather than depending on cwd.
    if (path.name == "Resources" and path.parent.name == "Contents"
            and path.parent.parent.suffix.lower() == ".app"):
        return path
    return None


def application_support_dir(home=None):
    home = Path.home() if home is None else Path(home)
    return home / "Library" / "Application Support" / "UVRModern"


def preferences_path(home=None, environ=None):
    environ = os.environ if environ is None else environ
    override = environ.get("UVR_SETTINGS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    if bundle_resources(environ) is not None:
        return application_support_dir(home) / "preferences.json"
    home = Path.home() if home is None else Path(home)
    return home / ".uvr" / "modern_models.json"


def bundled_python(environ=None):
    resources = bundle_resources(environ)
    if resources is None:
        return None
    return resources / "runtime" / "bin" / "python{}.{}".format(*sys.version_info[:2])


def default_model_dir(app_dir, home=None, environ=None):
    if bundle_resources(environ) is not None:
        return application_support_dir(home) / "models"
    return Path(app_dir) / "models" / "Modern_Models"


def default_output_dir(app_dir, home=None, environ=None):
    if bundle_resources(environ) is not None:
        home = Path.home() if home is None else Path(home)
        return home / "Downloads" / "UVR Separated"
    return Path(app_dir) / "separated"


def inside_bundle(path, environ=None):
    resources = bundle_resources(environ)
    if resources is None:
        return False
    try:
        Path(path).expanduser().resolve().relative_to(resources.parent.parent)
        return True
    except ValueError:
        return False


def worker_environment(environ=None, home=None):
    """Prevent an embedding host's Python/Tk settings leaking into the worker."""
    environment = dict(os.environ if environ is None else environ)
    for key in (
        "PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__",
        "VIRTUAL_ENV", "TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH",
        "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "DYLD_FALLBACK_LIBRARY_PATH",
    ):
        environment.pop(key, None)
    if bundle_resources(environment) is not None:
        home = Path.home() if home is None else Path(home)
        environment["NUMBA_CACHE_DIR"] = str(home / "Library" / "Caches" / "UVRModern" / "numba")
    return environment
