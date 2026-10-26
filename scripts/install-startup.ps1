# Make Kroger Reorder start, hidden, every time you sign in to Windows.
#
#   .\scripts\install-startup.ps1            # add the startup entry
#   .\scripts\install-startup.ps1 -Remove    # take it out again
#
# It drops a shortcut in your Startup folder that runs the venv's pythonw.exe (no console window)
# with `-m app --no-browser`. Output goes to %LOCALAPPDATA%\kroger-reorder\server.log.
# You can also toggle it under Settings > Apps > Startup, or Task Manager > Startup apps.
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'Kroger Reorder.lnk'

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; Write-Host "Removed $link" } else { Write-Host 'No startup entry found.' }
    return
}

if (-not (Test-Path $pythonw)) { throw "Missing $pythonw. Create the venv first (see README)." }
if (-not (Test-Path (Join-Path $root '.env'))) { throw "Missing .env in $root. Copy .env.example and fill it in first." }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $pythonw
$sc.Arguments = '-m app --no-browser'
$sc.WorkingDirectory = $root
$sc.WindowStyle = 7   # minimized; pythonw has no window anyway
$sc.Description = 'Kroger Reorder local server'
$sc.Save()
Write-Host "Installed $link"
Write-Host "It starts at your next sign-in. To start it now without signing out, double-click the shortcut."
