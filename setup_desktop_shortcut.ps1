param(
    [string]$RepoRoot = "C:\Users\user\Documents\BAC\ict_trading_bot",
    [string]$BatchPath = "C:\Users\user\Documents\BAC\ict_trading_bot\start_bot.bat",
    [string]$ShortcutName = "ICT Trading Bot",
    [string]$PngIconPath = "C:\Users\user\Documents\BAC\ict_trading_bot\bot icons\bot algo.png",
    [string]$IcoIconPath = "C:\Users\user\Documents\BAC\ict_trading_bot\bot icons\bot algo.ico"
)

function Convert-PngToIco {
    param(
        [Parameter(Mandatory=$true)][string]$PngPath,
        [Parameter(Mandatory=$true)][string]$IcoPath
    )

    if (-not (Test-Path $PngPath)) {
        throw "PNG icon not found: $PngPath"
    }

    $pngBytes = [System.IO.File]::ReadAllBytes($PngPath)
    $icoDir = Split-Path -Parent $IcoPath
    if (-not (Test-Path $icoDir)) {
        New-Item -Path $icoDir -ItemType Directory -Force | Out-Null
    }

    $fs = [System.IO.File]::Open($IcoPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
    try {
        $bw = New-Object System.IO.BinaryWriter($fs)
        try {
            $bw.Write([UInt16]0)
            $bw.Write([UInt16]1)
            $bw.Write([UInt16]1)

            $bw.Write([Byte]0)
            $bw.Write([Byte]0)
            $bw.Write([Byte]0)
            $bw.Write([Byte]0)
            $bw.Write([UInt16]1)
            $bw.Write([UInt16]32)
            $bw.Write([UInt32]$pngBytes.Length)
            $bw.Write([UInt32]22)

            $bw.Write($pngBytes)
        }
        finally {
            $bw.Dispose()
        }
    }
    finally {
        $fs.Dispose()
    }
}

if (-not (Test-Path $BatchPath)) {
    throw "Batch launcher not found: $BatchPath"
}

$iconForShortcut = $PngIconPath
$iconExtension = [System.IO.Path]::GetExtension($iconForShortcut).ToLowerInvariant()

if ($iconExtension -eq ".png") {
    Convert-PngToIco -PngPath $PngIconPath -IcoPath $IcoIconPath
    $iconForShortcut = $IcoIconPath
}

if (-not (Test-Path $iconForShortcut)) {
    throw "Icon file not found: $iconForShortcut"
}

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop ($ShortcutName + ".lnk")

$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $BatchPath
$shortcut.WorkingDirectory = $RepoRoot
$shortcut.IconLocation = "$iconForShortcut,0"
$shortcut.WindowStyle = 1
$shortcut.Description = "Launch ICT Trading Bot"
$shortcut.Save()

Write-Host "Shortcut created: $shortcutPath"
Write-Host "Icon used: $iconForShortcut"
