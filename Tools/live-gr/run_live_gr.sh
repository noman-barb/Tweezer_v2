#!/bin/bash
# Live g(r) Visualizer Launch Script
# Activates conda environment and runs the visualizer

echo "========================================"
echo "Live g(r) Pair Correlation Visualizer"
echo "========================================"
echo ""

# Check if conda environment exists
if conda env list | grep -q "^tweezer "; then
    echo "Using conda environment: tweezer"
    source "$(conda info --base)/etc/profile.d/conda.sh"
    conda activate tweezer
    echo ""
else
    echo "Warning: conda environment 'tweezer' not found"
    echo "Using default Python environment"
    echo ""
fi

# Run the visualizer with any provided arguments
python live-gr.py "$@"

exit_code=$?

# Keep terminal open if there was an error
if [ $exit_code -ne 0 ]; then
    echo ""
    echo "Press Enter to exit..."
    read
fi

exit $exit_code
