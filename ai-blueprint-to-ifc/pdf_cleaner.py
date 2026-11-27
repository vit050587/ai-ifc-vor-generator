from walls_processor import WallsProcessor
from layout_processor import LayoutProcessor
from pdf_processor import PdfProcessor
from rectangle_utils import get_obb_points
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path
from joblib import Memory

import cv2
import numpy as np
import shapely as sh
import pymupdf

from config import settings

memory = Memory("cache", verbose=0)

_mupdf = pymupdf.mupdf
_PAINT_OPERATIONS = {
    "fill_path", "stroke_path", "fill_text", "stroke_text",
    "fill_image", "fill_image_mask", "fill_shade",
}
_DEVICE_OPERATIONS = tuple(
    name.removeprefix("use_virtual_")
    for name in dir(_mupdf.FzDevice2)
    if name.startswith("use_virtual_")
    and name not in {"use_virtual_close_device", "use_virtual_drop_device"}
)


def _forward_to(draw, *, paint=False):
    """Передать команду MuPDF дальше, учитывая видимость слоя при отрисовке."""

    def forward(self, _ctx, *args):
        if paint and self._hides_paint():
            return
        return draw(self.target.m_internal, *args)

    return forward


class _LayerFilterDevice(_mupdf.FzDevice2):
    def __init__(self, target, excluded_layers):
        super().__init__()
        self.target = target
        self.excluded_layers = frozenset(excluded_layers)
        self.layers = []
        self.mask_depth = 0
        for operation in _DEVICE_OPERATIONS:
            getattr(self, f"use_virtual_{operation}")()

    def _hides_paint(self):
        # Маску прозрачности нужно построить даже на скрытом слое.
        if self.mask_depth or not self.layers:
            return False
        return self.layers[-1] in self.excluded_layers

    def begin_layer(self, _ctx, name):
        self.layers.append(name or "")

    def end_layer(self, _ctx):
        if self.layers:
            self.layers.pop()

    def begin_mask(self, _ctx, *args):
        self.mask_depth += 1
        return _mupdf.ll_fz_begin_mask(self.target.m_internal, *args)

    def end_mask(self, _ctx, transfer_function):
        self.mask_depth -= 1
        return _mupdf.ll_fz_end_mask_tr(self.target.m_internal, transfer_function)

    def fill_text(self, _ctx, text, matrix, colorspace, color, alpha, color_params):
        if self._hides_paint():
            return
        # MuPDF передаёт указатель на цвет, а PyMuPDF принимает его компоненты.
        count = _mupdf.ll_fz_colorspace_n(colorspace) if colorspace else 0
        components = tuple(_mupdf.floats_getitem(color, i) for i in range(count))
        return _mupdf.ll_fz_fill_text(
            self.target.m_internal, text, matrix, colorspace, components, alpha, color_params,
        )

    begin_tile = _forward_to(_mupdf.ll_fz_begin_tile_tid)


# Для остальных команд сигнатуры MuPDF совпадают, поэтому обработчик общий.
for _operation in _DEVICE_OPERATIONS:
    if _operation not in vars(_LayerFilterDevice):
        _draw = getattr(_mupdf, f"ll_fz_{_operation}")
        setattr(
            _LayerFilterDevice, _operation,
            _forward_to(_draw, paint=_operation in _PAINT_OPERATIONS),
        )


def _filtered(display: pymupdf.DisplayList, excluded_layers: set[str]) -> pymupdf.DisplayList:
    """Replay a page display list with the named layers' visible paint suppressed."""
    result = pymupdf.DisplayList(_mupdf.fz_bound_display_list(display.this))
    with ExitStack() as devices:
        target = _mupdf.fz_new_list_device(result.this)
        devices.callback(_mupdf.fz_close_device, target)
        gate = _LayerFilterDevice(target, excluded_layers)
        devices.callback(_mupdf.fz_close_device, gate)
        _mupdf.fz_run_display_list(
            display.this, gate, _mupdf.FzMatrix(),
            _mupdf.FzRect(_mupdf.FzRect.Fixed_INFINITE), _mupdf.FzCookie(),
        )
    return result


