[Kinoloom](../../README.md) › [Docs](../../docs/README.md) › LoRA training

# Character LoRA training: install guide (kohya sd-scripts)

> **In short:** This page shows how to install kohya sd-scripts and train a
> character face LoRA (a small add-on file that teaches the video model one
> face) from the command line. The base model is the legacy Wan 2.2 TI2V-5B
> (`models\train_models\wan2.2_ti2v_5B_fp16.safetensors`), so the LoRA applies
> to Wan 2.2 renders only; LTX-2.5 and MiniMax H3 shots use the cast's
> reference images instead. The LoRAs trained so far came from the studio's
> Wan 2.2 LoRA training (ai-toolkit, linked from the foot of the Casting page);
> there is no saved record of a run of this command-line path.

Run every command from the repository root. All paths resolve relative to it.

1. Clone sd-scripts at the commit `tool\requirements.txt` pins (`git` on PATH,
   or `tools\git\cmd\git.exe`):
```cmd
git clone https://github.com/kohya-ss/sd-scripts tools\sd-scripts
git -C tools\sd-scripts checkout 37a1cbbc5725ed2a3575506e7bd2001c9908ac92
```
   Nothing else needs installing: `tool\requirements.txt` already installs
   sd-scripts' `library` package and its dependencies at the same commit and
   versions. Don't run `pip install -r tools\sd-scripts\requirements.txt`; on
   another commit it would change the studio's pinned packages.

2. Put 10-20 consistent images of the character (same face, varied angles)
   into `roster\datasets\face\<char>\`.

3. Train:
```cmd
tool\.venv\Scripts\python.exe tool\train\train_lora.py --type face --dataset kael --id_name kael
```

4. The script writes `models\loras\char_<char>.safetensors` and copies it to
   `roster\loras\`. For the first main or supporting character of a shot, the
   video LoRA chain (`tool/lora_video.py`) uses `char_<char>.safetensors`, or
   else `<char>_face.safetensors`, from the video LoRA registry, at
   `lora.char_strength` (0.8 in the code, 0.7 in
   `tool/settings.example.yaml`). The face-fix pass (`tool/post.py`) only
   looks for `models\loras\<char>_face.safetensors`.

Notes:
- Portable setup: run from the repository root. All paths resolve from there, with no fixed drive letters.
- Memory settings: the script trains with an fp8 base (`--fp8_base`), fp16 mixed precision, adafactor, gradient checkpointing and latents cached to disk, chosen to fit an 8 GB card such as the RTX 3070.
- Trained face LoRAs apply to the legacy Wan 2.2 renders on the render farm (port 8188) and to the face-fix pass.
