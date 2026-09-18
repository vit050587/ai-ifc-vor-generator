from dataclasses import dataclass

import numpy as np

from contours_processor import RegionContour


# Checking only right and bottom neighbors counts every shared pixel edge once.
FORWARD_NEIGHBOR_OFFSETS = (
    (1, 0),
    (0, 1),
)


@dataclass
class ContourAdjacency:
    first: RegionContour | None
    second: RegionContour | None
    boundary_size: int | None


def find_contour_boundaries(
    contours: list[RegionContour],
) -> list[ContourAdjacency]:
    """Find cross-channel contour pairs and count their shared pixel edges."""
    point_owners = _build_boundary_point_index(contours)
    boundary_counts: dict[tuple[int, int], int] = {}
    region_pairs: dict[
        tuple[int, int],
        tuple[RegionContour, RegionContour],
    ] = {}

    for (x, y), region in point_owners.items():
        for offset_x, offset_y in FORWARD_NEIGHBOR_OFFSETS:
            neighbor = point_owners.get((x + offset_x, y + offset_y))

            if neighbor is None or neighbor.channel_index == region.channel_index:
                continue

            first, second = _ordered_pair(region, neighbor)
            pair_key = (first.global_id, second.global_id)
            boundary_counts[pair_key] = boundary_counts.get(pair_key, 0) + 1
            region_pairs[pair_key] = (first, second)

    return [
        ContourAdjacency(
            first=region_pairs[pair_key][0],
            second=region_pairs[pair_key][1],
            boundary_size=boundary_counts[pair_key],
        )
        for pair_key in sorted(boundary_counts)
    ]


def _build_boundary_point_index(
    contours: list[RegionContour],
) -> dict[tuple[int, int], RegionContour]:
    point_owners: dict[tuple[int, int], RegionContour] = {}

    for region in contours:
        for boundary in (region.contour, *region.holes):
            _add_boundary_points(point_owners, region, boundary)

    return point_owners


def _add_boundary_points(
    point_owners: dict[tuple[int, int], RegionContour],
    region: RegionContour,
    boundary: np.ndarray,
) -> None:
    # A thin or degenerate contour can visit the same pixel more than once.
    # Converting to a set prevents such pixels from inflating boundary_size.
    unique_points = {
        (int(point[0]), int(point[1]))
        for point in boundary
    }

    for point in unique_points:
        existing_owner = point_owners.get(point)

        if existing_owner is not None and existing_owner.global_id != region.global_id:
            raise ValueError(
                "Contours overlap at pixel "
                f"{point}: global_id={existing_owner.global_id} and "
                f"global_id={region.global_id}"
            )

        point_owners[point] = region


def _ordered_pair(
    first: RegionContour,
    second: RegionContour,
) -> tuple[RegionContour, RegionContour]:
    if first.global_id <= second.global_id:
        return first, second

    return second, first
