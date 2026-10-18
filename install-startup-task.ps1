$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $ProjectDir 'start.ps1'
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`"" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'AI-Learning-Bot' -Action $Action -Trigger $Trigger -Settings $Settings -Description '飞书 AI 个性化学习机器人' -Force
Start-ScheduledTask -TaskName 'AI-Learning-Bot'
Write-Host 'AI-Learning-Bot 已安装为登录后自动启动任务。'

