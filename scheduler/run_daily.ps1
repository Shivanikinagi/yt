# Local (Windows) fallback runner for Task Scheduler. Only runs while the PC is on.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
$LogDir = Join-Path $Root "output\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
& $Py -m shorts_factory run 2>&1 | Tee-Object -FilePath (Join-Path $LogDir "task_$Stamp.log")
exit $LASTEXITCODE
