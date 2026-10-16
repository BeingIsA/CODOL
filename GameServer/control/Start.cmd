@echo off
rem CODOL control service launcher (M1).
rem Runs in foreground; close this window or run Stop.cmd to stop.
rem Closing the browser admin page does NOT stop the service.
rem Notes: keep this file ASCII-only; cmd parses it in the ANSI codepage
rem before `chcp 65001` takes effect, and UTF-8 Chinese text here breaks
rem line parsing on GBK consoles (observed 2026-10-15 with Stop.cmd).
setlocal
cd /d "%~dp0"
chcp 65001 >nul
"..\python\python.exe" -X utf8 -m codol_control %*
endlocal
