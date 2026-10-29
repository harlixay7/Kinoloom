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
  - Before a first install it checks the free disk space, and asks before it
    installs into a folder that OneDrive syncs.

  Python and MinGit are pinned in install\components.json ("tools") by URL, size and
  SHA-256: a download that does not match is deleted, never used. The Visual C++
  installer has no fixed hash (Microsoft updates it in place), so it runs only with a
  valid Microsoft Corporation signature. Nothing is added to PATH.

  Everything it prints goes to logs\install\install.log as well; when it stops, the
  reason is also saved in logs\install\LAST-FAILURE.txt.

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
$LogDir = Join-Path $Root 'logs\install'
$InstallLog = Join-Path $LogDir 'install.log'
$LastFailure = Join-Path $LogDir 'LAST-FAILURE.txt'
$StepName = 'step 1 of 5, Python 3.12, Git and the Visual C++ runtime'
$Rerun = if ($env:KINOLOOM_RERUN) { $env:KINOLOOM_RERUN } else { 'run INSTALL.bat again' }
$RerunCap = $Rerun.Substring(0, 1).ToUpper() + $Rerun.Substring(1)
$Cat = $null              # install\components.json, read once the functions below exist
# binaries built with Visual Studio 2022 17.10 or later (torch among them) crash on an
# older msvcp140.dll
$MinVcRuntime = [version]'14.40'
$VcRedistUrls = @('https://aka.ms/vc14/vc_redist.x64.exe',
                  'https://aka.ms/vs/17/release/vc_redist.x64.exe')
# the deepest file of a full install sits about 211 characters below the studio folder
$MaxShortRoot = 45
# what a first install needs free on the studio folder's drive while it runs, in GB
# (10^9 bytes): the install, pip's download cache and its temporary files, which
# install\install_support.py keeps on this drive and deletes at the end
$NeedFreshGB = 30     # 25.2 to 27.0 GB at the peak of the measured installs
$CacheGB = 6          # what pip downloaded in them
$KeepGB = 17          # what stayed: 16.2 GB, 17.5 GB with Qwen-Image 2.1 on CUDA 12.6
$Utf8 = New-Object System.Text.UTF8Encoding $false
$script:DownloadError = ''

function Write-Log([string]$Text) {
    # a log that cannot be written never stops the install
    try {
        [void][IO.Directory]::CreateDirectory($LogDir)
        [IO.File]::AppendAllText($InstallLog, "$Text`r`n", $Utf8)
    } catch { }
}

function Say([string]$Text) {
    Write-Host "  $Text"
    Write-Log "  $Text"
}

function Format-Wrapped([string]$Text, [int]$Width = 73) {
    $out = @()
    $line = ''
    foreach ($w in ($Text -split '\s+')) {
        if ($line -and ($line.Length + 1 + $w.Length) -gt $Width) { $out += $line; $line = $w }
        elseif ($line) { $line += " $w" }
        else { $line = $w }
    }
    if ($line) { $out += $line }
    return $out
}

function Stop-Bootstrap([string]$Text, [switch]$Calm) {
    # the same report install\install_support.py gives for the later steps: on the
    # screen, in install.log and in LAST-FAILURE.txt (start.bat shows that one)
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $body = @(' THE INSTALL STOPPED', '', " Step: $StepName")
    $body += @(Format-Wrapped $Text | ForEach-Object { " $_" })
    $body += @('', " Saved to:  $LastFailure", " Full log:  $InstallLog")
    $rule = ' ' + ('=' * 74)
    $color = if ($Calm) { 'Gray' } else { 'Red' }
    Write-Host ''
    Write-Host $rule -ForegroundColor $color
    foreach ($l in $body) { Write-Host $l }
    Write-Host $rule -ForegroundColor $color
    Write-Log "==== FAILED $stamp ===="
    foreach ($l in $body) { Write-Log $l }
    Write-Log '==== end of failure ===='
    try {
        [void][IO.Directory]::CreateDirectory($LogDir)
        [IO.File]::WriteAllText($LastFailure, ((@("Kinoloom install, $stamp") + $body) -join "`r`n") + "`r`n", $Utf8)
    } catch { }
    exit 1
}

