import cv2
import numpy as np
import torch

from skimage.morphology import medial_axis

def get_component_mask(
    matrix: torch.Tensor,
    channel_index: int,
    point: tuple[int, int],
    bounds: tuple[int, int, int, int],
) -> np.ndarray:
    """Return the connected component at point inside the cropped channel."""
    foreground_value = 1
    component_fill_value = 2
    component_connectivity = 8

    x_start, y_start, x_end, y_end = bounds
    point_x, point_y = point
    if x_start >= x_end or y_start >= y_end:
        raise ValueError(f"Empty component bounds: {bounds}")
    if not (x_start <= point_x < x_end and y_start <= point_y < y_end):
        raise ValueError(f"Point {point} is outside component bounds {bounds}")

    channel_crop = (
        matrix[channel_index, y_start:y_end, x_start:x_end]
        .detach()
        .cpu()
        .numpy()
        .astype(np.uint8, copy=True)
    )
    channel_crop[channel_crop != 0] = foreground_value
    seed_point = (point_x - x_start, point_y - y_start)
    if channel_crop[seed_point[1], seed_point[0]] != foreground_value:
        raise ValueError(f"Point {point} is not set in channel {channel_index}")

    cv2.floodFill(
        channel_crop,
        mask=None,
        seedPoint=seed_point,
        newVal=component_fill_value,
        flags=component_connectivity,
    )
    return channel_crop == component_fill_value

def get_polygon_thickness(
    polygon_contour: np.ndarray,
    polygon_holes: list[np.ndarray],
) -> float:
    """Вычисляет доминирующую толщину полигона на основе medial axis."""

    bin_size = 2.0

    contour = np.rint(np.asarray(polygon_contour, dtype=np.float64)).astype(np.int32)
    holes = [
        np.rint(np.asarray(hole, dtype=np.float64)).astype(np.int32)
        for hole in polygon_holes
    ]

    if len(contour) < 3:
        return 0.0

    # Добавляем небольшой padding, чтобы внешний контур
    # гарантированно был окружён фоном.
    padding = 2

    x, y, w, h = cv2.boundingRect(contour)

    offset = np.array(
        [x - padding, y - padding],
        dtype=np.int32,
    )

    mask = np.zeros(
        (
            h + padding * 2,
            w + padding * 2,
        ),
        dtype=np.uint8,
    )

    local_contour = contour - offset

    cv2.fillPoly(
        mask,
        [local_contour],
        1,
    )

    # Вырезаем отверстия.
    for hole in holes:
        if len(hole) < 3:
            continue

        local_hole = hole - offset

        cv2.fillPoly(
            mask,
            [local_hole],
            0,
        )

    # Medial axis + distance transform.
    skeleton, distance = medial_axis(
        mask.astype(bool),
        return_distance=True,
    )

    if not skeleton.any():
        return 0.0

    # Берём расстояния только на skeleton.
    thicknesses = distance[skeleton] * 2.0

    quantized = (
        np.round(thicknesses / bin_size)
        * bin_size
    )

    values, counts = np.unique(
        quantized,
        return_counts=True,
    )

    dominant_thickness = values[
        np.argmax(counts)
    ]

    return float(dominant_thickness)

def get_polygon_area(
    polygon_contour: np.ndarray,
    polygon_holes: list[np.ndarray],
) -> float:
    contour = np.asarray(polygon_contour, dtype=np.float32)

    area = cv2.contourArea(contour)

    for hole in polygon_holes:
        if len(hole) < 3:
            continue

        area -= cv2.contourArea(np.asarray(hole, dtype=np.float32))

    return float(area)
