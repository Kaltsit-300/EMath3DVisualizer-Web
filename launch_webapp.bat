@echo off
chcp 65001 >nul
setlocal
set "PY=D:\Python 3.14\python.exe"
set "DIR=%~dp0"
cd /d "%DIR%"
"%PY%" api_server.py
endlocal
