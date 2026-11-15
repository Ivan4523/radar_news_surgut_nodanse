@echo off
rem Stop a running radar: it holds port 8900 (dashboard).
set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8900" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
    set FOUND=1
)
if "%FOUND%"=="1" (echo Radar stopped.) else (echo Radar is not running.)
pause