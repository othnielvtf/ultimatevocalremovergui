#!/usr/bin/env python3
"""Build a self-contained Apple Silicon .app with its own Python/ML runtime.

Run with the Python from .venv-modern after scripts/install_modern.py. Xcode
Command Line Tools are required to compile the small native Cocoa launcher.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "UVR Modern"
BUNDLE_ID = "local.uvr.modern"
VERSION = "1.1.0"
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}


def run(command, **kwargs):
    print("Running: {}".format(command[0]), flush=True)
    return subprocess.run([str(value) for value in command], check=True, **kwargs)


def runtime_info(python):
    script = """import json, platform, site, sys
print(json.dumps({'base': sys.base_prefix, 'site': site.getsitepackages()[0],
                  'version': list(sys.version_info[:2]), 'machine': platform.machine()}))
"""
    return json.loads(subprocess.check_output([str(python), "-I", "-c", script], text=True))


def is_macho(path):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(4) in MACHO_MAGICS


def check_internal_symlinks(bundle):
    """Reject references back to the checkout, managed Python, or build machine."""
    bundle = Path(bundle).resolve()
    for path in bundle.rglob("*"):
        if path.is_symlink():
            try:
                path.resolve(strict=True).relative_to(bundle)
            except (OSError, ValueError) as error:
                raise RuntimeError("Bundle contains an external or broken symlink: {}".format(path)) from error


def sign_bundle(bundle):
    """Sign native files inside-out; the outer app seals Python source/data too."""
    native_files = [path for path in bundle.rglob("*") if is_macho(path)]
    print("Ad-hoc signing {} native binaries and the app".format(len(native_files)), flush=True)
    for path in sorted(native_files, key=lambda value: len(value.parts), reverse=True):
        subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(path)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    run(["codesign", "--force", "--sign", "-", "--timestamp=none", bundle])
    run(["codesign", "--verify", "--deep", "--strict", bundle])


def build_app(runtime_python, destination):
    info = runtime_info(runtime_python)
    if info["version"] != [3, 12] or info["machine"] != "arm64":
        raise RuntimeError("This build targets the tested Python 3.12 Apple Silicon runtime.")
    base = Path(info["base"]).resolve()
    site_packages = Path(info["site"]).resolve()
    if not (base / "lib" / "libpython3.12.dylib").is_file():
        raise RuntimeError("Use a relocatable uv-managed Python 3.12 runtime.")
    if destination.exists():
        raise FileExistsError("Build destination already exists: {}. Choose a new --dist directory.".format(destination))

    contents = destination / "Contents"
    resources = contents / "Resources"
    runtime = resources / "runtime"
    source = resources / "app"
    (contents / "MacOS").mkdir(parents=True)
    source.mkdir(parents=True)
    print("Copying standalone Python and the installed model engine...", flush=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store")
    shutil.copytree(base, runtime, symlinks=True, ignore=ignore)
    target_site = runtime / "lib" / "python3.12" / "site-packages"
    if target_site.exists():
        shutil.rmtree(target_site)
    shutil.copytree(site_packages, target_site, symlinks=True, ignore=ignore)

    ffmpeg_source = subprocess.check_output(
        [str(runtime_python), "-I", "-c", "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
        text=True,
    ).strip()
    shutil.copy2(ffmpeg_source, runtime / "bin" / "ffmpeg")
    shutil.copy2(ROOT / "modern_models.py", source / "modern_models.py")
    shutil.copy2(ROOT / "studio_audio.py", source / "studio_audio.py")
    (source / "gui_data").mkdir()
    for filename in ("modern_models_dialog.py", "app_paths.py", "studio_view.py", "studio_widgets.py"):
        shutil.copy2(ROOT / "gui_data" / filename, source / "gui_data" / filename)
    shutil.copy2(ROOT / "packaging" / "macos" / "bootstrap.py", resources / "bootstrap.py")
    shutil.copy2(ROOT / "gui_data" / "img" / "UVR.icns", resources / "UVR.icns")
    shutil.copy2(ROOT / "requirements-modern.txt", resources / "requirements-modern.txt")

    # uv updates this install name to its managed installation directory. The
    # app must load the copied library through its own executable-relative rpath.
    run(["install_name_tool", "-id", "@rpath/libpython3.12.dylib", runtime / "lib" / "libpython3.12.dylib"])
    executable = contents / "MacOS" / APP_NAME
    run([
        "xcrun", "clang", "-arch", "arm64", "-mmacosx-version-min=14.0",
        "-O2", "-fobjc-arc", "-framework", "Cocoa",
        "-I", base / "include" / "python3.12",
        ROOT / "packaging" / "macos" / "launcher.m",
        "-L", runtime / "lib", "-lpython3.12",
        "-Wl,-rpath,@executable_path/../Resources/runtime/lib", "-o", executable,
    ])
    metadata = {
        "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID, "CFBundleExecutable": APP_NAME,
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": "1", "CFBundleIconFile": "UVR.icns",
        "LSMinimumSystemVersion": "14.0", "LSArchitecturePriority": ["arm64"],
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication",
    }
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(metadata, handle)
    (contents / "PkgInfo").write_bytes(b"APPL????")
    versions = subprocess.check_output([str(runtime_python), "-I", "-m", "pip", "freeze"], text=True)
    (resources / "runtime-versions.txt").write_text(versions, encoding="utf-8")
    check_internal_symlinks(destination)
    sign_bundle(destination)
    return destination


def self_test(bundle):
    executable = bundle / "Contents" / "MacOS" / APP_NAME
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY", "DYLD_LIBRARY_PATH"):
        env.pop(key, None)
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    result = subprocess.check_output([str(executable), "--self-test"], cwd="/", env=env, text=True)
    print(result, flush=True)
    # Imports must never leave bytecode or other generated files in the sealed app.
    run(["codesign", "--verify", "--deep", "--strict", bundle])
    return json.loads(result.strip().splitlines()[-1])


def migrate_models(source, target):
    """Copy complete existing models once; never overwrite a user's cache."""
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_file() and not path.name.startswith(".") and not (target / path.name).exists():
            print("Reusing cached model: {}".format(path.name), flush=True)
            temporary = target / ("." + path.name + ".copying")
            shutil.copy2(path, temporary)
            temporary.replace(target / path.name)


