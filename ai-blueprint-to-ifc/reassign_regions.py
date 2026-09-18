from contour_adjacency import ContourAdjacency
from contours_processor import RegionContour, process_region
from matrix_region_editor import MatrixRegionEditor

from config import settings

def reassign_regions_by_boundary(matrix_sync: MatrixRegionEditor, contours_boundaries: list[ContourAdjacency]):
    for contour_pair in contours_boundaries:
        first = contour_pair.first
        second = contour_pair.second
        boundary_size = contour_pair.boundary_size

        if first is None or second is None or boundary_size is None:
            continue

        if (first.error is not None and first.error > settings.MATRIX_PROCESSOR.error_threshold
                and boundary_size / matrix_sync.get_contour_pixel_perimeter(first) > settings.MATRIX_PROCESSOR.boundary_threshold):
            if _switch_channel(first, second, matrix_sync, contours_boundaries):
                continue
        if (second.error is not None and second.error > settings.MATRIX_PROCESSOR.error_threshold
                and boundary_size / matrix_sync.get_contour_pixel_perimeter(second) > settings.MATRIX_PROCESSOR.boundary_threshold):
            _switch_channel(second, first, matrix_sync, contours_boundaries)


def _switch_channel(contour: RegionContour, target_contour: RegionContour, matrix_sync: MatrixRegionEditor, contours_boundaries: list[ContourAdjacency]) -> bool:
    if not matrix_sync.move_contour_to_channel(contour, target_contour.channel_index):
        return False
    process_region(matrix_sync.matrix, contour, target_contour)

    for contour_pair in contours_boundaries:
        if contour is contour_pair.first or contour is contour_pair.second:
            contour_pair.first = None
            contour_pair.second = None
            contour_pair.boundary_size = None
    return True
