<#
.SYNOPSIS
    Set up Lysdexic TTS: virtual environment, dependencies, model, shortcuts.

.DESCRIPTION
    Written for Windows PowerShell 5.1, which is what ships with Windows, so it
    avoids the 7.x-only syntax (&&, ternary, ??) that would fail to parse there.

    Nothing here needs administrator rights. The venv, the model cache, the
    Start menu entry and the Run key are all per-user, which also means an
    uninstall is deleting a folder and two registry-adjacent items.

.PARAMETER Unattended
    Accept the default answer for every prompt. Intended for re-runs and CI,
    not for a first install where you may want to decline the shortcuts.

.PARAMETER NoShortcut
    Skip the Start menu entry.

.PARAMETER NoAutostart
    Skip the "start with Windows" entry.
#>
[CmdletBinding()]
param(
    [switch]$Unattended,
    [switch]$NoShortcut,
    [switch]$NoAutostart
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root 'venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$VenvPyw = Join-Path $Venv 'Scripts\pythonw.exe'
$Launcher = Join-Path $Root 'run_reader.pyw'

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

function Invoke-Native {
    <#
        Run a native command, judging success by exit code alone.

        pip, spacy and huggingface all write progress to stderr, and under
        $ErrorActionPreference='Stop' PowerShell 5.1 turns any captured stderr
        line into a terminating NativeCommandError -- so a normal download
        progress bar would abort the install. Relax the preference around the
        call and let the exit code decide.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # Out-Host, not the pipeline: the command's own output belongs on the
        # console, and returning it would mix it into the caller's variable.
        # $LASTEXITCODE is global, so the caller reads the result from there.
        #
        # Deliberately no 2>&1 -- redirecting a native command's stderr is what
        # wraps each line in an ErrorRecord and prints a NativeCommandError
        # block. Left alone, stderr goes straight to the console as intended.
        & $Exe @Arguments | Out-Host
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Ask($question, $defaultYes) {
    if ($Unattended) { return $defaultYes }
    if ($defaultYes) { $hint = '[Y/n]' } else { $hint = '[y/N]' }
    $answer = Read-Host "$question $hint"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $defaultYes }
    return $answer -match '^(y|yes)$'
}

# --- Python -----------------------------------------------------------------
# 3.11 is the floor because config reading uses stdlib tomllib. The py launcher
# is checked first: on a machine with several Pythons it is the only reliable
# way to ask for a specific version rather than whatever won the PATH race.
Write-Step 'Looking for Python 3.11 or newer'

$PyCmd = $null
$PyArgs = @()

# 3.11 is the floor (stdlib tomllib). The ceiling is deliberate: torch is pinned
# to 2.13.0 and a Python newer than SUPPORTED_MAX may have no wheel for it, which
# would fail deep inside pip after a long download rather than here. Raise the
# ceiling once a newer Python is known to resolve.
$SupportedMin = [version]'3.11'
$SupportedMax = [version]'3.13'

$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'

# Ask the launcher what exists instead of probing for versions that may not be
# installed -- a failed probe prints "No suitable Python runtime found", which
# looks like a fatal error to anyone reading along.
$found = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($line in (& py --list)) {
        if ($line -match '-V:(\d+\.\d+)') { $found += [version]$Matches[1] }
    }
}
$usable = @($found | Where-Object { $_ -ge $SupportedMin -and $_ -le $SupportedMax } |
           Sort-Object -Descending)
if ($usable.Count -gt 0) {
    $PyCmd = 'py'
    $PyArgs = @("-$($usable[0])")
}

if (-not $PyCmd) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $ver = & $cmd.Source -c "import sys; print('%d.%d' % sys.version_info[:2])"
        if ($LASTEXITCODE -eq 0 -and $ver) {
            $pv = [version]$ver
            if ($pv -ge $SupportedMin -and $pv -le $SupportedMax) { $PyCmd = $cmd.Source }
        }
    }
}

$ErrorActionPreference = $prevEap

# Distinguish "no Python" from "only a Python too new to work", because the
# remedy is different and the second case is easy to hit: the py launcher
# defaults to the newest installed version.
if (-not $PyCmd -and $found.Count -gt 0) {
    Write-Host ''
    Write-Host 'No supported Python found.' -ForegroundColor Red
    Write-Host ("Installed: " + (($found | Sort-Object) -join ', '))
    Write-Host "Supported: $SupportedMin to $SupportedMax (torch 2.13.0 has no wheel beyond that)."
    Write-Host 'Install Python 3.12 from https://www.python.org/downloads/ and run this again.'
    Write-Host 'It will sit alongside your existing versions; nothing is replaced.'
    exit 1
}
if (-not $PyCmd) {
    Write-Host ''
    Write-Host 'Python 3.11 or newer was not found.' -ForegroundColor Red
    Write-Host 'Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),'
    Write-Host 'then run this script again. No other prerequisite is needed.'
    exit 1
}
Write-Ok ("using " + ("$PyCmd $($PyArgs -join ' ')").Trim())

