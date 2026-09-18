"""Extract rectangles from straight wall spans; retain other geometry.

Coordinates and lengths use matrix pixels. Touching edges are allowed; positive
area overlaps are not. Overlapping input polygons are owned by the first input.
"""

from dataclasses import dataclass
from itertools import chain
from math import atan2, cos, degrees, radians, sin

import numpy as np
from shapely import make_valid
from shapely.affinity import affine_transform, translate
from shapely.geometry import GeometryCollection, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from shapely.strtree import STRtree

from config import settings
from contours_processor import RegionContour
from polygon_converter import RegionPolygon


# Numerical noise from double-precision polygon intersections, in square pixels.
AREA_EPSILON = 1e-7


@dataclass
class RegionOBB:
    source_polygon: RegionPolygon
    center: tuple[float, float]
    width: float
    height: float
    angle: float  # degrees in [0, 180), clockwise in image coordinates (y down)

    entry_index: int | None = None

    @property
    def source(self) -> RegionContour:
        return self.source_polygon.source

    @property
    def global_id(self) -> int:
        return self.source_polygon.global_id

    @property
    def channel_index(self) -> int:
        return self.source_polygon.channel_index

    @property
    def polygon(self) -> np.ndarray:
        angle = radians(self.angle)
        axes = np.array([[cos(angle), sin(angle)], [-sin(angle), cos(angle)]])
        corners = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], dtype=float)
        return corners * [self.width / 2, self.height / 2] @ axes + self.center

    @property
    def holes(self) -> list[np.ndarray]:
        return []

    @property
    def area(self) -> float:
        return self.width * self.height


def _polygons(geometry: BaseGeometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry] if geometry.area > AREA_EPSILON else []
    return [part for child in getattr(geometry, "geoms", ()) for part in _polygons(child)]


def _geometry(region: RegionPolygon) -> BaseGeometry:
    if len(region.polygon) < 3:
        return GeometryCollection()
    # A point/line hole has no area. The upstream PolygonConverter removes
    # holes that collapse to fewer than three points during simplification.
    holes = [hole for hole in region.holes if len(hole) >= 3]
    return unary_union(_polygons(make_valid(Polygon(region.polygon, holes))))


