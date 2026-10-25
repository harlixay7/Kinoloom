<#
.SYNOPSIS
  Gets a Kinoloom folder ready for install\INSTALL-MAIN.bat on 64-bit Windows 10 or 11.

.DESCRIPTION
  - Python 3.12: a private copy in tools\python, unless tool\.venv already exists.
    A Python installed on the PC is neither used nor changed.
  - Git: a private MinGit in tools\git when no git is on PATH. pip needs it for the
    two packages tool\requirements.txt installs from git.
  - The Microsoft Visual C++ Redistributable (x64) that torch needs: when it is
    missing or older than 14.40, it offers to install the current one from Microsoft
    (Windows asks for administrator rights).
  - Windows long paths: when this folder's path is long enough that the deepest
    installed files would pass 260 characters, and the setting is off, it offers to
    turn it on (LongPathsEnabled; administrator rights again). Declined, it stops.
    These two are the only changes outside this folder.

  Python and MinGit are pinned in install\components.json ("tools") by URL, size and
  SHA-256: a download that does not match is deleted, never used. The Visual C++
  installer has no fixed hash (Microsoft updates it in place), so it runs only with a
  valid Microsoft Corporation signature. Nothing is added to PATH.

  Run by INSTALL.bat and install\INSTALL-MAIN.bat. Safe to run again: what is there
  is kept. Exit code 0 = ready, 1 = stopped (the reason is printed).
#>
[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest crawls with its progress bar on

$Root = Split-Path -Parent $PSScriptRoot
$Work = Join-Path $Root 'temp\bootstrap'
$Cat = Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot 'components.json') | ConvertFrom-Json
# binaries built with Visual Studio 2022 17.10 or later (torch among them) crash on an
# older msvcp140.dll
$MinVcRuntime = [version]'14.40'
$VcRedistUrls = @('https://aka.ms/vc14/vc_redist.x64.exe',
                  'https://aka.ms/vs/17/release/vc_redist.x64.exe')
# the deepest file of a full install sits about 211 characters below the studio folder
$MaxShortRoot = 45

function Say([string]$Text) { Write-Host "  $Text" }

function Stop-Bootstrap([string]$Text) {
    Write-Host ''
    Write-Host "[FAIL] $Text" -ForegroundColor Red
    exit 1
}

function Confirm-Step([string]$Question, [bool]$Default) {
    $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
    $a = (Read-Host "  $Question $hint").Trim().ToLowerInvariant()
    if ($a -eq '') { return $Default }
    return $a -in 'y', 'yes'
}

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    # Windows PowerShell 5.1 turns a program's stderr (curl's progress bar) into errors
    # when its output is redirected; here only the exit code decides
    $ErrorActionPreference = 'Continue'
    & $Exe @Arguments
    return $LASTEXITCODE
}

function Invoke-Download([string]$Url, [string]$Dest) {
    # curl.exe (Windows 10 1803 and later) shows progress; Invoke-WebRequest is the fallback
    New-Item -ItemType Directory -Force (Split-Path $Dest) | Out-Null
    Remove-Item -Force -ErrorAction SilentlyContinue $Dest
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        $code = Invoke-Native $curl.Source @('-fL', '--retry', '3', '--retry-delay', '3',
                                             '--progress-bar', '-o', $Dest, $Url)
        if ($code -eq 0) { return $true }
    } else {
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        try {
            Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
            return $true
        } catch {
            Say "download failed: $($_.Exception.Message)"
        }
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $Dest
    return $false
}

