# Build the launcher exe (onefile, windowed) from the restored app sources.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
# Requires Python 3.14 + pyinstaller:  python -m pip install pyinstaller
param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$app = Join-Path $root 'src'
$ico = Join-Path $root 'assets\Deepseek.ico'

$modules = @('paths', 'fix_links', 'core_runner', 'core_versions', 'core_launch',
             'core_plugins', 'core_backup', 'ui_main', 'ui_dialogs')
$hidden = $modules | ForEach-Object { "--hidden-import=$_" }

Push-Location $app
$args = @('--noconfirm', '--clean', '--onefile', '--windowed',
          '--name', 'DeepSeek Harness Launcher',
          "--icon=$ico") + $hidden + @('main.py')
& $Python -m PyInstaller @args
$rc = $LASTEXITCODE
Pop-Location
Write-Output "build exit: $rc"
exit $rc
