# videolab bootstrap: create a venv, install dependencies, optionally wire the
# custom nodes and the downloaded models into an existing ComfyUI install.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -CpuOnly
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -ComfyUI "<path to your ComfyUI folder>"
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -ComfyUI "<path>" -Force
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -ComfyUI "<path>" -WriteModelPaths
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -ComfyUI "<path>" -WriteModelPaths -ModelsDir ".\models"
#
# -CpuOnly installs only the tooling needed for `videolab.py doctor`,
# `download`, and the offline parts of `selftest` (no PyTorch).
# -ComfyUI copies nodes/ (first-party and vendored third-party, licenses
# included) into <ComfyUI>\custom_nodes and prints the model-wiring options.
# A node pack folder that already exists there is skipped; -Force replaces it.
# -WriteModelPaths writes (only when absent) <ComfyUI>\extra_model_paths.yaml so
# ComfyUI can read the videolab models tree; -ModelsDir overrides the default
# <repo>\models base path. See README.md, "4b. Point ComfyUI to downloaded
# models".

[CmdletBinding()]
param(
    [string]$ComfyUI = "",
    [switch]$CpuOnly,
    [string]$ModelsDir = "",
    [switch]$WriteModelPaths,
    [switch]$Force
)

if ($WriteModelPaths -and $ComfyUI -eq "") {
    throw "-WriteModelPaths requires -ComfyUI <path>."
}

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $root ".venv"

function Find-Python {
    # Returns @{ exe = <command>; args = <fixed args> } for a working Python >= 3.12,
    # probing the version launcher first so the version check is exact.
    $candidates = @(
        @{ exe = "py";      args = @("-3.12") },
        @{ exe = "py";      args = @("-3") },
        @{ exe = "python";  args = @() },
        @{ exe = "python3"; args = @() }
    )
    foreach ($cand in $candidates) {
        if (-not (Get-Command $cand.exe -ErrorAction SilentlyContinue)) { continue }
        $probe = @($cand.args) + @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)")
        & $cand.exe $probe 2>$null
        if ($LASTEXITCODE -eq 0) { return $cand }
    }
    throw "Python 3.12+ was not found on PATH. Install it from python.org and re-run."
}

$py = Find-Python
$pyArgs = @($py.args)
Write-Host "[videolab] using $($py.exe) $($pyArgs -join ' ')" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $venv)) {
    Write-Host "[videolab] creating venv at $venv"
    & $py.exe @pyArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
}

$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPy)) { $venvPy = Join-Path $venv "bin/python" }
& $venvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed (exit $LASTEXITCODE)" }

if ($CpuOnly) {
    Write-Host "[videolab] installing CPU-only tooling (psutil, Pillow, huggingface_hub)"
    & $venvPy -m pip install psutil Pillow huggingface_hub
} else {
    Write-Host "[videolab] installing requirements.txt (torch cu130 + comfy-kitchen included)"
    & $venvPy -m pip install -r (Join-Path $root "requirements.txt")
}
if ($LASTEXITCODE -ne 0) { throw "dependency install failed (exit $LASTEXITCODE)" }

