"""Feature Configuration Manager for SLM hologram generation.

This module mirrors the calibration configuration manager but targets
optional hologram generation features like apodization and axial focus control.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class SlmFeatureConfig:
    """Persistent configuration for hologram generation features."""

    name: str
    description: str = ""
    apodization_enabled: bool = False
    apodization_strength: float = 0.6
    z_focus_enabled: bool = False
    z_focus_offset: float = 0.0
    z_focus_scale: float = 0.5
    default_point_z: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SlmFeatureConfig":
        valid = {field.name for field in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in valid}
        return cls(**filtered)


class SlmFeatureConfigManager:
    """Manage named feature configurations stored on disk."""

    def __init__(self, config_dir: Path):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.configs_file = self.config_dir / "feature_configs.json"
        self.default_config_name = "default"

        self._ensure_default()

        self._configs: Dict[str, SlmFeatureConfig] = {}
        self._current_config_name: str = self.default_config_name
        self.load_all_configs()

    def _ensure_default(self) -> None:
        default_config = SlmFeatureConfig(
            name=self.default_config_name,
            description="Default feature config (features disabled)",
        )
        default_file = self.config_dir / f"{self.default_config_name}.json"
        if not default_file.exists():
            self.save_config_to_file(default_config, default_file)

    def load_all_configs(self) -> None:
        self._configs.clear()

        for config_file in self.config_dir.glob("*.json"):
            if config_file.name == self.configs_file.name:
                continue
            try:
                config = self.load_config_from_file(config_file)
                self._configs[config.name] = config
            except Exception as exc:
                logging.warning("Failed to load feature config %s: %s", config_file, exc)

        if self.default_config_name not in self._configs:
            self._ensure_default()
            default_file = self.config_dir / f"{self.default_config_name}.json"
            self._configs[self.default_config_name] = self.load_config_from_file(default_file)

        if self.configs_file.exists():
            try:
                with open(self.configs_file, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
                    self._current_config_name = meta.get("current_config", self.default_config_name)
            except Exception as exc:
                logging.warning("Failed to load feature config metadata: %s", exc)
                self._current_config_name = self.default_config_name

        if self._current_config_name not in self._configs:
            self._current_config_name = self.default_config_name

    def save_config_to_file(self, config: SlmFeatureConfig, file_path: Path) -> None:
        try:
            with open(file_path, "w", encoding="utf-8") as fh:
                json.dump(config.to_dict(), fh, indent=2)
        except Exception as exc:
            raise RuntimeError(f"Failed to save feature config to {file_path}: {exc}") from exc

    def load_config_from_file(self, file_path: Path) -> SlmFeatureConfig:
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return SlmFeatureConfig.from_dict(data)
        except Exception as exc:
            raise RuntimeError(f"Failed to load feature config from {file_path}: {exc}") from exc

    def save_metadata(self) -> None:
        try:
            metadata = {
                "current_config": self._current_config_name,
                "available_configs": list(self._configs.keys()),
            }
            with open(self.configs_file, "w", encoding="utf-8") as fh:
                json.dump(metadata, fh, indent=2)
        except Exception as exc:
            logging.error("Failed to save feature config metadata: %s", exc)

    def get_config(self, name: str) -> Optional[SlmFeatureConfig]:
        return self._configs.get(name)

    def get_current_config(self) -> SlmFeatureConfig:
        config = self._configs.get(self._current_config_name)
        if config is None:
            config = self._configs.get(self.default_config_name)
            if config is None:
                config = SlmFeatureConfig(name=self.default_config_name)
        return config

    def set_current_config(self, name: str) -> bool:
        if name not in self._configs:
            return False
        self._current_config_name = name
        self.save_metadata()
        return True

    def create_config(self, name: str, description: str = "", base_config: Optional[str] = None) -> SlmFeatureConfig:
        if base_config is None:
            base_config = self._current_config_name
        template = self.get_config(base_config) or SlmFeatureConfig(name="template")
        new_config = SlmFeatureConfig(
            name=name,
            description=description,
            apodization_enabled=template.apodization_enabled,
            apodization_strength=template.apodization_strength,
            z_focus_enabled=template.z_focus_enabled,
            z_focus_offset=template.z_focus_offset,
            z_focus_scale=template.z_focus_scale,
            default_point_z=template.default_point_z,
        )
        config_file = self.config_dir / f"{name}.json"
        self.save_config_to_file(new_config, config_file)
        self._configs[name] = new_config
        self.save_metadata()
        return new_config

    def update_config(self, name: str, config: SlmFeatureConfig) -> bool:
        if name not in self._configs:
            return False
        config.name = name
        self._configs[name] = config
        config_file = self.config_dir / f"{name}.json"
        self.save_config_to_file(config, config_file)
        return True

    def delete_config(self, name: str) -> bool:
        if name == self.default_config_name or name not in self._configs:
            return False
        del self._configs[name]
        config_file = self.config_dir / f"{name}.json"
        if config_file.exists():
            config_file.unlink()
        if self._current_config_name == name:
            self._current_config_name = self.default_config_name
        self.save_metadata()
        return True

    def reset_to_default(self) -> None:
        self._current_config_name = self.default_config_name
        self.save_metadata()

    def list_configs(self) -> List[str]:
        return list(self._configs.keys())

    def get_current_config_name(self) -> str:
        return self._current_config_name
