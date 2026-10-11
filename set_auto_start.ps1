param(
    [switch]$Disable
)

$startupFolder = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$shortcutPath = Join-Path $startupFolder "Dr Arvind Website.lnk"

if ($Disable) {
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath
        Write-Output "Website auto-start disabled."
    }
    else {
        Write-Output "Website auto-start was not enabled."
    }
    exit 0
}

$launcherPath = Join-Path $PSScriptRoot "start_website_background.ps1"
if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw "Background website launcher not found: $launcherPath"
}

if (-not (Test-Path -LiteralPath $startupFolder)) {
    New-Item -ItemType Directory -Path $startupFolder -Force | Out-Null
}

$powershellPath = Join-Path $PSHOME "powershell.exe"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powershellPath
$shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $launcherPath + '"'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = "Run the Dr. Arvind website quietly in the background after sign-in."
$shortcut.Save()
Write-Output "Website server will start quietly in the background when you sign in to Windows."
