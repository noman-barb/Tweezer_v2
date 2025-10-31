cd C:\Users\Admin\Desktop\slm-driver-server

@echo off
REM Batch script to start the SLM Driver Service

echo Starting SLM Driver Service...
echo.

:retry
REM Activate conda environment and run the service
call conda activate tweezer
if errorlevel 1 (
    echo ERROR: Failed to activate conda environment 'tweezer'
    echo.
    goto ask_retry
)

python slm_service.py
if errorlevel 1 (
    echo.
    echo ERROR: slm_service.py failed with error code %errorlevel%
    echo.
    goto ask_retry
)

echo.
echo Service exited normally.
pause
exit /b 0

:ask_retry
echo Press any key to retry, or close this window to exit...
pause >nul
echo.
echo Retrying...
echo.
goto retry
