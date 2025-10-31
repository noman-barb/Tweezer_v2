"""
Tweezer Services Manager - GUI application for managing all Tweezer services.

This application provides a graphical interface to:
- View all configured services
- Start/stop services
- Edit service parameters
- View service logs in real-time
- Monitor service status

Requirements:
    pip install dearpygui pyyaml psutil
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO

import dearpygui.dearpygui as dpg
import yaml

try:
    import psutil
except ImportError:
    print("Error: psutil is required. Install it with: pip install psutil")
    sys.exit(1)


@dataclass
class ServiceConfig:
    """Configuration for a single service."""
    key: str
    name: str
    enabled: bool
    script_path: str
    python_script: str
    cpu_list: str
    restart_on_exit: bool
    restart_delay: int
    log_subdir: str
    args: Dict[str, Any]
    
    # Runtime state
    process: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    status: str = "Stopped"
    start_time: Optional[float] = None
    command: Optional[str] = None  # Store the command that was run
    output_buffer: List[str] = field(default_factory=list)  # Store captured output chunks
    
    # Additional config fields
    process_cpu_list: Optional[str] = None
    tracker_cpu_list: Optional[str] = None
    grpc_workers: Optional[int] = None
    log_file_path: Optional[Path] = None
    log_file_handle: Optional[TextIO] = None


@dataclass
class GlobalConfig:
    """Global configuration settings."""
    python_bin: str = "python3"
    conda_env: Optional[str] = None
    repo_root: str = "../../"
    log_base_dir: str = "../../logs/service_logs"
    stop_services_on_exit: bool = True


class ServicesManager:
    """Manages loading, starting, stopping, and monitoring services."""
    
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.services_dir = config_path.parent
        self.global_config = GlobalConfig()
        self.services: Dict[str, ServiceConfig] = {}
        self.monitoring = False
        self.monitor_thread: Optional[threading.Thread] = None
        
    def load_config(self) -> None:
        """Load configuration from YAML file."""
        with open(self.config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        # Load global config
        global_cfg = config.get('global', {})
        self.global_config.python_bin = global_cfg.get('python_bin', 'python3')
        self.global_config.conda_env = global_cfg.get('conda_env')
        self.global_config.repo_root = global_cfg.get('repo_root', '../../')
        self.global_config.log_base_dir = global_cfg.get('log_base_dir', '../../logs/service_logs')
        self.global_config.stop_services_on_exit = global_cfg.get('stop_services_on_exit', True)
        
        # Load services
        services_cfg = config.get('services', {})
        self.services.clear()
        
        for key, svc in services_cfg.items():
            service = ServiceConfig(
                key=key,
                name=svc.get('name', key),
                enabled=svc.get('enabled', True),
                script_path=svc.get('script_path', ''),
                python_script=svc.get('python_script', ''),
                cpu_list=svc.get('cpu_list', ''),
                restart_on_exit=svc.get('restart_on_exit', True),
                restart_delay=svc.get('restart_delay', 5),
                log_subdir=svc.get('log_subdir', ''),
                args=svc.get('args', {}),
                process_cpu_list=svc.get('process_cpu_list'),
                tracker_cpu_list=svc.get('tracker_cpu_list'),
                grpc_workers=svc.get('grpc_workers'),
            )
            self.services[key] = service
    
    def save_config(self) -> None:
        """Save current configuration to YAML file."""
        config = {
            'global': {
                'python_bin': self.global_config.python_bin,
                'conda_env': self.global_config.conda_env,
                'repo_root': self.global_config.repo_root,
                'log_base_dir': self.global_config.log_base_dir,
                'stop_services_on_exit': self.global_config.stop_services_on_exit,
            },
            'services': {}
        }
        
        for key, svc in self.services.items():
            service_cfg = {
                'enabled': svc.enabled,
                'name': svc.name,
                'script_path': svc.script_path,
                'python_script': svc.python_script,
                'cpu_list': svc.cpu_list,
                'restart_on_exit': svc.restart_on_exit,
                'restart_delay': svc.restart_delay,
                'log_subdir': svc.log_subdir,
                'args': svc.args,
            }
            
            # Add optional fields if present
            if svc.process_cpu_list:
                service_cfg['process_cpu_list'] = svc.process_cpu_list
            if svc.tracker_cpu_list:
                service_cfg['tracker_cpu_list'] = svc.tracker_cpu_list
            if svc.grpc_workers:
                service_cfg['grpc_workers'] = svc.grpc_workers
            
            config['services'][key] = service_cfg
        
        with open(self.config_path, 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    
    def build_command(self, service: ServiceConfig) -> tuple[List[str], bool]:
        """Build command to start a service. Returns (command, use_shell)."""
        repo_root = (self.services_dir / self.global_config.repo_root).resolve()
        python_script = repo_root / service.python_script
        
        # Determine if we're on Windows
        is_windows = sys.platform == 'win32'
        
        # Build Python command with arguments
        python_cmd_parts = [self.global_config.python_bin, '-u', str(python_script)]
        
        # Add arguments from config
        for arg_key, arg_value in service.args.items():
            if arg_value is None or arg_value == 'null':
                continue
            
            # Handle mixed underscore/hyphen conventions in different scripts
            # Check which Python script is being used to determine naming convention
            script_name = service.python_script.lower()
            
            if 'grpc_server_streaming' in script_name:
                # Arduino script: serial_port uses underscore, but max_workers/log_level use hyphens
                if arg_key in ['serial_port']:
                    arg_name = f"--{arg_key}"  # Keep underscore
                else:
                    arg_name = f"--{arg_key.replace('_', '-')}"  # Use hyphen
            else:
                # Other scripts: convert all underscores to hyphens
                arg_name = f"--{arg_key.replace('_', '-')}"
            
            if isinstance(arg_value, bool):
                if arg_value:
                    # For boolean flags
                    if arg_key == 'track_preprocess':
                        continue  # Default is true, skip
                    python_cmd_parts.append(arg_name)
            else:
                python_cmd_parts.append(arg_name)
                python_cmd_parts.append(str(arg_value))
        
        # Build final command with conda activation if needed
        if self.global_config.conda_env:
            if is_windows:
                # Windows: Use shell with conda activate
                shell_cmd = f"conda activate {self.global_config.conda_env} && {' '.join(python_cmd_parts)}"
                return ([shell_cmd], True)
            else:
                # Linux: Use bash with conda activate
                # This is the key - use shell to activate conda properly
                shell_cmd = f"source $(conda info --base)/etc/profile.d/conda.sh && conda activate {self.global_config.conda_env} && {' '.join(python_cmd_parts)}"
                return (['/bin/bash', '-c', shell_cmd], False)
        else:
            return (python_cmd_parts, False)
    
    def start_service(self, service_key: str) -> bool:
        """Start a service."""
        service = self.services.get(service_key)
        if not service or not service.enabled:
            return False
        
        if service.process and service.process.poll() is None:
            # Already running
            return False
        
        try:
            cmd, use_shell = self.build_command(service)
            
            # Set up environment to ensure unbuffered output
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            
            # Store the command for display
            full_cmd = []

            # Prepare log file for this run
            self._cleanup_log_handle(service)
            self._prepare_log_file(service)
            
            # On Windows, we can't use taskset, so we'll start the process
            # and set affinity after
            if sys.platform == 'win32':
                full_cmd = cmd if isinstance(cmd, list) else [cmd[0]]
                service.process = subprocess.Popen(
                    full_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                    shell=use_shell,
                    creationflags=0  # Don't create new console, capture output
                )
            else:
                # On Linux, use taskset for CPU affinity
                if service.process_cpu_list:
                    cpu_list = service.process_cpu_list
                elif service.cpu_list:
                    cpu_list = service.cpu_list
                else:
                    cpu_list = None
                
                # Add taskset to the command
                if cpu_list:
                    if use_shell:
                        # If using shell, wrap the shell invocation with taskset
                        full_cmd = ['taskset', '--cpu-list', cpu_list] + cmd
                    else:
                        full_cmd = ['taskset', '--cpu-list', cpu_list] + cmd
                else:
                    full_cmd = cmd
                
                # Capture output via PIPE and write to log file ourselves
                print(f"[DEBUG] Starting subprocess with command: {' '.join(full_cmd) if not use_shell else full_cmd}")
                print(f"[DEBUG] Log file: {service.log_file_path}")
                
                service.process = subprocess.Popen(
                    full_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    shell=use_shell,
                    bufsize=0,  # Unbuffered
                    env=env,
                    executable='/bin/bash' if use_shell else None
                )
                
                print(f"[DEBUG] Started process PID={service.process.pid} for {service_key}")
            
            # Store the command as a string for display
            cmd_display = ' '.join(full_cmd) if isinstance(full_cmd, list) else str(full_cmd)
            service.command = cmd_display
            
            service.pid = service.process.pid
            service.status = "Running"
            service.start_time = time.time()
            
            # On Windows, set CPU affinity after process creation
            if sys.platform == 'win32' and service.cpu_list:
                try:
                    p = psutil.Process(service.pid)
                    # Parse CPU list and create affinity mask
                    cpu_nums = self._parse_cpu_list(service.cpu_list)
                    if cpu_nums:
                        p.cpu_affinity(cpu_nums)
                        self._append_output_chunk(service, f"Set CPU affinity to: {cpu_nums}\n")
                except Exception as e:
                    print(f"Warning: Could not set CPU affinity: {e}")
                    self._append_output_chunk(service, f"Warning: Could not set CPU affinity: {e}\n")
            
            # Start output reading thread
            output_thread = threading.Thread(
                target=self._read_output, 
                args=(service_key,), 
                daemon=True
            )
            output_thread.start()
            
            return True
            
        except Exception as e:
            service.status = f"Error: {str(e)}"
            service.output_buffer = []
            self._append_output_chunk(service, f"Error starting service: {str(e)}\n")
            self._cleanup_log_handle(service)
            return False
    
    def stop_service(self, service_key: str) -> bool:
        """Stop a service."""
        service = self.services.get(service_key)
        if not service:
            return False
        if not service.process and not service.pid:
            service.status = "Stopped"
            return True
        
        try:
            self._terminate_process_tree(service)
            if service.process:
                try:
                    service.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        service.process.kill()
                        service.process.wait(timeout=5)
                    except Exception:
                        pass
            service.status = "Stopped"
            self._cleanup_service_state(service)
            return True
            
        except Exception as e:
            service.status = f"Error stopping: {str(e)}"
            return False
    
    def restart_service(self, service_key: str) -> bool:
        """Restart a service."""
        self.stop_service(service_key)
        time.sleep(0.5)
        return self.start_service(service_key)
    
    def update_service_status(self, service_key: str) -> None:
        """Update the status of a service."""
        service = self.services.get(service_key)
        if not service:
            return
        
        if not service.process:
            service.status = "Stopped"
            self._cleanup_service_state(service)
            return
        
        # Check if process is still running
        poll_result = service.process.poll()
        if poll_result is not None:
            # Process has exited
            service.status = f"Exited (code {poll_result})"
            self._cleanup_service_state(service)
        else:
            # Process is running
            service.status = "Running"
            if service.pid and psutil.pid_exists(service.pid):
                try:
                    p = psutil.Process(service.pid)
                    cpu_percent = p.cpu_percent(interval=0.1)
                    mem_mb = p.memory_info().rss / 1024 / 1024
                    service.status = f"Running (CPU: {cpu_percent:.1f}%, MEM: {mem_mb:.1f} MB)"
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    service.status = "Running"
    
    def monitor_services(self) -> None:
        """Background thread to monitor service status."""
        while self.monitoring:
            for service_key in self.services.keys():
                self.update_service_status(service_key)
            time.sleep(2)
    
    def start_monitoring(self) -> None:
        """Start background monitoring thread."""
        if not self.monitoring:
            self.monitoring = True
            self.monitor_thread = threading.Thread(target=self.monitor_services, daemon=True)
            self.monitor_thread.start()
    
    def stop_monitoring(self) -> None:
        """Stop background monitoring thread."""
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)

    def _cleanup_log_handle(self, service: ServiceConfig) -> None:
        """Close any active log file handle."""
        if service.log_file_handle:
            try:
                service.log_file_handle.flush()
                service.log_file_handle.close()
            except Exception:
                pass
            service.log_file_handle = None

    def _prepare_log_file(self, service: ServiceConfig) -> Optional[Path]:
        """Prepare log file and return its path."""
        log_base_dir = (self.services_dir / self.global_config.log_base_dir).resolve()
        log_dir_name = service.log_subdir or service.key
        log_dir = log_base_dir / log_dir_name
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"{service.key}_{timestamp}.log"
        service.log_file_path = log_path
        
        # Write command header to log file
        with open(log_path, 'w', encoding='utf-8') as f:
            cmd, use_shell = self.build_command(service)
            cmd_str = ' '.join(cmd) if isinstance(cmd, list) else str(cmd)
            f.write(f"Command: {cmd_str}\n")
            f.write(f"Log file: {log_path}\n")
            f.write("=" * 80 + "\n")
        
        # Don't keep file handle open - let subprocess write to it
        service.log_file_handle = None
        return log_path

    def _append_output_chunk(self, service: ServiceConfig, chunk: str) -> None:
        """Append output chunk to buffer (log file is written by subprocess directly)."""
        if not chunk:
            return

        service.output_buffer.append(chunk)
        if len(service.output_buffer) > 1000:
            service.output_buffer = service.output_buffer[-1000:]

    def _cleanup_service_state(self, service: ServiceConfig) -> None:
        """Reset runtime process state for a service."""
        # Close PTY file descriptor if it exists (Linux only)
        if hasattr(service.process, 'pty_fd') and service.process:
            try:
                if service.process.stdout:
                    service.process.stdout.close()
            except Exception:
                pass
        
        service.process = None
        service.pid = None
        service.start_time = None

    def _terminate_process_tree(self, service: ServiceConfig) -> None:
        """Terminate the service process and any child processes."""
        procs: List[psutil.Process] = []

        if service.pid and psutil.pid_exists(service.pid):
            try:
                parent = psutil.Process(service.pid)
                procs.extend(parent.children(recursive=True))
                procs.append(parent)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                procs = []

        # Fall back to the Popen handle if psutil could not find the PID
        if not procs and service.process and service.process.pid:
            try:
                procs = [psutil.Process(service.process.pid)]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                procs = []

        if service.process:
            try:
                service.process.terminate()
            except Exception:
                pass

        for proc in procs:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        try:
            _, alive = psutil.wait_procs(procs, timeout=5)
        except Exception:
            alive = []

        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    
    def _parse_cpu_list(self, cpu_str: str) -> List[int]:
        """Parse CPU list string like '0,1' or '2-37' into list of CPU numbers."""
        cpus = []
        for part in cpu_str.split(','):
            part = part.strip()
            if '-' in part:
                start, end = part.split('-')
                cpus.extend(range(int(start), int(end) + 1))
            else:
                cpus.append(int(part))
        return cpus
    
    def _tail_log_file(self, service_key: str) -> None:
        """Tail the log file to populate output buffer (alternative to reading from subprocess)."""
        service = self.services.get(service_key)
        if not service or not service.log_file_path:
            print(f"[DEBUG] _tail_log_file: No log file for {service_key}")
            return
        
        print(f"[DEBUG] Starting to tail log file: {service.log_file_path}")
        
        # Add header to output buffer
        self._append_output_chunk(service, "[Tailing log file for real-time output]\n")
        
        try:
            # Open log file for reading
            with open(service.log_file_path, 'r', encoding='utf-8', errors='replace') as log_file:
                # Seek to end of file
                log_file.seek(0, 2)  # Seek to end
                file_size = log_file.tell()
                
                # If file already has content, read last 4KB
                if file_size > 0:
                    start_pos = max(0, file_size - 4096)
                    log_file.seek(start_pos)
                    initial_content = log_file.read()
                    if initial_content:
                        self._append_output_chunk(service, initial_content)
                        print(f"[DEBUG] Read {len(initial_content)} bytes of existing log content")
                
                line_count = 0
                # Now tail the file
                while True:
                    # Check if process has exited
                    if service.process and service.process.poll() is not None:
                        print(f"[DEBUG] Process exited, reading final log content")
                        # Read any remaining content
                        final_content = log_file.read()
                        if final_content:
                            self._append_output_chunk(service, final_content)
                        break
                    
                    # Read new lines
                    line = log_file.readline()
                    if line:
                        line_count += 1
                        self._append_output_chunk(service, line)
                        if line_count % 10 == 0:
                            print(f"[DEBUG] Tailed {line_count} lines so far")
                    else:
                        # No new data, sleep briefly
                        time.sleep(0.1)
                
                print(f"[DEBUG] Finished tailing log file, read {line_count} lines")
                
        except Exception as e:
            print(f"[DEBUG] Error tailing log file: {e}")
            import traceback
            traceback.print_exc()
    
    def _read_output(self, service_key: str) -> None:
        """Read output from service process and write to both buffer and log file."""
        service = self.services.get(service_key)
        if not service or not service.process or not service.process.stdout:
            return
        
        try:
            process = service.process
            stream = process.stdout
            
            # Open log file for writing
            log_file = None
            if service.log_file_path:
                try:
                    log_file = open(service.log_file_path, 'ab', buffering=0)
                except Exception as e:
                    print(f"[DEBUG] Could not open log file: {e}")
            
            # Binary mode with select on Linux
            is_binary = not hasattr(stream, 'mode') or 'b' in getattr(stream, 'mode', '')
            use_select = hasattr(stream, 'fileno') and sys.platform != 'win32'
            
            if use_select and is_binary:
                import select
                fd = stream.fileno()
                
                while True:
                    if process.poll() is not None:
                        # Read remaining
                        try:
                            while True:
                                ready, _, _ = select.select([fd], [], [], 0)
                                if ready:
                                    chunk = os.read(fd, 4096)
                                    if chunk:
                                        decoded = chunk.decode('utf-8', errors='replace')
                                        self._append_output_chunk(service, decoded)
                                        if log_file:
                                            log_file.write(chunk)
                                    else:
                                        break
                                else:
                                    break
                        except Exception:
                            pass
                        break
                    
                    ready, _, _ = select.select([fd], [], [], 0.1)
                    if ready:
                        try:
                            chunk = os.read(fd, 4096)
                            if chunk:
                                decoded = chunk.decode('utf-8', errors='replace')
                                self._append_output_chunk(service, decoded)
                                if log_file:
                                    log_file.write(chunk)
                                    log_file.flush()
                        except (OSError, BlockingIOError):
                            pass
            else:
                # Windows fallback
                while True:
                    if process.poll() is not None:
                        try:
                            remaining = stream.read()
                            if remaining:
                                self._append_output_chunk(service, remaining)
                                if log_file:
                                    log_file.write(remaining.encode('utf-8', errors='replace'))
                        except Exception:
                            pass
                        break
                    
                    try:
                        line = stream.readline()
                        if line:
                            self._append_output_chunk(service, line)
                            if log_file:
                                log_file.write(line.encode('utf-8', errors='replace'))
                                log_file.flush()
                        else:
                            time.sleep(0.05)
                    except Exception:
                        break
                    
        except Exception as e:
            print(f"[DEBUG] Exception in _read_output: {e}")
        finally:
            if log_file:
                try:
                    log_file.close()
                except Exception:
                    pass


class ServicesGUI:
    """GUI application for managing services."""
    
    def __init__(self, manager: ServicesManager):
        self.manager = manager
        self.service_windows: Dict[str, int] = {}  # service_key -> window tag
        self.refresh_timer = 0
        self.output_windows: Dict[str, int] = {}  # Track open output windows: service_key -> refresh_counter
        
    def create_main_window(self) -> None:
        """Create the main application window."""
        dpg.create_context()
        
        # Configure theme
        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 5)
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 5)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)
                dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 5)
                dpg.add_theme_style(dpg.mvStyleVar_TabRounding, 5)
        
        dpg.bind_theme(global_theme)
        
        # Create main window
        with dpg.window(label="Tweezer Services Manager", tag="main_window", 
                       width=1300, height=800, pos=[50, 50]):
            
            # Menu bar
            with dpg.menu_bar():
                with dpg.menu(label="File"):
                    dpg.add_menu_item(label="Reload Config", callback=self.reload_config)
                    dpg.add_menu_item(label="Save Config", callback=self.save_config)
                    dpg.add_separator()
                    dpg.add_menu_item(label="Exit", callback=self.exit_app)
                
                with dpg.menu(label="Services"):
                    dpg.add_menu_item(label="Start All Enabled", callback=self.start_all_services)
                    dpg.add_menu_item(label="Stop All", callback=self.stop_all_services)
                    dpg.add_menu_item(label="Restart All", callback=self.restart_all_services)
                
                with dpg.menu(label="Help"):
                    dpg.add_menu_item(label="About", callback=self.show_about)
            
            # Status bar
            with dpg.group(horizontal=True):
                dpg.add_text("Config: ")
                dpg.add_text(str(self.manager.config_path), color=(100, 200, 255))
                dpg.add_spacer(width=20)
                dpg.add_text("Services: ")
                dpg.add_text(f"{len(self.manager.services)}", tag="service_count", color=(100, 255, 100))
            
            dpg.add_separator()
            
            # Services list
            with dpg.child_window(tag="services_container", border=True, height=-40):
                self.populate_services_list()
            
            # Bottom controls
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="Refresh", callback=self.refresh_services, width=100)
                dpg.add_spacer(width=10)
                dpg.add_text("Auto-refresh enabled", tag="refresh_status", color=(100, 255, 100))
        
        # Set up viewport
        dpg.create_viewport(title="Tweezer Services Manager", width=1350, height=850)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("main_window", True)
        
        # Start monitoring
        self.manager.start_monitoring()
        
        # Register render callback for auto-refresh
        dpg.set_frame_callback(frame=1, callback=self.frame_update)
    
    def populate_services_list(self) -> None:
        """Populate the services list in the UI."""
        dpg.delete_item("services_container", children_only=True)
        
        with dpg.table(header_row=True, resizable=True, policy=dpg.mvTable_SizingStretchProp,
                      borders_innerH=True, borders_outerH=True, borders_innerV=True,
                      borders_outerV=True, parent="services_container"):
            
            dpg.add_table_column(label="Service", width_fixed=True, init_width_or_weight=200)
            dpg.add_table_column(label="Status", width_fixed=True, init_width_or_weight=300)
            dpg.add_table_column(label="Enabled", width_fixed=True, init_width_or_weight=70)
            dpg.add_table_column(label="CPU", width_fixed=True, init_width_or_weight=100)
            dpg.add_table_column(label="Actions", width_fixed=True, init_width_or_weight=450)
            
            for service_key, service in self.manager.services.items():
                with dpg.table_row():
                    # Service name
                    dpg.add_text(service.name)
                    
                    # Status with color
                    status_color = self.get_status_color(service.status)
                    dpg.add_text(service.status, tag=f"status_{service_key}", color=status_color)
                    
                    # Enabled checkbox
                    dpg.add_checkbox(default_value=service.enabled, 
                                   tag=f"enabled_{service_key}",
                                   callback=lambda s, a, u: self.toggle_enabled(u), 
                                   user_data=service_key)
                    
                    # CPU affinity
                    cpu_display = service.process_cpu_list or service.cpu_list or "N/A"
                    dpg.add_text(cpu_display)
                    
                    # Action buttons
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Start", tag=f"start_{service_key}",
                                     callback=lambda s, a, u: self.start_service(u),
                                     user_data=service_key, width=60)
                        dpg.add_button(label="Stop", tag=f"stop_{service_key}",
                                     callback=lambda s, a, u: self.stop_service(u),
                                     user_data=service_key, width=60)
                        dpg.add_button(label="Restart", tag=f"restart_{service_key}",
                                     callback=lambda s, a, u: self.restart_service(u),
                                     user_data=service_key, width=70)
                        dpg.add_button(label="Edit", tag=f"edit_{service_key}",
                                     callback=lambda s, a, u: self.edit_service(u),
                                     user_data=service_key, width=60)
                        dpg.add_button(label="Output", tag=f"output_{service_key}",
                                     callback=lambda s, a, u: self.show_output(u),
                                     user_data=service_key, width=70)
                        dpg.add_button(label="Logs", tag=f"logs_{service_key}",
                                     callback=lambda s, a, u: self.show_logs(u),
                                     user_data=service_key, width=60)
    
    def get_status_color(self, status: str) -> tuple:
        """Get color for status text."""
        if "Running" in status:
            return (100, 255, 100)
        elif "Stopped" in status:
            return (150, 150, 150)
        elif "Error" in status or "Exited" in status:
            return (255, 100, 100)
        else:
            return (255, 255, 100)
    
    def frame_update(self) -> None:
        """Called every frame to update UI."""
        self.refresh_timer += 1
        
        # Auto-refresh every 60 frames (~1 second at 60fps)
        if self.refresh_timer >= 60:
            self.refresh_timer = 0
            self.update_service_statuses()
        
        # Update output windows every 30 frames (~0.5 seconds)
        if self.refresh_timer % 30 == 0:
            self.update_output_windows()
        
        # Re-register for next frame
        dpg.set_frame_callback(frame=dpg.get_frame_count() + 1, callback=self.frame_update)
    
    def update_output_windows(self) -> None:
        """Update all open output windows."""
        # Check each tracked output window
        for service_key in list(self.output_windows.keys()):
            window_tag = f"output_window_{service_key}"
            if dpg.does_item_exist(window_tag):
                # Window still exists, refresh it
                self.refresh_output(service_key, window_tag)
            else:
                # Window was closed, remove from tracking
                del self.output_windows[service_key]
    
    def update_service_statuses(self) -> None:
        """Update service status displays without full refresh."""
        for service_key, service in self.manager.services.items():
            status_tag = f"status_{service_key}"
            if dpg.does_item_exist(status_tag):
                status_color = self.get_status_color(service.status)
                dpg.set_value(status_tag, service.status)
                dpg.configure_item(status_tag, color=status_color)
    
    def start_service(self, service_key: str) -> None:
        """Start a service."""
        success = self.manager.start_service(service_key)
        if success:
            self.show_notification(f"Started {self.manager.services[service_key].name}")
        else:
            self.show_error(f"Failed to start {self.manager.services[service_key].name}")
        self.update_service_statuses()
    
    def stop_service(self, service_key: str) -> None:
        """Stop a service."""
        success = self.manager.stop_service(service_key)
        if success:
            self.show_notification(f"Stopped {self.manager.services[service_key].name}")
        else:
            self.show_error(f"Failed to stop {self.manager.services[service_key].name}")
        self.update_service_statuses()
    
    def restart_service(self, service_key: str) -> None:
        """Restart a service."""
        success = self.manager.restart_service(service_key)
        if success:
            self.show_notification(f"Restarted {self.manager.services[service_key].name}")
        else:
            self.show_error(f"Failed to restart {self.manager.services[service_key].name}")
        self.update_service_statuses()
    
    def toggle_enabled(self, service_key: str) -> None:
        """Toggle service enabled state."""
        checkbox_tag = f"enabled_{service_key}"
        new_value = dpg.get_value(checkbox_tag)
        self.manager.services[service_key].enabled = new_value
        self.manager.save_config()
        self.show_notification(f"{'Enabled' if new_value else 'Disabled'} {self.manager.services[service_key].name}")
    
    def edit_service(self, service_key: str) -> None:
        """Open service edit window."""
        service = self.manager.services[service_key]
        
        # Check if window already exists
        window_tag = f"edit_window_{service_key}"
        if dpg.does_item_exist(window_tag):
            dpg.focus_item(window_tag)
            return
        
        # Create edit window
        with dpg.window(label=f"Edit: {service.name}", tag=window_tag,
                       width=600, height=700, pos=[200, 100], modal=False, 
                       on_close=lambda: dpg.delete_item(window_tag)):
            
            dpg.add_text("Service Configuration", color=(255, 200, 100))
            dpg.add_separator()
            
            # Basic settings
            dpg.add_text("Basic Settings:")
            dpg.add_input_text(label="Service Name", default_value=service.name,
                             tag=f"{window_tag}_name", width=400)
            dpg.add_checkbox(label="Enabled", default_value=service.enabled,
                           tag=f"{window_tag}_enabled")
            dpg.add_input_text(label="CPU List", default_value=service.cpu_list,
                             tag=f"{window_tag}_cpu_list", width=400)
            
            if service.process_cpu_list:
                dpg.add_input_text(label="Process CPU List", default_value=service.process_cpu_list,
                                 tag=f"{window_tag}_process_cpu_list", width=400)
            
            if service.tracker_cpu_list:
                dpg.add_input_text(label="Tracker CPU List", default_value=service.tracker_cpu_list,
                                 tag=f"{window_tag}_tracker_cpu_list", width=400)
            
            dpg.add_separator()
            dpg.add_text("Service Arguments:")
            
            # Arguments in a scrollable child window
            with dpg.child_window(height=350, border=True):
                for arg_key, arg_value in service.args.items():
                    input_tag = f"{window_tag}_arg_{arg_key}"
                    
                    if isinstance(arg_value, bool):
                        dpg.add_checkbox(label=arg_key, default_value=arg_value, tag=input_tag)
                    elif isinstance(arg_value, int):
                        dpg.add_input_int(label=arg_key, default_value=arg_value, tag=input_tag, width=300)
                    elif isinstance(arg_value, float):
                        dpg.add_input_float(label=arg_key, default_value=arg_value, tag=input_tag, width=300)
                    else:
                        dpg.add_input_text(label=arg_key, default_value=str(arg_value) if arg_value else "",
                                         tag=input_tag, width=400)
            
            dpg.add_separator()
            
            # Action buttons
            with dpg.group(horizontal=True):
                dpg.add_button(label="Save & Apply", width=150,
                             callback=lambda: self.save_service_edits(service_key, window_tag))
                dpg.add_button(label="Cancel", width=100,
                             callback=lambda: dpg.delete_item(window_tag))
    
    def save_service_edits(self, service_key: str, window_tag: str) -> None:
        """Save edits from service edit window."""
        service = self.manager.services[service_key]
        was_running = service.process and service.process.poll() is None
        
        # Update basic settings
        service.name = dpg.get_value(f"{window_tag}_name")
        service.enabled = dpg.get_value(f"{window_tag}_enabled")
        service.cpu_list = dpg.get_value(f"{window_tag}_cpu_list")
        
        if dpg.does_item_exist(f"{window_tag}_process_cpu_list"):
            service.process_cpu_list = dpg.get_value(f"{window_tag}_process_cpu_list")
        
        if dpg.does_item_exist(f"{window_tag}_tracker_cpu_list"):
            service.tracker_cpu_list = dpg.get_value(f"{window_tag}_tracker_cpu_list")
        
        # Update arguments
        for arg_key in service.args.keys():
            input_tag = f"{window_tag}_arg_{arg_key}"
            if dpg.does_item_exist(input_tag):
                new_value = dpg.get_value(input_tag)
                service.args[arg_key] = new_value
        
        # Save to file
        self.manager.save_config()
        
        # Close edit window
        dpg.delete_item(window_tag)
        
        # Refresh display
        self.populate_services_list()
        
        # Ask to restart if was running
        if was_running:
            self.show_restart_dialog(service_key)
        else:
            self.show_notification(f"Saved configuration for {service.name}")
    
    def show_restart_dialog(self, service_key: str) -> None:
        """Show dialog asking to restart service."""
        service = self.manager.services[service_key]
        
        dialog_tag = f"restart_dialog_{service_key}"
        
        with dpg.window(label="Restart Service?", tag=dialog_tag, modal=True,
                       width=400, height=150, pos=[400, 300],
                       no_resize=True, no_move=True):
            dpg.add_text(f"Service '{service.name}' is running.")
            dpg.add_text("Restart to apply changes?")
            dpg.add_separator()
            
            with dpg.group(horizontal=True):
                dpg.add_button(label="Yes, Restart", width=150,
                             callback=lambda: self.confirm_restart(service_key, dialog_tag))
                dpg.add_button(label="No", width=100,
                             callback=lambda: dpg.delete_item(dialog_tag))
    
    def confirm_restart(self, service_key: str, dialog_tag: str) -> None:
        """Confirm and restart service."""
        dpg.delete_item(dialog_tag)
        self.restart_service(service_key)
    
    def show_logs(self, service_key: str) -> None:
        """Show logs window for a service."""
        service = self.manager.services[service_key]
        
        # Build log path
        log_base = (self.manager.services_dir / self.manager.global_config.log_base_dir).resolve()
        log_dir = log_base / service.log_subdir
        
        log_window_tag = f"logs_window_{service_key}"
        
        if dpg.does_item_exist(log_window_tag):
            dpg.focus_item(log_window_tag)
            return
        
        with dpg.window(label=f"Logs: {service.name}", tag=log_window_tag,
                       width=900, height=600, pos=[150, 150],
                       on_close=lambda: dpg.delete_item(log_window_tag)):
            
            dpg.add_text(f"Log Directory: {log_dir}")
            dpg.add_separator()
            
            if log_dir.exists():
                log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
                
                if log_files:
                    dpg.add_text(f"Latest log file: {log_files[0].name}")
                    dpg.add_separator()
                    
                    try:
                        with open(log_files[0], 'r') as f:
                            # Read last 100 lines
                            lines = f.readlines()
                            log_content = ''.join(lines[-100:])
                    except Exception as e:
                        log_content = f"Error reading log file: {e}"
                    
                    dpg.add_input_text(default_value=log_content, multiline=True,
                                     readonly=True, width=-1, height=-50)
                    
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Open Log Folder", width=150,
                                     callback=lambda: self.open_folder(log_dir))
                        dpg.add_button(label="Refresh", width=100,
                                     callback=lambda: self.show_logs(service_key))
                else:
                    dpg.add_text("No log files found.")
            else:
                dpg.add_text("Log directory does not exist yet.")
    
    def show_output(self, service_key: str) -> None:
        """Show real-time output window for a service."""
        service = self.manager.services[service_key]
        
        output_window_tag = f"output_window_{service_key}"
        
        if dpg.does_item_exist(output_window_tag):
            dpg.focus_item(output_window_tag)
            return
        
        # Register this window for auto-refresh
        self.output_windows[service_key] = 0
        
        with dpg.window(label=f"Output: {service.name}", tag=output_window_tag,
                       width=1000, height=700, pos=[100, 100],
                       on_close=lambda: self.on_output_window_close(service_key, output_window_tag)):
            
            # Command display
            dpg.add_text("Command:", color=(255, 200, 100))
            if service.command:
                dpg.add_input_text(default_value=service.command, multiline=False,
                                 readonly=True, width=-1, tag=f"{output_window_tag}_cmd")
            else:
                dpg.add_text("Service not started yet", color=(150, 150, 150))
            
            dpg.add_separator()
            
            # Output display in a scrollable child window
            dpg.add_text("Output:", color=(255, 200, 100))
            
            # Use child window for better scrolling control
            with dpg.child_window(tag=f"{output_window_tag}_output_container", 
                                 height=-80, border=True):
                output_text = ''.join(service.output_buffer) if service.output_buffer else "No output yet"
                dpg.add_text(output_text, tag=f"{output_window_tag}_output", wrap=-1)
            
            dpg.add_separator()
            
            # Control buttons
            with dpg.group(horizontal=True):
                dpg.add_button(label="Refresh", width=100,
                             callback=lambda: self.refresh_output(service_key, output_window_tag))
                dpg.add_button(label="Clear", width=100,
                             callback=lambda: self.clear_output(service_key, output_window_tag))
                dpg.add_button(label="Copy Command", width=150,
                             callback=lambda: self.copy_command(service_key))
                dpg.add_checkbox(label="Auto-refresh", default_value=True,
                               tag=f"{output_window_tag}_autorefresh")
                dpg.add_checkbox(label="Auto-scroll", default_value=True,
                               tag=f"{output_window_tag}_autoscroll")
            
            # Start auto-refresh if enabled
            dpg.set_frame_callback(frame=dpg.get_frame_count() + 30, 
                                  callback=lambda: self.auto_refresh_output(service_key, output_window_tag))
    
    def on_output_window_close(self, service_key: str, window_tag: str) -> None:
        """Handle output window closing."""
        # Remove from tracking
        if service_key in self.output_windows:
            del self.output_windows[service_key]
        # Delete the window
        dpg.delete_item(window_tag)
    
    def refresh_output(self, service_key: str, window_tag: str) -> None:
        """Refresh output display and auto-scroll to bottom."""
        if not dpg.does_item_exist(window_tag):
            return
        
        service = self.manager.services[service_key]
        output_tag = f"{window_tag}_output"
        output_container_tag = f"{window_tag}_output_container"
        cmd_tag = f"{window_tag}_cmd"
        autoscroll_tag = f"{window_tag}_autoscroll"
        
        if dpg.does_item_exist(output_tag):
            output_text = ''.join(service.output_buffer) if service.output_buffer else "No output yet"
            
            # Update the text - use configure_item for text widgets
            dpg.configure_item(output_tag, default_value=output_text)
            
            # Auto-scroll to bottom if enabled
            if dpg.does_item_exist(autoscroll_tag) and dpg.get_value(autoscroll_tag):
                if dpg.does_item_exist(output_container_tag):
                    # Scroll the child window to the bottom
                    # Get max scroll and set to maximum
                    try:
                        max_scroll = dpg.get_y_scroll_max(output_container_tag)
                        if max_scroll > 0:
                            dpg.set_y_scroll(output_container_tag, max_scroll)
                    except:
                        pass
        
        if dpg.does_item_exist(cmd_tag) and service.command:
            dpg.set_value(cmd_tag, service.command)
    
    def auto_refresh_output(self, service_key: str, window_tag: str) -> None:
        """Auto-refresh output if window is open and auto-refresh is enabled."""
        if not dpg.does_item_exist(window_tag):
            return
        
        autorefresh_tag = f"{window_tag}_autorefresh"
        if dpg.does_item_exist(autorefresh_tag) and dpg.get_value(autorefresh_tag):
            self.refresh_output(service_key, window_tag)
        
        # Schedule next refresh (every 0.5 seconds = 30 frames at 60fps)
        # Use absolute frame number instead of relative
        next_frame = dpg.get_frame_count() + 30
        dpg.set_frame_callback(frame=next_frame,
                              callback=lambda: self.auto_refresh_output(service_key, window_tag))
    
    def clear_output(self, service_key: str, window_tag: str) -> None:
        """Clear output buffer."""
        service = self.manager.services[service_key]
        service.output_buffer = []
        self.refresh_output(service_key, window_tag)
        self.show_notification(f"Cleared output for {service.name}")
    
    def copy_command(self, service_key: str) -> None:
        """Copy command to clipboard."""
        service = self.manager.services[service_key]
        if service.command:
            try:
                dpg.set_clipboard_text(service.command)
                self.show_notification("Command copied to clipboard")
            except:
                self.show_notification(f"Command: {service.command}")
        else:
            self.show_notification("No command to copy - service not started")
    
    def open_folder(self, path: Path) -> None:
        """Open folder in file explorer."""
        import platform
        if platform.system() == 'Windows':
            os.startfile(path)
        elif platform.system() == 'Darwin':  # macOS
            subprocess.Popen(['open', str(path)])
        else:  # Linux
            subprocess.Popen(['xdg-open', str(path)])
    
    def start_all_services(self) -> None:
        """Start all enabled services."""
        count = 0
        for service_key, service in self.manager.services.items():
            if service.enabled and (not service.process or service.process.poll() is not None):
                if self.manager.start_service(service_key):
                    count += 1
        self.show_notification(f"Started {count} service(s)")
        self.update_service_statuses()
    
    def stop_all_services(self) -> None:
        """Stop all running services."""
        count = 0
        for service_key in self.manager.services.keys():
            if self.manager.stop_service(service_key):
                count += 1
        self.show_notification(f"Stopped {count} service(s)")
        self.update_service_statuses()
    
    def restart_all_services(self) -> None:
        """Restart all enabled services."""
        self.stop_all_services()
        time.sleep(1)
        self.start_all_services()
    
    def reload_config(self) -> None:
        """Reload configuration from file."""
        self.manager.load_config()
        self.populate_services_list()
        self.show_notification("Configuration reloaded")
    
    def save_config(self) -> None:
        """Save configuration to file."""
        self.manager.save_config()
        self.show_notification("Configuration saved")
    
    def show_notification(self, message: str) -> None:
        """Show a notification message."""
        print(f"[INFO] {message}")
        # Could implement toast notifications here
    
    def show_error(self, message: str) -> None:
        """Show an error message."""
        print(f"[ERROR] {message}")
        # Could implement error dialog here
    
    def refresh_services(self) -> None:
        """Manually refresh services list."""
        self.populate_services_list()
        self.show_notification("Refreshed services list")
    
    def show_about(self) -> None:
        """Show about dialog."""
        about_tag = "about_window"
        
        if dpg.does_item_exist(about_tag):
            dpg.focus_item(about_tag)
            return
        
        with dpg.window(label="About", tag=about_tag, modal=True,
                       width=400, height=250, pos=[400, 300],
                       on_close=lambda: dpg.delete_item(about_tag)):
            dpg.add_text("Tweezer Services Manager", color=(255, 200, 100))
            dpg.add_text("Version 1.0")
            dpg.add_separator()
            dpg.add_text("A GUI application for managing Tweezer services.")
            dpg.add_text("")
            dpg.add_text("Features:")
            dpg.add_text("  • Start/Stop/Restart services")
            dpg.add_text("  • Edit service parameters")
            dpg.add_text("  • View service logs")
            dpg.add_text("  • Monitor service status")
            dpg.add_separator()
            dpg.add_button(label="Close", width=100, 
                         callback=lambda: dpg.delete_item(about_tag))
    
    def exit_app(self) -> None:
        """Exit the application."""
        # Stop all services before exit
        for service_key in self.manager.services.keys():
            self.manager.stop_service(service_key)
        
        self.manager.stop_monitoring()
        dpg.destroy_context()
        sys.exit(0)
    
    def run(self) -> None:
        """Run the application."""
        self.create_main_window()
        dpg.start_dearpygui()
        dpg.destroy_context()


def main():
    """Main entry point."""
    # Determine config file path
    if len(sys.argv) > 1:
        config_path = Path(sys.argv[1])
    else:
        # Default to services_config.yaml in script directory
        script_dir = Path(__file__).parent
        config_path = script_dir / "services_config.yaml"
    
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        print("Usage: python services_manager_gui.py [config_file]")
        sys.exit(1)
    
    # Create manager and load config
    manager = ServicesManager(config_path)
    manager.load_config()
    
    # Register cleanup handlers to stop services on exit
    def cleanup_handler():
        """Stop all services when GUI exits."""
        if not manager.global_config.stop_services_on_exit:
            print("\nGUI exiting. Services will continue running.")
            manager.stop_monitoring()
            return
        
        print("\nStopping all services before exit...")
        for service_key in list(manager.services.keys()):
            if manager.services[service_key].process:
                print(f"  Stopping {manager.services[service_key].name}...")
                manager.stop_service(service_key)
        manager.stop_monitoring()
        print("All services stopped.")
    
    # Register cleanup for normal exit
    atexit.register(cleanup_handler)
    
    # Register signal handlers for Ctrl+C and termination
    def signal_handler(signum, frame):
        """Handle interrupt signals."""
        print(f"\nReceived signal {signum}. Cleaning up...")
        cleanup_handler()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Create and run GUI
    gui = ServicesGUI(manager)
    try:
        gui.run()
    except KeyboardInterrupt:
        print("\nKeyboard interrupt received.")
        cleanup_handler()
    except Exception as e:
        print(f"\nError: {e}")
        cleanup_handler()
        raise
    finally:
        # Ensure cleanup runs even if there's an exception
        cleanup_handler()


if __name__ == "__main__":
    main()