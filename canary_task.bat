@echo off
rem Weekly signal-health + funding-regime canaries. Non-zero exit leaves .execution\canary_ALERT.
cd /d "d:\Project\AI Engineer\Binance-Analyst"
"C:\Users\Minh Nhat\AppData\Local\Programs\Python\Python311\python.exe" -B run_canaries.py >> canary_task.log 2>&1
if errorlevel 1 echo see canary_log.csv > .execution\canary_ALERT
