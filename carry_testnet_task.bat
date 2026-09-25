@echo off
rem Daily testnet stage (07:20 local = 00:20 UTC). Same chain on every OS: python run_daily.py testnet
cd /d "%~dp0"
"C:\Users\Minh Nhat\AppData\Local\Programs\Python\Python311\python.exe" -X utf8 run_daily.py testnet >> run_daily_task.log 2>&1
