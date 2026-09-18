from collections.abc import Iterable

import numpy as np
import torch

from contours_processor import RegionContour
from matrix_utils import get_component_mask

class MatrixRegionEditor:
    def __init__(
        self,
        matrix: torch.Tensor,
        forbidden_target_channels: Iterable[int] = (),
    ) -> None:
        self.matrix = matrix
        self.forbidden_target_channels = frozenset(forbidden_target_channels)
        for channel_id in self.forbidden_target_channels:
            _validate_channel_id(matrix, channel_id, "forbidden target channel_id")

    def remove_contour_from_matrix(self, contour: RegionContour) -> None:
        """Remove a RegionContour's connected pixel region from its channel in-place."""
        region_mask, bounds = _get_region_mask(self.matrix, contour)
        x_start, y_start, x_end, y_end = bounds
        source_region = self.matrix[
            contour.channel_index,
            y_start:y_end,
            x_start:x_end,
        ]
        source_region.masked_fill_(_to_tensor_mask(region_mask, self.matrix), False)

    def move_contour_to_channel(
        self,
        contour: RegionContour,
        channel_id: int,
    ) -> bool:
        """Move the region in-place; return True only when a move occurs."""
        _validate_channel_id(self.matrix, channel_id, "target channel_id")
        if channel_id == contour.channel_index:
            return False
        if channel_id in self.forbidden_target_channels:
            return False

        region_mask, bounds = _get_region_mask(self.matrix, contour)
        x_start, y_start, x_end, y_end = bounds
        tensor_mask = _to_tensor_mask(region_mask, self.matrix)

        source_region = self.matrix[
            contour.channel_index,
            y_start:y_end,
            x_start:x_end,
        ]
        target_region = self.matrix[
            channel_id,
            y_start:y_end,
            x_start:x_end,
        ]

        source_region.masked_fill_(tensor_mask, False)
        target_region.masked_fill_(tensor_mask, True)
        contour.channel_index = channel_id
        return True

    def get_contour_pixel_perimeter(self, contour: RegionContour) -> int:
        """Return the region perimeter as a count of exposed 4-connected edges."""
        region_mask, _ = _get_region_mask(self.matrix, contour)
        padded_mask = np.pad(
            region_mask,
            pad_width=1,
            mode="constant",
            constant_values=False,
        )
        horizontal_edges = np.count_nonzero(
            padded_mask[:, 1:] != padded_mask[:, :-1]
        )
        vertical_edges = np.count_nonzero(
            padded_mask[1:, :] != padded_mask[:-1, :]
        )
        return int(horizontal_edges + vertical_edges)


def _get_region_mask(
    matrix: torch.Tensor,
    contour: RegionContour,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    _validate_channel_id(matrix, contour.channel_index, "contour channel_index")

    matrix_height, matrix_width = matrix.shape[-2:]
    x, y, width, height = contour.bounding_box
    x_start = max(0, x)
    y_start = max(0, y)
    x_end = min(matrix_width, x + width)
    y_end = min(matrix_height, y + height)

    if x_start >= x_end or y_start >= y_end:
        raise ValueError(
            f"Contour global_id={contour.global_id} has an empty bounding box"
        )

    point_x, point_y = contour.contour_start_point
    if not (x_start <= point_x < x_end and y_start <= point_y < y_end):
        raise ValueError(
            f"Contour global_id={contour.global_id} point {contour.contour_start_point} "
            "is outside its bounding box"
        )

    bounds = (x_start, y_start, x_end, y_end)
    region_mask = get_component_mask(
        matrix,
        contour.channel_index,
        (point_x, point_y),
        bounds,
    )
    return region_mask, bounds


def _to_tensor_mask(
    region_mask: np.ndarray,
    matrix: torch.Tensor,
) -> torch.Tensor:
    return torch.from_numpy(region_mask).to(device=matrix.device)


def _validate_channel_id(
    matrix: torch.Tensor,
    channel_id: int,
    argument_name: str,
) -> None:
    if not isinstance(channel_id, int):
        raise TypeError(f"{argument_name} must be int, got {type(channel_id).__name__}")

    if not 0 <= channel_id < matrix.shape[0]:
        raise ValueError(
            f"{argument_name}={channel_id} is outside "
            f"the valid range [0, {matrix.shape[0] - 1}]"
        )
