@echo off
rem CODOL control service stopper (M1).
rem Asks the running service to shut down gracefully and waits for the
rem port to be released. Keep this file ASCII-only (see Start.cmd notes).
setlocal
cd /d "%~dp0"
chcp 65001 >nul
"..\python\python.exe" -X utf8 -m codol_control.stopclient %*
endlocal
