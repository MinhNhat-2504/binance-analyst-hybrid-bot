@echo off
rem Bam dup file nay MOT LAN de tao 2 task tu dong (paper 07:05, testnet 07:20).
rem Chay lai cung khong sao: /f ghi de task cu.
schtasks /create /f /tn CarryPaperDaily   /tr "\"d:\Project\AI Engineer\Binance-Analyst\carry_paper_task.bat\""   /sc daily /st 07:05
schtasks /create /f /tn CarryTestnetDaily /tr "\"d:\Project\AI Engineer\Binance-Analyst\carry_testnet_task.bat\"" /sc daily /st 07:20
echo.
echo Kiem tra:
schtasks /query /tn CarryPaperDaily /fo list | findstr /i "TaskName Status Next"
schtasks /query /tn CarryTestnetDaily /fo list | findstr /i "TaskName Status Next"
echo.
pause
