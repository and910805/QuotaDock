$ErrorActionPreference = 'Stop'

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $project 'release\QuotaDock.exe'
if (-not (Test-Path -LiteralPath $source)) {
    throw 'Run build_release.ps1 first.'
}

$installDir = Join-Path $env:LOCALAPPDATA 'Programs\QuotaDock'
$target = Join-Path $installDir 'QuotaDock.exe'
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
if (Test-Path -LiteralPath $target) {
    $resolvedTarget = [System.IO.Path]::GetFullPath($target)
    Get-CimInstance Win32_Process |
        Where-Object { $_.ExecutablePath -eq $resolvedTarget } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Milliseconds 500
}
Copy-Item -LiteralPath $source -Destination $target -Force

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutName = 'QuotaDock.lnk'
$shortcutPath = Join-Path $desktop $shortcutName
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $installDir
$shortcut.Description = 'View Codex and Claude Code subscription usage and reset times'
$shortcut.IconLocation = "$target,0"
$shortcut.Save()

$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
Set-ItemProperty -Path $runKey -Name 'QuotaDock' -Value ('"' + $target + '"')
Remove-ItemProperty -Path $runKey -Name 'CodexUsageWidget' -ErrorAction SilentlyContinue

Start-Process -FilePath $target
Write-Output $shortcutPath
