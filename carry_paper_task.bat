@echo off
rem Daily paper-trading run for CARRY-7d. Safe to run any time; missed days are
rem reconstructed automatically (catch-up), so double-runs and gaps are harmless.
cd /d "d:\Project\AI Engineer\Binance-Analyst"
"C:\Users\Minh Nhat\AppData\Local\Programs\Python\Python311\python.exe" run_carry_paper.py >> carry_paper_task.log 2>&1
