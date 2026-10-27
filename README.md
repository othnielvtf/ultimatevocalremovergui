# UVR Modern

Local audio separation with **BS-Roformer and Mel-Band Roformer**, a standalone
Mac application, and a command-line worker. Extract vocals and instrumentals,
prepare karaoke tracks, or use specialized denoise, dereverb, and instrument
separation models.

This checkout extends [Ultimate Vocal Remover GUI 5.6](https://github.com/Anjok07/ultimatevocalremovergui)
with an isolated modern engine powered by [audio-separator](https://github.com/nomadkaraoke/python-audio-separator).
**UVR Modern 1.0.0** is the packaged modern-model window. The original interface
and its VR, MDX-Net/MDX23C, and Demucs engines remain available through `UVR.py`.

- [Use the Mac app](#use-the-mac-app)
- [Model presets](#model-presets)
- [Run from source](#run-from-source)
- [Command line](#command-line)
- [Build the Mac app](#build-the-mac-app)
- [Files and settings](#files-and-settings)
- [Verification](#verification)
- [Troubleshooting and current limits](#troubleshooting-and-current-limits)
- [Original UVR and credits](#original-uvr-and-credits)

## What has been added

- Eight model presets for vocals, instrumentals, karaoke, denoising, dereverb,
  and six-stem separation, plus custom model filenames from the backend catalog.
- A modern-model window that runs independently or opens from the original
  interface via **Settings → Modern Models → Open Modern Models**.
- Batch file selection and recursive audio-folder selection in the window.
- WAV, FLAC, and MP3 exports, optional single-stem output, and overlap controls.
- Automatic Apple MPS, NVIDIA CUDA, or CPU selection, with a forced CPU option.
- Optional native float16 inference for supported Roformer GPU models.
- Persistent model downloads, protected against interrupted transfers, and a
  Cancel button that stops the worker and its child processes.
- A fresh output subfolder for every input and run to avoid overwriting files.
- A standalone Apple Silicon `.app` and drag-to-Applications `.dmg` builder.
- Regression tests and CI for the worker, controller, and packaged-app paths.

Audio separation runs locally. Internet access is used to retrieve the model
catalog and any missing weights or configuration files. Some checkpoints require
more than 1 GB of storage each.

## Use the Mac app

The packaged **UVR Modern.app** supports **Apple Silicon and macOS 14 or newer**.
It includes Python 3.12, Tcl/Tk, the separation engine and its dependencies, and
FFmpeg. It runs independently of this repository and its virtual environment.

1. Open a locally built `UVR-Modern-1.0.0-arm64.dmg` and drag **UVR Modern.app**
   into **Applications**, or use the builder's `--install` option.
2. Launch **UVR Modern** from Applications.
3. Choose a preset and use **Add files…** or **Add folder…** to select audio.
4. Choose an output folder and format, then click **Separate audio**.
5. Read the saved paths in the log when processing completes.

**Download model only** caches a model before processing. On first use, weights
and configuration files download automatically. Model weights are stored in your
user Library, outside the app bundle, so they can be reused across app builds.

For a faster first pass on a supported GPU, set **Overlap** to `2` and enable
**GPU float16** with **Device** set to `auto`. Float16 is optional; leaving overlap
blank uses the model's configuration. Processing time and separation quality
depend on the model, recording, hardware, and selected settings.

Build outputs are generated locally and ignored by Git:

| Artifact | Default path |
| --- | --- |
| Standalone application | `dist/UVR Modern.app` |
| Optional disk image | `dist/UVR-Modern-1.0.0-arm64.dmg` |
| Installed application with `--install` | `/Applications/UVR Modern.app` |

See [Build the Mac app](#build-the-mac-app) to generate these artifacts. The
upstream UVR download links distribute its own application, rather than this
checkout's newly built app.

This local build is **ad-hoc signed and not Apple-notarized**. Public distribution
with notarization requires a Developer ID and a separate release-signing process.

## Model presets

| Preset ID | Model | Intended use |
| --- | --- | --- |
| `melband-vocals` | MelBand Roformer, Kim FT2 by unwa | Default vocal/instrumental separation |
| `bs-vocals` | BS-Roformer by Viperx | Alternative vocal/instrumental separation |
| `melband-viperx` | MelBand Roformer by Viperx | Alternative vocal/instrumental separation |
| `melband-instrumental` | MelBand Roformer by becruily | Instrumental extraction |
| `melband-karaoke` | MelBand Roformer by aufr33 / Viperx | Lead-vocal removal while retaining backing vocals |
| `melband-denoise` | MelBand Roformer by aufr33 | Vocal or speech denoising |
| `bs-dereverb` | BS-Roformer De-reverb | Reverb separation |
| `bs-six-stem` | BS-Roformer SW by jarredou | Vocals, drums, bass, guitar, piano, and other |

Use **Model filename** for another supported catalog entry. **Browse model
catalog…** opens the external catalog; a searchable catalog inside the window
has not been implemented. The CLI also provides `--list-models`.

Output stem names depend on the model. With the default Kim FT2 preset,
`(vocals)` is the vocal track and `(other)` is the instrumental track. The preset
list describes available workflows; the full-song validation below used Kim FT2.

## Run from source

Use **Python 3.12** for the tested modern runtime. The installation helper accepts
Python 3.11 or newer. Tkinter support is required for the window.

Run commands from the repository root:

```bash
# Recommended with uv, including the relocatable runtime needed by the Mac builder
uv run --managed-python --python 3.12 scripts/install_modern.py

# Alternatively, with an existing Python 3.12 installation on macOS / Linux
python3.12 scripts/install_modern.py
```

On Windows:

```powershell
py -3.12 scripts/install_modern.py
```

The helper creates `.venv-modern`, installs the pinned dependencies, supplies
FFmpeg, and checks the engine. The backend is pinned to **audio-separator 0.47.1**;
[audioread](requirements-modern.txt) and the bundled FFmpeg dependency are also
specified explicitly.

Keep `.venv-modern` separate from the original `requirements.txt` environment:
the modern engine uses newer NumPy, librosa, and PyTorch dependencies.

Launch the window on macOS / Linux:

```bash
.venv-modern/bin/python -m gui_data.modern_models_dialog
```

On Windows:

```powershell
.venv-modern\Scripts\python.exe -m gui_data.modern_models_dialog
```

Source launches detect `.venv-modern` automatically. Select another Python
executable in the window or set `UVR_MODERN_PYTHON` to use a different runtime.
The packaged Mac app uses its bundled runtime and hides the interpreter controls.

Apple Silicon uses MPS automatically when available. For NVIDIA ONNX acceleration
on Windows/Linux, add `--gpu` to the setup command. A compatible CUDA-enabled
PyTorch build is also needed for CUDA-based Roformer inference. The default CPU
ONNX dependency does not disable PyTorch GPU acceleration.

## Command line

Examples use the macOS/Linux runtime path. On Windows, substitute
`.venv-modern\Scripts\python.exe` for `.venv-modern/bin/python`.

```bash
# Check the environment and list presets or catalog models
.venv-modern/bin/python modern_models.py --check
.venv-modern/bin/python modern_models.py --list-presets
.venv-modern/bin/python modern_models.py --list-models

# Separate one file into WAV stems
.venv-modern/bin/python modern_models.py --preset melband-vocals --output-dir separated "song.mp3"

# Process multiple files with the same loaded model and faster GPU settings
.venv-modern/bin/python modern_models.py --preset melband-vocals --native-fp16 --overlap 2 --output-dir separated "song one.mp3" "song two.flac"

# Export only the vocals as FLAC
.venv-modern/bin/python modern_models.py --preset melband-vocals --single-stem Vocals --output-format FLAC --output-dir separated "song.mp3"

# Download a model and its configuration without inference
.venv-modern/bin/python modern_models.py --preset bs-dereverb --download-only
```

The CLI accepts **file paths**; recursive folder expansion is provided by the
window. Use `--` before input filenames beginning with `-`.

| Option | Behavior |
| --- | --- |
| `--preset ID` | Select a bundled preset; defaults to `melband-vocals` |
| `--model FILENAME` | Select another supported catalog filename instead of a preset |
| `--output-dir PATH` | Output root; defaults to `modern_outputs` |
| `--model-dir PATH` | Persistent model cache; defaults to `models/Modern_Models` in this checkout |
| `--output-format WAV\|FLAC\|MP3` | Output format; defaults to WAV |
| `--device auto\|cpu` | Automatic acceleration or forced CPU |
| `--native-fp16` | Request native float16 on supported GPU models; ignored for forced CPU |
| `--single-stem NAME` | Export only a stem supported by the selected model |
| `--overlap 2..50` | Prediction overlap; omitting it uses the model configuration |
| `--download-only` | Cache the selected model without processing audio |

Each input gets its own output subfolder. Repeated names receive numbered
folders, such as `song`, `song (2)`, and `song (3)`. Failed inputs are reported,
remaining batch inputs are attempted, and any failures produce a nonzero exit
status. Worker status and output paths are emitted as JSON lines on stdout;
backend logs are written to stderr.

## Build the Mac app

Build on an **Apple Silicon Mac**, using a **relocatable uv-managed Python 3.12**
runtime and **Xcode Command Line Tools**. Prepare the engine first:

```bash
uv run --managed-python --python 3.12 scripts/install_modern.py

# Build the app and a disk image, reusing already downloaded model files
.venv-modern/bin/python scripts/build_macos.py --migrate-models --dmg
```

The setup helper reuses an existing `.venv-modern`. For packaging, that
environment must have been created with the uv-managed Python above; rerunning
setup does not change an existing environment’s Python distribution.

The builder copies the standalone Python runtime and installed engine, compiles
a native Cocoa launcher, and signs the bundle. Its self-test checks the embedded
GUI and the separate worker, then verifies the signature after execution.

| Build option | Purpose |
| --- | --- |
| `--install` | Copy the verified app into Applications |
| `--applications PATH` | Choose another installation directory when using `--install` |
| `--migrate-models` | Copy existing checkout models into the user's app cache without overwriting cached files |
| `--dmg` | Create the drag-to-Applications disk image |
| `--dist PATH` | Choose a fresh build output directory |
| `--runtime-python PATH` | Choose the installed modern Python runtime to package |

For example, to build and install in one operation:

```bash
.venv-modern/bin/python scripts/build_macos.py --install --migrate-models --dmg
```

Existing application, build, and disk-image destinations are preserved. For
another build, choose a fresh `--dist` directory. To install alongside an existing
app, also choose another `--applications` directory.

The app bundles its runtime and engine. Model weights are separate downloads;
`--migrate-models` reuses weights already present in this checkout. Runtime package
versions are recorded inside the app at `Contents/Resources/runtime-versions.txt`.

## Files and settings

| Data | Packaged Mac app | Source window / CLI |
| --- | --- | --- |
| Model cache | `~/Library/Application Support/UVRModern/models` | `models/Modern_Models` |
| Preferences | `~/Library/Application Support/UVRModern/preferences.json` | `~/.uvr/modern_models.json` for the window |
| Application log | `~/Library/Logs/UVRModern/app.log` | Live window log or CLI stderr |
| Default output | `~/Downloads/UVR Separated` | Window: `separated`; CLI: `modern_outputs` |

The window allows different output and cache locations. Packaged-app preferences,
model weights, logs, and generated caches are stored outside the signed bundle.
The source window and packaged app use separate preference files.

## Verification

The modern integration has **39 regression tests** covering command validation,
batches, output verification, collision handling, interrupted downloads,
cancellation, environment isolation, and packaged-app paths.

```bash
.venv-modern/bin/python -m unittest discover -s tests -v
```

The tests need Python with Tkinter support but do not download models or require
the ML stack. CI is configured to run the suite on Python 3.9 and 3.12; the
modern runtime installer still requires Python 3.11 or newer.

Manual checks completed for the Mac build:

- A full 12-minute-28-second MP3 was separated with Kim FT2 using Apple MPS,
  native float16, overlap `2`, and WAV output. Both stems matched the input's
  duration, sample rate, and channel count, with finite audio and no clipping.
- The installed app selected a file through the native picker, completed a GPU
  separation using its bundled runtime, wrote both stems, saved settings, and
  handled normal Mac Quit.
- The app passed its runtime self-test and signature verification after launch;
  the DMG passed checksum verification.

These checks validate this Mac build and workflow. They do not establish quality
or performance rankings across every preset. Windows/Linux source launches and
NVIDIA CUDA execution have not been manually verified in this modernization.

## Troubleshooting and current limits

- **App will not start:** inspect `~/Library/Logs/UVRModern/app.log`. The packaged
  build targets Apple Silicon and macOS 14 or newer.
- **Source runtime is missing or broken:** rerun `scripts/install_modern.py` with
  Python 3.12 and run `modern_models.py --check` through `.venv-modern`.
- **Tkinter is missing:** use a Python installation with Tk support for the source
  window. Tcl/Tk is already included in the packaged app.
- **Processing takes too long:** try overlap `2` and GPU float16 on a supported
  GPU. Forced CPU remains available but can take substantially longer.
- **One song stays at 0%:** the main progress bar counts completed files. Chunk
  progress appears in the live backend log; a per-song ETA is not implemented.
- **An export seems to be missing:** check the input's output subfolder and the
  log. Stem names vary by model; Kim FT2 calls the instrumental stem `other`.

The modern window has its own **Cancel** button and does not run concurrently
with a legacy separation launched from the same UVR window. It does not apply
the legacy ensemble or secondary-model settings. Synchronized playback, short
A/B previews, modern-model ensembles, and resuming within an interrupted song
remain future work.

## Original UVR and credits

The original `UVR.py` interface and its dependencies remain in this repository.
Use its separate `requirements.txt` environment and consult the
[upstream UVR documentation](https://github.com/Anjok07/ultimatevocalremovergui#installation)
for the original Windows, macOS, and Linux application. The
[upstream development announcement](https://github.com/Anjok07/ultimatevocalremovergui/issues/2399)
provides context on the maintainer's UVR 6 work.

Thanks to the original UVR developers [Anjok07](https://github.com/Anjok07) and
[aufr33](https://github.com/aufr33), the
[audio-separator maintainers](https://github.com/nomadkaraoke/python-audio-separator),
and the model authors credited in the presets and backend catalog.

Original project credits also include ZFTurbo (MDX23C), DilanBoskan, Bas Curtiz
(UVR artwork), tsurumeso (VR Architecture), Kuielab and Woosung Choi (MDX-Net),
Alexandre Défossez and the Demucs contributors, Kimberley Jensen, and Hv.

- [Support UVR](https://www.buymeacoffee.com/uvr5)
- [Upstream licensing information](https://github.com/Anjok07/ultimatevocalremovergui#license)
- [Original paper: Multi-scale Multi-band DenseNets for Audio Source Separation](https://arxiv.org/pdf/1706.09588.pdf)

Refer to the upstream licensing information and the bundled dependencies' license
files for attribution and licensing terms. Model weights retain the terms
provided by their authors.
