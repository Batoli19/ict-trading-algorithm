$FilesToWatch = @("pass13a_results.csv", "pass13a_report.txt")
$FolderToWatch = "C:\Users\user\Documents\BAC\ict_trading_bot"

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "👀 Watching for Pass 13A backtest completion..." -ForegroundColor Cyan
Write-Host "Monitoring: $FolderToWatch"
Write-Host "=========================================`n" -ForegroundColor Cyan

$Found = $false

while (-not $Found) {
    Start-Sleep -Seconds 5
    
    $AllExist = $true
    foreach ($file in $FilesToWatch) {
        $filePath = Join-Path $FolderToWatch $file
        if (-not (Test-Path $filePath)) {
            $AllExist = $false
            break
        }
    }
    
    if ($AllExist) {
        $Found = $true
        Write-Host "✅ BACKTEST COMPLETE! Both report and CSV files have been generated." -ForegroundColor Green
        
        # Play a sound
        [console]::beep(800, 300)
        Start-Sleep -Milliseconds 100
        [console]::beep(1000, 400)
        Start-Sleep -Milliseconds 100
        [console]::beep(1200, 500)
        
        # Pop a message box
        Add-Type -AssemblyName PresentationCore
        $msg = [System.Windows.MessageBox]::Show(
            "The 16-month Personality A backtest has finished! You can now check the results.", 
            "Backtest Complete", 
            [System.Windows.MessageBoxButton]::OK, 
            [System.Windows.MessageBoxImage]::Information
        )
    }
}
