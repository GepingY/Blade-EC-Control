[CmdletBinding()]
param(
    [ValidateSet('Quiet', 'Performance', 'Status')][string]$Action = 'Status',
    [Parameter(DontShow)][switch]$ElevatedRetry
)

# Persistent, manually selected presets. No startup items, services or tasks.
# Quiet limits CPU boost and lets the firmware control both fans automatically.
# Performance restores the original High Performance plan and Balanced/Auto.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$defaultOriginalGuid = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'
$processorGroup = '54533251-82be-4824-96c1-47b60b740d00'
$minimumSetting = '893dee8e-2bef-41e0-89c6-b55d0929964c'
$maximumSetting = 'bc5038f7-23e0-4960-96da-33abaf5935ec'
$boostSetting = 'be337238-0d82-4146-a960-4f3749d470c7'
$repoRoot = Split-Path -Path $PSScriptRoot -Parent
$presetRoot = Join-Path $repoRoot 'diagnostics\presets'
$configPath = Join-Path $presetRoot 'state.json'
$powercfg = Join-Path $env:SystemRoot 'System32\powercfg.exe'
$nativeHelper = Join-Path $PSScriptRoot 'blade_profile.py'
$python = (Get-Command python.exe -ErrorAction Stop).Source
New-Item -ItemType Directory -Path $presetRoot -Force | Out-Null
$stamp = [DateTime]::Now.ToString('yyyyMMdd-HHmmss-fff')
$script:LogPath = Join-Path $presetRoot ("preset-$Action-$stamp.jsonl")

function Log {
    param([string]$Event, [hashtable]$Data = @{})
    $entry = [ordered]@{ time = [DateTimeOffset]::Now.ToString('o'); event = $Event }
    foreach ($key in $Data.Keys) { $entry[$key] = $Data[$key] }
    $entry | ConvertTo-Json -Compress -Depth 10 | Add-Content -LiteralPath $script:LogPath -Encoding UTF8
}

