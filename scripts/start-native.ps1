param(
    [switch]$ExperimentalNative,
    [switch]$Dev,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$LauncherArgs
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RunArgs = @($LauncherArgs)
if ($Dev) { $RunArgs += "--dev" }
if (-not $ExperimentalNative -and (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    $Distros = (& wsl.exe --list --quiet) -replace "`0", ""
    if ($Distros) {
        & wsl.exe --cd $ProjectRoot -- bash scripts/start-native.sh @RunArgs
        exit $LASTEXITCODE
    }
}
Write-Host "Using experimental direct Windows startup. WSL is recommended."
try {
    $HasPython = $false
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            & py -3.12 --version 2>$null
            $HasPython = $LASTEXITCODE -eq 0
        } catch { $HasPython = $false }
    }
    if (-not $HasPython) {
        & winget install --id Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw "Python installation failed" }
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    }
    & py -3.12 "$ProjectRoot/scripts/run-native.py" @RunArgs
    if ($LASTEXITCODE -ne 0) { throw "Native startup failed" }
} catch {
    Write-Error "$_`nInstall WSL using 'wsl --install', restart Windows if requested, then rerun this script."
    exit 1
}
