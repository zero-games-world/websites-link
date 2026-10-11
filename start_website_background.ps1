$ErrorActionPreference = "Stop"
$serverPath = Join-Path $PSScriptRoot "server.py"
$pythonPath = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$dataPath = Join-Path $PSScriptRoot "data"

if (-not (Test-Path -LiteralPath $serverPath)) {
    throw "Website server not found: $serverPath"
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -eq $python) {
        throw "Python 3.10+ is required to run this website."
    }
    $pythonPath = $python.Source
}

if (-not (Test-Path -LiteralPath $dataPath)) {
    New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
}

$env:AUTO_OPEN_BROWSER = "1"
$stdoutLog = Join-Path $dataPath "auto_start.log"
$stderrLog = Join-Path $dataPath "auto_start_error.log"
$arguments = '"' + $serverPath + '"'
Start-Process -FilePath $pythonPath `
    -ArgumentList $arguments `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog | Out-Null
