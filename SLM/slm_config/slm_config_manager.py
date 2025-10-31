"""SLM Configuration Manager for Tweezer Services.

This module manages SLM affine parameter configurations, including:
- Loading and saving configuration files
- Managing default configurations
- Providing configuration utilities
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


@dataclass
class SlmConfig:
    """SLM configuration containing affine parameters and metadata."""
    name: str
    description: str = ""
    
    # Legacy affine parameters (used by current system)
    cam_x0: float = 0.0
    cam_y0: float = 0.0
    slm_x0: float = 0.0
    slm_y0: float = 0.0
    cam_x1: float = 0.0
    cam_y1: float = 0.0
    slm_x1: float = 0.0
    slm_y1: float = 0.0
    cam_x2: float = 0.0
    cam_y2: float = 0.0
    slm_x2: float = 0.0
    slm_y2: float = 0.0
    
    # Extended affine parameters (for future use)
    translate_x: float = 0.0
    translate_y: float = 0.0
    translate_z: float = 0.0
    rotate_x_deg: float = 0.0
    rotate_y_deg: float = 0.0
    rotate_z_deg: float = 0.0
    scale_x: float = 0.0
    scale_y: float = 0.0
    scale_z: float = 0.0
    shear_xy: float = 0.0
    shear_yz: float = 0.0
    shear_xz: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SlmConfig":
        """Create from dictionary loaded from JSON."""
        # Filter out any extra keys that aren't part of the dataclass
        valid_keys = {field.name for field in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered_data)
    
    def get_legacy_params(self) -> Dict[str, float]:
        """Get legacy parameter format used by dashboard."""
        return {
            "cam_x0": self.cam_x0,
            "cam_y0": self.cam_y0,
            "slm_x0": self.slm_x0,
            "slm_y0": self.slm_y0,
            "cam_x1": self.cam_x1,
            "cam_y1": self.cam_y1,
            "slm_x1": self.slm_x1,
            "slm_y1": self.slm_y1,
            "cam_x2": self.cam_x2,
            "cam_y2": self.cam_y2,
            "slm_x2": self.slm_x2,
            "slm_y2": self.slm_y2,
        }
    
    def update_from_legacy_params(self, params: Dict[str, float]) -> None:
        """Update configuration from legacy parameter format."""
        for key, value in params.items():
            if hasattr(self, key):
                setattr(self, key, value)


class SlmConfigManager:
    """Manager for SLM configurations."""
    
    def __init__(self, config_dir: Path):
        """Initialize the configuration manager.
        
        Args:
            config_dir: Directory containing SLM configuration files
        """
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        
        self.configs_file = self.config_dir / "configs.json"
        self.default_config_name = "default"
        
        # Initialize with default configuration if needed
        self._ensure_default_config()
        
        # Load existing configurations
        self._configs: Dict[str, SlmConfig] = {}
        self._current_config_name: str = self.default_config_name
        self.load_all_configs()
    
    def _ensure_default_config(self) -> None:
        """Ensure default configuration exists."""
        default_config = SlmConfig(
            name="default",
            description="Default SLM configuration with zero affine parameters"
        )
        
        default_file = self.config_dir / "default.json"
        if not default_file.exists():
            self.save_config_to_file(default_config, default_file)
    
    def load_all_configs(self) -> None:
        """Load all configurations from the config directory."""
        self._configs.clear()
        
        # Load all .json files in the config directory
        for config_file in self.config_dir.glob("*.json"):
            if config_file.name == "configs.json":
                continue  # Skip the main configs file
            
            try:
                config = self.load_config_from_file(config_file)
                self._configs[config.name] = config
            except Exception as exc:
                logging.warning("Failed to load SLM config %s: %s", config_file, exc)
        
        # Ensure we have at least the default config
        if "default" not in self._configs:
            self._ensure_default_config()
            default_file = self.config_dir / "default.json"
            self._configs["default"] = self.load_config_from_file(default_file)
        
        # Load current config name from configs.json
        if self.configs_file.exists():
            try:
                with open(self.configs_file, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                    self._current_config_name = meta.get("current_config", "default")
            except Exception as exc:
                logging.warning("Failed to load SLM config metadata: %s", exc)
                self._current_config_name = "default"
        
        # Ensure current config exists
        if self._current_config_name not in self._configs:
            self._current_config_name = "default"
    
    def save_config_to_file(self, config: SlmConfig, file_path: Path) -> None:
        """Save a configuration to a specific file."""
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(config.to_dict(), f, indent=2)
        except Exception as exc:
            raise RuntimeError(f"Failed to save SLM config to {file_path}: {exc}") from exc
    
    def load_config_from_file(self, file_path: Path) -> SlmConfig:
        """Load a configuration from a specific file."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return SlmConfig.from_dict(data)
        except Exception as exc:
            raise RuntimeError(f"Failed to load SLM config from {file_path}: {exc}") from exc
    
    def save_metadata(self) -> None:
        """Save metadata (current config, etc.) to configs.json."""
        try:
            metadata = {
                "current_config": self._current_config_name,
                "available_configs": list(self._configs.keys())
            }
            with open(self.configs_file, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2)
        except Exception as exc:
            logging.error("Failed to save SLM config metadata: %s", exc)
    
    def get_config(self, name: str) -> Optional[SlmConfig]:
        """Get a configuration by name."""
        return self._configs.get(name)
    
    def get_current_config(self) -> SlmConfig:
        """Get the currently active configuration."""
        config = self._configs.get(self._current_config_name)
        if config is None:
            # Fallback to default
            config = self._configs.get("default")
            if config is None:
                # Last resort: create a temporary default
                config = SlmConfig(name="default")
        return config
    
    def set_current_config(self, name: str) -> bool:
        """Set the current configuration by name.
        
        Returns:
            True if successful, False if config doesn't exist
        """
        if name not in self._configs:
            return False
        
        self._current_config_name = name
        self.save_metadata()
        return True
    
    def create_config(self, name: str, description: str = "", base_config: Optional[str] = None) -> SlmConfig:
        """Create a new configuration.
        
        Args:
            name: Name for the new configuration
            description: Description of the configuration
            base_config: Name of config to copy from (default: current config)
            
        Returns:
            The created configuration
        """
        if base_config is None:
            base_config = self._current_config_name
        
        # Get base configuration
        base = self.get_config(base_config)
        if base is None:
            base = SlmConfig(name="temp")
        
        # Create new config based on the base
        new_config = SlmConfig(
            name=name,
            description=description,
            **{k: v for k, v in asdict(base).items() if k not in ("name", "description")}
        )
        
        # Save to file and add to configs
        config_file = self.config_dir / f"{name}.json"
        self.save_config_to_file(new_config, config_file)
        self._configs[name] = new_config
        self.save_metadata()
        
        return new_config
    
    def update_config(self, name: str, config: SlmConfig) -> bool:
        """Update an existing configuration.
        
        Returns:
            True if successful, False if config doesn't exist
        """
        if name not in self._configs:
            return False
        
        # Update the config object
        config.name = name  # Ensure name matches
        self._configs[name] = config
        
        # Save to file
        config_file = self.config_dir / f"{name}.json"
        self.save_config_to_file(config, config_file)
        
        return True
    
    def delete_config(self, name: str) -> bool:
        """Delete a configuration.
        
        Returns:
            True if successful, False if config doesn't exist or is default
        """
        if name == "default" or name not in self._configs:
            return False
        
        # Remove from memory
        del self._configs[name]
        
        # Remove file
        config_file = self.config_dir / f"{name}.json"
        if config_file.exists():
            config_file.unlink()
        
        # If this was the current config, switch to default
        if self._current_config_name == name:
            self._current_config_name = "default"
        
        self.save_metadata()
        return True
    
    def reset_to_default(self) -> None:
        """Reset current configuration to default."""
        self._current_config_name = "default"
        self.save_metadata()
    
    def list_configs(self) -> List[str]:
        """Get list of available configuration names."""
        return list(self._configs.keys())
    
    def get_current_config_name(self) -> str:
        """Get the name of the current configuration."""
        return self._current_config_name
    
    def export_config(self, name: str, export_path: Path) -> bool:
        """Export a configuration to an external file.
        
        Returns:
            True if successful, False if config doesn't exist
        """
        config = self.get_config(name)
        if config is None:
            return False
        
        try:
            self.save_config_to_file(config, export_path)
            return True
        except Exception:
            return False
    
    def import_config(self, import_path: Path, new_name: Optional[str] = None) -> Optional[str]:
        """Import a configuration from an external file.
        
        Args:
            import_path: Path to configuration file to import
            new_name: Optional new name for the imported config
            
        Returns:
            Name of imported config if successful, None otherwise
        """
        try:
            config = self.load_config_from_file(import_path)
            
            if new_name:
                config.name = new_name
            
            # Ensure unique name
            original_name = config.name
            counter = 1
            while config.name in self._configs:
                config.name = f"{original_name}_{counter}"
                counter += 1
            
            # Save the imported config
            config_file = self.config_dir / f"{config.name}.json"
            self.save_config_to_file(config, config_file)
            self._configs[config.name] = config
            self.save_metadata()
            
            return config.name
        except Exception as exc:
            logging.error("Failed to import SLM config from %s: %s", import_path, exc)
            return None