function Get-PinnedFile($Pin, [string]$Dest) {
    $part = "$Dest.part"
    if (-not (Invoke-Download $Pin.url $part)) { Stop-Bootstrap "could not download $($Pin.url)" }
    $size = (Get-Item $part).Length
    $hash = (Get-FileHash $part -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($size -ne $Pin.size -or $hash -ne $Pin.sha256) {
        Remove-Item -Force $part
        Stop-Bootstrap ("$($Pin.url) does not match install\components.json " +
                        "(size $size, sha256 $hash). It was deleted, not used.")
    }
    Move-Item -Force $part $Dest
}

function Expand-Pinned($Pin, [string]$Archive, [string]$Target) {
    # unpack in temp\bootstrap, then move into place, so a broken run leaves no half folder
    $x = Join-Path $Work ('x-' + (Split-Path -Leaf $Target))
    if (Test-Path $x) { Remove-Item -Recurse -Force $x }
    New-Item -ItemType Directory $x | Out-Null
    if ((Invoke-Native 'tar.exe' @('-xf', $Archive, '-C', $x)) -ne 0) { Stop-Bootstrap "could not unpack $Archive" }
    $src = $x
    if ($Pin.PSObject.Properties['subdir']) { $src = Join-Path $x $Pin.subdir }
    New-Item -ItemType Directory -Force (Split-Path $Target) | Out-Null
    Move-Item $src $Target
    if (Test-Path $x) { Remove-Item -Recurse -Force $x }
    Remove-Item -Force $Archive
}

function Test-Python312([string]$Exe) {
    $check = 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32 else 1)'
    return (Invoke-Native $Exe @('-I', '-c', $check)) -eq 0
}

function Get-VcRuntimeVersion {
    # a 32-bit PowerShell sees SysWOW64 under System32; Sysnative is the real one
    $sys = if ([Environment]::Is64BitProcess) { 'System32' } else { 'Sysnative' }
    $dir = Join-Path $env:SystemRoot $sys
    $dll = Join-Path $dir 'msvcp140.dll'
    if (-not (Test-Path $dll) -or -not (Test-Path (Join-Path $dir 'vcruntime140_1.dll'))) { return $null }
    $v = (Get-Item $dll).VersionInfo
    return New-Object Version $v.FileMajorPart, $v.FileMinorPart, $v.FileBuildPart, $v.FilePrivatePart
}

# ------------------------------------------------------------------ checks
function Assert-Machine {
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    if ($arch -ne 'AMD64') { Stop-Bootstrap "Kinoloom needs a 64-bit Intel or AMD PC (this one is $arch)." }
    if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) {
        Stop-Bootstrap 'tar.exe is missing: Kinoloom needs Windows 10 version 1803 or later.'
    }
    if ($Root.Length -le $MaxShortRoot -or (Test-LongPaths)) { return }
    Write-Host ''
    Say "This folder's path is long:"
    Say "  $Root"
    Say 'Some files Kinoloom installs would get a path longer than the 260 characters'
    Say 'Windows allows by default. Windows can lift that limit with its "long paths"'
    Say 'setting. Changing it needs administrator rights, so Windows will ask you to'
    Say 'allow it.'
    if (Confirm-Step 'Turn on long paths?' $true) {
        if (Enable-LongPaths) {
            Say 'OK   long paths are on'
            return
        }
        Say 'The setting was not changed.'
    }
    Say 'Instead, you can move the Kinoloom folder somewhere short, like C:\Kinoloom,'
    Say 'and run INSTALL.bat there.'
    exit 1
}

function Test-LongPaths {
    $v = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' `
                          -Name LongPathsEnabled -ErrorAction SilentlyContinue
    return [bool]($v -and $v.LongPathsEnabled -eq 1)
}

