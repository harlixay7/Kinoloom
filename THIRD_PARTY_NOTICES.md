[Kinoloom](README.md) › [Docs](docs/README.md) › Third-party notices

# Third-party notices

> **In short:** Kinoloom's own code is under the [MIT License](LICENSE). A few files in this repository come from other projects and keep their own licenses; they are listed in section 1. The studio also runs software and model weights that are not in this repository; sections 2 and 3 name them and their licenses. The model licenses matter most: some of them forbid commercial use or limit where you may use the model.

## 1. Files in this repository that are not under the MIT License

| File | From | License | License text |
| :--- | :--- | :--- | :--- |
| `ComfyUI_LTX/comfy_extras/nodes_lt_audio.py` | [ComfyUI](https://github.com/comfyanonymous/ComfyUI) v0.36.0 (commit `ee71d5c`), modified | GPL-3.0 | [ComfyUI_LTX/LICENSE](ComfyUI_LTX/LICENSE) |
| `ComfyUI/custom_nodes/ComfyUI-Impact-Pack/modules/impact/impact_onnx.py` | [ComfyUI-Impact-Pack](https://github.com/ltdrdata/ComfyUI-Impact-Pack) by Dr.Lt.Data, 8.28.3 (commit `429d015`), modified | GPL-3.0 | [its LICENSE.txt](ComfyUI/custom_nodes/ComfyUI-Impact-Pack/LICENSE.txt) |
| `ComfyUI_LTX/custom_nodes/Comfyui_Minimax_h3_latent_Upscaler/nodes/MMH3_Split_Upscale.py` | [Comfyui_Minimax_h3_latent_Upscaler](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler) by LBH-123-AI (commit `40316cf`), modified | MIT, Copyright (c) 2026 LBH-123-AI | [its LICENSE](ComfyUI_LTX/custom_nodes/Comfyui_Minimax_h3_latent_Upscaler/LICENSE) |
| `videolab/nodes/third_party/ComfyUI-LTX2.5-MSR/` | [ComfyUI-LTX2.5-MSR](https://github.com/liconstudio/ComfyUI-LTX2.5-MSR) by Licon Studio (v1.2.2), unmodified | Apache-2.0 | [its LICENSE](videolab/nodes/third_party/ComfyUI-LTX2.5-MSR/LICENSE) |
| `videolab/nodes/third_party/Comfyui_Minimax_h3_latent_Upscaler/` | [Comfyui_Minimax_h3_latent_Upscaler](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler) by LBH-123-AI (commit `40316cf`), modified | MIT, Copyright (c) 2026 LBH-123-AI | [its LICENSE](videolab/nodes/third_party/Comfyui_Minimax_h3_latent_Upscaler/LICENSE) |
| `tool/static/fonts/archivo-var.woff2` | [Archivo](https://github.com/Omnibus-Type/Archivo), The Archivo Project Authors | SIL Open Font License 1.1 | [OFL-Archivo.txt](tool/static/fonts/OFL-Archivo.txt) |
| `tool/static/fonts/courier-prime-*.woff2` | [Courier Prime](https://github.com/quoteunquoteapps/CourierPrime), The Courier Prime Project Authors | SIL Open Font License 1.1 | [OFL-CourierPrime.txt](tool/static/fonts/OFL-CourierPrime.txt) |
| `tool/static/fonts/ibm-plex-mono-*.woff2` | [IBM Plex Mono](https://github.com/IBM/plex), IBM Corp. | SIL Open Font License 1.1 | [OFL-IBMPlexMono.txt](tool/static/fonts/OFL-IBMPlexMono.txt) |

The three modified files in the ComfyUI trees start with a header that names where they come from and what was changed; the changes to the vendored videolab copy of the upscaler pack are listed in [videolab/nodes/third_party/README.md](videolab/nodes/third_party/README.md). Kinoloom's own node packs inside the ComfyUI trees are under the MIT License like the rest of Kinoloom, and each carries its own copy of it ([ComfyUI-LTX25-Studio](ComfyUI_LTX/custom_nodes/ComfyUI-LTX25-Studio/LICENSE), [ComfyUI-Qwen21-Optimized](ComfyUI_Qwen21/custom_nodes/ComfyUI-Qwen21-Optimized/LICENSE)). The `ComfyUI_LTX/LICENSE` above the LTX pack is ComfyUI's GPL-3.0, for ComfyUI's own files there. `videolab/` carries its own MIT [LICENSE](videolab/LICENSE).

## 2. Software the studio runs, but this repository does not include

The installers (`INSTALL.bat` with `install/bootstrap.ps1`, and `install/install_components.py`) download these from their own publishers, or you install them yourself ([RESTORE_INSTRUCTIONS.md](docs/setup/RESTORE_INSTRUCTIONS.md)). Each one has its own license:

- **Python 3.12** (a [python-build-standalone](https://github.com/astral-sh/python-build-standalone) build, in `tools/python`): the Python Software Foundation License. `tools/python/LICENSE.txt` also holds the licenses of the bzip2 and Tcl/Tk code it ships. The other libraries in its `DLLs` folder come under their own licenses: OpenSSL (Apache-2.0), libffi (MIT), SQLite (public domain) and zlib (zlib License).
- **Git** ([MinGit](https://github.com/git-for-windows/git), in `tools/git`, only when there is no `git.exe` on the PATH): GPL-2.0, text in `tools/git/LICENSE.txt`.
- **Microsoft Visual C++ Redistributable** (from Microsoft, only when the runtime is missing or older than 14.40): Microsoft's license terms for the redistributable.
- **ComfyUI** (all three trees): GPL-3.0.
- **ComfyUI node packs** on the development machine: ComfyUI-Impact-Pack, ComfyUI-KJNodes, ComfyUI-Manager, ComfyUI-MultiGPU, ComfyUI-VideoHelperSuite, ComfyUI_CFGStar and ComfyUI_IPAdapter_plus are GPL-3.0; ComfyUI-GGUF, ComfyUI-SeedVR2_VideoUpscaler and ComfyUI-WanVideoWrapper are Apache-2.0; ComfyUI-Florence2 and ComfyUI-Frame-Interpolation are MIT; ComfyUI-LTX2.5-MSR is Apache-2.0 (its `pyproject.toml`; the LICENSE file is in the videolab copy). ComfyUI-PuLID ships no license file in the copy on the development machine; check its repository.
- **Python packages** in `tool/requirements.txt` and `install/requirements-frozen/`, **FFmpeg**, and **ai-toolkit** for LoRA training: see each project.

## 3. Model weights

No model weights are in this repository. The installer downloads them from the repositories listed in `install/components.json`, except MiniMax H3 and RealVisXL, and prints the licenses of the parts you choose before it starts. Each model has its own license, and the licenses differ a lot. Read the one for every model you download. This is a summary of the parts that matter most for this studio, not legal advice:

| Model | License | What to know |
| :--- | :--- | :--- |
| LTX-2.5 (Lightricks) | [LTX-2 Community License](https://github.com/Lightricks/LTX-2/blob/main/LICENSE-2) | Entities with annual revenue of at least $10 million need a separate commercial license. Attachment A lists uses that are not allowed (among them deepfakes and impersonation). If you pass the model on, you must include the agreement. |
| MiniMax H3 | [MiniMax H3 Community License](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE) | The grant covers the world **except the European Union, the United Kingdom, South Korea and the United States** (§I.5, §II), and §V.4 extends that limit to using or displaying the model's outputs. Commercial products above $20 million revenue need written authorization, and commercial products must show "MiniMax H3" in their interface (§IV). |
| Qwen-Image 2.1 | [Qwen Research License](https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE) | Non-commercial use only (research or evaluation) without a separate commercial license (§2). |
| Wan 2.2 TI2V-5B (legacy) | [Apache-2.0](https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B) | |
| RealVisXL V5 | [openrail++](https://huggingface.co/SG161222/RealVisXL_V5.0) | Use restrictions apply to the model and its outputs. |
| Z-Image Turbo, its text encoder and VAE | [Apache-2.0](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) | |
| CyberRealistic XL | [CreativeML OpenRAIL-M](https://huggingface.co/cyberdelia/CyberRealisticXL) | Use restrictions apply to the model and its outputs. |
| IP-Adapter (SDXL) and its CLIP-ViT-H image encoder | [Apache-2.0](https://huggingface.co/h94/IP-Adapter) | |
| 4x-UltraSharp (Qwen-Image finish) | [CC BY-NC-SA 4.0](https://openmodeldb.info/models/4x-UltraSharp), by Kim2091 | Non-commercial. The repository the installer downloads it from declares no license; the author's listing on OpenModelDB gives this one. |
| Real-ESRGAN x4plus | [BSD-3-Clause](https://github.com/xinntao/Real-ESRGAN) | |
| RIFE 4.7 | MIT ([ComfyUI-Frame-Interpolation](https://github.com/Fannovel16/ComfyUI-Frame-Interpolation)) | |
| face_yolov8m (face detector) | AGPL-3.0, as an Ultralytics YOLOv8 model ([Bingsu/adetailer](https://huggingface.co/Bingsu/adetailer) tags it Apache-2.0) | The installer exports it to ONNX on your PC. |
| SeedVR2 3B | [Apache-2.0](https://huggingface.co/ByteDance-Seed/SeedVR2-3B) | |
| LoRAs: Licon MSR V1 (LTX-2.5), Pruna 8-step (Qwen-Image), Wan 2.2 Turbo, CQ enhancer (LTX-2.5) | MSR: Apache-2.0; Pruna: Qwen Research License; the Wan Turbo and CQ LoRAs declare no license on their repositories | Each runs with its base model, whose license applies to that use. |

`tool/scripts/publish_models_hf.py` copies your installed weights to a private Hugging Face repository of your own, for your own machines (for example the Colab worker). Keep that repository private: several of these licenses do not allow you to re-publish the weights.
