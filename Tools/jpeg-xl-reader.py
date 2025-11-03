"""JPEG XL Image Viewer with navigation, animation, and export capabilities."""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, cast

try:
    import dearpygui.dearpygui as dpg  # type: ignore[import]
except ImportError as exc:
    raise RuntimeError("DearPyGui must be installed to run the JPEG XL viewer") from exc


@dataclass
class ViewerState:
    """State management for the JPEG XL viewer."""
    
    current_directory: Path = field(default_factory=lambda: Path.cwd())
    image_files: List[Path] = field(default_factory=list)
    current_index: int = 0
    
    # Display settings
    zoom_level: float = 1.0
    display_width: int = 1200
    display_height: int = 800
    
    # Animation settings
    is_animating: bool = False
    animation_fps: float = 10.0
    last_frame_time: float = 0.0
    
    # Image data
    current_image: Optional[Any] = None  # numpy array
    texture_data: Optional[bytes] = None
    texture_tag: str = "image_texture"
    
    # Mouse hover data
    mouse_x: int = -1
    mouse_y: int = -1
    pixel_value: str = "N/A"


class JpegXLReader:
    """Reader for JPEG XL images using jxlpy."""
    
    @staticmethod
    def can_decode() -> bool:
        """Check if jxlpy is available."""
        try:
            importlib.import_module("jxlpy")
            return True
        except ImportError:
            return False
    
    @staticmethod
    def decode(file_path: Path) -> Tuple[Any, str, int]:
        """
        Decode a JPEG XL image.
        
        Returns:
            Tuple of (numpy_array, colorspace, bit_depth)
        """
        try:
            jxlpy = importlib.import_module("jxlpy")
            np = importlib.import_module("numpy")
        except ImportError as exc:
            raise RuntimeError(
                "jxlpy and numpy are required. Install with: pip install jxlpy numpy"
            ) from exc
        
        data = file_path.read_bytes()
        decoder = jxlpy.JXLPyDecoder()
        
        try:
            decoder.decode(data)
            frame = decoder.get_frame(0)
            
            colorspace = decoder.colorspace
            bit_depth = decoder.bit_depth
            width = decoder.width
            height = decoder.height
            
            # Convert bytes to numpy array
            if "A" in colorspace:  # Has alpha channel
                channels = 4
            elif colorspace == "RGB":
                channels = 3
            elif colorspace == "L":
                channels = 1
            else:
                channels = 3  # Default to RGB
            
            dtype = np.uint16 if bit_depth > 8 else np.uint8
            image_array = np.frombuffer(frame, dtype=dtype)
            
            if channels == 1:
                image_array = image_array.reshape((height, width))
            else:
                image_array = image_array.reshape((height, width, channels))
            
            return image_array, colorspace, bit_depth
        
        finally:
            try:
                decoder.close()
            except Exception:
                pass


