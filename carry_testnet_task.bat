@echo off
rem Daily testnet stage (07:20 local = 00:20 UTC). Same chain on every OS: python run_daily.py testnet
rem Each stage redirects to ITS OWN file. cmd opens a redirect target without write sharing,
rem so two stages sharing one log meant the second never started (lost run, 2026-10-11).
cd /d "%~dp0"
"C:\Users\Minh Nhat\AppData\Local\Programs\Python\Python311\python.exe" -X utf8 run_daily.py testnet >> run_daily_testnet.log 2>&1
