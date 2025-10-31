#!/bin/bash

# Dual Monitor GUI Launcher Script for Linux
# Launches Services Manager GUI on smaller monitor and Dashboard GUI on larger monitor
# 
# Prerequisites:
# - Python environment with required packages (dearpygui, pyyaml, psutil, etc.)
# - Two monitors connected to the system
# - wmctrl package installed for window positioning
# 
# Usage: ./launch_dual_monitor.sh

set -e  # Exit on any error

# Configuration variables
CONDA_ENV_NAME="${CONDA_ENV_NAME:-tweezer}"  # Conda environment name
USE_CONDA="${USE_CONDA:-true}"  # Set to false to skip conda activation

# Script directory (where this script is located)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Python executable (will be set after conda activation)
PYTHON_EXE="${PYTHON_EXE:-python3}"

# GUI script paths
SERVICES_MANAGER_SCRIPT="$SCRIPT_DIR/services_manager_gui.py"
DASHBOARD_GUI_SCRIPT="$SCRIPT_DIR/dashboard_gui.py"

# Log directory
LOG_DIR="$SCRIPT_DIR/../logs/gui_logs"
mkdir -p "$LOG_DIR"

# Get current timestamp for log files
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

echo "=== Tweezer Dual Monitor GUI Launcher ==="

# Activate conda environment if enabled
if [[ "$USE_CONDA" == "true" ]]; then
    echo "Activating conda environment: $CONDA_ENV_NAME"
    
    # Initialize conda for bash (required for conda activate to work in scripts)
    if [[ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]]; then
        source "$HOME/miniconda3/etc/profile.d/conda.sh"
    elif [[ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]]; then
        source "$HOME/anaconda3/etc/profile.d/conda.sh"
    elif [[ -f "/opt/conda/etc/profile.d/conda.sh" ]]; then
        source "/opt/conda/etc/profile.d/conda.sh"
    else
        echo "Warning: Could not find conda.sh. Trying to activate conda environment anyway..."
    fi
    
    # Activate the conda environment
    if conda activate "$CONDA_ENV_NAME" 2>/dev/null; then
        echo "Successfully activated conda environment: $CONDA_ENV_NAME"
        PYTHON_EXE="python"  # Use conda's python
    else
        echo "Warning: Failed to activate conda environment '$CONDA_ENV_NAME'"
        echo "Falling back to system Python: $PYTHON_EXE"
    fi
else
    echo "Conda activation disabled, using: $PYTHON_EXE"
fi

echo "Starting Services Manager and Dashboard GUIs..."
echo "Logs will be written to: $LOG_DIR"
echo

# Check if GUI scripts exist
if [[ ! -f "$SERVICES_MANAGER_SCRIPT" ]]; then
    echo "Error: Services Manager script not found at: $SERVICES_MANAGER_SCRIPT"
    exit 1
fi

if [[ ! -f "$DASHBOARD_GUI_SCRIPT" ]]; then
    echo "Error: Dashboard GUI script not found at: $DASHBOARD_GUI_SCRIPT"
    exit 1
fi

# Check if wmctrl is available for window positioning
if ! command -v wmctrl >/dev/null 2>&1; then
    echo "Warning: wmctrl not found. Window positioning will not be available."
    echo "Install wmctrl for automatic window positioning: sudo apt-get install wmctrl"
fi

# Check if xrandr is available for monitor detection
if ! command -v xrandr >/dev/null 2>&1; then
    echo "Warning: xrandr not found. Monitor detection will not be available."
    echo "Install xrandr (usually part of x11-xserver-utils): sudo apt-get install x11-xserver-utils"
fi

