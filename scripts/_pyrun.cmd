@echo off
REM Cross-platform Python launcher for AI log hooks (Windows cmd.exe).
REM Tries py -3 -> python -> python3 in order, runs the given script with all args.
REM Exits 0 silently if no Python is found - hooks must never block the AI tool.
REM
REM Each candidate is probed by actually running it, not by asking `where`
REM whether it exists. Windows puts an "App Execution Alias" on PATH at
REM AppData\Local\Microsoft\WindowsApps\python3.exe: a real executable that
REM prints an advert for the Microsoft Store and exits 49. `where` finds it and
REM is satisfied. The sibling _pyrun.sh had exactly that bug and silently wrote
REM no AI log for an entire session; `python3` being last here made it unlikely
REM rather than impossible, which is not the same as safe.

py -3 -c "import sys" >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 %*
  exit /b %ERRORLEVEL%
)

python -c "import sys" >nul 2>nul
if %ERRORLEVEL%==0 (
  python %*
  exit /b %ERRORLEVEL%
)

python3 -c "import sys" >nul 2>nul
if %ERRORLEVEL%==0 (
  python3 %*
  exit /b %ERRORLEVEL%
)

exit /b 0
