@echo off
cd /d C:\Users\DS-ML-Account\Desktop\Camera

REM Initialize conda for batch
call "C:\ProgramData\anaconda3\Scripts\activate.bat" tweezer

:retry
python ImageWatcher.py R:\Temp --server-host 192.168.5.1 --server-port 50052
if %errorlevel% neq 0 (
    echo Script failed, retrying in 5 seconds...
    timeout /t 5 >nul
    goto retry
)
