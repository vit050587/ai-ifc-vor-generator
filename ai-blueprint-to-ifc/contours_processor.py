import torch
from pathlib import Path
import cv2
import time
import numpy as np

from dataclasses import dataclass

from config import settings
from matrix_utils import get_component_mask
@dataclass
class RegionContour:
    global_id: int
    channel_index: int

    bounding_box: tuple[int, int, int, int]  # x, y, w, h
    holes: list[np.ndarray]

    contour: np.ndarray
    error: float | None = None

    @property
    def area(self) -> float:
        area = cv2.contourArea(self.contour)

        for hole in self.holes:
            area -= cv2.contourArea(hole)

        return float(area)

    @property
    def top_left(self) -> tuple[int, int]:
        x, y, _, _ = self.bounding_box
        return x, y

    @property
    def contour_start_point(self) -> tuple[int, int]:
        x, y = self.contour[0]
        return int(x), int(y)

    @property
    def outer_contour_length_px(self) -> float:
        return float(
            cv2.arcLength(
                self.contour,
                closed=True,
            )
        )

def extract_and_score_contours(matrix: torch.Tensor):
    contours = extract_contours(matrix)
    process_contours(contours, settings.MATRIX_PROCESSOR.window_radius)
    return contours


def add_empty_holes_channel(matrix: torch.Tensor) -> torch.Tensor:
    contours = extract_contours(matrix)
    occupied = matrix.any(dim=0).detach().cpu().numpy()
    holes_channel = np.zeros(occupied.shape, dtype=np.uint8)

    for contour in contours:
        for hole in contour.holes:
            has_inner_contour = any(
                other is not contour
                and cv2.pointPolygonTest(hole, other.contour_start_point, False) >= 0
                for other in contours
            )

            if not has_inner_contour:
                cv2.fillPoly(holes_channel, [hole], 1)

    holes_channel &= ~occupied

    if not holes_channel.any():
        return matrix

    holes_channel = torch.as_tensor(
        holes_channel,
        dtype=matrix.dtype,
        device=matrix.device,
    ).unsqueeze(0)
    return torch.cat((matrix, holes_channel), dim=0)


def process_region(
    matrix: torch.Tensor,
    contour: RegionContour,
    target_contour: RegionContour,
) -> None:
    """Rebuild target_contour after contour has been added to its channel."""
    first_x, first_y, first_width, first_height = contour.bounding_box
    second_x, second_y, second_width, second_height = target_contour.bounding_box
    x = min(first_x, second_x)
    y = min(first_y, second_y)
    x_end = max(first_x + first_width, second_x + second_width)
    y_end = max(first_y + first_height, second_y + second_height)
    point = target_contour.contour_start_point
    channel_index = target_contour.channel_index
    matrix_height, matrix_width = matrix.shape[-2:]
    x_start = max(0, x)
    y_start = max(0, y)
    x_end = min(matrix_width, x_end)
    y_end = min(matrix_height, y_end)
    component_mask = get_component_mask(
        matrix,
        channel_index,
        point,
        (x_start, y_start, x_end, y_end),
    )
    region_contours, hierarchy = cv2.findContours(
        component_mask.astype(np.uint8),
        cv2.RETR_CCOMP,
        cv2.CHAIN_APPROX_NONE,
    )

    if hierarchy is None:
        raise ValueError(f"No contour found at point {point}")

    hierarchy = hierarchy[0]
    outer_indices = [
        index
        for index, item in enumerate(hierarchy)
        if item[3] == -1
    ]

    if len(outer_indices) != 1:
        raise ValueError(
            f"Expected one connected contour at point {point}, "
            f"found {len(outer_indices)}"
        )

    outer_index = outer_indices[0]
    offset = np.array([x_start, y_start], dtype=np.int32)
    outer_contour = region_contours[outer_index][:, 0, :] + offset
    holes = []
    child_index = hierarchy[outer_index][2]

    while child_index != -1:
        hole = region_contours[child_index][:, 0, :] + offset
        holes.append(hole)
        child_index = hierarchy[child_index][0]

    region = RegionContour(
        global_id=target_contour.global_id,
        channel_index=channel_index,
        bounding_box=cv2.boundingRect(outer_contour),
        holes=holes,
        contour=outer_contour,
    )
    process_contours([region], settings.MATRIX_PROCESSOR.window_radius)
    target_contour.bounding_box = region.bounding_box
    target_contour.holes = region.holes
    target_contour.contour = region.contour
    target_contour.error = region.error