function Enable-LongPaths {
    # One registry value under HKLM, hence the administrator prompt. The Python this
    # installs declares longPathAware, so the programs started after the change (pip
    # among them) can write past 260 characters.
    try {
        $p = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\reg.exe') `
                           -ArgumentList 'add', 'HKLM\SYSTEM\CurrentControlSet\Control\FileSystem',
                                         '/v', 'LongPathsEnabled', '/t', 'REG_DWORD', '/d', '1', '/f' `
                           -Verb RunAs -Wait -PassThru -WindowStyle Hidden
    } catch {
        return $false       # the administrator prompt was declined
    }
    return ($p.ExitCode -eq 0 -and (Test-LongPaths))
}

# ------------------------------------------------------------------ steps
function Install-VcRuntime {
    $have = Get-VcRuntimeVersion
    if ($have -and $have -ge $MinVcRuntime) {
        Say "OK   Microsoft Visual C++ runtime $have"
        return
    }
    $what = if ($have) { "version $have is older than $MinVcRuntime" } else { 'it is not installed' }
    Say "torch needs the Microsoft Visual C++ Redistributable (x64), and $what."
    $manual = "Install it from $($VcRedistUrls[0]), then run this again."
    if (-not (Confirm-Step 'Install the current one from Microsoft now? Windows asks for administrator rights.' $true)) {
        Stop-Bootstrap $manual
    }
    $exe = Join-Path $Work 'vc_redist.x64.exe'
    $got = $false
    foreach ($url in $VcRedistUrls) {
        if (Invoke-Download $url $exe) { $got = $true; break }
    }
    if (-not $got) { Stop-Bootstrap "could not download the Visual C++ Redistributable. $manual" }
    $sig = Get-AuthenticodeSignature $exe
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch '(^|, )O=Microsoft Corporation(,|$)') {
        Remove-Item -Force $exe
        Stop-Bootstrap "the downloaded vc_redist.x64.exe is not signed by Microsoft. It was deleted, not run."
    }
    try {
        $p = Start-Process -FilePath $exe -ArgumentList '/install', '/passive', '/norestart' `
                           -Verb RunAs -Wait -PassThru
    } catch {
        Remove-Item -Force -ErrorAction SilentlyContinue $exe
        Stop-Bootstrap "the Visual C++ Redistributable was not installed ($($_.Exception.Message)). $manual"
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $exe
    # 0 = installed, 3010 = installed (finishes at the next restart), 1638 = a newer one is there
    if ($p.ExitCode -notin 0, 3010, 1638) {
        Stop-Bootstrap "the Visual C++ Redistributable installer stopped with code $($p.ExitCode). $manual"
    }
    $have = Get-VcRuntimeVersion
    if (-not $have -or $have -lt $MinVcRuntime) {
        Stop-Bootstrap "the Visual C++ runtime is still $have after the install. Restart Windows and run this again."
    }
    Say "OK   Microsoft Visual C++ runtime $have"
}

function Install-Python {
    if (Test-Path (Join-Path $Root 'tool\.venv\Scripts\python.exe')) {
        Say 'OK   tool\.venv exists; it keeps the Python it was made with'
        return
    }
    $target = Join-Path $Root 'tools\python'
    $exe = Join-Path $target 'python.exe'
    if (Test-Path $exe) {
        if (-not (Test-Python312 $exe)) {
            Stop-Bootstrap 'tools\python is not a 64-bit Python 3.12. Move or delete that folder, then run this again.'
        }
        Say 'OK   Python 3.12 (tools\python)'
        return
    }
    if (Test-Path $target) {
        Stop-Bootstrap 'tools\python exists but has no python.exe. Delete that folder, then run this again.'
    }
    $pin = $Cat.tools.python
    Say ("...  Python {0} -> tools\python ({1:N0} MB)" -f $pin.version, ($pin.size / 1MB))
    $archive = Join-Path $Work 'python.tar.gz'
    Get-PinnedFile $pin $archive
    Expand-Pinned $pin $archive $target
    if (-not (Test-Python312 $exe)) { Stop-Bootstrap 'the unpacked tools\python does not run.' }
    Say 'OK   Python 3.12 (tools\python)'
}

function Install-Git {
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($git) {
        Say "OK   Git ($($git.Source))"
        return
    }
    $target = Join-Path $Root 'tools\git'
    if (Test-Path (Join-Path $target 'cmd\git.exe')) {
        Say 'OK   Git (tools\git)'
        return
    }
    if (Test-Path $target) {
        Stop-Bootstrap 'tools\git exists but has no cmd\git.exe. Delete that folder, then run this again.'
    }
    $pin = $Cat.tools.git
    Say ("...  {0} -> tools\git ({1:N0} MB)" -f $pin.version, ($pin.size / 1MB))
    $archive = Join-Path $Work 'mingit.zip'
    Get-PinnedFile $pin $archive
    Expand-Pinned $pin $archive $target
    if (-not (Test-Path (Join-Path $target 'cmd\git.exe'))) { Stop-Bootstrap 'tools\git\cmd\git.exe is missing after unpacking.' }
    Say 'OK   Git (tools\git)'
}

Write-Host ''
Write-Host '== Python 3.12, Git and the Visual C++ runtime (install\bootstrap.ps1) =='
Assert-Machine
New-Item -ItemType Directory -Force $Work | Out-Null
Install-VcRuntime
Install-Python
Install-Git
exit 0
