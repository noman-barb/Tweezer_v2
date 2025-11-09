@echo off
REM Live g(r) Visualizer Launch Script
REM Activates conda environment and runs the visualizer

echo ========================================
echo Live g(r) Pair Correlation Visualizer
echo ========================================
echo.

REM Check if conda environment exists
call conda activate tweezer 2>nul
if errorlevel 1 (
    echo Warning: conda environment 'tweezer' not found
    echo Using default Python environment
    echo.
) else (
    echo Using conda environment: tweezer
    echo.
)

REM Run the visualizer with default settings
python live-gr.py %*

REM Keep window open if there was an error
if errorlevel 1 (
    echo.
    echo Press any key to exit...
    pause >nul
)