class ImageProcessor:
    """Process images for display in DearPyGui."""
    
    @staticmethod
    def normalize_to_uint8(image: Any) -> Any:
        """Normalize image data to uint8 for display."""
        np = importlib.import_module("numpy")
        
        if image.dtype == np.uint8:
            return image
        
        if image.dtype == np.uint16:
            # Normalize 16-bit to 8-bit
            return (image / 256).astype(np.uint8)
        
        # For floating point or other types
        img_min = image.min()
        img_max = image.max()
        if img_max > img_min:
            normalized = ((image - img_min) / (img_max - img_min) * 255)
            return normalized.astype(np.uint8)
        return np.zeros_like(image, dtype=np.uint8)
    
    @staticmethod
    def ensure_rgba(image: Any) -> Any:
        """Convert image to RGBA format for DearPyGui texture."""
        np = importlib.import_module("numpy")
        
        if image.ndim == 2:  # Grayscale
            height, width = image.shape
            rgba = np.zeros((height, width, 4), dtype=np.uint8)
            rgba[:, :, 0] = image  # R
            rgba[:, :, 1] = image  # G
            rgba[:, :, 2] = image  # B
            rgba[:, :, 3] = 255    # A
            return rgba
        
        elif image.ndim == 3:
            height, width, channels = image.shape
            
            if channels == 1:  # Single channel
                rgba = np.zeros((height, width, 4), dtype=np.uint8)
                rgba[:, :, 0] = image[:, :, 0]
                rgba[:, :, 1] = image[:, :, 0]
                rgba[:, :, 2] = image[:, :, 0]
                rgba[:, :, 3] = 255
                return rgba
            
            elif channels == 3:  # RGB
                rgba = np.zeros((height, width, 4), dtype=np.uint8)
                rgba[:, :, :3] = image
                rgba[:, :, 3] = 255
                return rgba
            
            elif channels == 4:  # RGBA
                return image
        
        raise ValueError(f"Unsupported image shape: {image.shape}")
    
    @staticmethod
    def resize_for_display(image: Any, max_width: int, max_height: int, zoom: float = 1.0) -> Any:
        """Resize image to fit display area with zoom applied."""
        cv2 = importlib.import_module("cv2")
        
        height, width = image.shape[:2]
        
        # Apply zoom
        target_width = int(width * zoom)
        target_height = int(height * zoom)
        
        # Fit within display bounds
        scale_w = max_width / target_width if target_width > max_width else 1.0
        scale_h = max_height / target_height if target_height > max_height else 1.0
        scale = min(scale_w, scale_h, 1.0)  # Don't upscale beyond zoom
        
        final_width = int(target_width * scale)
        final_height = int(target_height * scale)
        
        if final_width != width or final_height != height:
            return cv2.resize(image, (final_width, final_height), interpolation=cv2.INTER_LINEAR)
        
        return image


