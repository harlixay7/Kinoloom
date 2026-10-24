# Copy model packages (install/model_packages.json) from a golden Kinoloom
# tree to a worker install. Called by INSTALL-WORKER.bat; can also be run by
# hand to add a package later:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File copy_packages.ps1 `
#       -Source \\MAIN\Kinoloom -Dest C:\Kinoloom_worker `
#       -Packages image-sdxl,post-upscale
#
# Each listed file is copied with robocopy (restartable, keeps the folder
# layout). Files already present with the same size are skipped. Files the
# source tree does not have are reported, not fatal: weights kept outside
# the tree (paths.extra_model_dirs on the main PC) are not visible through
# the share and must be copied separately.
# Exit code = number of failed copies (0 = success).
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Dest,
    [Parameter(Mandatory = $true)][string]$Packages
)
$ErrorActionPreference = 'Stop'
$manifest = Join-Path $Source 'install\model_packages.json'
if (-not (Test-Path -LiteralPath $manifest)) {
    Write-Host "  [FAIL] manifest not found: $manifest"
    exit 1
}
$m = Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json
$failed = 0
$copied = 0
$present = 0
$absent = @()
foreach ($name in ($Packages -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
    $pkg = $m.packages.$name
    if ($null -eq $pkg) {
        Write-Host "  [FAIL] unknown package '$name'"
        $failed++
        continue
    }
    Write-Host "  package $name - $($pkg.description)"
    foreach ($rel in $pkg.paths) {
        $relWin = $rel -replace '/', '\'
        $src = Join-Path $Source $relWin
        $dst = Join-Path $Dest $relWin
        if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
            Write-Host "    [miss] not on the source tree: $rel"
            $absent += $rel
            continue
        }
        if ((Test-Path -LiteralPath $dst -PathType Leaf) -and
            ((Get-Item -LiteralPath $dst).Length -eq (Get-Item -LiteralPath $src).Length)) {
            Write-Host "    [ok]   $rel (already present)"
            $present++
            continue
        }
        Write-Host "    [copy] $rel"
        & robocopy (Split-Path -Parent $src) (Split-Path -Parent $dst) (Split-Path -Leaf $src) `
            /Z /J /R:2 /W:5 /NFL /NDL /NP /NJH /NJS | Out-Null
        if ($LASTEXITCODE -ge 8) {
            Write-Host "    [FAIL] robocopy exit $LASTEXITCODE for $rel"
            $failed++
        } else {
            $copied++
        }
    }
}
Write-Host "  models: $copied copied, $present already present, $($absent.Count) missing on the source, $failed failed"
if ($absent.Count -gt 0) {
    Write-Host "  [WARN] the missing files are not under $Source. If the main PC keeps"
    Write-Host "         them elsewhere (paths.extra_model_dirs), copy them into"
    Write-Host "         $Dest\<same relative path> before rendering with them."
}
exit $failed
