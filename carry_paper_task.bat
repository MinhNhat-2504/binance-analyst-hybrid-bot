@echo off
rem Daily paper stage. Same chain on every OS: python run_daily.py paper
cd /d "%~dp0"
"C:\Users\Minh Nhat\AppData\Local\Programs\Python\Python311\python.exe" -X utf8 run_daily.py paper >> run_daily_task.log 2>&1
