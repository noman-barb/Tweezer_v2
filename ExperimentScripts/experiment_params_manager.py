"""
Manager for saving and loading experiment script parameter configurations.

This module provides functionality to save, load, and manage parameter
presets for experiment scripts, stored as YAML files in the experiment
script's directory.
"""

from __future__ import annotations
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ExperimentParamsConfig:
    """Configuration for experiment script parameters."""
    
    script_name: str
    """Name of the experiment script this config is for"""
    
    params: Dict[str, Any] = field(default_factory=dict)
    """Parameter values: {param_name: value}"""
    
    description: str = ""
    """User description of this configuration"""
    
    created_at: Optional[str] = None
    """ISO timestamp when config was created"""
    
    updated_at: Optional[str] = None
    """ISO timestamp when config was last updated"""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for YAML serialization."""
        return {
            'script_name': self.script_name,
            'params': self.params,
            'description': self.description,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ExperimentParamsConfig:
        """Create from dictionary loaded from YAML."""
        return cls(
            script_name=data.get('script_name', ''),
            params=data.get('params', {}),
            description=data.get('description', ''),
            created_at=data.get('created_at'),
            updated_at=data.get('updated_at'),
        )


class ExperimentParamsManager:
    """
    Manages experiment script parameter configurations.
    
    Stores parameter presets as YAML files in the experiment script's directory,
    allowing users to save and recall parameter sets for different experimental
    conditions.
    """
    
    def __init__(self, scripts_root: Path):
        """
        Initialize parameter manager.
        
        Args:
            scripts_root: Root directory containing experiment scripts
        """
        self.scripts_root = Path(scripts_root)
        self.scripts_root.mkdir(parents=True, exist_ok=True)
        
        # Cache of loaded configs: {script_name: {config_name: config}}
        self._config_cache: Dict[str, Dict[str, ExperimentParamsConfig]] = {}
        
        # Default config for each script
        self._default_configs: Dict[str, str] = {}
        
        # Load defaults index
        self._load_defaults_index()
    
    def _get_script_config_dir(self, script_name: str) -> Path:
        """
        Get the configuration directory for a specific script.
        
        Creates a 'params' subdirectory in the script's folder.
        
        Args:
            script_name: Name of the experiment script
            
        Returns:
            Path to the script's params directory
        """
        # Sanitize script name to create valid directory name
        safe_name = script_name.replace(' ', '_').replace('/', '_').replace('\\', '_')
        script_dir = self.scripts_root / safe_name
        params_dir = script_dir / "params"
        params_dir.mkdir(parents=True, exist_ok=True)
        return params_dir
    
    def _get_defaults_index_path(self) -> Path:
        """Get path to the defaults index file."""
        return self.scripts_root / "params_defaults.yaml"
    
    def _load_defaults_index(self) -> None:
        """Load the index of default configurations for each script."""
        index_path = self._get_defaults_index_path()
        if index_path.exists():
            try:
                with open(index_path, 'r') as f:
                    data = yaml.safe_load(f) or {}
                    self._default_configs = data.get('defaults', {})
            except Exception as e:
                print(f"Error loading defaults index: {e}")
                self._default_configs = {}
        else:
            self._default_configs = {}
    
    def _save_defaults_index(self) -> None:
        """Save the index of default configurations."""
        index_path = self._get_defaults_index_path()
        try:
            with open(index_path, 'w') as f:
                yaml.safe_dump({'defaults': self._default_configs}, f, default_flow_style=False)
        except Exception as e:
            print(f"Error saving defaults index: {e}")
    
    def save_config(
        self,
        script_name: str,
        config_name: str,
        params: Dict[str, Any],
        description: str = ""
    ) -> bool:
        """
        Save a parameter configuration.
        
        Args:
            script_name: Name of the experiment script
            config_name: Name for this configuration
            params: Parameter values to save
            description: Optional description
            
        Returns:
            True if successful, False otherwise
        """
        try:
            config_dir = self._get_script_config_dir(script_name)
            config_path = config_dir / f"{config_name}.yaml"
            
            # Check if updating existing config
            if config_path.exists():
                try:
                    with open(config_path, 'r') as f:
                        existing_data = yaml.safe_load(f) or {}
                        created_at = existing_data.get('created_at')
                except Exception:
                    created_at = None
            else:
                created_at = None
            
            # Create or update config
            now = datetime.now(timezone.utc).isoformat()
            config = ExperimentParamsConfig(
                script_name=script_name,
                params=params,
                description=description,
                created_at=created_at or now,
                updated_at=now,
            )
            
            # Save to file
            with open(config_path, 'w') as f:
                yaml.safe_dump(config.to_dict(), f, default_flow_style=False)
            
            # Update cache
            if script_name not in self._config_cache:
                self._config_cache[script_name] = {}
            self._config_cache[script_name][config_name] = config
            
            return True
            
        except Exception as e:
            print(f"Error saving config '{config_name}' for script '{script_name}': {e}")
            return False
    
    def load_config(self, script_name: str, config_name: str) -> Optional[ExperimentParamsConfig]:
        """
        Load a parameter configuration.
        
        Args:
            script_name: Name of the experiment script
            config_name: Name of the configuration to load
            
        Returns:
            ExperimentParamsConfig if found, None otherwise
        """
        # Check cache first
        if script_name in self._config_cache:
            if config_name in self._config_cache[script_name]:
                return self._config_cache[script_name][config_name]
        
        # Load from file
        try:
            config_dir = self._get_script_config_dir(script_name)
            config_path = config_dir / f"{config_name}.yaml"
            
            if not config_path.exists():
                return None
            
            with open(config_path, 'r') as f:
                data = yaml.safe_load(f)
                if not data:
                    return None
                
                config = ExperimentParamsConfig.from_dict(data)
                
                # Update cache
                if script_name not in self._config_cache:
                    self._config_cache[script_name] = {}
                self._config_cache[script_name][config_name] = config
                
                return config
                
        except Exception as e:
            print(f"Error loading config '{config_name}' for script '{script_name}': {e}")
            return None
    
    def delete_config(self, script_name: str, config_name: str) -> bool:
        """
        Delete a parameter configuration.
        
        Args:
            script_name: Name of the experiment script
            config_name: Name of the configuration to delete
            
        Returns:
            True if successful, False otherwise
        """
        try:
            config_dir = self._get_script_config_dir(script_name)
            config_path = config_dir / f"{config_name}.yaml"
            
            if config_path.exists():
                config_path.unlink()
            
            # Remove from cache
            if script_name in self._config_cache:
                self._config_cache[script_name].pop(config_name, None)
            
            # Clear default if this was it
            if self._default_configs.get(script_name) == config_name:
                self._default_configs.pop(script_name, None)
                self._save_defaults_index()
            
            return True
            
        except Exception as e:
            print(f"Error deleting config '{config_name}' for script '{script_name}': {e}")
            return False
    
    def list_configs(self, script_name: str) -> List[str]:
        """
        List all saved configurations for a script.
        
        Args:
            script_name: Name of the experiment script
            
        Returns:
            List of configuration names
        """
        try:
            config_dir = self._get_script_config_dir(script_name)
            
            if not config_dir.exists():
                return []
            
            configs = []
            for config_path in config_dir.glob("*.yaml"):
                configs.append(config_path.stem)
            
            return sorted(configs)
            
        except Exception as e:
            print(f"Error listing configs for script '{script_name}': {e}")
            return []
    
    def set_default_config(self, script_name: str, config_name: str) -> bool:
        """
        Set the default configuration for a script.
        
        Args:
            script_name: Name of the experiment script
            config_name: Name of the configuration to set as default
            
        Returns:
            True if successful, False otherwise
        """
        # Verify config exists
        if config_name not in self.list_configs(script_name):
            return False
        
        self._default_configs[script_name] = config_name
        self._save_defaults_index()
        return True
    
    def get_default_config(self, script_name: str) -> Optional[str]:
        """
        Get the default configuration name for a script.
        
        Args:
            script_name: Name of the experiment script
            
        Returns:
            Name of default configuration, or None if not set
        """
        return self._default_configs.get(script_name)
    
    def load_default_config(self, script_name: str) -> Optional[ExperimentParamsConfig]:
        """
        Load the default configuration for a script.
        
        Args:
            script_name: Name of the experiment script
            
        Returns:
            ExperimentParamsConfig if default exists, None otherwise
        """
        default_name = self.get_default_config(script_name)
        if default_name:
            return self.load_config(script_name, default_name)
        return None
    
    def get_config_info(self, script_name: str, config_name: str) -> Optional[Dict[str, Any]]:
        """
        Get metadata about a configuration without loading full params.
        
        Args:
            script_name: Name of the experiment script
            config_name: Name of the configuration
            
        Returns:
            Dictionary with config metadata, or None if not found
        """
        config = self.load_config(script_name, config_name)
        if config:
            return {
                'name': config_name,
                'script_name': config.script_name,
                'description': config.description,
                'created_at': config.created_at,
                'updated_at': config.updated_at,
                'is_default': self.get_default_config(script_name) == config_name,
            }
        return None
