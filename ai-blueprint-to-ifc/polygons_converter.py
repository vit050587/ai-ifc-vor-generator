from dataclasses import dataclass, field
import math

import cv2
import numpy as np

from config import PolygonConversionSettings
from mask_polygonizer import PdfMaskPolygon, PdfPoint, PdfPolygonizedMask


PdfObb = dict[str, float]


@dataclass(frozen=True)
class WallObb:
    """One straight wall segment represented by an OBB in PDF points."""

    bbox_pdf: PdfObb
    channel_id: int
    source_polygon_id: int
    length: float
    thickness: float
    angle_degrees: float
    approximated_curve: bool = False


@dataclass(frozen=True)
class PolygonObbConversionResult:
    walls: list[WallObb]
    skipped_polygon_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class _Edge:
    start: PdfPoint
    end: PdfPoint
    length: float
    axis: PdfPoint
    approximated_curve: bool = False


class PolygonsConverter:
    """Decompose polygonized wall masks into straight wall OBBs."""

    def __init__(self, settings: PolygonConversionSettings) -> None:
        if settings.max_wall_thickness <= settings.min_wall_thickness:
            raise ValueError(
                "max_wall_thickness must be greater than min_wall_thickness"
            )
        self.settings = settings

    def process(
        self,
        walls_polygons_pdf: PdfPolygonizedMask,
    ) -> PolygonObbConversionResult:
        if not isinstance(walls_polygons_pdf, PdfPolygonizedMask):
            raise TypeError("walls_polygons_pdf must be a PdfPolygonizedMask")

        walls: list[WallObb] = []
        skipped_polygon_ids: list[int] = []

        for polygon_id, polygon in enumerate(walls_polygons_pdf.polygons):
            polygon_walls = self._convert_polygon(polygon, polygon_id)
            if polygon_walls:
                walls.extend(polygon_walls)
            else:
                skipped_polygon_ids.append(polygon_id)

        return PolygonObbConversionResult(
            walls=self._deduplicate(walls),
            skipped_polygon_ids=skipped_polygon_ids,
        )

    def _convert_polygon(
        self,
        polygon: PdfMaskPolygon,
        polygon_id: int,
    ) -> list[WallObb]:
        approximate_curves = self.settings.curved_walls_mode == "approximate"
        rings = [polygon.exterior, *polygon.holes]
        edges: list[_Edge] = []

        for ring in rings:
            points, ring_was_approximated = self._simplify_ring(
                ring,
                approximate_curves,
            )
            edges.extend(self._ring_edges(points, ring_was_approximated))

        candidates: list[WallObb] = []
        for first_index, first in enumerate(edges):
            for second in edges[first_index + 1:]:
                wall = self._edges_to_wall(
                    first,
                    second,
                    polygon.channel_id,
                    polygon_id,
                )
                if wall is not None:
                    candidates.append(wall)

        return self._deduplicate(candidates)

    def _simplify_ring(
        self,
        ring: list[PdfPoint],
        approximate_curves: bool,
    ) -> tuple[list[PdfPoint], bool]:
        if len(ring) < 3:
            return [], False

        tolerance = (
            self.settings.curve_approximation_tolerance
            if approximate_curves
            else self.settings.straight_simplification_tolerance
        )
        if tolerance == 0:
            return [(float(x), float(y)) for x, y in ring], False

        contour = np.asarray(ring, dtype=np.float32).reshape((-1, 1, 2))
        simplified = cv2.approxPolyDP(contour, tolerance, True)
        points = [
            (float(point[0][0]), float(point[0][1]))
            for point in simplified
        ]
        was_approximated = approximate_curves and len(points) < len(ring)
        return points, was_approximated

    def _ring_edges(
        self,
        ring: list[PdfPoint],
        approximated_curve: bool,
    ) -> list[_Edge]:
        edges: list[_Edge] = []
        for index, start in enumerate(ring):
            end = ring[(index + 1) % len(ring)]
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            length = math.hypot(dx, dy)
            if length < self.settings.min_wall_length:
                continue
            edges.append(
                _Edge(
                    start=start,
                    end=end,
                    length=length,
                    axis=(dx / length, dy / length),
                    approximated_curve=approximated_curve,
                )
            )
        return edges

    def _edges_to_wall(
        self,
        first: _Edge,
        second: _Edge,
        channel_id: int,
        polygon_id: int,
    ) -> WallObb | None:
        axis = self._canonical_axis(first.axis)
        second_axis = self._canonical_axis(second.axis)
        if self._angle_difference(axis, second_axis) > math.radians(
            self.settings.parallel_angle_tolerance_degrees
        ):
            return None

        normal = (-axis[1], axis[0])
        first_start, first_end = sorted(
            (self._dot(first.start, axis), self._dot(first.end, axis))
        )
        second_start, second_end = sorted(
            (self._dot(second.start, axis), self._dot(second.end, axis))
        )
        overlap_start = max(first_start, second_start)
        overlap_end = min(first_end, second_end)
        length = overlap_end - overlap_start
        if length < self.settings.min_wall_length:
            return None

        overlap_ratio = length / min(first.length, second.length)
        if overlap_ratio < self.settings.min_parallel_overlap_ratio:
            return None

        first_offset = (
            self._dot(first.start, normal) + self._dot(first.end, normal)
        ) / 2
        second_offset = (
            self._dot(second.start, normal) + self._dot(second.end, normal)
        ) / 2
        thickness = abs(second_offset - first_offset)
        if not (
            self.settings.min_wall_thickness
            <= thickness
            <= self.settings.max_wall_thickness
        ):
            return None
        if length / thickness < self.settings.min_length_to_thickness_ratio:
            return None

        low_offset, high_offset = sorted((first_offset, second_offset))
        points = [
            self._from_axes(overlap_start, low_offset, axis, normal),
            self._from_axes(overlap_end, low_offset, axis, normal),
            self._from_axes(overlap_end, high_offset, axis, normal),
            self._from_axes(overlap_start, high_offset, axis, normal),
        ]
        bbox_pdf = {
            f"{coordinate}{index}": float(point[value_index])
            for index, point in enumerate(points, start=1)
            for coordinate, value_index in (("x", 0), ("y", 1))
        }
        angle = math.degrees(math.atan2(axis[1], axis[0])) % 180
        return WallObb(
            bbox_pdf=bbox_pdf,
            channel_id=channel_id,
            source_polygon_id=polygon_id,
            length=length,
            thickness=thickness,
            angle_degrees=angle,
            approximated_curve=(
                first.approximated_curve or second.approximated_curve
            ),
        )

    def _deduplicate(self, walls: list[WallObb]) -> list[WallObb]:
        tolerance = self.settings.deduplication_tolerance
        angle_tolerance = max(
            self.settings.parallel_angle_tolerance_degrees,
            0.001,
        )
        unique: dict[tuple, WallObb] = {}

        for wall in sorted(walls, key=lambda item: item.length, reverse=True):
            points = [
                (
                    wall.bbox_pdf[f"x{index}"],
                    wall.bbox_pdf[f"y{index}"],
                )
                for index in range(1, 5)
            ]
            center_x = sum(point[0] for point in points) / 4
            center_y = sum(point[1] for point in points) / 4
            key = (
                wall.channel_id,
                wall.source_polygon_id,
                round(center_x / tolerance),
                round(center_y / tolerance),
                round(wall.length / tolerance),
                round(wall.thickness / tolerance),
                round(wall.angle_degrees / angle_tolerance),
            )
            unique.setdefault(key, wall)

        return list(unique.values())

    @staticmethod
    def _canonical_axis(axis: PdfPoint) -> PdfPoint:
        if axis[0] < 0 or (abs(axis[0]) < 1e-12 and axis[1] < 0):
            return -axis[0], -axis[1]
        return axis

    @staticmethod
    def _angle_difference(first: PdfPoint, second: PdfPoint) -> float:
        dot = max(-1.0, min(1.0, PolygonsConverter._dot(first, second)))
        return math.acos(abs(dot))

    @staticmethod
    def _dot(first: PdfPoint, second: PdfPoint) -> float:
        return first[0] * second[0] + first[1] * second[1]

    @staticmethod
    def _from_axes(
        x: float,
        y: float,
        axis: PdfPoint,
        normal: PdfPoint,
    ) -> PdfPoint:
        return (
            x * axis[0] + y * normal[0],
            x * axis[1] + y * normal[1],
        )
