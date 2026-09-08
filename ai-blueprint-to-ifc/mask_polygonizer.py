from dataclasses import dataclass, field

import cv2
import numpy as np
import torch


Point = tuple[int, int]
PdfPoint = tuple[float, float]


@dataclass(frozen=True)
class MaskPolygon:
    """One selected region from a mask channel, in pixel coordinates."""

    channel_id: int
    exterior: list[Point]
    holes: list[list[Point]] = field(default_factory=list)
    area: float = 0.0


@dataclass(frozen=True)
class PolygonizedMask:
    """Polygon representation of an [N, H, W] binary mask."""

    width: int
    height: int
    channel_count: int
    polygons: list[MaskPolygon]


@dataclass(frozen=True)
class PdfMaskPolygon:
    """One mask region expressed in PDF points."""

    channel_id: int
    exterior: list[PdfPoint]
    holes: list[list[PdfPoint]] = field(default_factory=list)
    area: float = 0.0


@dataclass(frozen=True)
class PdfPolygonizedMask:
    """Polygon representation in the global coordinate system of a PDF page."""

    width: float
    height: float
    channel_count: int
    polygons: list[PdfMaskPolygon]


class MaskPolygonizer:
    def process(self, binary_matrix: torch.Tensor) -> PolygonizedMask:
        """Convert an [N, H, W] binary tensor to polygons grouped by channel."""
        self._validate(binary_matrix)

        channel_count, height, width = binary_matrix.shape
        polygons: list[MaskPolygon] = []

        for channel_id in range(channel_count):
            channel_mask = (
                binary_matrix[channel_id]
                .detach()
                .to(device="cpu", dtype=torch.uint8)
                .numpy()
            )
            channel_mask = np.ascontiguousarray(channel_mask)

            contours, hierarchy = cv2.findContours(
                channel_mask,
                cv2.RETR_CCOMP,
                cv2.CHAIN_APPROX_SIMPLE,
            )
            if hierarchy is None:
                continue

            hierarchy = hierarchy[0]
            for contour_index, contour in enumerate(contours):
                # In RETR_CCOMP, contours without a parent are filled regions;
                # their direct children describe holes in those regions.
                if hierarchy[contour_index][3] != -1:
                    continue

                exterior = self._contour_to_points(contour)
                if len(exterior) < 3:
                    continue

                holes: list[list[Point]] = []
                child_indices = self._child_indices(contour_index, hierarchy)
                for child_index in child_indices:
                    hole = self._contour_to_points(contours[child_index])
                    if len(hole) >= 3:
                        holes.append(hole)

                area = float(cv2.contourArea(contour)) - sum(
                    float(cv2.contourArea(contours[index]))
                    for index in child_indices
                )
                polygons.append(
                    MaskPolygon(
                        channel_id=channel_id,
                        exterior=exterior,
                        holes=holes,
                        area=max(area, 0.0),
                    )
                )

        return PolygonizedMask(
            width=width,
            height=height,
            channel_count=channel_count,
            polygons=polygons,
        )

    @staticmethod
    def _validate(binary_matrix: torch.Tensor) -> None:
        if not isinstance(binary_matrix, torch.Tensor):
            raise TypeError("binary_matrix must be a torch.Tensor")
        if binary_matrix.ndim != 3:
            raise ValueError(
                "binary_matrix must have shape [N, H, W], "
                f"got {tuple(binary_matrix.shape)}"
            )
        if binary_matrix.dtype != torch.bool:
            raise TypeError(
                "binary_matrix must have dtype torch.bool, "
                f"got {binary_matrix.dtype}"
            )

    @staticmethod
    def _contour_to_points(contour: np.ndarray) -> list[Point]:
        return [(int(point[0][0]), int(point[0][1])) for point in contour]

    @staticmethod
    def _child_indices(parent_index: int, hierarchy: np.ndarray) -> list[int]:
        indices: list[int] = []
        child_index = int(hierarchy[parent_index][2])
        while child_index != -1:
            indices.append(child_index)
            child_index = int(hierarchy[child_index][0])
        return indices
