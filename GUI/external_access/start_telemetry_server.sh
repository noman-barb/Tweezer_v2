#!/bin/bash
# Start script for Arduino Due Telemetry HTTP Server

set -e

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Default values
HTTP_HOST="${HTTP_HOST:-0.0.0.0}"
HTTP_PORT="${HTTP_PORT:-9002}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"
CONFIG_PATH="${CONFIG_PATH:-$REPO_ROOT/GUI/services_config.yaml}"

echo "Starting Arduino Due Telemetry HTTP Server..."
echo "  Repository Root: $REPO_ROOT"
echo "  Config Path: $CONFIG_PATH"
echo "  HTTP Host: $HTTP_HOST"
echo "  HTTP Port: $HTTP_PORT"
echo "  Log Level: $LOG_LEVEL"

# Change to the script directory
cd "$SCRIPT_DIR"

# Run the telemetry server
exec python telemetry_server_v2.py \
    --config "$CONFIG_PATH" \
    --http-host "$HTTP_HOST" \
    --http-port "$HTTP_PORT" \
    --log-level "$LOG_LEVEL"