class JpegXLViewer:
    """Main viewer application."""
    
    def __init__(self, initial_directory: Optional[Path] = None):
        self.state = ViewerState()
        if initial_directory:
            self.state.current_directory = initial_directory
        
        self._setup_logging()
        self._check_dependencies()
    
    def _setup_logging(self) -> None:
        """Configure logging."""
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.logger = logging.getLogger(__name__)
    
    def _check_dependencies(self) -> None:
        """Verify required dependencies are available."""
        missing = []
        
        try:
            importlib.import_module("jxlpy")
        except ImportError:
            missing.append("jxlpy")
        
        try:
            importlib.import_module("numpy")
        except ImportError:
            missing.append("numpy")
        
        try:
            importlib.import_module("cv2")
        except ImportError:
            missing.append("opencv-python")
        
        if missing:
            raise RuntimeError(
                f"Missing dependencies: {', '.join(missing)}\n"
                f"Install with: pip install {' '.join(missing)}"
            )
    
    def scan_directory(self, directory: Path) -> List[Path]:
        """Scan directory for JPEG XL files."""
        jxl_files = []
        
        if not directory.exists():
            self.logger.warning(f"Directory does not exist: {directory}")
            return jxl_files
        
        if not directory.is_dir():
            self.logger.warning(f"Path is not a directory: {directory}")
            return jxl_files
        
        # Find all .jxl files
        for file_path in sorted(directory.glob("*.jxl")):
            if file_path.is_file():
                jxl_files.append(file_path)
        
        self.logger.info(f"Found {len(jxl_files)} JPEG XL files in {directory}")
        return jxl_files
    
    def load_image(self, index: int) -> bool:
        """Load image at the specified index."""
        if not self.state.image_files or index < 0 or index >= len(self.state.image_files):
            return False
        
        file_path = self.state.image_files[index]
        
        try:
            # Decode JPEG XL
            image_array, colorspace, bit_depth = JpegXLReader.decode(file_path)
            
            # Normalize to uint8
            image_uint8 = ImageProcessor.normalize_to_uint8(image_array)
            
            # Store original image
            self.state.current_image = image_uint8
            self.state.current_index = index
            
            self.logger.info(
                f"Loaded {file_path.name}: {image_array.shape}, "
                f"colorspace={colorspace}, bit_depth={bit_depth}"
            )
            
            # Update display
            self._update_texture()
            self._update_filename_display()
            
            return True
        
        except Exception as exc:
            self.logger.error(f"Failed to load {file_path}: {exc}")
            return False
    
    def _update_texture(self) -> None:
        """Update the DearPyGui texture with current image."""
        if self.state.current_image is None:
            return
        
        # Resize for display with zoom
        display_image = ImageProcessor.resize_for_display(
            self.state.current_image,
            self.state.display_width,
            self.state.display_height,
            self.state.zoom_level
        )
        
        # Ensure RGBA format
        rgba_image = ImageProcessor.ensure_rgba(display_image)
        
        height, width = rgba_image.shape[:2]
        
        # Flatten to bytes and normalize to [0, 1] for DearPyGui
        np = importlib.import_module("numpy")
        texture_data = rgba_image.astype(np.float32) / 255.0
        flat_data = texture_data.flatten().tolist()
        
        # Update or create texture
        if dpg.does_item_exist(self.state.texture_tag):
            dpg.set_value(self.state.texture_tag, flat_data)
        else:
            with dpg.texture_registry():
                dpg.add_raw_texture(
                    width=width,
                    height=height,
                    default_value=flat_data,
                    format=dpg.mvFormat_Float_rgba,
                    tag=self.state.texture_tag
                )
        
        # Update image widget size
        if dpg.does_item_exist("image_widget"):
            dpg.configure_item("image_widget", width=width, height=height)
    
    def _update_filename_display(self) -> None:
        """Update the filename label."""
        if dpg.does_item_exist("filename_label"):
            if self.state.image_files and 0 <= self.state.current_index < len(self.state.image_files):
                filename = self.state.image_files[self.state.current_index].name
                index_info = f"[{self.state.current_index + 1}/{len(self.state.image_files)}]"
                dpg.set_value("filename_label", f"{filename} {index_info}")
            else:
                dpg.set_value("filename_label", "No image loaded")
    
    def next_image(self) -> None:
        """Load the next image."""
        if not self.state.image_files:
            return
        
        next_index = (self.state.current_index + 1) % len(self.state.image_files)
        self.load_image(next_index)
    
    def previous_image(self) -> None:
        """Load the previous image."""
        if not self.state.image_files:
            return
        
        prev_index = (self.state.current_index - 1) % len(self.state.image_files)
        self.load_image(prev_index)
    
    def set_zoom(self, zoom: float) -> None:
        """Set zoom level and update display."""
        self.state.zoom_level = max(0.1, min(10.0, zoom))
        self._update_texture()
        
        if dpg.does_item_exist("zoom_slider"):
            dpg.set_value("zoom_slider", self.state.zoom_level)
    
    def export_as_png(self, output_path: Path) -> bool:
        """Export current image as PNG."""
        if self.state.current_image is None:
            self.logger.warning("No image loaded to export")
            return False
        
        try:
            cv2 = importlib.import_module("cv2")
            
            # Convert RGB to BGR for OpenCV
            image = self.state.current_image
            if image.ndim == 3 and image.shape[2] >= 3:
                image_bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            else:
                image_bgr = image
            
            cv2.imwrite(str(output_path), image_bgr)
            self.logger.info(f"Exported image to {output_path}")
            return True
        
        except Exception as exc:
            self.logger.error(f"Failed to export image: {exc}")
            return False
    
    def toggle_animation(self) -> None:
        """Toggle animation playback."""
        self.state.is_animating = not self.state.is_animating
        
        if dpg.does_item_exist("play_button"):
            dpg.set_item_label("play_button", "Pause" if self.state.is_animating else "Play")
    
    def update_animation(self) -> None:
        """Update animation frame if playing."""
        if not self.state.is_animating or not self.state.image_files:
            return
        
        current_time = time.time()
        frame_interval = 1.0 / self.state.animation_fps
        
        if current_time - self.state.last_frame_time >= frame_interval:
            self.next_image()
            self.state.last_frame_time = current_time
    
    def update_mouse_info(self, mouse_pos: Tuple[int, int]) -> None:
        """Update pixel information at mouse position."""
        if self.state.current_image is None:
            self.state.pixel_value = "N/A"
            return
        
        # Get image widget position and size
        if not dpg.does_item_exist("image_widget"):
            return
        
        img_pos = dpg.get_item_pos("image_widget")
        img_state = dpg.get_item_state("image_widget")
        
        # Calculate relative position
        rel_x = mouse_pos[0] - img_pos[0]
        rel_y = mouse_pos[1] - img_pos[1]
        
        # Map to original image coordinates
        display_image = ImageProcessor.resize_for_display(
            self.state.current_image,
            self.state.display_width,
            self.state.display_height,
            self.state.zoom_level
        )
        
        height, width = display_image.shape[:2]
        
        if 0 <= rel_x < width and 0 <= rel_y < height:
            self.state.mouse_x = int(rel_x / self.state.zoom_level)
            self.state.mouse_y = int(rel_y / self.state.zoom_level)
            
            # Get pixel value from original image
            orig_height, orig_width = self.state.current_image.shape[:2]
            
            if 0 <= self.state.mouse_x < orig_width and 0 <= self.state.mouse_y < orig_height:
                pixel = self.state.current_image[self.state.mouse_y, self.state.mouse_x]
                
                if self.state.current_image.ndim == 2:
                    self.state.pixel_value = f"I: {pixel}"
                elif self.state.current_image.ndim == 3:
                    if self.state.current_image.shape[2] == 3:
                        self.state.pixel_value = f"R: {pixel[0]}, G: {pixel[1]}, B: {pixel[2]}"
                    elif self.state.current_image.shape[2] == 4:
                        self.state.pixel_value = f"R: {pixel[0]}, G: {pixel[1]}, B: {pixel[2]}, A: {pixel[3]}"
                
                # Update display
                if dpg.does_item_exist("mouse_info_label"):
                    dpg.set_value(
                        "mouse_info_label",
                        f"Position: ({self.state.mouse_x}, {self.state.mouse_y}) | {self.state.pixel_value}"
                    )
        else:
            if dpg.does_item_exist("mouse_info_label"):
                dpg.set_value("mouse_info_label", "Position: N/A")
    
    def create_ui(self) -> None:
        """Create the DearPyGui interface."""
        dpg.create_context()
        dpg.create_viewport(
            title="JPEG XL Image Viewer",
            width=1400,
            height=900
        )
        
        # Callbacks
        def on_folder_select(sender, app_data):
            selections = app_data.get("selections", {})
            if selections:
                folder_path = Path(list(selections.values())[0])
                self.state.current_directory = folder_path
                self.state.image_files = self.scan_directory(folder_path)
                
                if self.state.image_files:
                    self.load_image(0)
                else:
                    self.logger.warning(f"No JPEG XL files found in {folder_path}")
                
                dpg.set_value("directory_label", f"Directory: {folder_path}")
        
        def on_folder_button():
            if dpg.does_item_exist("folder_dialog"):
                dpg.show_item("folder_dialog")
        
        def on_previous():
            self.previous_image()
        
        def on_next():
            self.next_image()
        
        def on_play():
            self.toggle_animation()
        
        def on_zoom_changed(sender, app_data):
            self.set_zoom(app_data)
        
        def on_zoom_in():
            self.set_zoom(self.state.zoom_level * 1.2)
        
        def on_zoom_out():
            self.set_zoom(self.state.zoom_level / 1.2)
        
        def on_zoom_reset():
            self.set_zoom(1.0)
        
        def on_fps_changed(sender, app_data):
            self.state.animation_fps = max(0.1, min(60.0, app_data))
        
        def on_export():
            if dpg.does_item_exist("export_dialog"):
                dpg.show_item("export_dialog")
        
        def on_export_confirm(sender, app_data):
            selections = app_data.get("selections", {})
            if selections:
                output_path = Path(list(selections.values())[0])
                if not output_path.suffix:
                    output_path = output_path.with_suffix(".png")
                self.export_as_png(output_path)
        
        def on_image_hover(sender, app_data):
            mouse_pos = dpg.get_mouse_pos()
            self.update_mouse_info(mouse_pos)
        
        # File dialogs
        with dpg.file_dialog(
            directory_selector=True,
            show=False,
            callback=on_folder_select,
            tag="folder_dialog",
            width=700,
            height=400
        ):
            dpg.add_file_extension(".*", color=(255, 255, 255, 255))
        
        with dpg.file_dialog(
            directory_selector=False,
            show=False,
            callback=on_export_confirm,
            tag="export_dialog",
            width=700,
            height=400,
            default_filename="exported_image.png"
        ):
            dpg.add_file_extension(".png", color=(0, 255, 0, 255))
            dpg.add_file_extension(".*", color=(255, 255, 255, 255))
        
        # Main window
        with dpg.window(label="JPEG XL Viewer", tag="primary_window", no_close=True):
            
            # Top bar - File info
            with dpg.group(horizontal=True):
                dpg.add_text("No image loaded", tag="filename_label")
            
            dpg.add_separator()
            
            # Control panel
            with dpg.collapsing_header(label="Controls", default_open=True):
                
                # Directory selection
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Select Folder", callback=on_folder_button, width=120)
                    dpg.add_text(
                        f"Directory: {self.state.current_directory}",
                        tag="directory_label"
                    )
                
                dpg.add_separator()
                
                # Navigation controls
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Previous", callback=on_previous, width=100)
                    dpg.add_button(label="Next", callback=on_next, width=100)
                    dpg.add_button(label="Play", callback=on_play, tag="play_button", width=100)
                    dpg.add_text("FPS:")
                    dpg.add_slider_float(
                        default_value=10.0,
                        min_value=0.1,
                        max_value=60.0,
                        callback=on_fps_changed,
                        width=150
                    )
                
                dpg.add_separator()
                
                # Zoom controls
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Zoom In (+)", callback=on_zoom_in, width=100)
                    dpg.add_button(label="Zoom Out (-)", callback=on_zoom_out, width=100)
                    dpg.add_button(label="Reset (1:1)", callback=on_zoom_reset, width=100)
                    dpg.add_text("Zoom:")
                    dpg.add_slider_float(
                        default_value=1.0,
                        min_value=0.1,
                        max_value=10.0,
                        callback=on_zoom_changed,
                        tag="zoom_slider",
                        width=150
                    )
                
                dpg.add_separator()
                
                # Export controls
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Export as PNG", callback=on_export, width=150)
            
            dpg.add_separator()
            
            # Mouse information
            dpg.add_text("Position: N/A", tag="mouse_info_label")
            
            dpg.add_separator()
            
            # Image display area (scrollable)
            with dpg.child_window(
                width=-1,
                height=-1,
                border=True,
                tag="image_window"
            ):
                dpg.add_image(
                    self.state.texture_tag,
                    tag="image_widget"
                )
                
                # Mouse hover handler
                with dpg.item_handler_registry(tag="image_handler"):
                    dpg.add_item_hover_handler(callback=on_image_hover)
                
                dpg.bind_item_handler_registry("image_widget", "image_handler")
        
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("primary_window", True)
        
        # Initial scan
        self.state.image_files = self.scan_directory(self.state.current_directory)
        if self.state.image_files:
            self.load_image(0)
    
    def run(self) -> None:
        """Run the main application loop."""
        self.create_ui()
        
        try:
            while dpg.is_dearpygui_running():
                # Update animation if active
                self.update_animation()
                
                # Render frame
                dpg.render_dearpygui_frame()
        
        finally:
            dpg.destroy_context()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="JPEG XL Image Viewer with navigation, animation, and export capabilities."
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Initial directory to scan for JPEG XL images (default: current directory)."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging output."
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main entry point."""
    args = parse_args(argv)
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        viewer = JpegXLViewer(initial_directory=args.directory)
        viewer.run()
        return 0
    
    except Exception as exc:
        logging.error(f"Fatal error: {exc}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
