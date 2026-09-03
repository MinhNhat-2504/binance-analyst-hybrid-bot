@echo off
rem Bam dup file nay MOT LAN de tao 3 task tu dong (paper 07:05, testnet 07:20, canary CN 08:00).
rem Chay lai cung khong sao: /f ghi de task cu.
schtasks /create /f /tn CarryPaperDaily   /tr "wscript.exe \"%~dp0run_hidden.vbs\" \"d:\Project\AI Engineer\Binance-Analyst\carry_paper_task.bat\""   /sc daily /st 07:05
schtasks /create /f /tn CarryTestnetDaily /tr "wscript.exe \"%~dp0run_hidden.vbs\" \"d:\Project\AI Engineer\Binance-Analyst\carry_testnet_task.bat\"" /sc daily /st 07:20
schtasks /create /f /tn CarryCanaryWeekly /tr "wscript.exe \"%~dp0run_hidden.vbs\" \"d:\Project\AI Engineer\Binance-Analyst\canary_task.bat\"" /sc weekly /d SUN /st 08:00
echo.
rem Wake the machine for every task and run ASAP if the start was missed (schtasks /create resets these).
powershell -NoProfile -Command "$s=New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; foreach($t in 'CarryPaperDaily','CarryTestnetDaily','CarryCanaryWeekly'){Set-ScheduledTask -TaskName $t -Settings $s | Out-Null}; foreach($t in 'CarryPaperDaily','CarryTestnetDaily','CarryCanaryWeekly'){$x=(Get-ScheduledTask $t).Settings; Write-Host $t WakeToRun=$($x.WakeToRun) RunIfMissed=$($x.StartWhenAvailable)}"

echo Kiem tra:
schtasks /query /tn CarryPaperDaily /fo list | findstr /i "TaskName Status Next"
schtasks /query /tn CarryTestnetDaily /fo list | findstr /i "TaskName Status Next"
schtasks /query /tn CarryCanaryWeekly /fo list | findstr /i "TaskName Status Next"
echo.
pause