if ($ComfyUI -ne "") {
    if (-not (Test-Path -LiteralPath $ComfyUI)) {
        throw "ComfyUI directory not found: $ComfyUI"
    }
    $customNodes = Join-Path $ComfyUI "custom_nodes"
    if (-not (Test-Path -LiteralPath $customNodes)) {
        throw "Not a ComfyUI install (custom_nodes missing): $ComfyUI"
    }
    foreach ($pack in Get-ChildItem -LiteralPath (Join-Path $root "nodes") -Directory |
            Where-Object { $_.Name -ne "third_party" }) {
        $dest = Join-Path $customNodes $pack.Name
        if ((Test-Path -LiteralPath $dest) -and -not $Force) {
            Write-Host "[videolab] $dest already exists; skipping it (re-run with -Force to replace it)" -ForegroundColor Yellow
            continue
        }
        if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }
        Write-Host "[videolab] copying first-party node pack $($pack.Name) -> $dest"
        Copy-Item -LiteralPath $pack.FullName -Destination $dest -Recurse -Force
    }
    foreach ($pack in Get-ChildItem -LiteralPath (Join-Path $root "nodes\third_party") -Directory) {
        $dest = Join-Path $customNodes $pack.Name
        if ((Test-Path -LiteralPath $dest) -and -not $Force) {
            Write-Host "[videolab] $dest already exists; skipping it (re-run with -Force to replace it)" -ForegroundColor Yellow
            continue
        }
        if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }
        Write-Host "[videolab] copying vendored node pack $($pack.Name) -> $dest"
        Copy-Item -LiteralPath $pack.FullName -Destination $dest -Recurse -Force
    }
    Write-Host ""
    Write-Host "[videolab] Node packs copied (first-party + vendored third-party; licenses included; existing folders skipped unless -Force)." -ForegroundColor Yellow
    Write-Host "  H3 workflows: ComfyUI v0.36.0 core provides the MiniMax-H3 nodes"
    Write-Host "  (MiniMaxH3ReferenceToVideo / AddGuide / SigmaShift, ModelAttentionBackend are core)."
    Write-Host "  The vendored LBH-123-AI upscaler pack (MIT) supplies MinimaxH3LatentUpscaler3D,"
    Write-Host "  MMH3SpatialSplitParamsV10 and MMH3SplitUpscale."
    Write-Host "  LTX workflows: ComfyUI v0.36.0 core plus the vendored LiconStudio MSR pack v1.2.2"
    Write-Host "  (Apache-2.0; lab-notes commit edec572)."
    Write-Host "  Qwen workflows: the vendored comfyui_qwen21_optimized pack on ComfyUI v0.37.4."
    Write-Host ""
    Write-Host "  Workflow templates to submit are in: $(Join-Path $root 'workflows')"
    Write-Host ""

    if ($ModelsDir -eq "") { $ModelsDir = Join-Path $root "models" }
    $modelsPathYaml = $ModelsDir -replace '\\', '/'
    $modelPathsSnippet = @"
videolab:
  base_path: $modelsPathYaml
  diffusion_models: diffusion_models
  text_encoders: text_encoders
  vae: vae
  loras: loras
  latent_upscale_models: latent_upscale_models
  upscale_models: upscale_models
  prompt_generator: prompt_generator
"@
    if ($WriteModelPaths) {
        $yamlPath = Join-Path $ComfyUI "extra_model_paths.yaml"
        if (Test-Path -LiteralPath $yamlPath) {
            Write-Host "[videolab] $yamlPath already exists; not modifying it. Add this section manually:" -ForegroundColor Yellow
            Write-Host $modelPathsSnippet
        } else {
            Set-Content -LiteralPath $yamlPath -Value $modelPathsSnippet -Encoding ASCII
            Write-Host "[videolab] wrote model paths to $yamlPath (restart ComfyUI to apply)" -ForegroundColor Cyan
        }
    } else {
        Write-Host "[videolab] ComfyUI must see the downloaded weights. Either:"
        Write-Host "  - set VIDEOLAB_MODELS_DIR to '<ComfyUI>\models' before running videolab.py download, or"
        Write-Host "  - add this section to $ComfyUI\extra_model_paths.yaml (or re-run with -WriteModelPaths):"
        Write-Host $modelPathsSnippet
    }
}

Write-Host ""
Write-Host "[videolab] next steps:" -ForegroundColor Cyan
Write-Host "  $venvPy videolab.py doctor"
Write-Host "  $venvPy videolab.py download --profile smoke"
Write-Host "  $venvPy videolab.py selftest"
Write-Host "  $venvPy videolab.py bench --model ltx25 --comfy http://127.0.0.1:8188 --set PROMPT=`"a weathered wooden pier at blue hour, low camera push-in`" --set REF_IMAGE_1=character.png --set REF_IMAGE_2=prop.png --set REF_BACKGROUND=environment.png --set SHOT_ID=demo"
