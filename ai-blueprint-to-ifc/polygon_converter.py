
from __future__ import annotations

from contours_processor import RegionContour
from matrix_utils import get_polygon_area, get_polygon_thickness
import numpy as np
import cv2

from dataclasses import dataclass, field

from config import settings

@dataclass
class RegionPolygon:
    source: RegionContour

    polygon: np.ndarray
    holes: list[np.ndarray]
    source_polygon: RegionPolygon | None = None
    entry_index: int | None = None

    temp_area: float | None = None
    temp_width: float | None = None

    @property
    def global_id(self) -> int:
        return self.source.global_id

    @property
    def channel_index(self) -> int:
        return self.source.channel_index

    @property
    def width(self) -> float:
        return get_polygon_thickness(self.polygon, self.holes)

    @property
    def area(self) -> float:
        return get_polygon_area(self.polygon, self.holes)

class PolygonConverter:
    def __init__(self) -> None:
        pass
    def convert(self, contours: list[RegionContour]) -> list[RegionPolygon]:
        polygons = []
        for contour in contours:
            polygons.append(self._contour_to_polygon(contour, settings.MATRIX_PROCESSOR.epsilon))
        return polygons

    def _contour_to_polygon(
            self,
        region: RegionContour,
        epsilon: float,
    ) -> RegionPolygon:
        polygon = cv2.approxPolyDP(
            region.contour.reshape(-1, 1, 2).astype(np.float32),
            epsilon=epsilon,
            closed=True,
        )[:, 0, :]

        holes = [
            cv2.approxPolyDP(
                hole.reshape(-1, 1, 2).astype(np.float32),
                epsilon=epsilon,
                closed=True,
            )[:, 0, :]
            for hole in region.holes
        ]
        # delete small holes
        holes = [hole for hole in holes if len(hole) > 2]

        return RegionPolygon(
            source=region,
            polygon=polygon,
            holes=holes,
        )
