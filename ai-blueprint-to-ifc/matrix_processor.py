import torch
from pathlib import Path
import cv2
import time
import numpy as np

from contour_adjacency import (
    ContourAdjacency,
    find_contour_boundaries,
)
from contours_processor import RegionContour, add_empty_holes_channel, extract_and_score_contours
from matrix_region_editor import MatrixRegionEditor
from polygon_converter import PolygonConverter
from obb_converter import OBBConverter
import reassign_regions

from config import settings

class MatrixProcessor:
    def __init__(self) -> None:
        self.polygon_converter = PolygonConverter()
        self.obb_converter = OBBConverter()
    def process(
        self,
        matrix: torch.Tensor,
    ) -> dict:
        start_time = time.time()

        channel_count = matrix.shape[0]
        matrix = add_empty_holes_channel(matrix)
        matrix_sync = MatrixRegionEditor(
            matrix,
            forbidden_target_channels=(channel_count,) if matrix.shape[0] > channel_count else (),
        )
        contours = extract_and_score_contours(matrix)
        contour_boundaries = find_contour_boundaries(contours)
        reassign_regions.reassign_regions_by_boundary(matrix_sync, contour_boundaries)

        contours = extract_and_score_contours(matrix)
        contours = self._remove_high_error_areas(contours, matrix_sync)

        contours = [contour for contour in contours if contour.channel_index != channel_count]

        polygons = self.polygon_converter.convert(contours)
        geometry = self.obb_converter.convert(polygons)

        geometry = [geometry_entry for geometry_entry in geometry if geometry_entry.area > settings.MATRIX_PROCESSOR.minimum_obb_or_polygon_area]

        print(f"It took {time.time() - start_time:.3f}s")
        print("===============")

        matrix = matrix[:channel_count]
        return {
            "matrix": matrix,
            "geometry_parts": geometry,
            "source_polygons": polygons,
        }

    def _remove_high_error_areas(
        self,
        contours: list[RegionContour],
        matrix_sync: MatrixRegionEditor,
    ) -> list[RegionContour]:
        filtered_contours = []
        for contour in contours:
            if contour.error is not None and contour.error > settings.MATRIX_PROCESSOR.error_threshold:
                matrix_sync.remove_contour_from_matrix(contour)
            else:
                filtered_contours.append(contour)
        return filtered_contours
