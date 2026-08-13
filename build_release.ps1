$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$release = Join-Path $project 'release'

python (Join-Path $project 'make_icon.py')
python -m pytest (Join-Path $project 'test_app.py') -q

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name 'QuotaDock' `
    --icon (Join-Path $project 'codex_usage.ico') `
    --distpath $release `
    --workpath (Join-Path $project 'build') `
    --specpath $project `
    (Join-Path $project 'app.py')

Write-Output (Join-Path $release 'QuotaDock.exe')
