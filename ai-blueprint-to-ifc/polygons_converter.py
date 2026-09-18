from dataclasses import dataclass, field
import math
from time import perf_counter

import cv2
import numpy as np
import shapely
from shapely.geometry import LineString, Polygon as ShapelyPolygon
from shapely.ops import unary_union
from shapely.affinity import affine_transform
from shapely.geometry import box
from shapely.strtree import STRtree

from config import PolygonConversionSettings
from logger import setup_logger
from mask_polygonizer import PdfMaskPolygon, PdfPoint, PdfPolygonizedMask


PdfObb = dict[str, float]
logger = setup_logger(__name__)


@dataclass(frozen=True)
class WallObb:
    """One straight wall segment represented by an OBB in PDF points."""

    bbox_pdf: PdfObb
    legend_entry_id: int
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
        stage_start = perf_counter()
        skipped_polygon_ids: list[int] = []
        geometries_by_legend: dict[int, list] = {}

        for polygon_id, polygon in enumerate(walls_polygons_pdf.polygons):
            geometries_by_legend.setdefault(polygon.legend_entry_id, []).append(
                self._polygon_geometry(polygon)
            )
            polygon_walls = self._convert_polygon(polygon, polygon_id)
            if polygon_walls:
                walls.extend(polygon_walls)
            else:
                skipped_polygon_ids.append(polygon_id)

        logger.info('Polygon conversion: candidates %.2fs, polygons=%d, walls=%d',
                       perf_counter()-stage_start, len(walls_polygons_pdf.polygons), len(walls))
        stage_start = perf_counter()
        merged_walls: list[WallObb] = []
        for legend_entry_id in {wall.legend_entry_id for wall in walls}:
            legend_walls = [
                wall for wall in walls
                if wall.legend_entry_id == legend_entry_id
            ]
            legend_geometry = unary_union(geometries_by_legend[legend_entry_id])
            legend_walls = self._merge_collinear_walls(
                legend_walls,
                legend_geometry,
            )
            merged_walls.extend(
                self._close_perpendicular_junctions(
                    legend_walls,
                    legend_geometry,
                )
            )

        logger.info('Polygon conversion: junctions %.2fs, walls=%d', perf_counter()-stage_start, len(merged_walls))
        stage_start = perf_counter()
        merged_walls = self._partition_final_walls(
            merged_walls, geometries_by_legend, walls_polygons_pdf.polygons
        )
        logger.info('Polygon conversion: partition %.2fs, walls=%d', perf_counter()-stage_start, len(merged_walls))
        stage_start = perf_counter()
        merged_walls = self._coalesce_partition(merged_walls)
        merged_walls = self._filter_small_walls(merged_walls)
        logger.info('Polygon conversion: coalesce %.2fs, walls=%d', perf_counter()-stage_start, len(merged_walls))
        return PolygonObbConversionResult(
            walls=merged_walls,
            skipped_polygon_ids=skipped_polygon_ids,
        )

    def _filter_small_walls(self, walls: list[WallObb]) -> list[WallObb]:
        # Apply only after joining fragments; a thin but long wall is retained.
        threshold = self.settings.final_min_wall_length
        return [w for w in walls if max(w.length, w.thickness) >= threshold]

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

        polygon_geometry = self._polygon_geometry(polygon)

        candidates: list[WallObb] = []
        for first_index, first in enumerate(edges):
            for second_index in range(first_index + 1, len(edges)):
                second = edges[second_index]
                wall = self._edges_to_wall(
                    first,
                    second,
                    polygon.legend_entry_id,
                    polygon_id,
                )
                if wall is not None:
                    candidates.extend(
                        self._split_wall_by_coverage(wall, polygon_geometry)
                    )

        return self._deduplicate(candidates)

    @staticmethod
    def _polygon_geometry(polygon: PdfMaskPolygon):
        geometry = ShapelyPolygon(polygon.exterior, polygon.holes)
        return geometry if geometry.is_valid else geometry.buffer(0)

    @staticmethod
    def _wall_geometry(wall: WallObb):
        return ShapelyPolygon([
            (wall.bbox_pdf[f"x{i}"], wall.bbox_pdf[f"y{i}"])
            for i in range(1, 5)
        ])

    def _coalesce_partition(self, walls: list[WallObb]) -> list[WallObb]:
        """Merge touching fragments, tolerating bounded contour-edge jitter."""
        current = list(walls)
        buffered_references = {}
        original = {
            material: unary_union([
                self._wall_geometry(w) for w in walls if w.legend_entry_id == material
            ])
            for material in {w.legend_entry_id for w in walls}
        }
        while len(current) > 1:
            geometries = [self._wall_geometry(wall) for wall in current]
            tree = STRtree(geometries)
            consumed = set()
            result = []
            changed = False
            for i, wall in enumerate(current):
                if i in consumed:
                    continue
                combined_wall = wall
                # Only touching neighbours; sorting makes the result repeatable.
                for j in sorted(int(j) for j in tree.query(geometries[i].buffer(1e-7))):
                    if j <= i or j in consumed:
                        continue
                    other = current[j]
                    if wall.legend_entry_id != other.legend_entry_id:
                        continue
                    neighbour = geometries[j]
                    if not geometries[i].intersects(neighbour) and geometries[i].distance(neighbour) <= 1e-7:
                        neighbour = shapely.snap(neighbour, geometries[i], 1e-7)
                    union = geometries[i].union(neighbour)
                    if union.geom_type != "Polygon" or union.interiors:
                        continue
                    rectangle = union.minimum_rotated_rectangle
                    if rectangle.symmetric_difference(union).area > 1e-8:
                        tolerance = min(
                            self.settings.straight_simplification_tolerance,
                            min(wall.thickness, other.thickness) * 0.25,
                        )
                        if tolerance <= 0:
                            continue
                        # A bounded contour displacement, not a percentage of
                        # total wall area. Always compare with the original
                        # geometry so rounding does not accumulate over merges.
                        reference = original[wall.legend_entry_id]
                        buffer_key = (wall.legend_entry_id, tolerance)
                        if buffer_key not in buffered_references:
                            buffered_references[buffer_key] = reference.buffer(tolerance)
                        if not buffered_references[buffer_key].covers(rectangle):
                            continue
                        if rectangle.hausdorff_distance(union) > tolerance:
                            continue
                        added = rectangle.difference(union)
                        collision = False
                        for k in tree.query(rectangle):
                            k = int(k)
                            if k not in (i, j) and k not in consumed:
                                if added.intersection(geometries[k]).area > 1e-8:
                                    collision = True
                                    break
                        # Include already merged neighbours from this pass.
                        if collision or any(
                            added.intersection(self._wall_geometry(w)).area > 1e-8
                            for w in result
                        ):
                            continue
                    coords = list(rectangle.exterior.coords)[:4]
                    lengths = [math.dist(coords[k], coords[(k+1) % 4]) for k in range(4)]
                    if lengths[1] > lengths[0]:
                        coords = coords[1:] + coords[:1]
                    combined_wall = WallObb(
                        bbox_pdf={f"{name}{k+1}": float(point[d])
                                  for k, point in enumerate(coords)
                                  for d, name in enumerate(("x", "y"))},
                        legend_entry_id=wall.legend_entry_id,
                        source_polygon_id=wall.source_polygon_id,
                        length=math.dist(coords[0], coords[1]),
                        thickness=math.dist(coords[1], coords[2]),
                        angle_degrees=math.degrees(math.atan2(
                            coords[1][1]-coords[0][1], coords[1][0]-coords[0][0]
                        )) % 180,
                        approximated_curve=wall.approximated_curve or other.approximated_curve,
                    )
                    consumed.add(j)
                    changed = True
                    break
                result.append(combined_wall)
            current = result
            if not changed:
                break
        return current

    def _rectangles_inside(self, geometry, source: WallObb, *, align_components=False, junction=False) -> list[WallObb]:
        """Partition in the wall frame; never enclose holes in a bounding box."""
        if geometry.is_empty or geometry.area <= 1e-9:
            return []
        if align_components:
            parts = [geometry] if geometry.geom_type == 'Polygon' else [
                g for g in getattr(geometry, 'geoms', []) if g.geom_type == 'Polygon'
            ]
            result = []
            for part in parts:
                frame = part.minimum_rotated_rectangle
                if frame.geom_type != 'Polygon':
                    continue
                if part.area < frame.area * 0.9:
                    result.extend(self._rectangles_inside(part, source))
                    continue
                points = list(frame.exterior.coords)[:4]
                if math.dist(points[1], points[2]) > math.dist(points[0], points[1]):
                    points = points[1:] + points[:1]
                seed = WallObb(
                    {f'{c}{i+1}': p[k] for i, p in enumerate(points)
                     for k, c in enumerate(('x', 'y'))},
                    source.legend_entry_id, source.source_polygon_id,
                    math.dist(points[0], points[1]), math.dist(points[1], points[2]),
                    math.degrees(math.atan2(points[1][1]-points[0][1], points[1][0]-points[0][0])) % 180,
                    source.approximated_curve,
                )
                result.extend(self._rectangles_inside(part, seed))
            return result
        axis = self._wall_axis(source)
        normal = (-axis[1], axis[0])
        local = affine_transform(geometry, [*axis, *normal, 0, 0])
        polygons = [local] if local.geom_type == "Polygon" else [
            g for g in getattr(local, "geoms", []) if g.geom_type == "Polygon"
        ]
        rectangles = []
        for polygon in polygons:
            min_x, min_y, max_x, max_y = polygon.bounds
            # Boundary slivers cannot produce a wall of the minimum width.
            # Reject before slicing their potentially thousands of vertices.
            if min(max_x-min_x, max_y-min_y) < self.settings.min_wall_thickness:
                continue
            if polygon.buffer(-self.settings.min_wall_thickness * 0.499).is_empty:
                continue
            tolerance = min(self.settings.straight_simplification_tolerance,
                            source.thickness * 0.25)
            if junction:
                tolerance = max(tolerance, min(source.length, source.thickness))
            cuts = sorted({float(x) for ring in [polygon.exterior, *polygon.interiors]
                           for x, y in ring.coords})
            bands = []
            active = []
            for start, end in zip(cuts, cuts[1:]):
                if end - start <= 1e-8:
                    continue
                try:
                    strip = shapely.clip_by_rect(polygon, start, min_y, end, max_y)
                except shapely.errors.GEOSException:
                    strip = polygon.intersection(box(start, min_y, end, max_y))
                if not strip.is_valid:
                    strip = polygon.intersection(box(start, min_y, end, max_y))
                parts = [strip] if strip.geom_type == "Polygon" else getattr(strip, "geoms", [])
                for part in parts:
                    if part.geom_type != "Polygon" or part.area <= 1e-9:
                        continue
                    # Between vertex events the boundaries are straight. Their
                    # common vertical interval gives an inscribed rectangle.
                    left = part.intersection(LineString([(start, polygon.bounds[1]), (start, polygon.bounds[3])]))
                    right = part.intersection(LineString([(end, polygon.bounds[1]), (end, polygon.bounds[3])]))
                    if left.is_empty or right.is_empty:
                        continue
                    low = max(left.bounds[1], right.bounds[1])
                    high = min(left.bounds[3], right.bounds[3])
                    candidate = box(start, low, end, high)
                    if high <= low or candidate.difference(part).area > 1e-8:
                        continue
                    for index in active:
                        a, b, lo, hi, min_lo, max_hi = bands[index]
                        # Bound total boundary loss, not the change from the
                        # previous strip (which would accumulate on curves).
                        new_low, new_high = max(lo, low), min(hi, high)
                        if (abs(b - start) < 1e-8 and new_high > new_low
                                and (not junction or (end-a)*(new_high-new_low) >= (b-a)*(hi-lo)-1e-8)
                                and new_low - min(min_lo, low) <= tolerance + 1e-8
                                and max(max_hi, high) - new_high <= tolerance + 1e-8):
                            bands[index] = (a, end, new_low, new_high,
                                            min(min_lo, low), max(max_hi, high))
                            break
                    else:
                        bands.append((start, end, low, high, low, high))
                        active.append(len(bands)-1)
                active = [index for index in active if abs(bands[index][1]-end) < 1e-8]
            for start, end, low, high, _, _ in bands:
                # Junction patches can be shorter than a standalone wall.
                if min(end-start, high-low) < self.settings.min_wall_thickness:
                    continue
                wall = self._wall_from_axes(start, end, low, high, axis, normal, source)
                if self._wall_geometry(wall).difference(geometry).area <= 1e-8:
                    rectangles.append(wall)
        return rectangles

    def _partition_final_walls(self, walls, geometries_by_legend, polygons):
        """Assign each covered area once, including junctions missed by edge pairs."""
        masks = {key: unary_union(value) for key, value in geometries_by_legend.items()}
        occupied = []
        indexed_count = 0
        occupied_tree = None

        def subtract_occupied(geometry):
            nonlocal occupied_tree, indexed_count
            if len(occupied) - indexed_count >= 128:
                occupied_tree = STRtree(occupied)
                indexed_count = len(occupied)
            neighbours = [] if occupied_tree is None else [
                occupied[int(i)] for i in occupied_tree.query(geometry, predicate='intersects')
            ]
            neighbours.extend(g for g in occupied[indexed_count:] if g.intersects(geometry))
            return geometry.difference(unary_union(neighbours)) if neighbours else geometry

        result = []
        ordered = sorted(walls, key=lambda w: (-w.length, -w.thickness, w.legend_entry_id, w.source_polygon_id))
        sources_by_polygon = {}
        for wall in ordered:
            sources_by_polygon.setdefault(wall.source_polygon_id, wall)
            remaining = subtract_occupied(self._wall_geometry(wall).intersection(masks[wall.legend_entry_id]))
            pieces = self._rectangles_inside(remaining, wall, align_components=True)
            if pieces:
                result.extend(pieces)
                occupied.extend(self._wall_geometry(p) for p in pieces)
        source_shapes = [self._wall_geometry(w) for w in ordered]
        source_tree = STRtree(source_shapes)
        junction_gaps = self._angled_junction_gaps(ordered, source_shapes, source_tree)
        result = self._extend_partition_ends(result, masks, junction_gaps)
        occupied = [self._wall_geometry(w) for w in result]
        occupied_tree = STRtree(occupied)
        indexed_count = len(occupied)
        # Choose a local wall direction, not the first wall of a connected
        # polygon (one polygon can contain an entire floor's wall network).
        for polygon_id, polygon in enumerate(polygons):
            source = sources_by_polygon.get(polygon_id)
            if source is None:
                continue
            remaining = subtract_occupied(self._polygon_geometry(polygon))
            if polygon.legend_entry_id in junction_gaps:
                remaining = remaining.difference(junction_gaps[polygon.legend_entry_id])
            pieces = self._recover_local_residual(remaining, source, ordered, source_shapes, source_tree)
            if pieces:
                result.extend(pieces)
                occupied.extend(self._wall_geometry(p) for p in pieces)
        return result

    def _extend_partition_ends(self, walls, masks, junctions):
        """Extend OBBs into junction residuals, stopping at the first obstacle."""
        result = list(walls)
        shapes = [self._wall_geometry(w) for w in result]
        tree = STRtree(shapes)
        for index in range(len(walls)):
            wall = result[index]
            zone = junctions.get(wall.legend_entry_id)
            if zone is None or wall.length < 2*wall.thickness:
                continue
            axis = self._wall_axis(wall)
            normal = (-axis[1], axis[0])
            for end in (-1, 1):
                wall = result[index]
                along, across = self._wall_intervals(wall, axis, normal)
                tip = along[0] if end == -1 else along[1]
                center = sum(across)/2
                tip_point = shapely.geometry.Point(self._from_axes(tip, center, axis, normal))
                if zone.distance(tip_point) > wall.thickness:
                    continue
                reach = 2*self.settings.junction_gap_radius_ratio*wall.thickness
                def patch(distance, fraction):
                    start, stop = sorted((tip, tip+end*distance))
                    half = wall.thickness*fraction/2
                    return self._wall_from_axes(start,stop,center-half,center+half,axis,normal,wall)
                corridor = self._wall_geometry(patch(reach,1))
                neighbours = [shapes[int(j)] for j in tree.query(corridor) if int(j) != index]
                allowed = corridor.intersection(masks[wall.legend_entry_id]).intersection(zone.buffer(wall.thickness))
                if neighbours:
                    allowed = allowed.difference(unary_union(neighbours))
                best = None
                for fraction in (1.0, 0.75, 0.5):
                    low, high = 0.0, reach
                    for _ in range(22):
                        middle = (low+high)/2
                        candidate = patch(middle,fraction)
                        if self._wall_geometry(candidate).difference(allowed).area <= 1e-8:
                            low = middle
                        else:
                            high = middle
                    if low < self.settings.min_wall_thickness:
                        continue
                    candidate = patch(low,fraction)
                    if fraction == 1:
                        extended = self._wall_from_axes(
                            min(along[0], tip+end*low), max(along[1], tip+end*low),
                            across[0], across[1], axis, normal, wall)
                        result[index] = extended
                        shapes[index] = self._wall_geometry(extended)
                        allowed = allowed.difference(self._wall_geometry(candidate))
                        tip += end*low
                        reach -= low
                        best = None
                        if reach <= self.settings.min_wall_thickness:
                            break
                        continue
                    if best is None or candidate.length*candidate.thickness > best.length*best.thickness:
                        best = candidate
                if best is not None:
                    result.append(best)
                    shapes.append(self._wall_geometry(best))
                tree = STRtree(shapes)
        return result

    def _angled_junction_gaps(self, walls, shapes, tree):
        """Do not invent extra OBBs for wedges between existing angled walls."""
        ratio = self.settings.junction_gap_radius_ratio
        if ratio == 0:
            return {}
        zones = {}
        for i, first in enumerate(walls):
            if first.length < 2*first.thickness:
                continue
            for j in tree.query(shapes[i].buffer(2*ratio*first.thickness)):
                j = int(j)
                second = walls[j]
                if j <= i or first.legend_entry_id != second.legend_entry_id or second.length < 2*second.thickness:
                    continue
                angle = math.degrees(self._angle_difference(self._wall_axis(first), self._wall_axis(second)))
                if angle < 5 or angle > 85:
                    continue
                point = self._line_intersection(self._wall_center(first), self._wall_axis(first),
                                               self._wall_center(second), self._wall_axis(second))
                if point is None:
                    continue
                center = shapely.geometry.Point(point)
                thickness = min(first.thickness, second.thickness)
                if max(center.distance(shapes[i]), center.distance(shapes[j])) > 2*ratio*thickness:
                    continue
                radius = ratio*thickness
                x, y = point
                zones.setdefault(first.legend_entry_id, []).append(box(x-radius,y-radius,x+radius,y+radius))
        return {key: unary_union(value) for key, value in zones.items()}

    def _recover_local_residual(self, geometry, fallback, walls, shapes, tree, *, junction=False):
        parts = [geometry] if geometry.geom_type == 'Polygon' else [
            g for g in getattr(geometry, 'geoms', []) if g.geom_type == 'Polygon'
        ]
        result = []
        for part in parts:
            if part.area <= 1e-9:
                continue
            reach = max(self.settings.min_wall_length, fallback.thickness)
            neighbours = [int(i) for i in tree.query(part.buffer(reach))
                          if walls[int(i)].source_polygon_id == fallback.source_polygon_id
                          and walls[int(i)].legend_entry_id == fallback.legend_entry_id]
            neighbours.sort(key=lambda i: (part.distance(shapes[i]), -walls[i].length, i))
            directions = []
            for i in neighbours[:16]:
                wall = walls[i]
                if all(self._angle_difference(self._wall_axis(wall), self._wall_axis(w))
                       > math.radians(2) for w in directions):
                    directions.append(wall)
                if len(directions) == 3:
                    break
            if not directions:
                directions = [fallback]
            # Select the direction by contour support before doing any cuts.
            # Repeating a full sweep in several frames is expensive on raster
            # contours and itself generates thousands of rejected fragments.
            simplified = part.simplify(self.settings.straight_simplification_tolerance,
                                       preserve_topology=True)
            vectors = np.diff(np.asarray(simplified.exterior.coords), axis=0)
            lengths = np.linalg.norm(vectors, axis=1)
            valid = lengths > 1e-9
            vectors, lengths = vectors[valid], lengths[valid]
            def support(wall):
                alignment = np.abs(vectors @ np.asarray(self._wall_axis(wall))) / lengths
                return float(np.sum(lengths * alignment**16))
            direction = max(directions, key=support)
            result.extend(self._rectangles_inside(part, direction, junction=junction))
        return result

    @classmethod
    def _wall_is_inside_polygon(
        cls,
        wall: WallObb,
        polygon_geometry,
        minimum_coverage: float = 0.9,
    ) -> bool:
        points = [
            (
                wall.bbox_pdf[f"x{index}"],
                wall.bbox_pdf[f"y{index}"],
            )
            for index in range(1, 5)
        ]
        wall_geometry = ShapelyPolygon(points)
        if wall_geometry.is_empty or wall_geometry.area <= 0:
            return False

        covered_area = polygon_geometry.intersection(wall_geometry).area
        return covered_area / wall_geometry.area >= minimum_coverage

    def _split_wall_by_coverage(
        self,
        wall: WallObb,
        polygon_geometry,
        minimum_cross_section_coverage: float = 0.9,
    ) -> list[WallObb]:
        axis = self._wall_axis(wall)
        normal = (-axis[1], axis[0])
        axis_interval, normal_interval = self._wall_intervals(
            wall,
            axis,
            normal,
        )
        length = axis_interval[1] - axis_interval[0]
        thickness = normal_interval[1] - normal_interval[0]
        if length <= 0 or thickness <= 0:
            return []
        if polygon_geometry.covers(self._wall_geometry(wall)):
            return [wall]

        sample_step = max(
            self.settings.min_wall_length / 2,
            min(thickness / 2, 1.0),
        )
        sample_count = max(1, math.ceil(length / sample_step))
        sample_step = length / sample_count
        occupied: list[bool] = []

        # Shapely's array API avoids one Python/GEOS call per cross-section.
        # Batches bound temporary memory for very long walls.
        for offset in range(0, sample_count, 4096):
            positions = axis_interval[0] + (
                np.arange(offset, min(offset+4096, sample_count)) + 0.5
            ) * sample_step
            coordinates = (positions[:, None, None] * np.asarray(axis)
                           + np.asarray(normal_interval)[None, :, None] * np.asarray(normal))
            sections = shapely.linestrings(coordinates)
            lengths = shapely.length(shapely.intersection(polygon_geometry, sections))
            occupied.extend((lengths / thickness >= minimum_cross_section_coverage).tolist())

        segments: list[WallObb] = []
        run_start: int | None = None
        for sample_index, is_occupied in enumerate([*occupied, False]):
            if is_occupied and run_start is None:
                run_start = sample_index
            elif not is_occupied and run_start is not None:
                segment_start = axis_interval[0] + run_start * sample_step
                segment_end = axis_interval[0] + sample_index * sample_step
                segment_length = segment_end - segment_start
                if (
                    segment_length >= self.settings.min_wall_length
                    and segment_length / thickness
                    >= self.settings.min_length_to_thickness_ratio
                ):
                    segments.append(
                        self._wall_from_axes(
                            segment_start,
                            segment_end,
                            normal_interval[0],
                            normal_interval[1],
                            axis,
                            normal,
                            wall,
                        )
                    )
                run_start = None

        return segments

    def _merge_collinear_walls(
        self,
        walls: list[WallObb],
        polygon_geometry,
    ) -> list[WallObb]:
        merged = list(walls)

        while True:
            consumed = set()
            result = []
            for first_index, first in enumerate(merged):
                if first_index in consumed:
                    continue
                for second_index in range(first_index + 1, len(merged)):
                    if second_index in consumed:
                        continue
                    combined = self._try_merge_collinear_walls(
                        first,
                        merged[second_index],
                        polygon_geometry,
                    )
                    if combined is None:
                        continue

                    first = combined
                    consumed.add(second_index)
                    break
                result.append(first)
            if not consumed:
                return result
            merged = result

    def _try_merge_collinear_walls(
        self,
        first: WallObb,
        second: WallObb,
        polygon_geometry,
    ) -> WallObb | None:
        if first.legend_entry_id != second.legend_entry_id:
            return None

        axis = self._wall_axis(first)
        second_axis = self._wall_axis(second)
        if self._angle_difference(axis, second_axis) > math.radians(
            self.settings.parallel_angle_tolerance_degrees
        ):
            return None

        normal = (-axis[1], axis[0])
        first_axis_interval, first_normal_interval = self._wall_intervals(
            first,
            axis,
            normal,
        )
        second_axis_interval, second_normal_interval = self._wall_intervals(
            second,
            axis,
            normal,
        )

        thickness_tolerance = max(
            self.settings.deduplication_tolerance,
            min(first.thickness, second.thickness) * 0.25,
        )
        if abs(
            sum(first_normal_interval) / 2
            - sum(second_normal_interval) / 2
        ) > thickness_tolerance:
            return None
        if abs(first.thickness - second.thickness) > thickness_tolerance:
            return None

        if first_axis_interval[1] < second_axis_interval[0]:
            gap_start, gap_end = first_axis_interval[1], second_axis_interval[0]
        elif second_axis_interval[1] < first_axis_interval[0]:
            gap_start, gap_end = second_axis_interval[1], first_axis_interval[0]
        else:
            return None

        normal_start = max(first_normal_interval[0], second_normal_interval[0])
        normal_end = min(first_normal_interval[1], second_normal_interval[1])
        if normal_end <= normal_start:
            return None

        bridge = self._wall_from_axes(
            gap_start,
            gap_end,
            normal_start,
            normal_end,
            axis,
            normal,
            first,
        )
        if not self._wall_is_inside_polygon(
            bridge,
            polygon_geometry,
            minimum_coverage=0.95,
        ):
            return None

        return self._wall_from_axes(
            min(first_axis_interval[0], second_axis_interval[0]),
            max(first_axis_interval[1], second_axis_interval[1]),
            (first_normal_interval[0] + second_normal_interval[0]) / 2,
            (first_normal_interval[1] + second_normal_interval[1]) / 2,
            axis,
            normal,
            first,
            approximated_curve=(
                first.approximated_curve or second.approximated_curve
            ),
        )

    def _close_perpendicular_junctions(
        self,
        walls: list[WallObb],
        polygon_geometry,
    ) -> list[WallObb]:
        result = list(walls)
        angle_tolerance = math.radians(
            self.settings.parallel_angle_tolerance_degrees
        )

        for first_index in range(len(result)):
            for second_index in range(first_index + 1, len(result)):
                first = result[first_index]
                second = result[second_index]
                if first.legend_entry_id != second.legend_entry_id:
                    continue

                first_axis = self._wall_axis(first)
                second_axis = self._wall_axis(second)
                angle = self._angle_difference(first_axis, second_axis)
                if abs(angle - math.pi / 2) > angle_tolerance:
                    continue

                intersection = self._line_intersection(
                    self._wall_center(first),
                    first_axis,
                    self._wall_center(second),
                    second_axis,
                )
                if intersection is None:
                    continue

                if first.length >= second.length:
                    through_index, through = first_index, first
                    joining_index, joining = second_index, second
                else:
                    through_index, through = second_index, second
                    joining_index, joining = first_index, first

                extended_through = self._extend_wall_to_junction(
                    through,
                    intersection,
                    joining.thickness,
                    polygon_geometry,
                )
                fitted_joining = self._fit_wall_to_junction_face(
                    joining,
                    intersection,
                    through.thickness,
                    polygon_geometry,
                )
                if extended_through is not None:
                    result[through_index] = extended_through
                if fitted_joining is not None:
                    result[joining_index] = fitted_joining

        return result

    def _extend_wall_to_junction(
        self,
        wall: WallObb,
        intersection: PdfPoint,
        crossing_thickness: float,
        polygon_geometry,
    ) -> WallObb | None:
        axis = self._wall_axis(wall)
        normal = (-axis[1], axis[0])
        axis_interval, normal_interval = self._wall_intervals(
            wall,
            axis,
            normal,
        )
        intersection_position = self._dot(intersection, axis)
        maximum_gap = (
            crossing_thickness * 1.5
            + self.settings.deduplication_tolerance
        )

        axis_start, axis_end = axis_interval
        half_crossing_thickness = crossing_thickness / 2
        if intersection_position < axis_start:
            if axis_start - intersection_position > maximum_gap:
                return None
            axis_start = intersection_position - half_crossing_thickness
        elif intersection_position > axis_end:
            if intersection_position - axis_end > maximum_gap:
                return None
            axis_end = intersection_position + half_crossing_thickness
        else:
            return None

        extended = self._wall_from_axes(
            axis_start,
            axis_end,
            normal_interval[0],
            normal_interval[1],
            axis,
            normal,
            wall,
        )
        if not self._wall_is_inside_polygon(
            extended,
            polygon_geometry,
            minimum_coverage=0.95,
        ):
            return None
        return extended

    def _fit_wall_to_junction_face(
        self,
        wall: WallObb,
        intersection: PdfPoint,
        through_thickness: float,
        polygon_geometry,
    ) -> WallObb | None:
        axis = self._wall_axis(wall)
        normal = (-axis[1], axis[0])
        axis_interval, normal_interval = self._wall_intervals(
            wall,
            axis,
            normal,
        )
        intersection_position = self._dot(intersection, axis)
        half_thickness = through_thickness / 2
        maximum_gap = (
            through_thickness * 1.5
            + self.settings.deduplication_tolerance
        )
        axis_start, axis_end = axis_interval

        if intersection_position <= axis_start:
            target = intersection_position + half_thickness
            if axis_start - target > maximum_gap or target >= axis_end:
                return None
            axis_start = target
        elif intersection_position >= axis_end:
            target = intersection_position - half_thickness
            if target - axis_end > maximum_gap or target <= axis_start:
                return None
            axis_end = target
        else:
            return None

        fitted = self._wall_from_axes(
            axis_start,
            axis_end,
            normal_interval[0],
            normal_interval[1],
            axis,
            normal,
            wall,
        )
        if not self._wall_is_inside_polygon(
            fitted,
            polygon_geometry,
            minimum_coverage=0.95,
        ):
            return None
        return fitted

    @classmethod
    def _wall_center(cls, wall: WallObb) -> PdfPoint:
        points = [
            (wall.bbox_pdf[f"x{index}"], wall.bbox_pdf[f"y{index}"])
            for index in range(1, 5)
        ]
        return (
            sum(point[0] for point in points) / 4,
            sum(point[1] for point in points) / 4,
        )

    @staticmethod
    def _line_intersection(
        first_point: PdfPoint,
        first_axis: PdfPoint,
        second_point: PdfPoint,
        second_axis: PdfPoint,
    ) -> PdfPoint | None:
        cross = (
            first_axis[0] * second_axis[1]
            - first_axis[1] * second_axis[0]
        )
        if abs(cross) < 1e-9:
            return None

        delta = (
            second_point[0] - first_point[0],
            second_point[1] - first_point[1],
        )
        distance = (
            delta[0] * second_axis[1]
            - delta[1] * second_axis[0]
        ) / cross
        return (
            first_point[0] + first_axis[0] * distance,
            first_point[1] + first_axis[1] * distance,
        )

    @classmethod
    def _wall_axis(cls, wall: WallObb) -> PdfPoint:
        start = (wall.bbox_pdf["x1"], wall.bbox_pdf["y1"])
        end = (wall.bbox_pdf["x2"], wall.bbox_pdf["y2"])
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.hypot(dx, dy)
        return cls._canonical_axis((dx / length, dy / length))

    @classmethod
    def _wall_intervals(
        cls,
        wall: WallObb,
        axis: PdfPoint,
        normal: PdfPoint,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        points = [
            (wall.bbox_pdf[f"x{index}"], wall.bbox_pdf[f"y{index}"])
            for index in range(1, 5)
        ]
        axis_values = [cls._dot(point, axis) for point in points]
        normal_values = [cls._dot(point, normal) for point in points]
        return (
            (min(axis_values), max(axis_values)),
            (min(normal_values), max(normal_values)),
        )

    @classmethod
    def _wall_from_axes(
        cls,
        axis_start: float,
        axis_end: float,
        normal_start: float,
        normal_end: float,
        axis: PdfPoint,
        normal: PdfPoint,
        source: WallObb,
        approximated_curve: bool | None = None,
    ) -> WallObb:
        points = [
            cls._from_axes(axis_start, normal_start, axis, normal),
            cls._from_axes(axis_end, normal_start, axis, normal),
            cls._from_axes(axis_end, normal_end, axis, normal),
            cls._from_axes(axis_start, normal_end, axis, normal),
        ]
        bbox_pdf = {
            f"{coordinate}{index}": float(point[value_index])
            for index, point in enumerate(points, start=1)
            for coordinate, value_index in (("x", 0), ("y", 1))
        }
        return WallObb(
            bbox_pdf=bbox_pdf,
            legend_entry_id=source.legend_entry_id,
            source_polygon_id=source.source_polygon_id,
            length=axis_end - axis_start,
            thickness=normal_end - normal_start,
            angle_degrees=math.degrees(math.atan2(axis[1], axis[0])) % 180,
            approximated_curve=(
                source.approximated_curve
                if approximated_curve is None
                else approximated_curve
            ),
        )

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
        legend_entry_id: int,
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
            legend_entry_id=legend_entry_id,
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
                wall.legend_entry_id,
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
