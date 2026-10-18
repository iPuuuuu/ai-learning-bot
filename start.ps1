$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw "未找到虚拟环境。先在项目目录运行：python -m venv .venv；.\.venv\Scripts\pip install -r requirements.txt"
}
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir 'logs') | Out-Null
Set-Location -LiteralPath $ProjectDir
& $Python bot.py *>> (Join-Path $ProjectDir 'logs\bot.log')