def _flatten_curve(points):
    """Разбить кубическую кривую Безье на отрезки с допуском в точках PDF."""
    a, b, c, d = np.asarray(points, dtype=float)
    chord = d - a
    length = np.linalg.norm(chord)
    controls = np.array([b - a, c - a])
    if length:
        deviation = max(abs(chord[0] * controls[:, 1] - chord[1] * controls[:, 0])) / length
    else:
        deviation = max(np.linalg.norm(controls, axis=1))
    excess = sum(np.linalg.norm(step) for step in (b - a, c - b, d - c)) - length
    if deviation <= 0.025 and excess <= 0.10:
        return [tuple(a), tuple(d)]

    ab, bc, cd = (a + b) / 2, (b + c) / 2, (c + d) / 2
    left, right = (ab + bc) / 2, (bc + cd) / 2
    middle = (left + right) / 2
    return _flatten_curve((a, ab, left, middle))[:-1] + _flatten_curve((middle, right, cd, d))


def _subpaths(path):
    """Последовательности соединённых точек в координатах неповёрнутой страницы."""
    run = []
    for kind, *args in path["items"]:
        if kind in ("re", "qu"):
            if run:
                yield run
                run = []
            if kind == "re":
                x0, y0, x1, y1 = args[0]
                ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
                yield ring[::-1] if len(args) > 1 and args[1] < 0 else ring
            else:
                ul, ur, ll, lr = args[0]
                yield [ul, ur, lr, ll, ul]
            continue

        points = _flatten_curve(args) if kind == "c" else args
        if run and run[-1] != points[0]:
            yield run
            run = []
        if not run:
            run.append(points[0])
        run.extend(points[1:])
    if run:
        if path.get("closePath") and run[-1] != run[0]:
            run.append(run[0])
        yield run


def _clip_region(path):
    rings = []
    for points in _subpaths(path):
        if len(points) >= 3:
            rings.append(np.asarray(points if points[0] == points[-1] else [*points, points[0]]))
    if len(rings) == 1:
        polygon = sh.Polygon(rings[0])
        if polygon.is_valid:
            return polygon

    # Для составного контура применяем правило заливки PDF к каждой замкнутой области.
    edges = sh.union_all([sh.LineString(ring) for ring in rings])
    faces = sh.get_parts(sh.polygonize(sh.get_parts(edges)))
    included = []
    for face in faces:
        point = face.representative_point()
        winding = 0
        for ring in rings:
            x0, y0 = ring[:-1].T
            x1, y1 = ring[1:].T
            cross = (x1 - x0) * (point.y - y0) - (y1 - y0) * (point.x - x0)
            winding += np.count_nonzero((y0 <= point.y) & (point.y < y1) & (cross > 0))
            winding -= np.count_nonzero((y1 <= point.y) & (point.y < y0) & (cross < 0))
        if (winding % 2 if path.get("even_odd") else winding):
            included.append(face)
    return sh.union_all(included)


def _clip_edges(edges, region):
    if region.equals(region.envelope):
        x0, y0, x1, y1 = region.bounds
        inside = ((edges >= (x0, y0)) & (edges <= (x1, y1))).all(axis=(1, 2))
        if inside.all():
            return edges
    else:
        inside = np.zeros(len(edges), dtype=bool)

    clipped = sh.intersection(sh.linestrings(edges[~inside]), region)
    parts = sh.get_parts(sh.get_parts(clipped))
    lines = parts[(sh.get_type_id(parts) == sh.GeometryType.LINESTRING) & ~sh.is_empty(parts)]
    starts = sh.get_coordinates(sh.get_point(lines, 0))
    ends = sh.get_coordinates(sh.get_point(lines, -1))
    return np.concatenate((edges[inside], np.stack((starts, ends), axis=1)))