# Function to get monitor information
get_monitor_info() {
    if ! command -v xrandr >/dev/null 2>&1; then
        echo "Warning: xrandr not found. Cannot detect monitor configuration."
        return 1
    fi
    
    # Get connected monitors with their positions and sizes
    # Output format (per line): name|x|y|w|h|is_primary
    while read -r line; do
        name=$(echo "$line" | awk '{print $1}')
        is_primary=0
        echo "$line" | grep -q " primary " && is_primary=1
        geom=$(echo "$line" | sed -n 's/.* \([0-9][0-9]*x[0-9][0-9]*+[0-9][0-9]*+[0-9][0-9]*\).*/\1/p' | head -1)
        if [[ -n "$geom" ]]; then
            w=${geom%%x*}
            rest=${geom#*x}
            h=${rest%%+*}
            rest=${rest#*+}
            x=${rest%%+*}
            y=${rest#*+}
            echo "$name|$x|$y|$w|$h|$is_primary"
        fi
    done < <(xrandr --query | grep " connected")
}

# Choose monitors for dashboard/services based on size and primary flag
# Outputs globals: DASH_X,DASH_Y,DASH_W,DASH_H and SERV_X,SERV_Y,SERV_W,SERV_H
choose_target_monitors() {
    local lines=()
    while IFS= read -r l; do lines+=("$l"); done < <(get_monitor_info)
    if [[ ${#lines[@]} -lt 2 ]]; then
        echo "Warning: Need at least 2 monitors, found ${#lines[@]}"
        return 1
    fi

    # Build arrays of monitors with area
    local names=() xs=() ys=() ws=() hs=() prims=() areas=()
    for entry in "${lines[@]}"; do
        IFS='|' read -r n x y w h p <<< "$entry"
        names+=("$n"); xs+=("$x"); ys+=("$y"); ws+=("$w"); hs+=("$h"); prims+=("$p")
        areas+=("$((w*h))")
    done

    # Prefer primary for dashboard; if multiple primaries, choose largest
    local dash_idx=-1 max_area=-1
    for i in "${!names[@]}"; do
        if [[ "${prims[$i]}" -eq 1 ]]; then
            if [[ ${areas[$i]} -gt $max_area ]]; then
                max_area=${areas[$i]}; dash_idx=$i
            fi
        fi
    done
    # If no primary flagged, choose largest as dashboard
    if [[ $dash_idx -lt 0 ]]; then
        for i in "${!names[@]}"; do
            if [[ ${areas[$i]} -gt $max_area ]]; then
                max_area=${areas[$i]}; dash_idx=$i
            fi
        done
    fi

    # Choose services monitor as the smallest non-dash monitor
    local serv_idx=-1 min_area=9223372036854775807
    for i in "${!names[@]}"; do
        if [[ $i -ne $dash_idx ]]; then
            if [[ ${areas[$i]} -lt $min_area ]]; then
                min_area=${areas[$i]}; serv_idx=$i
            fi
        fi
    done

    # Export globals
    DASH_X=${xs[$dash_idx]}; DASH_Y=${ys[$dash_idx]}; DASH_W=${ws[$dash_idx]}; DASH_H=${hs[$dash_idx]}
    SERV_X=${xs[$serv_idx]}; SERV_Y=${ys[$serv_idx]}; SERV_W=${ws[$serv_idx]}; SERV_H=${hs[$serv_idx]}

    echo "Dashboard target monitor: ${names[$dash_idx]} ${DASH_W}x${DASH_H}+${DASH_X}+${DASH_Y} (primary=${prims[$dash_idx]})"
    echo "Services target monitor:  ${names[$serv_idx]} ${SERV_W}x${SERV_H}+${SERV_X}+${SERV_Y} (primary=${prims[$serv_idx]})"
}

# Function to launch Services Manager GUI
launch_services_manager() {
    echo "Launching Services Manager GUI..."
    
    # Launch with output redirection
    $PYTHON_EXE "$SERVICES_MANAGER_SCRIPT" \
        > "$LOG_DIR/services_manager_${TIMESTAMP}.log" 2>&1 &
    
    SERVICES_PID=$!
    echo "Services Manager PID: $SERVICES_PID"
}

# Function to launch Dashboard GUI
launch_dashboard() {
    echo "Launching Dashboard GUI..."
    
    # Launch with output redirection
    $PYTHON_EXE "$DASHBOARD_GUI_SCRIPT" \
        > "$LOG_DIR/dashboard_gui_${TIMESTAMP}.log" 2>&1 &
    
    DASHBOARD_PID=$!
    echo "Dashboard GUI PID: $DASHBOARD_PID"
}

# Function to position windows on monitors
position_windows() {
    if ! command -v wmctrl >/dev/null 2>&1; then
        echo "Warning: wmctrl not found. Cannot position windows automatically."
        echo "Manually arrange windows on your monitors."
        return 1
    fi

    echo "Waiting for windows to be created..."
    sleep 3

    echo "Detecting monitor configuration..."
    choose_target_monitors || return 1

    # Helper: find window id by PID with fallback title hints
    find_window_id() {
        local pid="$1"; shift
        local id
        id=$(wmctrl -lp | awk -v pid="$pid" '$3 == pid {print $1; exit}')
        if [[ -n "$id" ]]; then echo "$id"; return 0; fi
        # fallback via title hints provided as args
        local hint
        for hint in "$@"; do
            id=$(wmctrl -lp | grep -i "${hint}" | awk 'NR==1{print $1; exit}')
            if [[ -n "$id" ]]; then echo "$id"; return 0; fi
        done
        return 1
    }

    echo "Available windows:"
    wmctrl -lp

    # Try multiple attempts to allow windows to register with the WM
    local attempts=10
    local svc_id="" dash_id=""
    for i in $(seq 1 $attempts); do
        [[ -z "$svc_id" ]] && svc_id=$(find_window_id "$SERVICES_PID" "services" "manager" "tweezer")
        [[ -z "$dash_id" ]] && dash_id=$(find_window_id "$DASHBOARD_PID" "dashboard" "tweezer")
        if [[ -n "$svc_id" && -n "$dash_id" ]]; then break; fi
        sleep 0.5
    done

    if [[ -n "$svc_id" ]]; then
        echo "Moving Services Manager (id $svc_id) to +${SERV_X}+${SERV_Y} and maximizing (no fullscreen)"
        # Ensure not fullscreen, then move to target monitor origin without forcing size, then maximize
        wmctrl -i -r "$svc_id" -b remove,fullscreen,maximized_vert,maximized_horz || true
        wmctrl -i -r "$svc_id" -e 0,$SERV_X,$SERV_Y,-1,-1 || true
        wmctrl -i -r "$svc_id" -b add,maximized_vert,maximized_horz || true
    else
        echo "Warning: Could not locate Services Manager window (PID $SERVICES_PID)"
    fi

    if [[ -n "$dash_id" ]]; then
        echo "Moving Dashboard (id $dash_id) to +${DASH_X}+${DASH_Y} and maximizing (no fullscreen)"
        wmctrl -i -r "$dash_id" -b remove,fullscreen,maximized_vert,maximized_horz || true
        wmctrl -i -r "$dash_id" -e 0,$DASH_X,$DASH_Y,-1,-1 || true
        wmctrl -i -r "$dash_id" -b add,maximized_vert,maximized_horz || true
    else
        echo "Warning: Could not locate Dashboard window (PID $DASHBOARD_PID)"
    fi
}

# Function to cleanup processes on exit
cleanup() {
    echo
    echo "Cleaning up processes..."
    
    # Kill any remaining Python GUI processes
    pkill -f "services_manager_gui.py" 2>/dev/null || true
    pkill -f "dashboard_gui.py" 2>/dev/null || true
    
    echo "Cleanup complete."
    exit 0
}

# Set up signal handlers for graceful shutdown
trap cleanup SIGINT SIGTERM

# Launch both applications
echo "Starting applications..."
echo

# Launch Services Manager first
launch_services_manager

# Small delay between launches
sleep 1

# Launch Dashboard GUI
launch_dashboard

# Position windows on their respective monitors
echo
echo "Positioning windows on monitors..."
position_windows

echo
echo "=== Both GUIs Launched Successfully ==="
echo "Services Manager PID: $SERVICES_PID"
echo
echo "Monitor arrangement tips:"
echo "- Services Manager should appear on your smaller/secondary monitor"
echo "- Dashboard GUI should appear maximized on your larger/primary monitor"
echo "- If windows are not positioned correctly, drag them to the desired monitors"
echo
echo "Log files:"
echo "- Services Manager: $LOG_DIR/services_manager_${TIMESTAMP}.log"
echo "- Dashboard GUI: $LOG_DIR/dashboard_gui_${TIMESTAMP}.log"
echo
echo "Press Ctrl+C to stop both applications"
echo

# Wait for both processes to complete
# This keeps the script running until both GUIs are closed
wait $SERVICES_PID $DASHBOARD_PID 2>/dev/null

echo "All GUI processes have exited."
cleanup