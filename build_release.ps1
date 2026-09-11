$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$release = Join-Path $project 'release'

$pythonExe = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonExe = (Get-Command python -ErrorAction Stop).Source
}

& $pythonExe (Join-Path $project 'make_icon.py')
if ($LASTEXITCODE -ne 0) { throw '產生圖示失敗。' }
$previousQtPlatform = $env:QT_QPA_PLATFORM
try {
    $env:QT_QPA_PLATFORM = 'offscreen'
    & $pythonExe -m pytest $project -q
    if ($LASTEXITCODE -ne 0) { throw '測試失敗，停止打包。' }
} finally {
    $env:QT_QPA_PLATFORM = $previousQtPlatform
}

# 固定原生 DLL 搜尋路徑，避免 PATH 中其他工具的 ICU 等同名 DLL 混入。
$previousPath = $env:PATH
try {
    $pythonFolder = Split-Path -Parent $pythonExe
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$pythonFolder"
    & $pythonExe -m PyInstaller `
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
    if ($LASTEXITCODE -ne 0) { throw '打包失敗。' }
} finally {
    $env:PATH = $previousPath
}

Write-Output (Join-Path $release 'QuotaDock.exe')
