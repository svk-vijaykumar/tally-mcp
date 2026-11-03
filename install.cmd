@echo off
rem Sets up tally-mcp on this PC. Double-click it; it can be run again to update.
rem Uses install.ps1 next to it, or downloads the latest one when this file is on its own.
setlocal
set "PS1=%~dp0install.ps1"
if not exist "%PS1%" set "PS1=%TEMP%\tally-mcp-install.ps1"
if not exist "%~dp0install.ps1" powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = 'Tls12'; Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/svk-vijaykumar/tally-mcp/main/install.ps1' -OutFile $env:TEMP\tally-mcp-install.ps1" || goto failed
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
if errorlevel 1 goto failed
echo.
pause
exit /b 0

:failed
echo.
echo Setup did not finish. Read the message above, then run this file again.
pause
exit /b 1