class OBBConverter:
    def __init__(
        self,
        boundary_tolerance: float = settings.MATRIX_PROCESSOR.obb_boundary_tolerance,
        max_area_error: float = settings.MATRIX_PROCESSOR.obb_max_area_error,
        min_part_side: float = settings.MATRIX_PROCESSOR.obb_min_part_side,
        corner_angle_tolerance: float = settings.MATRIX_PROCESSOR.obb_corner_angle_tolerance,
        max_parts: int = settings.MATRIX_PROCESSOR.obb_max_parts,
        parallel_angle_tolerance: float = settings.MATRIX_PROCESSOR.obb_parallel_angle_tolerance,
        residual_margin: float = settings.MATRIX_PROCESSOR.obb_residual_margin,
    ) -> None:
        if not np.isfinite([boundary_tolerance, max_area_error, min_part_side,
                            corner_angle_tolerance, parallel_angle_tolerance, residual_margin]).all():
            raise ValueError("OBB settings must be finite")
        if boundary_tolerance < 0 or not 0 <= max_area_error < 1 or min_part_side <= 0 or residual_margin < 0:
            raise ValueError("Invalid OBB distance, area, or size tolerance")
        if not 0 <= corner_angle_tolerance < 45 or max_parts < 1:
            raise ValueError("Invalid OBB angle tolerance or part limit")
        if not 0 <= parallel_angle_tolerance < 45:
            raise ValueError("Invalid parallel edge angle tolerance")
        self.boundary_tolerance = boundary_tolerance
        self.max_area_error = max_area_error
        self.min_part_side = min_part_side
        self.corner_angle_tolerance = corner_angle_tolerance
        self.max_parts = max_parts
        self.parallel_angle_tolerance = parallel_angle_tolerance
        self.residual_margin = residual_margin

    def convert(self, polygons: list[RegionPolygon]) -> list[RegionOBB | RegionPolygon]:
        geometries = [_geometry(region) for region in polygons]
        tree = STRtree(geometries)
        result: list[RegionOBB | RegionPolygon] = []
        emitted: list[BaseGeometry] = []
        for index, (source, geometry) in enumerate(zip(polygons, geometries)):
            if geometry.is_empty:
                # Preserve degenerate contours as polygons, never as zero-size OBBs.
                result.append(RegionPolygon(source.source, source.polygon, source.holes, source))
                continue
            nearby = sorted(int(i) for i in tree.query(
                geometry.buffer(self.boundary_tolerance + 1e-6).envelope
            ) if i != index)
            earlier = unary_union([geometries[i] for i in nearby if i < index])
            remaining = geometry.difference(earlier)
            holes = [Polygon(ring) for part in _polygons(geometry) for ring in part.interiors]
            # Protect holes, all neighbouring inputs, and previously emitted OBB
            # extensions, including extensions into the gap between two inputs.
            neighbourhood = geometry.buffer(self.boundary_tolerance + 1e-6).envelope
            forbidden = unary_union(
                [geometries[i] for i in nearby] + holes
                + [part for part in emitted if part.intersects(neighbourhood)]
            )
            # Fit in local coordinates: GEOS's rotated rectangle calculation can
            # lose accuracy for a small wall far from the page origin.
            x, y = geometry.bounds[:2]
            parts = self._decompose(
                translate(remaining, xoff=-x, yoff=-y), source,
                translate(forbidden, xoff=-x, yoff=-y),
                whole_input=isinstance(remaining, Polygon) and remaining.equals(geometry),
            )
            for part in parts:
                if isinstance(part, RegionOBB):
                    part.center = (part.center[0] + x, part.center[1] + y)
                else:
                    part.polygon += [x, y]
                    for hole in part.holes:
                        hole += [x, y]
            result.extend(parts)
            emitted.extend(_geometry(part) if isinstance(part, RegionPolygon)
                           else Polygon(part.polygon) for part in parts)
        return result

    def _fit(self, shape: Polygon, source: RegionPolygon) -> RegionOBB | None:
        if shape.interiors or shape.area <= AREA_EPSILON:
            return None
        rectangle = shape.minimum_rotated_rectangle
        if not isinstance(rectangle, Polygon):
            return None
        if (rectangle.area - shape.area) / shape.area > self.max_area_error + 1e-12:
            return None
        if shape.boundary.hausdorff_distance(rectangle.boundary) > self.boundary_tolerance + 1e-8:
            return None
        points = np.asarray(rectangle.exterior.coords)[:4]
        edges = np.roll(points, -1, axis=0) - points
        lengths = np.linalg.norm(edges, axis=1)
        longest = int(np.argmax(lengths))
        direction = edges[longest]
        return RegionOBB(
            source_polygon=source,
            center=tuple(points.mean(axis=0)),
            width=float(lengths[longest]),
            height=float(lengths[(longest + 1) % 4]),
            angle=degrees(atan2(direction[1], direction[0])) % 180,
        )

    def _cut_candidates(self, shape: Polygon):
        """Try both half planes at reflex corners, including corners of holes.

        Only cuts yielding an acceptable rectangle are ever applied. Thus curved
        polygons are not tessellated into strips just to increase OBB coverage.
        """
        # Locate corners on a lightly simplified boundary, but cut the actual
        # shape. A two-pixel bevel should not hide a ninety-degree junction.
        corner_shape = orient(shape.simplify(self.boundary_tolerance), sign=1.0)
        xmin, ymin, xmax, ymax = shape.bounds
        reach = float(np.hypot(xmax - xmin, ymax - ymin) * 2 + 1)
        angle_limit = sin(radians(self.corner_angle_tolerance))
        seen = set()
        for ring in (corner_shape.exterior, *corner_shape.interiors):
            points = np.asarray(ring.coords)[:-1]
            for i, point in enumerate(points):
                incoming = point - points[i - 1]
                outgoing = points[(i + 1) % len(points)] - point
                lengths = np.linalg.norm(incoming), np.linalg.norm(outgoing)
                if min(lengths) < self.min_part_side:
                    continue
                incoming, outgoing = incoming / lengths[0], outgoing / lengths[1]
                cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
                if cross >= 0 or abs(incoming @ outgoing) > angle_limit + 1e-10:
                    continue
                for direction in (incoming, outgoing):
                    normal = np.array([-direction[1], direction[0]])
                    # A line and its opposite describe the same two half planes.
                    if normal[0] < -1e-10 or (abs(normal[0]) <= 1e-10 and normal[1] < 0):
                        normal = -normal
                    key = tuple(np.round([*normal, point @ normal], 7))
                    if key in seen:
                        continue
                    seen.add(key)
                    for side in (-1, 1):
                        a, b = point - direction * reach, point + direction * reach
                        offset = normal * reach * side
                        half_plane = Polygon([a, b, b + offset, a + offset])
                        for candidate in _polygons(shape.intersection(half_plane)):
                            if candidate.area < shape.area - AREA_EPSILON:
                                yield candidate

    def _fit_available(
        self, shape: Polygon, source: RegionPolygon, forbidden: BaseGeometry,
    ) -> RegionOBB | None:
        obb = self._fit(shape, source)
        if obb is None:
            return None
        rectangle = Polygon(obb.polygon)
        if rectangle.intersection(forbidden).area <= AREA_EPSILON:
            return obb
        # Trim only the sides that hit a neighbour instead of rejecting an
        # otherwise rectangular wall. Uncovered strips remain in the polygon.
        theta = radians(obb.angle)
        c, s = cos(theta), sin(theta)
        cx, cy = obb.center
        obstacles = affine_transform(forbidden.intersection(rectangle),
                                     [c, s, -s, c, -c * cx - s * cy, s * cx - c * cy])
        bounds = (-obb.width / 2, -obb.height / 2, obb.width / 2, obb.height / 2)
        for _ in range(16):
            overlaps = _polygons(box(*bounds).intersection(obstacles))
            if not overlaps:
                break
            hit = max(overlaps, key=lambda part: part.area)
            left, bottom, right, top = bounds
            x0, y0, x1, y1 = hit.bounds
            options = [(x1, bottom, right, top), (left, bottom, x0, top),
                       (left, y1, right, top), (left, bottom, right, y0)]
            options = [b for b in options if b[2] > b[0] and b[3] > b[1]]
            if not options:
                return None
            bounds = max(options, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        else:
            return None
        rectangle = affine_transform(box(*bounds), [c, -s, s, c, cx, cy])
        if rectangle.intersection(forbidden).area > AREA_EPSILON:
            return None
        if shape.symmetric_difference(rectangle).area / shape.area > self.max_area_error:
            return None
        if shape.boundary.hausdorff_distance(rectangle.boundary) > self.boundary_tolerance + 1e-8:
            return None
        return self._fit(rectangle, source)

    def _parallel_candidates(self, shape: Polygon, simplify: bool = True):
        """Find straight wall spans between opposing edges, including junctions.

        A single cut through the entire polygon cannot isolate a wall connected
        at both ends. Opposing straight sides provide a local rectangle instead.
        Gradually turning chains are excluded to avoid tiling arcs with OBBs.
        """
        simplified = orient(
            shape.simplify(self.boundary_tolerance) if simplify else shape,
            sign=1.0,
        )
        edges = []
        for ring in (simplified.exterior, *simplified.interiors):
            points = np.asarray(ring.coords)[:-1]
            vectors = np.roll(points, -1, axis=0) - points
            lengths = np.linalg.norm(vectors, axis=1)
            if np.any(lengths < 1e-8):
                continue
            directions = vectors / lengths[:, None]
            previous = np.roll(directions, 1, axis=0)
            turns = np.degrees(np.arctan2(
                previous[:, 0] * directions[:, 1] - previous[:, 1] * directions[:, 0],
                np.sum(previous * directions, axis=1),
            ))
            gradual = (np.abs(turns) > 0.5) & (np.abs(turns) < 40)
            # A long straight edge can have shallow turns where arcs join it;
            # those endpoint turns do not make the entire edge curved.
            comparable = lengths <= 3 * np.maximum(
                np.roll(lengths, 1), np.roll(lengths, -1)
            )
            curved = gradual & np.roll(gradual, -1) & (
                turns * np.roll(turns, -1) > 0
            ) & comparable
            for _ in range(len(points)):
                extended = curved | (np.roll(curved, 1) & gradual) | (
                    np.roll(curved, -1) & np.roll(gradual, -1))
                extended &= comparable
                if np.array_equal(extended, curved):
                    break
                curved = extended
            for i, point in enumerate(points):
                if not curved[i] and lengths[i] >= self.min_part_side:
                    edges.append((point, points[(i + 1) % len(points)], directions[i], lengths[i]))
        for i, (a, b, direction, length) in enumerate(edges):
            normal = np.array([-direction[1], direction[0]])
            for c, d, other_direction, other_length in edges[i + 1:]:
                if direction @ other_direction > -cos(radians(self.parallel_angle_tolerance)):
                    continue
                width = float(((c + d) / 2 - (a + b) / 2) @ normal)
                if width < self.min_part_side:
                    continue
                t0 = max(min(a @ direction, b @ direction), min(c @ direction, d @ direction))
                t1 = min(max(a @ direction, b @ direction), max(c @ direction, d @ direction))
                span = t1 - t0
                if span < max(self.min_part_side * 3, width * 2):
                    # A short square corner cap can still be a genuine OBB.
                    # Require a long supporting edge, a narrow width, and a
                    # nearly filled rectangle rather than treating arc chords
                    # as independent wall spans.
                    if (span < self.min_part_side
                            or width > self.min_part_side * 4
                            or max(length, other_length) < self.min_part_side * 3):
                        continue
                n0 = min(a @ normal, b @ normal)
                n1 = max(c @ normal, d @ normal)
                corners = np.array([[t0, n0], [t1, n0], [t1, n1], [t0, n1]])
                rectangle = Polygon(corners @ np.array([direction, normal]))
                for candidate in _polygons(shape.intersection(rectangle)):
                    if (span < max(self.min_part_side * 3, width * 2)
                            and candidate.area < rectangle.area * 0.8):
                        continue
                    if candidate.area < shape.area - AREA_EPSILON:
                        yield candidate

    def _decompose(
        self, remaining: BaseGeometry, source: RegionPolygon, forbidden: BaseGeometry,
        whole_input: bool,
    ) -> list[RegionOBB | RegionPolygon]:
        result: list[RegionOBB | RegionPolygon] = []
        while not remaining.is_empty and len(result) < self.max_parts:
            best: RegionOBB | None = None
            best_shape: Polygon | None = None
            best_area = 0.0
            for component in _polygons(remaining):
                whole = self._fit_available(component, source, forbidden)
                if whole is not None and (
                    (whole_input and not result)
                    or min(whole.width, whole.height) >= self.min_part_side - 1e-8
                ):
                    rectangle = Polygon(whole.polygon)
                    if rectangle.intersection(forbidden).area <= AREA_EPSILON:
                        best, best_shape = whole, rectangle
                        break
                for candidate in chain(self._cut_candidates(component), self._parallel_candidates(component)):
                    if candidate.area <= best_area:
                        continue
                    obb = self._fit_available(candidate, source, forbidden)
                    if obb is None or min(obb.width, obb.height) < self.min_part_side - 1e-8:
                        continue
                    rectangle = Polygon(obb.polygon)
                    if rectangle.intersection(forbidden).area > AREA_EPSILON:
                        continue
                    best, best_shape, best_area = obb, rectangle, candidate.area
            if best is None:
                # Simplification can erase a narrow wall's opposing edges when
                # it is attached to a much larger, curved contour.
                for component in _polygons(remaining):
                    for candidate in self._parallel_candidates(component, simplify=False):
                        if candidate.area <= best_area:
                            continue
                        obb = self._fit_available(candidate, source, forbidden)
                        if obb is None or min(obb.width, obb.height) < self.min_part_side - 1e-8:
                            continue
                        rectangle = Polygon(obb.polygon)
                        if rectangle.intersection(forbidden).area > AREA_EPSILON:
                            continue
                        best, best_shape, best_area = obb, rectangle, candidate.area
            if best is None or best_shape is None:
                break
            result.append(best)
            # Snap Boolean operations below pixel precision to prevent long,
            # near-zero-width spikes along shared edges of rotated rectangles.
            remaining = unary_union(_polygons(remaining.difference(best_shape, grid_size=1e-9)))
            forbidden = forbidden.union(best_shape)

        if result and self.residual_margin:
            obb_margins = unary_union([
                Polygon(obb.polygon).buffer(self.residual_margin, join_style=2)
                for obb in result
            ])
            # Opening identifies narrow leftovers without trimming wider walls nearby.
            thick_residual = remaining.buffer(
                -self.residual_margin / 2, join_style=2
            ).buffer(self.residual_margin / 2, join_style=2)
            thin_near_obbs = remaining.difference(thick_residual).intersection(obb_margins)
            remaining = remaining.difference(thin_near_obbs)

        for part in _polygons(remaining):
            result.append(RegionPolygon(
                source=source.source,
                polygon=np.asarray(part.exterior.coords)[:-1].copy(),
                holes=[np.asarray(ring.coords)[:-1].copy() for ring in part.interiors],
                source_polygon=source,
            ))
        return result
