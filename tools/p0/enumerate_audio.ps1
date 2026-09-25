<#
.SYNOPSIS
    P0 / T1 — 列舉 Windows 音訊「捕獲」端點（含藍牙 HFP 裝置）。

.DESCRIPTION
    直接讀 registry 的 MMDevices\Audio\Capture，不依賴 CIM/PnP。
    原因：本機實測 Get-PnpDevice / Get-CimInstance Win32_SoundDevice 在一般權限下
    可能不回傳任何裝置，registry 則穩定可見。

    DeviceState 對照：
        1         = ACTIVE    （已啟用、可使用）
        268435457 = 0x10000001 (DISABLED / 停用)
        8         = NOTPRESENT
        4         = UNPLUGGED

.EXAMPLE
    pwsh -File tools/p0/enumerate_audio.ps1
    pwsh -File tools/p0/enumerate_audio.ps1 -Filter AI_VOICE
#>
[CmdletBinding()]
param(
    # 只顯示名稱或描述含此關鍵字的端點
    [string]$Filter
)

$ErrorActionPreference = 'Stop'

$stateMap = @{
    1         = 'ACTIVE'
    2         = 'DISABLED'
    4         = 'NOTPRESENT'
    8         = 'UNPLUGGED'
    268435457 = 'DISABLED(0x10000001)'
}

# Windows 音訊端點的屬性 GUID
$PROP_DEVICEDESC = '{a45c254e-df1c-4efd-8020-67d146a850e0},2'  # 端點名稱，例：Headset
$PROP_FRIENDLY  = '{b3f8fa53-0004-438e-9003-51a46e139bfc},6'  # 裝置描述，例：AI_VOICE_MAX Hands-Free

$base = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture'
if (-not (Test-Path $base)) {
    Write-Error "找不到音訊捕獲端點機碼：$base"
    exit 1
}

function Convert-PropValue {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [byte[]]) {
        # 多數音訊端點屬性是 UTF-16LE 字串
        $s = [System.Text.Encoding]::Unicode.GetString($Value) -replace "`0", ''
        if ($s.Trim()) { return $s.Trim() }
        return ($Value | ForEach-Object { $_.ToString('X2') }) -join ''
    }
    return [string]$Value
}

$rows = foreach ($key in Get-ChildItem $base) {
    $propsPath = Join-Path $key.PSPath 'Properties'
    $props = Get-ItemProperty -Path $propsPath -ErrorAction SilentlyContinue
    $state = (Get-ItemProperty -Path $key.PSPath -ErrorAction SilentlyContinue).DeviceState

    [pscustomobject]@{
        Name        = Convert-PropValue $props.$PROP_DEVICEDESC
        Description = Convert-PropValue $props.$PROP_FRIENDLY
        State       = $state
        StateText   = if ($stateMap.ContainsKey([int]$state)) { $stateMap[[int]$state] } else { "0x$('{0:X}' -f $state)" }
        EndpointKey = $key.PSChildName
    }
}

if ($Filter) {
    $rows = $rows | Where-Object {
        ($_.Name -like "*$Filter*") -or ($_.Description -like "*$Filter*")
    }
}

$rows | Sort-Object -Property @{Expression = { $_.State -eq 1 }; Descending = $true }, Name |
    Format-Table -AutoSize -Property Name, Description, StateText, State

if ($Filter) {
    Write-Host ("符合 '{0}' 的端點數：{1}" -f $Filter, @($rows).Count)
    if (@($rows).Count -eq 0) {
        Write-Host '→ 沒找到。請確認裝置已配對並開機（藍牙 HFP 端點只在連線時出現）。'
    }
} else {
    Write-Host '提示：ACTIVE 才是目前可用的端點。藍牙裝置關機時端點會消失。'
}