def segments(page):
    """Выдать пары (имя слоя, массив отрезков N × 2 × 2) в координатах листа."""
    regions = [sh.box(*tuple(page.rect * page.derotation_matrix))]
    clip_stack = []
    by_layer_clip = defaultdict(list)
    for path in page.get_cdrawings(extended=True):
        level = path["level"]
        while clip_stack and clip_stack[-1][0] >= level:
            clip_stack.pop()
        region_id = clip_stack[-1][1] if clip_stack else 0
        kind = path["type"]
        if kind == "clip":
            region = regions[region_id].intersection(_clip_region(path))
            clip_stack.append((level, len(regions)))
            regions.append(region)
            continue

        name = path.get("layer")
        if not name or kind not in ("s", "f", "fs"):
            continue
        fill = "f" in kind and min(path.get("fill") or (1,)) < 232 / 255 and path["fill_opacity"] > 0
        stroke = "s" in kind and min(path.get("color") or (1,)) < 232 / 255 and path["stroke_opacity"] > 0
        if not (fill or stroke):
            continue
        batch = by_layer_clip[name, region_id]
        for points in _subpaths(path):
            if fill and len(points) > 2 and points[-1] != points[0]:
                points.append(points[0])
            batch.extend(zip(points, points[1:]))

    by_layer = defaultdict(list)
    for (name, region_id), pairs in by_layer_clip.items():
        region = regions[region_id]
        if pairs and region.area > 0:
            by_layer[name].append(_clip_edges(np.asarray(pairs, dtype=float), region))

    rotation = page.rotation_matrix
    matrix = np.array([[rotation.a, rotation.b], [rotation.c, rotation.d]])
    for name, batches in by_layer.items():
        edges = np.concatenate(batches) @ matrix + (rotation.e, rotation.f)
        # Убираем повторные отрезки, в том числе нарисованные в обратном направлении.
        keys = np.rint(edges * 10000).astype(np.int64)
        start, end = keys[:, 0], keys[:, 1]
        reverse = (start[:, 0] > end[:, 0]) | ((start[:, 0] == end[:, 0]) & (start[:, 1] > end[:, 1]))
        keys[reverse] = keys[reverse, ::-1]
        _, indices = np.unique(keys.reshape(-1, 4), axis=0, return_index=True)
        unique = edges[indices]
        yield name, unique[np.linalg.norm(unique[:, 1] - unique[:, 0], axis=1) > 1e-5]


def vector_fraction(edges, polygons, tree):
    """Доля суммарной длины отрезков внутри объединения прямоугольников YOLO."""
    lengths = np.linalg.norm(edges[:, 1] - edges[:, 0], axis=1)
    total_length = lengths.sum()
    if not total_length:
        return None

    covered = 0.0
    for offset in range(0, len(edges), 4000):
        batch = edges[offset:offset + 4000]
        line_ids, box_ids = tree.query(sh.linestrings(batch), predicate="intersects")
        if not len(line_ids):
            continue

        # Отрезок параметризован от 0 до 1. Обе оси OBB ограничивают интервал внутри него.
        boxes = polygons[box_ids]
        origins = batch[line_ids, 0] - boxes[:, 0]
        directions = batch[line_ids, 1] - batch[line_ids, 0]
        enter = np.zeros(len(line_ids))
        leave = np.ones(len(line_ids))
        for corner in (1, 3):
            axis = boxes[:, corner] - boxes[:, 0]
            extent = np.einsum("ij,ij->i", axis, axis)
            start = np.einsum("ij,ij->i", origins, axis)
            delta = np.einsum("ij,ij->i", directions, axis)
            parallel = np.abs(delta) < 1e-10
            divisor = np.where(parallel, 1, delta)
            t0, t1 = -start / divisor, (extent - start) / divisor
            enter = np.maximum(enter, np.where(parallel, 0, np.minimum(t0, t1)))
            leave = np.minimum(leave, np.where(parallel, 1, np.maximum(t0, t1)))
            leave[parallel & ((start < -1e-7) | (start > extent + 1e-7))] = 0

        # В перекрывающихся OBB длину между входом и выходом учитываем один раз.
        valid = leave > enter
        ids = np.tile(line_ids[valid], 2)
        positions = np.concatenate((enter[valid], leave[valid]))
        events = np.repeat([1, -1], valid.sum())
        order = np.lexsort((positions, ids))
        inside = np.cumsum(events[order])[:-1] > 0
        spans = np.diff(positions[order])[inside]
        covered += np.dot(spans, lengths[offset + ids[order[:-1]][inside]])
    return min(1.0, float(covered / total_length))