function Confirm-Step([string]$Question, [bool]$Default) {
    $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
    $a = "$(Read-Host "  $Question $hint")".Trim().ToLowerInvariant()
    Write-Log "  $Question $hint $a"
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

# what curl's exit codes mean, for the report
$CurlWhy = @{
    6  = "this PC could not look up the server's address (DNS)"
    7  = 'the server could not be reached'
    18 = 'the download was cut short'
    22 = 'the server answered with an error'
    23 = 'the file could not be written (is the disk full?)'
    28 = 'the connection timed out'
    35 = 'the secure (HTTPS) connection failed'
    56 = 'the connection broke off'
    60 = "the server's certificate was not trusted (an antivirus that scans HTTPS, or a company network, can cause this)"
}

function Invoke-Download([string]$Url, [string]$Dest) {
    # curl.exe (Windows 10 1803 and later) shows progress and retries a broken or
    # stalled transfer; Invoke-WebRequest, which uses the Windows proxy settings,
    # is the second try
    New-Item -ItemType Directory -Force (Split-Path $Dest) | Out-Null
    Remove-Item -Force -ErrorAction SilentlyContinue $Dest
    $script:DownloadError = ''
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        $code = Invoke-Native $curl.Source @('-fL', '--retry', '6', '--retry-delay', '10',
                                             '--retry-connrefused', '--connect-timeout', '30',
                                             '--speed-limit', '1024', '--speed-time', '120',
                                             '--progress-bar', '-o', $Dest, $Url)
        if ($code -eq 0) { return $true }
        $why = if ($CurlWhy.ContainsKey([int]$code)) { $CurlWhy[[int]$code] } else { "curl stopped with code $code" }
        $script:DownloadError = $why
        Say "curl could not download it: $why. Trying again with Windows' own downloader..."
        Remove-Item -Force -ErrorAction SilentlyContinue $Dest
    }
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
        return $true
    } catch {
        $msg = $_.Exception.Message
        Say "download failed: $msg"
        $script:DownloadError = if ($script:DownloadError) { "$($script:DownloadError); then: $msg" } else { $msg }
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $Dest
    return $false
}

