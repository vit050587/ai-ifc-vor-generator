from hatchfinder import HatchFinder
from tqdm import tqdm
from typing import Any
from PIL import Image, ImageDraw
import torch
import numpy as np
from scipy import ndimage

from logger import setup_logger
from config import settings, WallDetectionProfile

logger = setup_logger(__name__)



class HatchingDetector:
    def __init__(self, detection_settings: WallDetectionProfile) -> None:
        self.detection_settings = detection_settings
        self.hatch_finder = HatchFinder(load_model_path=settings.HATCH_FINDER_MODEL, device=settings.DEVICE)
        

    def get_walls(self, tiles: list[dict[str, Any]], legend_entries: list[dict[str, Any]]):
        """
        Возвращает стены в глобальных пиксельных координатах изображения PDF.
        """
        walls = []
        detection = self.detection_settings
        tile_size = self.detection_settings.image_size
        overlap = self.detection_settings.tile_overlap / 2

        width = max(tile["x1"] for tile in tiles)
        height = max(tile["y1"] for tile in tiles)

        result_matrix = torch.zeros((len(legend_entries), height, width), device=settings.DEVICE, dtype=torch.float16)
        mask_image = self._create_inner_mask(tile_size, tile_size, overlap)

        for tile_index, tile in enumerate(tqdm(tiles, desc="Обработка плиток", unit="tile")):
            for entry_index, legend_entry in enumerate(legend_entries):
                accumulated_matrix = torch.zeros((tile_size, tile_size), device=settings.DEVICE)
                for symbol in legend_entry["legend_symbols"]:
                    symbol_image = symbol["image"]
                    tile_matrix = self.hatch_finder.infer(tile["image"], mask_image, symbol_image).squeeze()
                    accumulated_matrix = torch.maximum(accumulated_matrix, tile_matrix)
                self.insert_patch(result_matrix, accumulated_matrix, entry_index, tile["x0"], tile["y0"], overlap)

        result_matrix = self._keep_channel_max(result_matrix)
        channels = result_matrix.shape[0]
        class_map = self._scores_to_class_map(
            result_matrix,
            settings.HATCHING_PIXELS_CONFIDENCE,
        )
        del result_matrix
        result_matrix_binary = self._class_map_to_bool(class_map, channels)
        del class_map

        min_pixels = self.scale_area_threshold(settings.MIN_PIXELS_AREA_REMOVE, settings.DPI)
        result_matrix_binary = self.remove_small_regions(result_matrix_binary, min_pixels)

        return walls

    def _create_inner_mask(
        self,
        width: int,
        height: int,
        margin_percent: float,
    ) -> Image.Image:
        """Создает маску"""
        mask = Image.new("L", (width, height), 0)

        margin_x = int(width * margin_percent / 100)
        margin_y = int(height * margin_percent / 100)

        draw = ImageDraw.Draw(mask)
        draw.rectangle(
            (
                margin_x,
                margin_y,
                width - margin_x - 1,
                height - margin_y - 1,
            ),
            fill=255,
        )

        return mask

    def insert_patch(
        self,
        matrix: torch.Tensor,
        patch: torch.Tensor,
        channel: int,
        x0: int,
        y0: int,
        margin_percent: float,
    ) -> None:
        """Insert patch into a channel, ignoring margins around its edges."""
        h, w = patch.shape

        margin_y = int(h * margin_percent / 100)
        margin_x = int(w * margin_percent / 100)

        matrix[
            channel,
            y0 + margin_y:y0 + h - margin_y,
            x0 + margin_x:x0 + w - margin_x,
        ] = patch[
            margin_y:h - margin_y,
            margin_x:w - margin_x,
        ]

    def _keep_channel_max(
        self,
        matrix: torch.Tensor,
        chunk_size: int = 1_000_000,
    ) -> torch.Tensor:
        """Оставляет максимальное значение канала в каждой позиции, остальные зануляет."""
        channels = matrix.shape[0]
        flat_matrix = matrix.view(channels, -1)

        with torch.no_grad():
            for start in range(0, flat_matrix.shape[1], chunk_size):
                chunk = flat_matrix[:, start:start + chunk_size]
                max_values, max_indices = chunk.max(dim=0)

                chunk.zero_()
                chunk.scatter_(
                    0,
                    max_indices.unsqueeze(0),
                    max_values.unsqueeze(0),
                )

        return matrix

    def _scores_to_class_map(
        self,
        matrix: torch.Tensor,
        threshold: float,
        chunk_size: int = 1_000_000,
    ) -> torch.Tensor:
        """Преобразует [C, H, W] scores в uint8-карту классов."""
        channels, height, width = matrix.shape
        flat_matrix = matrix.view(channels, -1)
        pixel_count = flat_matrix.shape[1]
        background_class = 255

        class_map = torch.empty(
            pixel_count,
            dtype=torch.uint8,
            device=matrix.device,
        )

        with torch.no_grad():
            for start in range(0, pixel_count, chunk_size):
                end = min(start + chunk_size, pixel_count)
                max_values, max_indices = flat_matrix[:, start:end].max(dim=0)
                max_indices.masked_fill_(max_values < threshold, background_class)
                class_map[start:end].copy_(max_indices)

        return class_map.view(height, width)

    def _class_map_to_bool(
        self,
        class_map: torch.Tensor,
        channels: int,
        chunk_size: int = 1_000_000,
    ) -> torch.Tensor:
        """Преобразует uint8-карту классов в [C, H, W] bool-матрицу."""
        height, width = class_map.shape
        flat_class_map = class_map.view(-1)
        pixel_count = flat_class_map.shape[0]
        background_class = 255

        with torch.no_grad():
            result = torch.zeros(
                (channels, pixel_count),
                dtype=torch.bool,
                device=class_map.device,
            )

            for start in range(0, pixel_count, chunk_size):
                end = min(start + chunk_size, pixel_count)
                class_chunk = flat_class_map[start:end]
                valid = class_chunk != background_class

                if valid.any():
                    pixel_indices = torch.arange(
                        start,
                        end,
                        device=class_map.device,
                    )[valid]
                    result[class_chunk[valid].long(), pixel_indices] = True

        return result.view(channels, height, width)
    
    def scale_area_threshold(
        self,
        base_pixels: int,
        dpi: int
    ) -> int:
        """Scale pixel area threshold relative to a reference DPI."""
        return int(base_pixels * (dpi / 900) ** 2)

    def remove_small_regions(
        self,
        matrix: torch.Tensor,
        min_pixels: int,
    ) -> torch.Tensor:
        """Remove connected regions smaller than min_pixels from each channel."""
        device = matrix.device
        result = matrix.cpu().numpy()

        structure = ndimage.generate_binary_structure(2, 2)

        for channel in range(result.shape[0]):
            labels, _ = ndimage.label(
                result[channel],
                structure=structure,
            )

            sizes = np.bincount(labels.ravel())

            remove = sizes < min_pixels
            remove[0] = False

            result[channel][remove[labels]] = False

        return torch.from_numpy(result).to(device)