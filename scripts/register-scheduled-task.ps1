<#
.SYNOPSIS
Registers a daily Windows Task Scheduler task that runs `tkn-genai-chat-archiver capture`.

.EXAMPLE
.\scripts\register-scheduled-task.ps1 -WhatIf

.EXAMPLE
.\scripts\register-scheduled-task.ps1 -ConfigPath "C:\path\to\config.yaml" -At '03:00'
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    # Installed executable. Defaults to `tkn-genai-chat-archiver` on PATH (after `uv tool install .`).
    [string]$ExecutablePath,

    # Explicit configuration file. Defaults to the user configuration.
    [string]$ConfigPath,

    [string]$TaskName = 'Tkn-GenAIChatArchiver-Daily',

    [string]$At = '03:00'
)

$ErrorActionPreference = 'Stop'

if ($ExecutablePath) {
    $executable = (Resolve-Path -LiteralPath $ExecutablePath).Path
}
else {
    $executable = (Get-Command tkn-genai-chat-archiver -ErrorAction Stop).Source
}

$arguments = 'capture --quiet'
if ($ConfigPath) {
    $config = (Resolve-Path -LiteralPath $ConfigPath).Path
    $arguments += " --config `"$config`""
}

# Run from the application folder so that no unrelated `.tkn\config.yaml` is picked up.
$workingDirectory = Join-Path $env:USERPROFILE '.tkn\genai_chat_archiver'

$action = New-ScheduledTaskAction -Execute $executable -Argument $arguments -WorkingDirectory $workingDirectory
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
# Interactive: runs only while the user is logged on, which is needed to read \\wsl.localhost sources.
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

$description = 'Archive local generative AI chat logs as byte-exact, versioned snapshots (no inference).'
if ($PSCmdlet.ShouldProcess($TaskName, "Register daily task at $At`: `"$executable`" $arguments")) {
    if (-not (Test-Path -LiteralPath $workingDirectory)) {
        New-Item -ItemType Directory -Path $workingDirectory | Out-Null
    }
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $description `
        -Force | Out-Null
    Get-ScheduledTask -TaskName $TaskName
}
