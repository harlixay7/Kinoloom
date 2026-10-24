[Kinoloom](../../README.md) › [Docs](../../docs/README.md) › Scripts

# tool/scripts

> **In short:** Helper scripts that run next to the studio: install checks,
> the chain lab and the showcase, benchmarks, and model tools. The studio
> itself starts only `warm_studio.py`; you run the others by hand from the
> repository root, with `tool\.venv\Scripts\python.exe`. Most of them talk to a
> ComfyUI instance, so they need a set-up studio, not just a clone.

## Checking an install

| Script | What it does |
| :--- | :--- |
| `verify_restore.py` | The check `install\verify_install.py` runs in a full run: torch and CUDA, the model files under `models\`, the movie models in the video registry, the optional extras and the studio's imports. Run it with an empty `PYTHONPATH`. |
| `verify_workflows.py` | Builds every workflow template in `tool/workflows/` and checks that each model file it references exists on disk. |
| `verify_ref_models.py` | Checks the SDXL reference-image (IP-Adapter) stack: file sizes and tensor keys offline, then, with the render farm up, ComfyUI's own model lists and the graph `models.build_ref_workflow()` builds. |
| `model_inventory.py` | Resolves every workflow template to the model files it loads and compares them with what is on disk. |
| `smoke_video.py` | Renders one short video through the running render farm, end to end, and checks the result. |

## The chain lab and the showcase

| Script | What it does |
| :--- | :--- |
| `chain_lab.py` | The lab harness for seamless chains: clean ComfyUI instances, graphs checked against `/object_info` before any GPU time, telemetry, one record per step under `output/seamlab/runs/`. The showcase chains ran through its `--request` mode ([SHOWCASE.md](../../docs/SHOWCASE.md) §16). |
| `_comfy_lab_boot.py` | Used by `chain_lab.py`: starts ComfyUI with an on-demand stack dump, for diagnosing a hung server. |
| `showcase.py` | Renders the showcase stills with Qwen-Image 2.1, records the picks, and builds the web media and comparisons. |

## Benchmarks and GPU tools

| Script | What it does |
| :--- | :--- |
| `bench.py` | A benchmark harness whose every run is fully specified and recorded (prompt, seed, resolution, frames, telemetry). |
| `_gpu_gate.py` | Used by `bench.py`: refuses to queue work while the studio's own renders are running or waiting, or while that cannot be read. |
| `gpu_mem.py` | Reads the device-wide dedicated VRAM from the Windows performance counters. |
| `analyze_crash.cmd` | Copies the Windows crash dumps to `temp\dumps` and analyses them with WinDbg / cdb, if one is installed (run as Administrator). |

## Models

| Script | What it does |
| :--- | :--- |
| `warm_studio.py` | Started by the launcher once the render farm answers: loads the default models, so the first render is not a cold start. |
| `download_wan_diffusers.py` | Downloads the diffusers-format Wan 2.2 TI2V-5B pipeline that ai-toolkit needs for LoRA training. |
| `publish_models_hf.py` | Copies your installed weights to a **private** Hugging Face repository of your own, for the Colab worker ([colab/README.md](../../colab/README.md)). It refuses to upload to a public repository, because several model licenses do not allow re-publishing the weights. |

The weights themselves are downloaded by `install\install_components.py`
([RESTORE_INSTRUCTIONS.md](../../docs/setup/RESTORE_INSTRUCTIONS.md), Step 6).