class PdfCleaner:
    def __init__(self, walls_processor: WallsProcessor, pdf_processor: PdfProcessor):
        self.walls_processor = walls_processor
        self.layout_processor = None
        self.pdf_processor = pdf_processor

    def specify_layout_processor(self, layout_processor: LayoutProcessor):
        self.layout_processor = layout_processor

    def clean(self, pdf_path: Path, dpi: int):
        if self.layout_processor is None:
            raise ValueError("self.layout_processor is None")
        
        with pymupdf.open(pdf_path) as doc:
            page = doc[0]  # первый лист
            layer_names = {name for _, _, name in page.get_bboxlog(layers=True) if name}

            if len(layer_names) < 2:
                self.remove_page_text(page)
                display = page.get_displaylist()
                return self.convert_display_to_image(display, dpi)

            excluded_layers = PdfCleaner._get_excluded_layers_cached(self, page, layer_names, settings.MIN_LAYER_SCORE, self.pdf_processor.pdf_sha256, self.walls_processor.yolo_service.model_sha256)

            self.remove_page_text(page)
            display = page.get_displaylist()

            if excluded_layers:
                display = _filtered(display, excluded_layers)
            image = self.convert_display_to_image(display, dpi)
            return image

    @memory.cache(ignore=["self", "page"])
    def _get_excluded_layers_cached(self, page, layer_names: set, min_layer_score: float, pdf_sha256: str, yolo_sha256: str):
        walls = self.walls_processor.get_walls_cords(0, [None], self.layout_processor)

        zoom = self.walls_processor.pdf_proc.zoom
        boxes = [
            [[x / zoom, y / zoom] for x, y in get_obb_points(wall["bbox"])]
            for wall in walls
        ]

        polygons = np.asarray(boxes, dtype=np.float64).reshape(-1, 4, 2)
        
        excluded_layers: set[str] = set()

        if len(polygons) > 0:
            x_by_layer = self.get_x_metric(page, polygons)
            y_by_layer = self.get_y_metric(page, polygons, layer_names)

            for name, y in y_by_layer.items():
                if y is None:
                    continue

                x = x_by_layer.get(name)
                score = y if x is None else (x + 2 * y) / 3
                if score < min_layer_score:
                    excluded_layers.add(name)

        return excluded_layers

    # Длина векторных отрезков слоя внутри прямоугольников / общая длина отрезков слоя
    def get_x_metric(self, page, polygons):
        tree = sh.STRtree(sh.polygons(polygons))
        return {
            layer: vector_fraction(edges, polygons, tree)
            for layer, edges in segments(page)
        }

    # Число видимых пикселей слоя внутри прямоугольников / число видимых пикселей слоя на всём листе
    def get_y_metric(self, page, polygons, names: set):
        """
        Чтобы найти эти пиксели, рендерим лист со слоем и без него и смотрим, где изображение заметно посветлело.
        Толстая линия или заливка может дать больше пикселей, а полностью перекрытая линия — ноль.
        """
        scale = 2  # Масштаб рендера для расчёта Y
        matrix = pymupdf.Matrix(scale, scale)

        def render_gray(display):
            pix = display.get_pixmap(
                matrix=matrix, clip=page.rect,
                colorspace=pymupdf.csRGB, alpha=False,
            )
            rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)
            return rgb.min(axis=2)

        display = page.get_displaylist()
        source = render_gray(display).astype(np.int16)

        wall_mask = np.zeros(source.shape, dtype=np.uint8)
        for polygon in polygons:
            points = np.rint(polygon * scale * 16).astype(np.int32)
            cv2.fillConvexPoly(wall_mask, points, 1, shift=4)

        y_by_layer = {}
        for name in names:
            without_layer = render_gray(_filtered(display, {name}))
            visible = (source < 232) & (without_layer > source + 16)
            y_by_layer[name] = float(wall_mask[visible].mean()) if visible.any() else None

        return y_by_layer

    def convert_display_to_image(self, display: pymupdf.DisplayList, dpi: int):
        scale = dpi / 72
        pix = display.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            colorspace=pymupdf.csRGB,
            alpha=False,
        )
        image = pix.pil_image()
        image.info["dpi"] = (dpi, dpi)
        return image

    def remove_page_text(self, page: pymupdf.Page) -> None:
        """Удаляет текстовые объекты с видимой части страницы."""
        rect = page.rect * page.derotation_matrix
        page.add_redact_annot(rect, fill=False, cross_out=False)
        page.apply_redactions(
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
        )

    