function Get-PinnedFile($Pin, [string]$Dest) {
    $part = "$Dest.part"
    if (-not (Invoke-Download $Pin.url $part)) {
        Stop-Bootstrap ("Could not download $($Pin.url): $($script:DownloadError). Check that " +
                        'this PC is online; a VPN, proxy, firewall or antivirus web filter can ' +
                        "block downloads. Then $Rerun.")
    }
    $size = (Get-Item $part).Length
    $hash = (Get-FileHash $part -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($size -ne $Pin.size -or $hash -ne $Pin.sha256) {
        Remove-Item -Force $part
        Stop-Bootstrap ("$($Pin.url) does not match install\components.json " +
                        "(size $size, sha256 $hash). It was deleted, not used. $RerunCap; " +
                        'if it happens again, a proxy or an antivirus may be changing downloads.')
    }
    Move-Item -Force $part $Dest
}

function Expand-Pinned($Pin, [string]$Archive, [string]$Target) {
    # unpack in temp\bootstrap, then move into place, so a broken run leaves no half folder
    $x = Join-Path $Work ('x-' + (Split-Path -Leaf $Target))
    if (Test-Path $x) { Remove-Item -Recurse -Force $x }
    New-Item -ItemType Directory $x | Out-Null
    # tar.exe reads its arguments in the PC's ANSI code page, so it cannot open a
    # path with a letter outside it (Chinese on a Western PC): it gets both names
    # relative to the folder they are in, which it runs in
    Push-Location -LiteralPath (Split-Path $Archive)
    try {
        $code = Invoke-Native 'tar.exe' @('-xf', (Split-Path -Leaf $Archive), '-C', (Split-Path -Leaf $x))
    } finally {
        Pop-Location
    }
    if ($code -ne 0) {
        Stop-Bootstrap "Could not unpack $Archive. An antivirus may have blocked it. $RerunCap."
    }
    $src = $x
    if ($Pin.PSObject.Properties['subdir']) { $src = Join-Path $x $Pin.subdir }
    New-Item -ItemType Directory -Force (Split-Path $Target) | Out-Null
    Move-Retry $src $Target
    if (Test-Path $x) { Remove-Item -Recurse -Force $x }
    Remove-Item -Force $Archive
}

function Move-Retry([string]$From, [string]$To) {
    # an antivirus that scans the files just unpacked can hold one for a moment
    for ($try = 1; ; $try++) {
        try {
            Move-Item -LiteralPath $From -Destination $To -ErrorAction Stop
            return
        } catch {
            if ($try -ge 5) { throw }
            Write-Log "  moving $From failed ($($_.Exception.Message)); trying again in 3 s"
            Start-Sleep -Seconds 3
        }
    }
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
function Assert-PlainPath {
    # Windows PowerShell and parts of Kinoloom read [ and ] in a path as wildcards
    if ($Root -notmatch '[\[\]]') { return }
    Stop-Bootstrap -Calm ("Nothing was installed. This folder's path has a [ or ], which Windows " +
                          'PowerShell and parts of Kinoloom read as wildcards: ' + $Root + '. Rename ' +
                          'the folder without them, or move it somewhere short like C:\Kinoloom, ' +
                          'and run INSTALL.bat there.')
}

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
    Stop-Bootstrap -Calm ('Long paths stay off, so nothing was installed. Move the Kinoloom folder ' +
                          'somewhere short, like C:\Kinoloom, and run INSTALL.bat there.')
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

function Get-OneDriveRoot {
    # the OneDrive folder this one is in, or $null
    foreach ($v in 'OneDrive', 'OneDriveConsumer', 'OneDriveCommercial') {
        $od = [Environment]::GetEnvironmentVariable($v)
        if ($od -and ($Root.TrimEnd('\') + '\').StartsWith($od.TrimEnd('\') + '\',
                                                           [StringComparison]::OrdinalIgnoreCase)) {
            return $od
        }
    }
    return $null
}

function Assert-NotInOneDrive {
    if (-not (Get-OneDriveRoot)) { return }
    Write-Host ''
    Say 'This folder is inside OneDrive:'
    Say "  $Root"
    Say "OneDrive would upload the whole install (about $KeepGB GB), and it can hold"
    Say 'files open while pip writes them, which makes the install fail. A folder'
    Say 'outside OneDrive, like C:\Kinoloom, avoids both.'
    if (Confirm-Step 'Install in this OneDrive folder?' $false) {
        Say 'OK   installing in the OneDrive folder'
        return
    }
    Stop-Bootstrap -Calm ('Nothing was installed. Move the Kinoloom folder out of OneDrive, ' +
                          'for example to C:\Kinoloom, and run INSTALL.bat there.')
}

function Assert-Room {
    # Only before a first install. A later run fills in what is missing, and an
    # earlier run's downloads (temp\pip-cache) already take up part of the space.
    foreach ($d in 'tool\.venv', 'temp\pip-cache', 'ComfyUI_LTX\venv', 'ComfyUI_Qwen21\venv') {
        if (Test-Path (Join-Path $Root $d)) { return }
    }
    $need = $NeedFreshGB
    if ($env:PIP_CACHE_DIR) { $need -= $CacheGB }    # pip's cache is somewhere else then
    try {
        $drive = New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($Root))
        $free = $drive.AvailableFreeSpace / 1e9
    } catch {
        return      # a network share: no drive letter to check
    }
    Write-Log ("  free space on {0}: {1:N1} GB (a first install needs about {2} GB)" -f $drive.Name, $free, $need)
    if ($free -ge $need) { return }
    Stop-Bootstrap (("There is not enough free disk space on {0}: the install needs about {1} GB " +
                     "while it runs ({2} GB stay once it has finished), and {0} has {3:N1} GB free. " +
                     "Free up space, or move the Kinoloom folder to a drive with more room, then " +
                     "{4}.") -f $drive.Name.TrimEnd('\'), $need, $KeepGB, $free, $Rerun)
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
    $manual = "Install it from $($VcRedistUrls[0]), then $Rerun."
    if (-not (Confirm-Step 'Install the current one from Microsoft now? Windows asks for administrator rights.' $true)) {
        Stop-Bootstrap -Calm $manual
    }
    $exe = Join-Path $Work 'vc_redist.x64.exe'
    $got = $false
    foreach ($url in $VcRedistUrls) {
        if (Invoke-Download $url $exe) { $got = $true; break }
    }
    if (-not $got) { Stop-Bootstrap "Could not download the Visual C++ Redistributable ($($script:DownloadError)). $manual" }
    $sig = Get-AuthenticodeSignature $exe
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch '(^|, )O=Microsoft Corporation(,|$)') {
        Remove-Item -Force $exe
        Stop-Bootstrap "The downloaded vc_redist.x64.exe is not signed by Microsoft. It was deleted, not run. $manual"
    }
    try {
        $p = Start-Process -FilePath $exe -ArgumentList '/install', '/passive', '/norestart' `
                           -Verb RunAs -Wait -PassThru
    } catch {
        Remove-Item -Force -ErrorAction SilentlyContinue $exe
        Stop-Bootstrap "The Visual C++ Redistributable was not installed ($($_.Exception.Message)). $manual"
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $exe
    # 0 = installed, 3010 = installed (finishes at the next restart), 1638 = a newer one is there
    if ($p.ExitCode -notin 0, 3010, 1638) {
        Stop-Bootstrap "The Visual C++ Redistributable installer stopped with code $($p.ExitCode). $manual"
    }
    $have = Get-VcRuntimeVersion
    if (-not $have -or $have -lt $MinVcRuntime) {
        Stop-Bootstrap "The Visual C++ runtime is still $have after the install. Restart Windows, then $Rerun."
    }
    Say "OK   Microsoft Visual C++ runtime $have"
}

function Install-Python {
    # a finished tool\.venv keeps the Python it was made with; an unfinished one, or one
    # whose Python no longer starts (made in another folder), is made again in step 2,
    # from tools\python
    $venv = Join-Path $Root 'tool\.venv'
    $venvPy = Join-Path $venv 'Scripts\python.exe'
    if ((Test-Path $venvPy) -and -not (Test-Path (Join-Path $venv '.kinoloom-incomplete')) -and
        (Test-Python312 $venvPy)) {
        Say 'OK   tool\.venv exists; it keeps the Python it was made with'
        return
    }
    $target = Join-Path $Root 'tools\python'
    $exe = Join-Path $target 'python.exe'
    if (Test-Path $exe) {
        if (-not (Test-Python312 $exe)) {
            Stop-Bootstrap "tools\python is not a 64-bit Python 3.12. Move or delete that folder, then $Rerun."
        }
        Say 'OK   Python 3.12 (tools\python)'
        return
    }
    if (Test-Path $target) {
        Stop-Bootstrap "tools\python exists but has no python.exe. Delete that folder, then $Rerun."
    }
    $pin = $Cat.tools.python
    Say ("...  Python {0} -> tools\python ({1:N0} MB)" -f $pin.version, ($pin.size / 1e6))
    $archive = Join-Path $Work 'python.tar.gz'
    Get-PinnedFile $pin $archive
    Expand-Pinned $pin $archive $target
    if (-not (Test-Python312 $exe)) {
        Stop-Bootstrap "The unpacked tools\python does not run. An antivirus may have removed a file. Delete tools\python, then $Rerun."
    }
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
        Stop-Bootstrap "tools\git exists but has no cmd\git.exe. Delete that folder, then $Rerun."
    }
    $pin = $Cat.tools.git
    Say ("...  {0} -> tools\git ({1:N0} MB)" -f $pin.version, ($pin.size / 1e6))
    $archive = Join-Path $Work 'mingit.zip'
    Get-PinnedFile $pin $archive
    Expand-Pinned $pin $archive $target
    if (-not (Test-Path (Join-Path $target 'cmd\git.exe'))) { Stop-Bootstrap "tools\git\cmd\git.exe is missing after unpacking. Delete tools\git, then $Rerun." }
    Say 'OK   Git (tools\git)'
}

# an error nobody planned for still ends in a report, never a bare red trace
trap {
    $where = if ($_.InvocationInfo) { " (install\bootstrap.ps1, line $($_.InvocationInfo.ScriptLineNumber))" } else { '' }
    Stop-Bootstrap ("Something unexpected went wrong$($where): $($_.Exception.Message) " +
                    "$RerunCap; if it fails the same way, please open an issue at " +
                    'https://github.com/harlixay7/Kinoloom/issues and attach logs\install\install.log.')
}

Write-Host ''
Write-Host '== Python 3.12, Git and the Visual C++ runtime (install\bootstrap.ps1) =='
Write-Log "  folder: $Root"
Write-Log ("  Windows {0}, PowerShell {1}, {2}-bit" -f [Environment]::OSVersion.Version, $PSVersionTable.PSVersion,
           $(if ([Environment]::Is64BitProcess) { 64 } else { 32 }))
try {
    # the edition (an N edition lacks the Media Feature Pack) and the full build
    $cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $parts = foreach ($n in 'EditionID', 'DisplayVersion', 'CurrentBuild', 'UBR') {
        if ($cv.PSObject.Properties[$n]) { "$n $($cv.$n)" }
    }
    Write-Log ("  Windows: " + ($parts -join ', '))
} catch { }
Assert-PlainPath
# -LiteralPath: a [ or ] in the folder's path is reported by Assert-PlainPath, not here
$Cat = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'components.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Assert-Machine
Assert-NotInOneDrive
Assert-Room
New-Item -ItemType Directory -Force $Work | Out-Null
Install-VcRuntime
Install-Python
Install-Git
exit 0
