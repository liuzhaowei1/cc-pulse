# ============================================================
# CC Pulse Viewer - PyInstaller 打包 (PowerShell 版)
# 产出: dist\CCPulse.exe  (--onefile --windowed, 目标 <=30MB)
# 用法（在项目根目录）:  powershell -ExecutionPolicy Bypass -File build\build.ps1
# ============================================================
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "[1/3] 检查 PyInstaller..."
try { python -m PyInstaller --version | Out-Null }
catch {
    Write-Host "    未安装，pip install pyinstaller ..."
    python -m pip install --upgrade pyinstaller
}

Write-Host "[2/3] 打包中（onefile + windowed + 内嵌 assets）..."
python -m PyInstaller `
    --onefile `
    --windowed `
    --name CCPulse `
    --add-data "assets;assets" `
    --icon "assets/app.ico" `
    --noconfirm `
    --clean `
    cc_pulse_viewer.py

Write-Host "[3/3] 完成。产物:"
$exe = "dist\CCPulse.exe"
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 2)
    Write-Host "    $((Resolve-Path $exe).Path)  =  $mb MB"
    if ($mb -le 30) { Write-Host "    ✅ 满足 <=30MB 硬约束" }
    else { Write-Host "    ⚠️ 超过 30MB，考虑 UPX 压缩或排除多余依赖" }
}
Write-Host "双击 dist\CCPulse.exe 直接运行。"
