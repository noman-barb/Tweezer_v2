"""SLM Fine-Tuning Configuration Manager for Auto-Calibration.

This module manages automatically generated fine-tuned SLM configurations that
improve alignment between camera coordinates and SLM positions through iterative
sampling and error minimization.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


@dataclass
class FineTuningSample:
    """Single sample point from fine-tuning process."""
    slm_x: float  # Target SLM position
    slm_y: float
    particle_x: float  # Actual detected particle position
    particle_y: float
    error: float  # Distance between target and actual


@dataclass
class FineTuningResult:
    """Results from a fine-tuning session."""
    name: str  # Datetime-based identifier
    timestamp: str  # ISO format datetime
    base_config_name: str  # Name of the config this was derived from
    
    # Sampling parameters
    sample_count: int
    margin_pixels: float
    sample_area_min_x: float
    sample_area_min_y: float
    sample_area_max_x: float
    sample_area_max_y: float
    
    # Results
    samples: List[Dict[str, float]]  # List of sample dictionaries
    rms_error_before: float  # RMS error before refinement
    rms_error_after: float  # RMS error after refinement
    max_error_before: float
    max_error_after: float
    mean_error_before: float
    mean_error_after: float
    
    # Refined calibration parameters (affine transformation)
    refined_cam_x0: float
    refined_cam_y0: float
    refined_slm_x0: float
    refined_slm_y0: float
    refined_cam_x1: float
    refined_cam_y1: float
    refined_slm_x1: float
    refined_slm_y1: float
    refined_cam_x2: float
    refined_cam_y2: float
    refined_slm_x2: float
    refined_slm_y2: float
    
    # Additional metadata
    description: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FineTuningResult":
        """Create from dictionary loaded from JSON."""
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered_data)
    
    def get_improvement_percentage(self) -> float:
        """Calculate percentage improvement in RMS error."""
        if self.rms_error_before == 0:
            return 0.0
        improvement = (self.rms_error_before - self.rms_error_after) / self.rms_error_before
        return improvement * 100.0


class FineTuningManager:
    """Manager for SLM fine-tuning configurations."""
    
    def __init__(self, base_config_dir: Path):
        """Initialize the fine-tuning manager.
        
        Args:
            base_config_dir: Base SLM config directory (finetuning/ will be created inside)
        """
        self.base_dir = Path(base_config_dir)
        self.finetuning_dir = self.base_dir / "finetuning"
        self.finetuning_dir.mkdir(parents=True, exist_ok=True)
        
        # Cache of loaded fine-tuning results
        self._results_cache: Dict[str, FineTuningResult] = {}
        self._load_all_results()
    
    def _load_all_results(self) -> None:
        """Load all fine-tuning results from the finetuning directory."""
        self._results_cache.clear()
        
        for result_file in self.finetuning_dir.glob("*.json"):
            try:
                with open(result_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                result = FineTuningResult.from_dict(data)
                self._results_cache[result.name] = result
            except Exception as exc:
                logging.warning("Failed to load fine-tuning result %s: %s", result_file, exc)
    
    def generate_name(self) -> str:
        """Generate a datetime-based name for a new fine-tuning result."""
        now = datetime.now(timezone.utc)
        return now.strftime("finetuning_%Y%m%d_%H%M%S")
    
    def save_result(self, result: FineTuningResult) -> bool:
        """Save a fine-tuning result to disk.
        
        Args:
            result: The fine-tuning result to save
            
        Returns:
            True if successful, False otherwise
        """
        try:
            result_file = self.finetuning_dir / f"{result.name}.json"
            with open(result_file, 'w', encoding='utf-8') as f:
                json.dump(result.to_dict(), f, indent=2)
            
            # Update cache
            self._results_cache[result.name] = result
            logging.info("Saved fine-tuning result: %s", result.name)
            return True
        except Exception as exc:
            logging.error("Failed to save fine-tuning result %s: %s", result.name, exc)
            return False
    
    def load_result(self, name: str) -> Optional[FineTuningResult]:
        """Load a fine-tuning result by name.
        
        Args:
            name: Name of the fine-tuning result
            
        Returns:
            FineTuningResult if found, None otherwise
        """
        if name in self._results_cache:
            return self._results_cache[name]
        
        result_file = self.finetuning_dir / f"{name}.json"
        if not result_file.exists():
            return None
        
        try:
            with open(result_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            result = FineTuningResult.from_dict(data)
            self._results_cache[name] = result
            return result
        except Exception as exc:
            logging.error("Failed to load fine-tuning result %s: %s", name, exc)
            return None
    
    def delete_result(self, name: str) -> bool:
        """Delete a fine-tuning result.
        
        Args:
            name: Name of the fine-tuning result to delete
            
        Returns:
            True if successful, False otherwise
        """
        result_file = self.finetuning_dir / f"{name}.json"
        if not result_file.exists():
            logging.warning("Fine-tuning result %s does not exist", name)
            return False
        
        try:
            result_file.unlink()
            if name in self._results_cache:
                del self._results_cache[name]
            logging.info("Deleted fine-tuning result: %s", name)
            return True
        except Exception as exc:
            logging.error("Failed to delete fine-tuning result %s: %s", name, exc)
            return False
    
    def list_results(self) -> List[str]:
        """List all available fine-tuning results sorted by timestamp (newest first).
        
        Returns:
            List of result names
        """
        return sorted(self._results_cache.keys(), reverse=True)
    
    def get_result_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a fine-tuning result.
        
        Args:
            name: Name of the fine-tuning result
            
        Returns:
            Dictionary with metadata or None if not found
        """
        result = self.load_result(name)
        if not result:
            return None
        
        return {
            "name": result.name,
            "timestamp": result.timestamp,
            "base_config": result.base_config_name,
            "samples": result.sample_count,
            "rms_error_before": result.rms_error_before,
            "rms_error_after": result.rms_error_after,
            "improvement_pct": result.get_improvement_percentage(),
            "description": result.description,
        }


