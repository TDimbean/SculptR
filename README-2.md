# SculptR

**Generate and enhance 3D models in Blender with local AI runtimes.**

SculptR is a Blender extension that adds image-to-3D generation, texture repainting, and geometry-enhancement workflows to the 3D View sidebar. It manages model weights, submits long-running work to a background engine, reports progress and hardware usage, and imports completed GLB files back into Blender.

The extension is designed around local inference. Network access is used to download and verify user-approved model files from Hugging Face; generation itself is delegated to isolated Python runtimes stored in the configured models directory.

> [!IMPORTANT]
> **Current repository status:** the checked-in code contains the Blender UI, job engine, model catalog and downloader, file-based IPC, resource monitoring, and inference launcher. It does **not** expose a runtime installer or provision the `sculptr_hunyuan_backend` and `sculptr_hi3d_backend` modules expected by the launcher. A clean checkout can manage and verify model weights, but generation and enhancement require separately prepared runtimes. Without them, inference jobs fail with `Runtime not installed` or `Backend unavailable`.

## Features

- Single-view image-to-3D generation from a required front image.
- Optional left, right, and back images for multiview generation.
- Texture repainting for the selected Blender mesh.
- Replacement-detail generation for the selected mesh through the Hi3D path.
- Configurable quality, target face count, texture generation, texture size, and seed.
- Drag-and-drop image assignment in the 3D View.
- Background jobs with progress reporting and cancellation.
- Resumable model downloads with pinned repository revisions and integrity checks.
- Model verification and staged, atomic installation.
- CPU, RAM, GPU, VRAM, and storage reporting.
- Configurable model-storage directory and cache cleanup.
- Manual import of completed GLB results into the current scene.

## Supported models

The model catalog is hard-coded and pinned to specific Hugging Face revisions.

| Internal ID | UI name | Purpose | Approximate catalog size | Upstream repositories |
|---|---|---|---:|---|
| `hunyuan` | Hunyuan | Single-view shape generation with the matching Hunyuan 2.1 VAE | 8.03 GB | `tencent/Hunyuan3D-2.1` |
| `hunyuan_mv` | Hunyuan More Views | Multiview shape generation with the Hunyuan 2.0 multiview model and matching VAE | 5.36 GB | `tencent/Hunyuan3D-2mv`, `tencent/Hunyuan3D-2` |
| `hi3d` | Hi3D for Enhance Detail | Replacement geometry through the Hi3D normal bridge | 6.50 GB | `Stable-X/trellis-normal-v0-1`, `Stable-X/yoso-normal-v1-8-1`, `facebook/dinov2-with-registers-large` |
| `texture` | Enhance Texture | Hunyuan 2.1 PBR texture repainting | 6.89 GB | `tencent/Hunyuan3D-2.1` |

Installing every catalog entry requires approximately **26.78 GB** before accounting for partial downloads, inference runtimes, caches, exported inputs, and generated outputs.

### Model licensing

The Hunyuan catalog entries are marked as restricted by the extension. SculptR requires explicit confirmation before downloading them. The catalog records these upstream license pages:

