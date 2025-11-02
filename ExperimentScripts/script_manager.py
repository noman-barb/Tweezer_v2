"""
Script manager for loading and executing experiment scripts.

Handles dynamic script discovery, lifecycle management, and execution.
"""

from __future__ import annotations
import importlib.util
import inspect
import sys
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Type
from base_script import ExperimentScript, ExperimentContext


class ScriptManager:
    """
    Manages loading, instantiation, and execution of experiment scripts.
    
    The manager scans a directory for Python files containing ExperimentScript
    subclasses and provides methods to start/stop/control them.
    """
    
    def __init__(self, scripts_dir: Path):
        """
        Initialize script manager.
        
        Args:
            scripts_dir: Directory to scan for experiment scripts
        """
        self.scripts_dir = Path(scripts_dir)
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        
        self.available_scripts: Dict[str, Type[ExperimentScript]] = {}
        """Map of script name -> script class"""
        
        self.current_script: Optional[ExperimentScript] = None
        """Currently running script instance"""
        
        self.script_errors: Dict[str, str] = {}
        """Map of script file -> error message for failed loads"""
        
        # Initial scan
        self.scan_scripts()
    
    def scan_scripts(self) -> None:
        """
        Scan scripts directory for valid experiment scripts.
        
        Looks for Python files containing ExperimentScript subclasses.
        Updates available_scripts dictionary.
        """
        self.available_scripts.clear()
        self.script_errors.clear()
        
        # Recursively find all Python files
        python_files = list(self.scripts_dir.rglob("*.py"))
        
        for script_path in python_files:
            # Skip __init__.py, private files, and base files
            if (script_path.name.startswith("_") or 
                script_path.name == "base_script.py" or
                script_path.name == "script_manager.py"):
                continue
            
            try:
                # Load module dynamically
                module_name = f"experiment_script_{script_path.stem}_{id(script_path)}"
                spec = importlib.util.spec_from_file_location(module_name, script_path)
                
                if spec is None or spec.loader is None:
                    self.script_errors[str(script_path)] = "Failed to create module spec"
                    continue
                
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                
                # Find ExperimentScript subclasses
                found_scripts = 0
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if (issubclass(obj, ExperimentScript) and 
                        obj is not ExperimentScript and
                        obj.__module__ == module_name):
                        
                        # Instantiate temporarily to get name
                        try:
                            temp_instance = obj()
                            script_name = temp_instance.name
                            
                            # Check for name conflicts
                            if script_name in self.available_scripts:
                                self.script_errors[str(script_path)] = (
                                    f"Script name '{script_name}' already exists"
                                )
                            else:
                                self.available_scripts[script_name] = obj
                                found_scripts += 1
                        except Exception as e:
                            self.script_errors[str(script_path)] = (
                                f"Failed to instantiate {name}: {e}"
                            )
                
                if found_scripts == 0 and str(script_path) not in self.script_errors:
                    self.script_errors[str(script_path)] = "No ExperimentScript subclass found"
                    
            except Exception as e:
                self.script_errors[str(script_path)] = f"Load error: {e}\n{traceback.format_exc()}"
    
    def get_available_scripts(self) -> List[str]:
        """
        Get list of available script names.
        
        Returns:
            List of script names
        """
        return sorted(self.available_scripts.keys())
    
    def get_script_info(self, script_name: str) -> Optional[Dict[str, str]]:
        """
        Get information about a script.
        
        Args:
            script_name: Name of the script
            
        Returns:
            Dictionary with 'name' and 'description', or None if not found
        """
        if script_name not in self.available_scripts:
            return None
        
        try:
            temp_instance = self.available_scripts[script_name]()
            return {
                'name': temp_instance.name,
                'description': temp_instance.description,
            }
        except Exception as e:
            return {
                'name': script_name,
                'description': f"Error getting info: {e}",
            }
    
    def load_script(self, script_name: str) -> Optional[ExperimentScript]:
        """
        Instantiate a script by name.
        
        Args:
            script_name: Name of the script to load
            
        Returns:
            Script instance, or None if not found or error
        """
        if script_name not in self.available_scripts:
            return None
        
        try:
            return self.available_scripts[script_name]()
        except Exception as e:
            print(f"Error instantiating script '{script_name}': {e}")
            return None
    
    def start_script(self, script_name: str, ctx: ExperimentContext) -> bool:
        """
        Start executing a script.
        
        Args:
            script_name: Name of the script to start
            ctx: Experiment context to pass to setup
            
        Returns:
            True if script started successfully, False otherwise
        """
        # Stop current script if running
        if self.current_script and self.current_script.is_running:
            ctx.log(f"Stopping current script: {self.current_script.name}", "INFO")
            self.stop_script(ctx)
        
        # Load new script
        script = self.load_script(script_name)
        if not script:
            ctx.log(f"Failed to load script: {script_name}", "ERROR")
            return False
        
        # Run setup
        try:
            ctx.log(f"Starting script: {script.name}", "INFO")
            if script.setup(ctx):
                self.current_script = script
                script.is_running = True
                script.frame_count = 0
                script.error_count = 0
                ctx.log(f"Script started successfully: {script.name}", "INFO")
                return True
            else:
                ctx.log(f"Script setup returned False: {script.name}", "WARNING")
                return False
        except Exception as e:
            ctx.log(f"Error in script setup: {e}\n{traceback.format_exc()}", "ERROR")
            return False
    
    def stop_script(self, ctx: ExperimentContext) -> None:
        """
        Stop the currently running script.
        
        Args:
            ctx: Experiment context to pass to teardown
        """
        if not self.current_script:
            return
        
        try:
            ctx.log(f"Stopping script: {self.current_script.name}", "INFO")
            self.current_script.teardown(ctx)
            ctx.log(f"Script stopped: {self.current_script.name}", "INFO")
        except Exception as e:
            ctx.log(f"Error in script teardown: {e}\n{traceback.format_exc()}", "ERROR")
        finally:
            self.current_script.is_running = False
            self.current_script = None
    
    def pause_script(self, ctx: ExperimentContext) -> None:
        """
        Pause the currently running script.
        
        Args:
            ctx: Experiment context
        """
        if not self.current_script or not self.current_script.is_running:
            return
        
        try:
            self.current_script.is_paused = True
            self.current_script.on_pause(ctx)
            ctx.log(f"Script paused: {self.current_script.name}", "INFO")
        except Exception as e:
            ctx.log(f"Error pausing script: {e}", "ERROR")
    
    def resume_script(self, ctx: ExperimentContext) -> None:
        """
        Resume a paused script.
        
        Args:
            ctx: Experiment context
        """
        if not self.current_script or not self.current_script.is_paused:
            return
        
        try:
            self.current_script.is_paused = False
            self.current_script.on_resume(ctx)
            ctx.log(f"Script resumed: {self.current_script.name}", "INFO")
        except Exception as e:
            ctx.log(f"Error resuming script: {e}", "ERROR")
    
    def process_frame(self, ctx: ExperimentContext) -> None:
        """
        Process one frame with the current script.
        
        This should be called on each new frame from the camera.
        
        Args:
            ctx: Experiment context with current frame data
        """
        if not self.current_script or not self.current_script.is_running:
            return
        
        if self.current_script.is_paused:
            return
        
        try:
            self.current_script.frame_count += 1
            should_continue = self.current_script.on_frame(ctx)
            
            if not should_continue:
                ctx.log(f"Script completed: {self.current_script.name}", "INFO")
                self.stop_script(ctx)
                
        except Exception as e:
            ctx.log(f"Error in script on_frame: {e}\n{traceback.format_exc()}", "ERROR")
            
            # Call error handler
            try:
                should_continue = self.current_script.on_error(ctx, e)
                if not should_continue:
                    ctx.log(f"Script stopped due to error: {self.current_script.name}", "ERROR")
                    self.stop_script(ctx)
            except Exception as e2:
                ctx.log(f"Error in error handler: {e2}", "ERROR")
                self.stop_script(ctx)
    
    def get_current_status(self) -> Dict[str, Any]:
        """
        Get status of current script.
        
        Returns:
            Dictionary with current script status
        """
        if not self.current_script:
            return {
                'running': False,
                'script_name': None,
            }
        
        return {
            'running': self.current_script.is_running,
            'paused': self.current_script.is_paused,
            'script_name': self.current_script.name,
            'frame_count': self.current_script.frame_count,
            'error_count': self.current_script.error_count,
        }


# Make Any available for type hints
from typing import Any