def compute_affine_transform(
    source_points: np.ndarray,
    target_points: np.ndarray
) -> Tuple[np.ndarray, float]:
    """Compute affine transformation from source to target points.
    
    Uses least-squares fitting to find the best affine transform that maps
    source points to target points.
    
    Args:
        source_points: Nx2 array of source coordinates (camera coords)
        target_points: Nx2 array of target coordinates (SLM coords)
        
    Returns:
        Tuple of (3x3 affine matrix, RMS error)
    """
    if len(source_points) < 3:
        raise ValueError("Need at least 3 points for affine transformation")
    
    # Build the system of equations for affine transform
    # [x', y', 1] = [a, b, c] @ [x, y, 1]
    #                [d, e, f]
    #                [0, 0, 1]
    
    num_points = len(source_points)
    
    # Create matrices for least squares: Ax = b
    # For x': x' = a*x + b*y + c
    # For y': y' = d*x + e*y + f
    
    A = np.zeros((num_points * 2, 6))
    b = np.zeros(num_points * 2)
    
    for i in range(num_points):
        x, y = source_points[i]
        x_prime, y_prime = target_points[i]
        
        # Equation for x'
        A[2*i, 0] = x
        A[2*i, 1] = y
        A[2*i, 2] = 1
        b[2*i] = x_prime
        
        # Equation for y'
        A[2*i+1, 3] = x
        A[2*i+1, 4] = y
        A[2*i+1, 5] = 1
        b[2*i+1] = y_prime
    
    # Solve least squares
    params, residuals, rank, s = np.linalg.lstsq(A, b, rcond=None)
    
    # Construct affine matrix
    affine_matrix = np.array([
        [params[0], params[1], params[2]],
        [params[3], params[4], params[5]],
        [0, 0, 1]
    ])
    
    # Compute RMS error
    transformed = transform_points(source_points, affine_matrix)
    errors = np.linalg.norm(transformed - target_points, axis=1)
    rms_error = np.sqrt(np.mean(errors ** 2))
    
    return affine_matrix, rms_error


def transform_points(points: np.ndarray, affine_matrix: np.ndarray) -> np.ndarray:
    """Apply affine transformation to points.
    
    Args:
        points: Nx2 array of points
        affine_matrix: 3x3 affine transformation matrix
        
    Returns:
        Nx2 array of transformed points
    """
    # Add homogeneous coordinate
    homogeneous = np.hstack([points, np.ones((len(points), 1))])
    
    # Apply transformation
    transformed = homogeneous @ affine_matrix.T
    
    # Remove homogeneous coordinate
    return transformed[:, :2]