function Power {
    param([string[]]$Arguments)
    $output = @(& $powercfg @Arguments 2>&1 | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    $code = $LASTEXITCODE
    Log 'powercfg' @{ arguments = $Arguments; exit_code = $code; output = $output }
    if ($code -ne 0) {
        $failure = [Exception]::new("powercfg failed ($code): $output")
        $failure.Data['NeedsAdmin'] = ($code -in @(5, 1314) -or $output -match 'Access is denied|privilege|拒绝访问|权限|特权|0x522')
        throw $failure
    }
    return $output
}

function ActiveGuid {
    $text = Power @('/getactivescheme')
    $match = [regex]::Match($text, '(?i)[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}')
    if (-not $match.Success) { throw 'Active power-plan GUID could not be read' }
    return $match.Value.ToLowerInvariant()
}

function Setting {
    param([string]$Guid, [string]$Id)
    $text = Power @('/qh', $Guid, $processorGroup, $Id)
    $values = @([regex]::Matches($text, '(?i)0x([0-9a-f]{8})'))
    if ($values.Count -lt 2) { throw "AC/DC settings unavailable: $Id" }
    return [pscustomobject]@{ AC = [Convert]::ToUInt32($values[-2].Groups[1].Value, 16); DC = [Convert]::ToUInt32($values[-1].Groups[1].Value, 16) }
}

function NativeProfile {
    param([string]$NativeAction)
    $resultPath = Join-Path $presetRoot ("native-result-$NativeAction-$stamp.json")
    $arguments = @($nativeHelper, '--action', $NativeAction, '--output', $resultPath)
    if ($NativeAction -ne 'status') { $arguments += '--apply' }
    & $python @arguments | Out-Host
    $code = $LASTEXITCODE
    Log 'native_profile' @{ action = $NativeAction; exit_code = $code; result_path = $resultPath }
    if ($code -ne 0) { throw "Native $NativeAction profile did not verify; see $resultPath" }
    return Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
}

function ReadConfig {
    if (-not (Test-Path -LiteralPath $configPath)) { return $null }
    $state = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $parsed = [guid]::Empty
    $origParsed = [guid]::Empty
    if ($state.Version -ne 1 -or $state.CreatedBy -ne 'BladeQuietPreset' -or
        -not [guid]::TryParse([string]$state.OriginalGuid, [ref]$origParsed) -or
        -not [guid]::TryParse([string]$state.QuietGuid, [ref]$parsed) -or $parsed -eq $origParsed) { throw 'Preset state does not identify an owned quiet plan' }
    $expectedBackup = [IO.Path]::GetFullPath((Join-Path $presetRoot 'original-high-performance.pow'))
    if ([IO.Path]::GetFullPath([string]$state.BackupPath) -ne $expectedBackup -or -not (Test-Path -LiteralPath $expectedBackup)) { throw 'Preset backup is unavailable' }
    if ((Get-FileHash -LiteralPath $expectedBackup -Algorithm SHA256).Hash -ne $state.BackupSha256) { throw 'Preset backup hash changed' }
    return $state
}

function GetOriginalGuid {
    $state = ReadConfig
    if ($null -ne $state) { return [string]$state.OriginalGuid }
    $active = ActiveGuid
    if ($active) { return $active }
    return $defaultOriginalGuid
}

function PrepareQuietPlan {
    $state = ReadConfig
    $originalGuid = if ($null -ne $state) { [string]$state.OriginalGuid } else { GetOriginalGuid }
    $originalMin = Setting $originalGuid $minimumSetting
    $originalMax = Setting $originalGuid $maximumSetting
    $originalBoost = Setting $originalGuid $boostSetting
    if ($originalMax.AC -ne 100) { throw 'Original AC maximum changed from 100%; refusing to overwrite that preference' }
    if ($null -eq $state) {
        $backupPath = Join-Path $presetRoot 'original-high-performance.pow'
        if (Test-Path -LiteralPath $backupPath) { throw 'An untracked original-plan backup exists; refusing to overwrite it' }
        Power @('/export', $backupPath, $originalGuid) | Out-Null
        if (-not (Test-Path -LiteralPath $backupPath)) { throw 'Original-plan export failed' }
        $state = [pscustomobject]@{
            Version = 1; CreatedBy = 'BladeQuietPreset'; OriginalGuid = $originalGuid
            QuietGuid = [guid]::NewGuid().ToString(); QuietName = 'Blade Quiet (boost disabled)'
            BackupPath = $backupPath; BackupSha256 = (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash
            CreatedAt = [DateTimeOffset]::Now.ToString('o')
        }
        $state | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $configPath -Encoding UTF8
        [ordered]@{ Minimum = $originalMin; Maximum = $originalMax; Boost = $originalBoost } | ConvertTo-Json -Depth 6 |
            Set-Content -LiteralPath (Join-Path $presetRoot 'original-settings.json') -Encoding UTF8
    }
    $plans = Power @('/list')
    if ($plans -notmatch [regex]::Escape($state.QuietGuid)) {
        Power @('/duplicatescheme', $originalGuid, $state.QuietGuid) | Out-Null
        Power @('/changename', $state.QuietGuid, $state.QuietName) | Out-Null
    }
    Power @('/setacvalueindex', $state.QuietGuid, $processorGroup, $minimumSetting, '5') | Out-Null
    Power @('/setacvalueindex', $state.QuietGuid, $processorGroup, $boostSetting, '0') | Out-Null
    $quietMin = Setting $state.QuietGuid $minimumSetting
    $quietMax = Setting $state.QuietGuid $maximumSetting
    $quietBoost = Setting $state.QuietGuid $boostSetting
    if ($quietMin.AC -ne 5 -or $quietMax.AC -ne 100 -or $quietBoost.AC -ne 0 -or
        $quietMin.DC -ne $originalMin.DC -or $quietMax.DC -ne $originalMax.DC -or $quietBoost.DC -ne $originalBoost.DC) { throw 'Quiet plan values or unchanged battery settings did not verify' }
    return $state
}

$needsAdmin = $false
try {
    Log 'action_started' @{ action = $Action }
    if ($Action -eq 'Status') {
        $active = ActiveGuid
        $state = ReadConfig
        $native = NativeProfile 'status'
        [ordered]@{ ActivePowerGuid = $active; PresetState = $state; Controller = $native } | ConvertTo-Json -Depth 10
    } elseif ($Action -eq 'Quiet') {
        $state = PrepareQuietPlan
        $native = NativeProfile 'quiet'
        Power @('/setactive', $state.QuietGuid) | Out-Null
        if ((ActiveGuid) -ne $state.QuietGuid) { throw 'Quiet plan activation did not verify' }
        Write-Output 'Quiet enabled: Silent/Auto, AC minimum 5%, boost disabled, maximum 100%. Fans remain controlled by firmware and may restart with heat.'
    } else {
        $restoreGuid = GetOriginalGuid
        Power @('/setactive', $restoreGuid) | Out-Null
        $native = NativeProfile 'performance'
        if ((ActiveGuid) -ne $restoreGuid) { throw 'Original power plan activation did not verify' }
        Write-Output 'Performance restored: original Windows power plan and Balanced/Auto fan control.'
    }
    Log 'action_verified' @{ action = $Action }
} catch {
    $failure = $_.Exception
    if ($failure.Data.Contains('NeedsAdmin')) { $needsAdmin = [bool]$failure.Data['NeedsAdmin'] }
    Log 'action_failed' @{ action = $Action; error = $failure.ToString() }
    if ($Action -eq 'Quiet') {
        # Every changing failure returns both independent controls to the known baseline.
        try { Power @('/setactive', (GetOriginalGuid)) | Out-Null } catch { Log 'power_recovery_failed' @{ error = $_.Exception.Message } }
        try { NativeProfile 'performance' | Out-Null } catch { Log 'controller_recovery_failed' @{ error = $_.Exception.Message } }
    }
    $isAdmin = ([Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($Action -ne 'Status' -and $needsAdmin -and -not $isAdmin -and -not $ElevatedRetry) {
        $shellPath = Join-Path $PSHOME 'pwsh.exe'
        if (-not (Test-Path -LiteralPath $shellPath)) { $shellPath = Join-Path $PSHOME 'powershell.exe' }
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'), '-Action', $Action, '-ElevatedRetry')
        $helper = Start-Process -FilePath $shellPath -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        exit $helper.ExitCode
    }
    Write-Error $failure.Message
    exit 2
}
exit 0
