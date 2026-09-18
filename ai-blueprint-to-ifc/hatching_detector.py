from hatchfinder import HatchFinder
from tqdm import tqdm
from typing import Any
from PIL import Image, ImageDraw
from pathlib import Path
import torch
import torch.nn.functional as F
import numpy as np
import hashlib
from scipy import ndimage
from joblib import Memory

import debug_manager

from logger import setup_logger
from config import settings, WallDetectionProfile
from pdf_prcoessor import PdfProcessor
from matrix_processor import MatrixProcessor
from polygon_converter import RegionPolygon
from obb_converter import RegionOBB

logger = setup_logger(__name__)

memory = Memory("cache/hatching_detector", verbose=0)

class HatchingDetector:
    def __init__(
        self,
        detection_settings: WallDetectionProfile,
        pdf_processor: PdfProcessor,
    ) -> None:
        self.detection_settings = detection_settings
        self.pdf_processor = pdf_processor
        self.hatch_finder = HatchFinder(load_model_path=settings.HATCH_FINDER_MODEL, device=settings.DEVICE)
        self.matrix_processor = MatrixProcessor()

        self.memory = memory
        

    def get_walls(
        self,
        tiles: list[dict[str, Any]],
        legend_entries: list[dict[str, Any]],
        drawing_index: int = 0,
    ):
        """
        Возвращает стены в глобальных пиксельных координатах изображения PDF.
        """
        detection = self.detection_settings
        tile_size = self.detection_settings.image_size
        overlap = self.detection_settings.tile_overlap / 2

        width = max(tile["x1"] for tile in tiles)
        height = max(tile["y1"] for tile in tiles)

        channel_id_2_entry_id = self._get_channel_id_2_entry_id_dict(legend_entries)

        # Если не найдено ни одной стены в легенде
        if not channel_id_2_entry_id:
            return {"walls": [], "matrix_debug_object": None}

        result_matrix = torch.zeros((len(channel_id_2_entry_id), height, width), device=settings.DEVICE, dtype=torch.float16)
        mask_image = self._create_inner_mask(tile_size, tile_size, overlap)

        if settings.USE_TILES_CACHE:
            self._calculate_symbols_hash(legend_entries)
            mask_hash = self._image_hash(mask_image)
            model_version = self.get_model_version(settings.HATCH_FINDER_MODEL)

        for tile_index, tile in enumerate(tqdm(tiles, desc="Обработка плиток", unit="tile")):
            if settings.USE_TILES_CACHE:
                tile_hash = self._image_hash(tile["image"])
            for channel_id, entry_id in channel_id_2_entry_id.items():
                legend_entry = legend_entries[entry_id]
                accumulated_matrix = torch.zeros((tile_size, tile_size), device=settings.DEVICE, dtype=torch.float16)
                for symbol in legend_entry["legend_symbols"]:
                    symbol_image = symbol["image"]
                    if settings.USE_TILES_CACHE:
                        tile_matrix = self.infer_tile(tile_hash, mask_hash, symbol["hash"], model_version, tile["image"], mask_image, symbol_image)
                    else:
                        tile_matrix = (
                            self.hatch_finder
                            .infer(tile["image"], mask_image, symbol_image)
                            .squeeze()
                            .to(
                                device=accumulated_matrix.device,
                                dtype=accumulated_matrix.dtype,
                            )
                        )
                    accumulated_matrix = torch.maximum(accumulated_matrix, tile_matrix)
                self.insert_patch(result_matrix, accumulated_matrix, channel_id, tile["x0"], tile["y0"], overlap)

        result_matrix = self.resize_probability_matrix(result_matrix, settings.DPI, settings.MATRIX_COMPRESSION_DPI)

        if settings.SAVE_PROBABILITY_HEATMAPS:
            debug_manager.save_probability_heatmaps_by_material(
                folder_name="full",
                drawing_index=drawing_index,
                matrix=result_matrix,
                channel_id_2_entry_id=channel_id_2_entry_id,
                pdf_processor=self.pdf_processor,
                legends=legend_entries,
                source_dpi=settings.MATRIX_COMPRESSION_DPI,
                target_dpi=settings.PROBABILITY_HEATMAP_DEBUG_DPI,
                threshold=settings.HATCHING_PIXELS_CONFIDENCE,
            )

        result_matrix = self._keep_channel_max(result_matrix)
        channels = result_matrix.shape[0]
        class_map = self._scores_to_class_map(
            result_matrix,
            settings.HATCHING_PIXELS_CONFIDENCE,
        )
        del result_matrix
        result_matrix_binary = self._class_map_to_bool(class_map, channels)
        del class_map

        matrix_result = self.matrix_processor.process(result_matrix_binary)
        polygons_and_obb: list[RegionPolygon | RegionOBB] = matrix_result["geometry_parts"]
        result_matrix_binary = matrix_result["matrix"]

        for entry in polygons_and_obb:
            entry.entry_index = channel_id_2_entry_id[entry.channel_index]

        debug_manager.save_torch_matrix(result_matrix_binary, Path(f"{settings.DEBUG_DIR}/{drawing_index}/matrix.pt"))

        debug_matrix = self.resize_binary_matrix(
            result_matrix_binary,
            source_dpi=settings.MATRIX_COMPRESSION_DPI,
            target_dpi=settings.DEBUG_DPI,
        )

        del result_matrix_binary

        return {"walls": polygons_and_obb, "matrix_debug_object": {"matrix": debug_matrix.cpu(), "channel_id_2_entry_id": channel_id_2_entry_id}}

    def _get_channel_id_2_entry_id_dict(self, legends: list[dict[str, Any]]):
        channel_id_2_entry_id = {}
        channel_id_current = 0
        for e_i, entry in enumerate(legends):
            if not entry.get("element_type", None) == "not_wall":
                channel_id_2_entry_id[channel_id_current] = e_i
                channel_id_current += 1

        return channel_id_2_entry_id

    def infer_tile(
            self, 
            tile_hash: str,
            mask_hash: str,
            symbol_hash: str,
            model_version: str,
            tile_image,
            mask_image,
            symbol_image,):
        matrix = self._infer_cached(
            tile_hash,
            mask_hash,
            symbol_hash,
            model_version,
            self.hatch_finder.infer,
            tile_image,
            mask_image,
            symbol_image,
        )
        return torch.from_numpy(matrix).to(settings.DEVICE)

    @staticmethod
    @memory.cache(
        ignore=[
            "infer_function",
            "tile_image",
            "mask_image",
            "symbol_image",
        ]
    )
    def _infer_cached(
        tile_hash,
        mask_hash,
        symbol_hash,
        model_version,
        infer_function,
        tile_image,
        mask_image,
        symbol_image,
    ):
        result = infer_function(
            tile_image,
            mask_image,
            symbol_image,
        ).squeeze()

        return result.detach().cpu().to(torch.float16).numpy()

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

    def resize_binary_matrix(
        self,
        matrix: torch.Tensor,
        source_dpi: int,
        target_dpi: int,
    ) -> torch.Tensor:
        channels, height, width = matrix.shape
        scale = target_dpi / source_dpi

        target_height = round(height * scale)
        target_width = round(width * scale)

        result = torch.empty(
            (channels, target_height, target_width),
            dtype=torch.bool,
            device=matrix.device,
        )

        for channel in range(channels):
            resized = F.interpolate(
                matrix[channel][None, None].to(torch.float32),
                size=(target_height, target_width),
                mode="nearest",
            )[0, 0]

            result[channel].copy_(resized >= 0.5)

        return result

    

    @staticmethod
    def _image_hash(image: Image.Image) -> str:
        digest = hashlib.sha256()
        digest.update(image.mode.encode())
        digest.update(str(image.size).encode())
        digest.update(image.tobytes())
        return digest.hexdigest()

    def _calculate_symbols_hash(self, legends: list[dict[str, Any]]):
        for entry_index, legend_entry in enumerate(legends):
            for symbol in legend_entry["legend_symbols"]:
                symbol["hash"] = self._image_hash(symbol["image"])

    @staticmethod
    def get_model_version(model_path: str | Path) -> str:
        path = Path(model_path).resolve()
        stat = path.stat()

        return f"{path}:{stat.st_size}:{stat.st_mtime_ns}"

    def resize_probability_matrix(
        self,
        matrix: torch.Tensor,
        source_dpi: int,
        target_dpi: int,
    ) -> torch.Tensor:
        channels, height, width = matrix.shape
        scale = target_dpi / source_dpi

        target_height = round(height * scale)
        target_width = round(width * scale)

        result = torch.empty(
            (channels, target_height, target_width),
            dtype=matrix.dtype,
            device=matrix.device,
        )

        for channel in range(channels):
            resized = F.interpolate(
                matrix[channel][None, None],
                size=(target_height, target_width),
                mode="area",
            )[0, 0]

            result[channel].copy_(resized)

        return result
