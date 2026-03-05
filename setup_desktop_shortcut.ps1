param(
    [string]$RepoRoot = "C:\Users\user\Documents\BAC\ict_trading_bot",
    [string]$ShortcutName = "ICT Trading Bot",
    [string]$LauncherPath = "",
    [string]$PythonwPath = "",
    [string]$IconPath = ""
)

if (-not (Test-Path $RepoRoot)) {
    throw "Repo root not found: $RepoRoot"
}

if ([string]::IsNullOrWhiteSpace($LauncherPath)) {
    $rootLauncher = Join-Path $RepoRoot "bot_launcher.pyw"
    $fallbackLauncher = Join-Path $RepoRoot "bot luncher\bot_launcher.pyw"
    if (Test-Path $rootLauncher) {
        $LauncherPath = $rootLauncher
    } elseif (Test-Path $fallbackLauncher) {
        $LauncherPath = $fallbackLauncher
    }
}

if (-not (Test-Path $LauncherPath)) {
    throw "Launcher not found. Checked: '$LauncherPath' and default locations under $RepoRoot"
}

if ([string]::IsNullOrWhiteSpace($PythonwPath)) {
    $pythonwCmd = Get-Command pythonw -ErrorAction SilentlyContinue
    if ($null -eq $pythonwCmd) {
        throw "pythonw.exe not found in PATH. Install Python or pass -PythonwPath explicitly."
    }
    $PythonwPath = $pythonwCmd.Source
}

if (-not (Test-Path $PythonwPath)) {
    throw "pythonw.exe not found: $PythonwPath"
}

if ([string]::IsNullOrWhiteSpace($IconPath)) {
    $rootIcon = Join-Path $RepoRoot "bot_icon.ico"
    $fallbackIcon = Join-Path $RepoRoot "bot icons\bot algo.ico"
    if (Test-Path $rootIcon) {
        $IconPath = $rootIcon
    } elseif (Test-Path $fallbackIcon) {
        Copy-Item -Force $fallbackIcon $rootIcon
        $IconPath = $rootIcon
    }
}

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop ($ShortcutName + ".lnk")

$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $PythonwPath
$shortcut.Arguments = "`"$LauncherPath`""
$shortcut.WorkingDirectory = $RepoRoot
$shortcut.WindowStyle = 1
$shortcut.Description = "Launch ICT Trading Bot"

if (-not [string]::IsNullOrWhiteSpace($IconPath) -and (Test-Path $IconPath)) {
    $shortcut.IconLocation = "$IconPath,0"
}

$shortcut.Save()

Write-Host "Shortcut created: $shortcutPath"
Write-Host "Target: $PythonwPath $($shortcut.Arguments)"
if (-not [string]::IsNullOrWhiteSpace($shortcut.IconLocation)) {
    Write-Host "Icon: $($shortcut.IconLocation)"
}