def affine_matrix_to_legacy_params(
    affine_matrix: np.ndarray,
    reference_points: Optional[np.ndarray] = None
) -> Dict[str, float]:
    """Convert affine matrix to legacy 3-point calibration format.
    
    The legacy format uses three calibration points to define the transformation:
    (cam_x0, cam_y0) -> (slm_x0, slm_y0)
    (cam_x1, cam_y1) -> (slm_x1, slm_y1)
    (cam_x2, cam_y2) -> (slm_x2, slm_y2)
    
    Args:
        affine_matrix: 3x3 affine transformation matrix
        reference_points: Optional Nx2 array of camera points to use as basis.
                         If None, uses standard reference points based on the data range.
    
    Returns:
        Dictionary with legacy calibration parameters
    """
    if reference_points is None or len(reference_points) < 3:
        # Use standard reference points (origin and two unit vectors)
        # These serve as a generic basis
        camera_ref_points = np.array([
            [0.0, 0.0],      # Origin
            [100.0, 0.0],    # X-axis point
            [0.0, 100.0]     # Y-axis point
        ])
    else:
        # Use carefully selected points from the actual data to ensure
        # the calibration is representative of the sampled region
        
        # Strategy: Pick 3 well-distributed points from the sample set
        # 1. Point closest to centroid (central reference)
        # 2. Point furthest in +X direction from centroid
        # 3. Point furthest in +Y direction from centroid
        centroid = np.mean(reference_points, axis=0)
        
        # Find point closest to centroid
        distances_to_centroid = np.linalg.norm(reference_points - centroid, axis=1)
        center_idx = np.argmin(distances_to_centroid)
        
        # Find point furthest in +X from centroid
        x_distances = reference_points[:, 0] - centroid[0]
        x_idx = np.argmax(x_distances)
        
        # Find point furthest in +Y from centroid
        y_distances = reference_points[:, 1] - centroid[1]
        y_idx = np.argmax(y_distances)
        
        # Use these three points as reference
        selected_indices = [center_idx, x_idx, y_idx]
        camera_ref_points = reference_points[selected_indices]
        
        logging.info(
            "Fine-tuning: Using %d samples, selected 3 reference points from actual data:\n"
            "  Center: cam=(%.2f, %.2f)\n"
            "  +X:     cam=(%.2f, %.2f)\n"
            "  +Y:     cam=(%.2f, %.2f)",
            len(reference_points),
            camera_ref_points[0, 0], camera_ref_points[0, 1],
            camera_ref_points[1, 0], camera_ref_points[1, 1],
            camera_ref_points[2, 0], camera_ref_points[2, 1],
        )
    
    # Transform reference points to get SLM coordinates
    slm_points = transform_points(camera_ref_points, affine_matrix)
    
    legacy_params = {
        "cam_x0": float(camera_ref_points[0, 0]),
        "cam_y0": float(camera_ref_points[0, 1]),
        "slm_x0": float(slm_points[0, 0]),
        "slm_y0": float(slm_points[0, 1]),
        "cam_x1": float(camera_ref_points[1, 0]),
        "cam_y1": float(camera_ref_points[1, 1]),
        "slm_x1": float(slm_points[1, 0]),
        "slm_y1": float(slm_points[1, 1]),
        "cam_x2": float(camera_ref_points[2, 0]),
        "cam_y2": float(camera_ref_points[2, 1]),
        "slm_x2": float(slm_points[2, 0]),
        "slm_y2": float(slm_points[2, 1]),
    }
    
    logging.info(
        "Legacy affine parameters:\n"
        "  Point 0: cam=(%.2f, %.2f) -> slm=(%.2f, %.2f)\n"
        "  Point 1: cam=(%.2f, %.2f) -> slm=(%.2f, %.2f)\n"
        "  Point 2: cam=(%.2f, %.2f) -> slm=(%.2f, %.2f)",
        legacy_params["cam_x0"], legacy_params["cam_y0"],
        legacy_params["slm_x0"], legacy_params["slm_y0"],
        legacy_params["cam_x1"], legacy_params["cam_y1"],
        legacy_params["slm_x1"], legacy_params["slm_y1"],
        legacy_params["cam_x2"], legacy_params["cam_y2"],
        legacy_params["slm_x2"], legacy_params["slm_y2"],
    )
    
    return legacy_params
