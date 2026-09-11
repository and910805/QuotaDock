$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $project 'release\QuotaDock.exe'
if (-not (Test-Path -LiteralPath $source)) {
    throw '請先執行 build_release.ps1 產生執行檔。'
}
# 由執行檔共用安裝流程，保留使用者的自動啟動偏好。
$installer = Start-Process -FilePath $source -ArgumentList '--install' -WindowStyle Hidden -Wait -PassThru
if ($installer.ExitCode -ne 0) { throw '安裝失敗，請查看執行檔顯示的錯誤。' }
Write-Output '安裝程序已結束；一般使用也可直接執行免安裝檔。'
