' Runs a .bat with no console window. Task Scheduler launches this instead of the .bat
' directly, so a run can never be killed by someone closing the window that pops up
' (CTRL_CLOSE -> Python KeyboardInterrupt, which is what stopped the 31/08 and 01/09 runs).
' Usage: wscript.exe run_hidden.vbs "full\path\to\task.bat"
Set sh = CreateObject("WScript.Shell")
sh.Run """" & WScript.Arguments(0) & """", 0, False
