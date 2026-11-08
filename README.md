# SculptR

SculptR is a Blender 4.2.1+ extension for image-to-3D generation, model management, texture repainting, and geometry enhancement. It opens in the 3D View sidebar (`N`) with **Generate** and **Models** tabs.

This repository is a blind-coded source build. No Blender, model, Python runtime, package, or CUDA dependency was installed or downloaded during development.

## Current scope

- Native Blender panels, collapsible sections, file pickers, and area-level image drag and drop.
- Main image plus optional left, right, and back views.
- Fast, High, and Ultra quality; face count; texture toggle and size; seed.
- Live job, download, hardware, storage, cancellation, retry, and model-check status.
- Hunyuan generation and texture cards, Hunyuan multiview, and Hi3D detail enhancement.
- Separate background engine process. Blender never imports ML packages.
- Safe `Runtime not installed` failures until a compatible managed runtime is supplied.

The controller and fixed inference adapter are included; heavy CUDA runtimes and backend packages are intentionally not bundled. `Enhance Detail` replaces geometry generated from a render and normal bridge; Hi3D does not refine existing vertices in place. Blender Undo retains the previous object.

## Source layout

```text
SculptR/
  STRATEGY.md          researched model and architecture decisions
  README.md
  sculptr/             Blender extension and standard-library bridge
  tests/               Blender-free tests
```

The extension archive must contain one top-level `sculptr/` directory. Its `blender_manifest.toml`, `__init__.py`, and remaining modules stay together. Do not include tests, model files, partial downloads, caches, or a Python runtime.

## Development checks

Run from this directory with an existing Python 3.11+ interpreter:

```powershell
python -m compileall -q sculptr tests
python -m unittest discover -s tests -v
```

These checks use temporary directories and local child processes only. They do not install packages, access model endpoints, or require Blender.

## Blender setup

1. Package `sculptr` as the ZIP's only top-level directory.
2. In Blender 4.2.1 or newer, install that ZIP as an extension.
3. Open the 3D View sidebar and choose **SculptR**.
4. In **Models**, choose a model directory.
5. Supply a separately qualified SculptR runtime before inference. This repository does not create or modify Python environments.

The bridge launches the engine in a separate process using Blender's interpreter arguments, isolated flags `-I -s -u`, a fixed trusted bootstrap, and no shell. Each launch creates:

```text
<models>/.sculptr/session/<nonce>/
  commands/
  states/
  signals/
  client-heartbeat.json
  engine-heartbeat.json
  resources.json
```

Commands and state use atomic JSON replacement. Cancellation uses nonce-scoped signal files. Closing or disabling SculptR requests clean engine shutdown and removes only its own session. Virtual environments isolate dependency conflicts; they are not a hostile-code security sandbox.

## Model sources

SculptR accepts model payloads only from pinned Hugging Face repositories:

- **Hunyuan:** [tencent/Hunyuan3D-2.1](https://huggingface.co/tencent/Hunyuan3D-2.1), v2.1 shape and matching VAE, about 8.03 GB selected.
- **Hunyuan More Views:** [tencent/Hunyuan3D-2mv](https://huggingface.co/tencent/Hunyuan3D-2mv), standard multiview checkpoint plus its matching v2.0 VAE from [tencent/Hunyuan3D-2](https://huggingface.co/tencent/Hunyuan3D-2), about 5.36 GB selected.
- **Hi3D for Enhance Detail:** [Stable-X/trellis-normal-v0-1](https://huggingface.co/Stable-X/trellis-normal-v0-1), [Stable-X/yoso-normal-v1-8-1](https://huggingface.co/Stable-X/yoso-normal-v1-8-1), and [facebook/dinov2-with-registers-large](https://huggingface.co/facebook/dinov2-with-registers-large), about 6.50 GB selected.
- **Enhance Texture:** [tencent/Hunyuan3D-2.1 PBR Paint](https://huggingface.co/tencent/Hunyuan3D-2.1/tree/main/hunyuan3d-paintpbr-v2-1), about 6.89 GB selected.

Checks require exact relative paths, expected sizes, and known SHA-256 values. Manually placed complete files pass the same checks. Downloads stage under `.partial`, resume with HTTP Range, and promote only after validation.

## Hunyuan license acceptance

Hunyuan 2.1 uses the [Tencent Hunyuan 3D 2.1 Community License](https://huggingface.co/tencent/Hunyuan3D-2.1/blob/main/LICENSE), while the multiview model uses the [Tencent Hunyuan 3D 2.0 Community License](https://huggingface.co/tencent/Hunyuan3D-2/blob/main/LICENSE).

SculptR keeps Hunyuan downloads disabled until the user accepts the applicable license. 
Acceptance does not change or grant rights beyond that license. 
Local file checks remain available because they do not download, alter, or upload model data.

## Runtime hand-off

A production runtime should provide signed platform-specific CPython archives, locked wheels, verified hashes, and these isolated environments:

```text
<models>/.sculptr/runtime/
  hunyuan/
  hi3d/
```