# --- Virtual environment ----------------------------------------------------
Write-Step 'Creating the virtual environment'
if (Test-Path $VenvPy) {
    Write-Ok 'venv already exists, reusing it'
} else {
    & $PyCmd @PyArgs -m venv $Venv
    if (-not (Test-Path $VenvPy)) { throw "venv creation failed: $VenvPy missing" }
    Write-Ok 'created'
}

# --- Dependencies -----------------------------------------------------------
# Roughly 1.5 GB with torch, so this is the slow step on a fresh machine.
Write-Step 'Installing dependencies (this pulls ~1.5 GB and takes a while)'
Invoke-Native $VenvPy -m pip install --upgrade pip --quiet
Invoke-Native $VenvPy -m pip install -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
Write-Ok 'dependencies installed'

# --- spaCy sentence model ---------------------------------------------------
# Nothing declares en_core_web_sm as a dependency, so pip will not bring it in.
# Without it the splitter silently falls back to regex, which still works but
# splits sentences worse -- a failure you would not notice, so install it here.
Write-Step 'Installing the spaCy sentence model'
Invoke-Native $VenvPy -c "import en_core_web_sm"
if ($LASTEXITCODE -eq 0) {
    Write-Ok 'already present'
} else {
    Invoke-Native $VenvPy -m spacy download en_core_web_sm
    if ($LASTEXITCODE -ne 0) {
        Write-Warn 'could not install en_core_web_sm; sentence splitting will use the regex fallback'
    } else {
        Write-Ok 'installed'
    }
}

# --- Model ------------------------------------------------------------------
# Fetching it now rather than on first run keeps the first launch quick and
# makes a network problem surface here, where the message is readable.
Write-Step 'Downloading the Kokoro-82M model (~330 MB)'
# A real script file rather than python -c: PowerShell strips the inner quotes
# when it hands a multi-line string to a native command, which turns the repo id
# into a syntax error.
Invoke-Native $VenvPy (Join-Path $Root 'tools\fetch_model.py')
if ($LASTEXITCODE -ne 0) {
    Write-Warn 'model download failed. It will retry on first run.'
    Write-Warn 'On a corporate network this usually means huggingface.co is blocked.'
} else {
    Write-Ok 'model cached'
}

# --- Icon -------------------------------------------------------------------
Write-Step 'Generating the application icon'
Invoke-Native $VenvPy (Join-Path $Root 'tools\make_icon.py')
$IconPath = Join-Path $Root 'app.ico'
if (-not (Test-Path $IconPath)) {
    Write-Warn 'icon generation failed; the shortcut will use the Python icon'
    $IconPath = $VenvPyw
}

# --- Start menu shortcut ----------------------------------------------------
Write-Step 'Start menu entry'
$wantShortcut = $false
if (-not $NoShortcut) {
    $wantShortcut = Ask 'Add Lysdexic TTS to the Start menu?' $true
}
if ($wantShortcut) {
    $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $lnk = Join-Path $programs 'Lysdexic TTS.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = $VenvPyw
    $sc.Arguments = '"' + $Launcher + '"'
    $sc.WorkingDirectory = $Root
    $sc.Description = 'Text-to-speech for dyslexic readers'
    $sc.IconLocation = $IconPath
    $sc.WindowStyle = 7
    $sc.Save()
    Write-Ok "created: $lnk"
    Write-Ok 'press Start and type "lysdexic"'
} else {
    Write-Ok 'skipped'
}

# --- Autostart --------------------------------------------------------------
# Same per-user Run key the tray toggle uses, so the two agree and either one
# can turn it back off later.
Write-Step 'Start with Windows'
$wantAuto = $false
if (-not $NoAutostart) {
    $wantAuto = Ask 'Start Lysdexic TTS automatically when you sign in?' $true
}
if ($wantAuto) {
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    $value = '"' + $VenvPyw + '" "' + $Launcher + '"'
    Set-ItemProperty -Path $runKey -Name 'KokoroReader' -Value $value
    Write-Ok 'enabled (turn it off in the tray menu, or Settings > Apps > Startup)'
} else {
    Write-Ok 'skipped'
}

# --- Done -------------------------------------------------------------------
Write-Host ''
Write-Host 'Install complete.' -ForegroundColor Green
Write-Host ''
Write-Host '  Start it:  Start menu > Lysdexic TTS, or double-click LysdexicTTS.cmd'
Write-Host '  It runs in the system tray. Windows 11 hides new tray icons under the'
Write-Host '  ^ arrow next to the clock -- drag it out to pin it.'
Write-Host ''
Write-Host '  If it does not start, run LysdexicTTS-debug.cmd for a console window.'
Write-Host ''