def install_app(bundle, applications):
    applications.mkdir(parents=True, exist_ok=True)
    target = applications / bundle.name
    if target.exists():
        raise FileExistsError("An app already exists at {}; it was preserved.".format(target))
    # ditto preserves the signed bundle's relative symlinks and file metadata.
    run(["ditto", bundle, target])
    run(["codesign", "--verify", "--deep", "--strict", target])
    return target


def create_dmg(bundle, image):
    if image.exists():
        raise FileExistsError("Disk image already exists: {}".format(image))
    with tempfile.TemporaryDirectory(prefix="uvr-dmg-") as temporary:
        staging = Path(temporary)
        run(["ditto", bundle, staging / bundle.name])
        (staging / "Applications").symlink_to("/Applications")
        (staging / "Read me.txt").write_text(
            "Drag UVR Modern.app to Applications.\n\n"
            "Apple Silicon, macOS 14 or newer. Python, the model engine and FFmpeg are included.\n"
            "Model weights download on first use and are stored in your user Library.\n"
            "This local build is ad-hoc signed, without Apple notarization.\n", encoding="utf-8")
        run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", staging,
             "-format", "UDZO", "-imagekey", "zlib-level=6", image])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-python", type=Path, default=ROOT / ".venv-modern" / "bin" / "python")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--install", action="store_true", help="Install the verified app in Applications.")
    parser.add_argument("--applications", type=Path, default=Path("/Applications"))
    parser.add_argument("--migrate-models", action="store_true", help="Reuse the checkout's downloaded models in the user cache.")
    parser.add_argument("--dmg", action="store_true", help="Also create a drag-to-Applications disk image.")
    args = parser.parse_args(argv)
    if sys.platform != "darwin" or platform.machine() != "arm64":
        parser.error("Build this Apple Silicon application on an Apple Silicon Mac.")
    try:
        dist = args.dist.expanduser().absolute()
        dist.mkdir(parents=True, exist_ok=True)
        bundle = build_app(args.runtime_python.expanduser().absolute(), dist / (APP_NAME + ".app"))
        self_test(bundle)
        if args.migrate_models:
            migrate_models(ROOT / "models" / "Modern_Models",
                           Path.home() / "Library" / "Application Support" / "UVRModern" / "models")
        if args.install:
            installed = install_app(bundle, args.applications.expanduser().absolute())
            self_test(installed)
            print("Installed: {}".format(installed), flush=True)
        if args.dmg:
            create_dmg(bundle, dist / "UVR-Modern-{}-arm64.dmg".format(VERSION))
        print("Built: {}".format(bundle), flush=True)
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print("Mac app build failed: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
