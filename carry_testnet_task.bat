@echo off
rem Unattended daily TESTNET rehearsal for CARRY-7d. Runs AFTER the paper task (07:05) so
rem today's paper state exists. Silent on a clean day; on anything else it leaves
rem .execution\ATTENTION and appends to carry_paper_incidents.md, then refuses to run
rem again until you delete the marker (see EXECUTION_RUNBOOK.md).
rem Structurally testnet-only: refuses to start if execution_ceilings declares live > 0.
cd /d "d:\Project\AI Engineer\Binance-Analyst"
"C:\Users\Minh Nhat\AppData\Local\Programs\Python\Python311\python.exe" -B run_carry_testnet_daily.py >> carry_testnet_task.log 2>&1
rem After the run, drop a plain-text status on the Desktop so nothing has to be typed to check on the bot.
"C:\Users\Minh Nhat\AppData\Local\Programs\Python\Python311\python.exe" -X utf8 status.py > "%USERPROFILE%\Desktop\BINANCE BOT - TINH TRANG.txt" 2>&1
"C:\Users\Minh Nhat\AppData\Local\Programs\Python\Python311\python.exe" -X utf8 notify_markers.py >> notify_markers_task.log 2>&1