- [Hunyuan3D 2.1 license](https://huggingface.co/tencent/Hunyuan3D-2.1/blob/main/LICENSE)
- [Hunyuan3D 2.0 license](https://huggingface.co/tencent/Hunyuan3D-2/blob/main/LICENSE)

The current Blender panels do not expose those URLs directly. The checkbox is only a guard in SculptR. It does not modify the upstream license or grant additional rights. Review every model's own license before downloading or using its output, especially for commercial work.

## Requirements

- **Blender 4.2.1 or newer**, as declared by `blender_manifest.toml`.
- Enough free disk space for the selected models, runtime environments, temporary files, and outputs.
- Internet access for model downloads and model checks.
- Blender's **Online Access** setting enabled when downloading models.
- A pre-provisioned Hunyuan runtime for generation and texture repainting.
- A pre-provisioned Hi3D runtime for detail enhancement.

No minimum RAM, VRAM, CUDA version, GPU model, or operating-system requirement is encoded in the repository. Those requirements depend on the external inference backends installed into the runtimes.

GPU and VRAM telemetry currently uses `nvidia-smi`. On unsupported systems or when `nvidia-smi` is unavailable, those values appear as unavailable; this does not by itself determine whether a backend can run.

## Installation

### Build the Blender extension

Clone the repository, enter the directory containing `blender_manifest.toml`, validate it, and build the extension archive:

```bash
git clone https://github.com/TDimbean/SculptR.git
cd SculptR/sculptr

blender --command extension validate
blender --command extension build
```

The manifest identifies the package as `sculptr` version `0.1.0`, so Blender's default build name is expected to be:

```text
sculptr-0.1.0.zip
```

When `blender` is not on `PATH`, invoke the Blender executable by its full path.

### Install in Blender

1. Open **Edit > Preferences**.
2. Open **Get Extensions**.
3. Use **Install from Disk**.
4. Select the generated ZIP archive.
5. Enable **SculptR** if Blender does not enable it automatically.
6. Open a 3D View and press **N** to show the sidebar.
7. Select the **SculptR** tab.

The ZIP must contain `blender_manifest.toml` and `__init__.py` at the extension package root.

## First-time setup

### 1. Choose a models directory

Open the **Models** tab in the SculptR sidebar. The default location is Blender's user data directory under:

```text
sculptr/models
```

Use **Change** to select another existing folder. SculptR will not switch folders while jobs are active.

### 2. Review model licenses

Before downloading a restricted Hunyuan model, review the applicable upstream license listed above and enable the license-confirmation checkbox in the Models tab or the extension preferences.

The current UI describes this confirmation in the context of a US deployment. Treat that wording as an application guard, not legal advice.

### 3. Download the required model entries

Use **Download** beside each model you need. Downloads are staged in a `.partial` directory, can resume through HTTP range requests, and are verified before being moved into the final model directory.

Typical combinations are:

| Workflow | Required catalog entries |
|---|---|
| Single-view geometry without texture | Hunyuan |
| Single-view textured geometry | Hunyuan + Enhance Texture |
| Multiview geometry without texture | Hunyuan More Views |
| Multiview textured geometry | Hunyuan More Views + Enhance Texture |
| Texture repainting | Enhance Texture |
| Detail enhancement | Hi3D for Enhance Detail |

Use **Check** to verify installed files. Model status is derived from jobs in the current engine session, so after restarting Blender you may need to run **Check** again before generation controls become enabled.

> [!NOTE]
> A model check resolves the pinned repository metadata before verifying local files, so it currently requires network access as well.

### 4. Provision the inference runtimes

The engine expects the following layout inside the configured models directory:

```text
<models-directory>/
└── .sculptr/
    └── runtime/
        ├── hunyuan/
        │   ├── Scripts/python.exe       # Windows
        │   └── bin/python               # Linux/macOS alternative
        └── hi3d/
            ├── Scripts/python.exe       # Windows
            └── bin/python               # Linux/macOS alternative
```

Only the platform-appropriate interpreter is required in each runtime. The Hunyuan environment must make the following module importable:

```text
sculptr_hunyuan_backend
```

The Hi3D environment must make this module importable:

```text
sculptr_hi3d_backend
```

Runtime creation and dependency installation are not implemented in the current extension code.

## Usage

### Generate a model

1. Open the **Generate** tab.
2. Under **Image**, choose a main/front image.
3. Optionally add left, right, and back views.
4. Configure the generation settings.
5. Click **Generate Model**.
6. Follow the job card for progress, errors, or cancellation.
7. When the job completes, click **Import Result**.

Supported image formats in the Blender UI are:

```text
PNG, JPG, JPEG, WebP
```

Images can also be dropped anywhere in the 3D View. SculptR fills the first empty slot; once every slot is populated, it replaces the most recently targeted slot.

#### Generation settings

| Setting | Values | Default |
|---|---|---|
| Quality | `Fast`, `High`, `Ultra` | `High` |
| Target faces | 10,000 to 1,000,000 | 100,000 |
| Generate texture | On or off | On |
| Texture size | 1K, 2K, 4K | 2K |
| Seed | 0 to 2,147,483,647 | 0 |

Supplying any optional view selects the multiview Hunyuan model. Enabling texture generation additionally requires the Enhance Texture model.

### Repaint a selected mesh

1. Select a Blender mesh object.
2. Choose a valid main reference image.
3. Install and check **Enhance Texture**.
4. Click **Repaint Texture**.
5. Import the completed GLB from the job card.

SculptR exports the current Blender selection to a temporary GLB before submitting the job. The generated result is imported as a separate result; the source object is not modified in place by the job.

### Enhance mesh detail

1. Select a Blender mesh object.
2. Choose a valid main reference image.
3. Install and check **Hi3D for Enhance Detail**.
4. Click **Enhance Detail**.
5. Import the completed replacement geometry from the job card.

The **Undo** button in this panel invokes Blender's normal undo operation. It does not cancel a running inference job.

### Manage active jobs

Active jobs expose a **Cancel** button. Cancellation is communicated to the background engine through a per-job signal file.

For model downloads, already downloaded `.part` data is retained so a later retry can resume. Inference cancellation terminates the isolated runtime process for that job.

### Clear temporary cache

The **Clear Cache** action removes and recreates:

```text
<models-directory>/.sculptr/cache
```

It does not delete installed models. It also does not remove the separate `inputs`, `outputs`, `runtime`, or active `session` directories.

## Architecture

```text
┌──────────────────────────────────────────────────────────┐
│ Blender                                                   │
│                                                          │
│  SculptR panels and operators                            │
│      │                                                   │
│      ▼                                                   │
│  BridgeClient                                            │
│      │  atomic JSON commands, state files, heartbeats    │
└──────┼───────────────────────────────────────────────────┘
       │
       ▼
<models>/.sculptr/session/<nonce>/
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│ SculptR engine subprocess                                │
│                                                          │
│  • validates commands and paths                          │
│  • downloads and checks pinned model files               │
│  • samples CPU/RAM/GPU/VRAM/storage                      │
│  • handles cancellation and stale clients                │
│  • launches isolated inference runtimes                  │
└───────────────┬─────────────────────────┬────────────────┘
                │                         │
                ▼                         ▼
   .sculptr/runtime/hunyuan   .sculptr/runtime/hi3d
                │                         │
                ▼                         ▼
 sculptr_hunyuan_backend      sculptr_hi3d_backend
                │                         │
                └─────────── GLB ─────────┘
                              │
                              ▼
                  Manual import into Blender
```

### Blender-side modules

| Module | Responsibility |
|---|---|
| `__init__.py` | Registers and unregisters the extension. |
| `properties.py` | Add-on preferences and per-scene settings. |
| `operators.py` | Image selection, model actions, generation, enhancement, cancellation, cache cleanup, and result import. |
| `ui.py` | Sidebar panels, model cards, job cards, and polling. |
| `ui_helpers.py` | Validation, status normalization, formatting, and snapshot helpers. |
| `catalog.py` | Pinned model and file manifests. |
| `bridge.py` | Engine lifecycle and file-based IPC. |

### Engine modules

| Module | Responsibility |
|---|---|
| `engine/daemon.py` | Command consumption, job execution, cancellation, runtime launch, and state forwarding. |
| `engine/hub.py` | Hugging Face metadata resolution, resumable downloads, staging, and integrity checking. |
| `engine/protocol.py` | Command validation, path containment, and JSON state format. |
| `engine/system.py` | Resource and storage sampling plus cache cleanup. |
| `engine/inference_runner.py` | Runtime backend loading, progress forwarding, and GLB output validation. |

## Runtime backend contract

Each runtime backend must provide a callable named `run_action`:

```python
from pathlib import Path

def run_action(request: dict, progress) -> str | Path:
    """Run one action and return the generated GLB path."""
```

The progress callback accepts:

```python
progress(phase: str, percent: int, message: str)
```

### Supported backend actions

| Action | Runtime | Backend module |
|---|---|---|
| `generate` | `hunyuan` | `sculptr_hunyuan_backend` |
| `repaint_texture` | `hunyuan` | `sculptr_hunyuan_backend` |
| `enhance_detail` | `hi3d` | `sculptr_hi3d_backend` |

### Request shape

Inference requests contain fields equivalent to:

```json
{
  "action": "generate | repaint_texture | enhance_detail",
  "job_id": "...",
  "inputs": {
    "front": "/absolute/path/front.png",
    "left": null,
    "right": null,
    "back": null,
    "mesh": "/absolute/path/input.glb"
  },
  "settings": {
    "quality": "fast | high | ultra",
    "faces": 100000,
    "texture": true,
    "texture_size": 2048,
    "seed": 0
  },
  "output_directory": "/absolute/path/inside/.sculptr/outputs/...",
  "model_directory": "/absolute/path/to/models"
}
```

`mesh` is omitted for `generate` and required for enhancement actions. `object_name` may also be present for mesh enhancement.

The operator also submits a `models_directory` field; the engine adds the singular `model_directory` field shown above.

The returned file must:

- Exist.
- Have a `.glb` extension.
- Be located inside the request's `output_directory`.

Any other output is rejected by the inference runner.

## Storage layout

```text
<models-directory>/
├── hunyuan/                    # Installed single-view model
├── hunyuan_mv/                 # Installed multiview model
├── hi3d/                       # Installed detail-enhancement models
├── texture/                    # Installed texture model
├── <model-id>.partial/         # Resumable staging area during download
└── .sculptr/
    ├── cache/
    │   └── jobs/<job-id>/      # Inference request and runtime state
    ├── inputs/                 # Temporary GLB exports from Blender
    ├── outputs/<uuid>/         # Generated GLB outputs
    ├── runtime/
    │   ├── hunyuan/            # External Hunyuan Python environment
    │   └── hi3d/               # External Hi3D Python environment
    └── session/<nonce>/
        ├── commands/           # Commands from Blender
        ├── states/             # Job state from the engine
        ├── signals/            # Cancellation signals
        ├── client-heartbeat.json
        ├── engine-heartbeat.json
        ├── resources.json
        └── engine.log
```

A normally stopped bridge removes its active session directory. Inspect `engine.log` while reproducing an engine failure, before disabling the extension or closing Blender.

## Download and process safeguards

The current implementation includes several defensive checks:

- Model repositories are pinned to explicit revisions.
- Required files can be pinned to SHA-256 values.
- Other Hugging Face files are checked against metadata-provided hashes where available.
- Downloads use `.part` files and staged directories before final installation.
- Redirects from the default Hugging Face endpoint are restricted to HTTPS hosts under `huggingface.co` or `hf.co`.
- Model, cache, and output paths are checked to prevent directory traversal.
- Sensitive model and staging paths reject symlink-based replacement.
- Blender-to-engine JSON files are written atomically.
- Every engine session uses a random nonce and validates it on commands.
- The engine exits when the Blender-side heartbeat becomes stale.
- Inference runs in isolated Python processes with a restricted environment-variable allowlist.

These checks reduce accidental corruption and some path/process risks; they are not a substitute for reviewing third-party model and backend code before running it.

## Troubleshooting

### Generate Model is disabled

Confirm all of the following:

- The main image exists and uses PNG, JPG, JPEG, or WebP.
- The required shape model has status **Installed**.
- **Enhance Texture** is installed when texture generation is enabled.
- No generation job is currently active.

After restarting Blender, use **Check** on the required models to repopulate their session status.

### `Runtime not installed`

The expected interpreter does not exist at one of these paths:

```text
<models>/.sculptr/runtime/hunyuan/Scripts/python.exe
<models>/.sculptr/runtime/hunyuan/bin/python
<models>/.sculptr/runtime/hi3d/Scripts/python.exe
<models>/.sculptr/runtime/hi3d/bin/python
```

Provision the runtime required by the selected action.

### `Backend unavailable`

The runtime started, but it could not import the expected backend module or find a callable `run_action` function. Install the appropriate backend into that isolated environment.

### Blender asks for Online Access

Enable online access in Blender preferences. SculptR checks this setting before starting a model download.

### Hunyuan download is blocked

Review the applicable Hunyuan license listed in this README and enable the license-confirmation checkbox. SculptR refuses restricted downloads without it.

### A Hugging Face download returns an authorization error

The current downloader does not send a Hugging Face access token. Repositories that require authenticated access cannot be downloaded by the checked-in implementation.

### GPU or VRAM displays `N/A`

The engine reports NVIDIA utilization through `nvidia-smi`. Missing `nvidia-smi`, a non-NVIDIA GPU, or a command failure results in unavailable GPU telemetry.

### Model check fails while offline

Checks first resolve the pinned Hugging Face metadata and then validate local files. Reconnect and retry.

### A completed result cannot be imported

SculptR only accepts an existing `.glb` or `.gltf` path for Blender import, while the inference runner itself requires its backend to produce a `.glb` inside the assigned output directory.

## Development

From the `sculptr` package directory:

```bash
blender --command extension validate
blender --command extension build
```

The manifest excludes the following from release archives:

```text
__pycache__/
.git
*.zip
tests/
```

The Blender-facing control plane uses Blender's Python API and the Python standard library. Heavy inference dependencies are expected in separate runtime environments rather than Blender's own Python environment.

## Current limitations

- Runtime environment creation and backend installation are not implemented.
- There is no text-prompt input; generation is image-driven.
- Results are imported manually and do not replace source objects automatically.
- Installed model status is session-derived and must be checked again after a new engine session.
- Model checks require Hugging Face metadata access.
- Hugging Face authentication tokens are not supported.
- GPU and VRAM telemetry is NVIDIA-specific.
- Inference output is restricted to GLB.
- Generated inputs and outputs are not removed by **Clear Cache**.
- The repository does not currently define concrete hardware requirements for its external backends.

## License

The Blender extension manifest declares SculptR under **GPL-3.0-or-later**.

Model weights, model source code, and separately installed inference backends remain subject to their own licenses. Review those licenses independently before use or redistribution.
