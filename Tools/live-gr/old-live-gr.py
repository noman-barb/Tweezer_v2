import asyncio
import websockets
import json
import threading
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import trackpy as tp
from collections import deque
import numpy as np

# Global DataFrame to store 'x' and 'y' values
data_df = pd.DataFrame(columns=["x", "y"])

# Lock for thread-safe DataFrame updates
data_lock = threading.Lock()

# Deque to store the last 20 (r, g_r) pairs
rg_deque = deque(maxlen=20)

# Global variables for plots
scat1 = None
line2 = None
ax1 = None
ax2 = None

async def connect_to_source():
    async with websockets.connect("ws://10.0.63.153:4012/ws") as source_ws:
        print(f"Connected to source: {source_ws.remote_address}")
        async for message in source_ws:
            # Process the message and update the DataFrame
            updated_data = process_message(message)
            update_dataframe(updated_data)

def process_message(message):
    """
    Processes the received message from the source and returns a list of dictionaries
    with individual 'x' and 'y' values.
    """
    try:
        data = json.loads(message)
        x_values = data.get('x', [])
        y_values = data.get('y', [])
        
        points = [{"x": x, "y": y} for x, y in zip(x_values, y_values)]
        return points
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
        return []

def update_dataframe(data):
    """
    Updates the global DataFrame with new data received from the source.
    """
    with data_lock:
        global data_df
        new_rows = pd.DataFrame(data)
        data_df = new_rows
        print(f"Updated DataFrame:\n{data_df}")

def animate(i):
    """
    Animation function to update the scatter plot with the latest data.
    """
    with data_lock:
        if not data_df.empty:
            # Compute pair correlation
            r, gr = tp.static.pair_correlation_2d(data_df, cutoff=200, fraction=1.0, dr=1.0)
            r = r[:-1]  # Remove last element from r to match the length of g_r

            # remove first element from r and gr
            r = r[5:]/32.0
            gr = gr[5:]

            # Store the (r, g_r) pair in the deque
            rg_deque.append((r, gr))

            # Calculate the average r and g_r from the deque
            avg_r = np.mean([item[0] for item in rg_deque], axis=0)
            avg_gr = np.mean([item[1] for item in rg_deque], axis=0)

            # Update data in scatter plot
            scat1.set_offsets(np.c_[data_df['x'], data_df['y']])

            # Update data in line plot
            line2.set_data(avg_r, avg_gr)

            # Optional: adjust plot limits if new data goes beyond current limits
            # ax1.relim()
            # ax1.autoscale_view()

            # limit ax1 from 0 to 1500, 0 to 1500
            ax1.set_xlim(0, 1456)
            ax1.set_ylim(0, 1090)


            ax2.relim()
            ax2.autoscale_view()

def main_loop():
    """
    Main loop for plotting, using FuncAnimation to update the plot dynamically.
    """
    global ax1, ax2, scat1, line2
    plt.style.use('dark_background')  # Use dark background style
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(6, 12))

    # Initialize scatter plot
    scat1 = ax1.scatter([], [], color='yellow')  # Change color to cyan for contrast
    ax1.set_title("Real-Time Tracked Particles", color='white')
    ax1.set_xlabel("x", fontsize=16, color='white')
    ax1.set_ylabel("y", fontsize=16, color='white')

    # Initialize line plot
    line2, = ax2.plot([], [], color='cyan', linewidth=4)  # Change color to yellow for contrast
    ax2.set_title("Pair Correlation g(r)", color='white')
    ax2.set_xlabel(r'r ($\sigma$)', fontsize=16, color='white')
    ax2.set_ylabel("g(r)", fontsize=16, color='white')
    ax2.axhline(y=1, color='white', linestyle='--', linewidth=2)  # Add a horizontal line at y=1

    # tickslabel size 
    ax1.tick_params(axis='both', which='major', labelsize=20)
    ax2.tick_params(axis='both', which='major', labelsize=20)

    # Show grid in ax2 with a light color for contrast
    ax2.grid(True, color='gray')

    # Set the face color of the figure and axes to black
    fig.patch.set_facecolor('black')
    ax1.set_facecolor('black')
    ax2.set_facecolor('black')

    plt.tight_layout()

    ani = FuncAnimation(fig, animate, interval=100)  # Update every 100 ms
    plt.show()

if __name__ == "__main__":
    # Start the WebSocket source connection in a separate thread
    threading.Thread(target=lambda: asyncio.run(connect_to_source()), daemon=True).start()

    # Run the Matplotlib animation loop in the main thread
    main_loop()