def extract_contours(
    matrix: torch.Tensor,
) -> list[RegionContour]:
    matrix_np = (
        matrix
        .detach()
        .cpu()
        .numpy()
        .astype(np.uint8)
    )

    result = []
    extraction_id = 0

    for channel_index, channel in enumerate(matrix_np):
        contours, hierarchy = cv2.findContours(
            channel,
            cv2.RETR_CCOMP,
            cv2.CHAIN_APPROX_NONE,
        )

        if hierarchy is None:
            continue

        hierarchy = hierarchy[0]

        for i, contour in enumerate(contours):
            # Только внешние контуры
            if hierarchy[i][3] != -1:
                continue

            contour = contour[:, 0, :]

            holes = []

            child_index = hierarchy[i][2]

            while child_index != -1:
                hole = contours[child_index][:, 0, :]
                holes.append(hole)

                child_index = hierarchy[child_index][0]

            x, y, w, h = cv2.boundingRect(contour)

            region = RegionContour(
                global_id=extraction_id,
                channel_index=channel_index,
                bounding_box=(x, y, w, h),
                holes=holes,
                contour=contour,
            )

            result.append(region)
            extraction_id += 1

    return result

def process_contours(
    contours: list[RegionContour],
    radius: int,
):
    for region in contours:
        contour = region.contour

        if len(contour) < radius * 2 + 1:
            region.error = float("inf")
            continue

        contour_errors = []
        for i in range(len(contour)):
            if not i % settings.MATRIX_PROCESSOR.contour_sample_stride == 0:
                continue
            window = get_contour_window(
                contour,
                i,
                radius,
            )

            points = window.astype(np.float64)
            extent = get_extent(points)

            arc_error = _get_arc_error(points, extent)
            line_error = _get_line_error(points, extent)

            error = min(arc_error, line_error)

            contour_errors.append(error)

        errors_mean = np.mean(contour_errors)

        region.error = float(errors_mean)

def get_contour_window(
    contour: np.ndarray,
    center_index: int,
    radius: int,
) -> np.ndarray:
    indices = (
        np.arange(
            center_index - radius,
            center_index + radius + 1,
        )
        % len(contour)
    )

    return contour[indices]


def _get_arc_error(
    points: np.ndarray, extent: float
) -> float:
    x = points[:, 0]
    y = points[:, 1]

    a = np.column_stack([
        2.0 * x,
        2.0 * y,
        np.ones_like(x),
    ])

    b = x**2 + y**2

    try:
        solution, _, _, _ = np.linalg.lstsq(
            a,
            b,
            rcond=None,
        )
    except np.linalg.LinAlgError:
        return float("inf")

    cx, cy, c = solution

    radius_squared = cx**2 + cy**2 + c

    if radius_squared <= 0:
        return float("inf")

    center = np.array(
        [cx, cy],
        dtype=np.float64,
    )

    distances = np.linalg.norm(
        points - center,
        axis=1,
    )

    radius = distances.mean()

    if radius <= 1e-8:
        return float("inf")

    radial_errors = np.abs(
        distances - radius
    )

    if extent <= 1e-8:
        return float("inf")

    return float(
        np.sqrt(np.mean(radial_errors ** 2)) / extent
    )

def _get_line_error(
    points: np.ndarray, extent: float
) -> float:
    center = points.mean(axis=0)
    centered = points - center

    _, _, vh = np.linalg.svd(
        centered,
        full_matrices=False,
    )

    direction = vh[0]

    normal = np.array(
        [-direction[1], direction[0]],
        dtype=np.float64,
    )

    distances = np.abs(
        centered @ normal
    )

    if extent <= 1e-8:
        return float("inf")

    return float(
        np.sqrt(np.mean(distances ** 2)) / extent
    )

def get_extent(
    points: np.ndarray,
) -> float:
    return float(
        np.linalg.norm(
            points.max(axis=0)
            - points.min(axis=0)
        )
    )
