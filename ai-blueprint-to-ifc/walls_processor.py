from pdf_prcoessor import PdfProcessor
from layout_processor import LayoutProcessor
from yolo_service import YoloService
from pathlib import Path
from typing import Any, Tuple
from tqdm import tqdm
from dataclasses import asdict

from config import settings
from obb_converter import RegionOBB
from polygon_converter import RegionPolygon
from polygons_converter import WallObb
from rectangle_utils import rectangles_to_yolo_obb, get_two_points_bbox

class WallsProcessor:
    def __init__(self, pdf_path, detection_settings, pdf_processor: PdfProcessor | None = None, zoom: float | None = None, model: Path | None = None, dpi: int | None = None):
        self.PDF_PATH = pdf_path
        self.detection_settings = detection_settings

        if pdf_processor:
            self.pdf_proc = pdf_processor
        else:
            self.pdf_proc = PdfProcessor(self.PDF_PATH)
        
        self.yolo_service = YoloService(model or settings.MODELS_DIR / detection_settings.model_name)
        self.tiles_path = settings.DEBUG_DIR / self.detection_settings.tiles_dir
        self.tiles_path.mkdir(parents=True, exist_ok=True)

        self.zoom = zoom or self.detection_settings.zoom if not dpi else None
        self.dpi = dpi

        self.walls = None

    def _get_blueprint_crops(self, drawing_index: int, drawing_bbox: dict | None, exclude_bboxes: list | None = None):
        blueprint = self.detection_settings
        tiles = self.pdf_proc.split_image_to_tiles(
            drawing_bbox,
            blueprint.tile_size,
            blueprint.tile_size,
            blueprint.tile_overlap,
            self.zoom,
            exclude_bboxes,
            self.dpi
        )

        for i, tile in enumerate(tiles):
            image_path = self.tiles_path / f"page_{self.PDF_PATH.parent.name}_{self.PDF_PATH.stem}_{drawing_index}_tile_{i}.png"
            tile["image"].save(image_path)

        return tiles

    def get_tiles(self, drawing_index: int, drawings: list, layout_processor: LayoutProcessor):
        drawings_bboxes = [drawing["object"]["bbox"] if drawing else None for drawing in drawings]

        layouts_bboxes = []
        layouts = layout_processor.get_layouts()
        for layout_type, layout_objects in layouts.items():
            if layout_type == "drawing_area":
                continue

            for layout in layout_objects:
                layouts_bboxes.append(layout["object"]["bbox"])

        drawing_bbox_2points = get_two_points_bbox(drawings_bboxes[drawing_index])
        tiles = self._get_blueprint_crops(drawing_index, drawing_bbox_2points, exclude_bboxes=(drawings_bboxes[:drawing_index] + drawings_bboxes[drawing_index + 1:] + layouts_bboxes))
        return tiles

    def get_walls_cords(self, drawing_index: int, drawings: list, layout_processor: LayoutProcessor):
        """
        Возвращает стены в глобальных пиксельных координатах изображения PDF.
        """
        tiles = self.get_tiles(drawing_index, drawings, layout_processor)
        walls = []
        detection = self.detection_settings

        for i, tile in enumerate(tqdm(tiles, desc="Обработка плиток", unit="tile")):
            walls_bboxes_raw = self.yolo_service.detect(
                tile["image"],
                confidence=detection.confidence,
                iou=detection.iou,
                imgsz=detection.image_size,
                classes=[0],
            )

            for wall in walls_bboxes_raw:
                walls.append(
                    self._to_global_coords(
                        wall,
                        tile_offset=(tile["x0"], tile["y0"]),
                    )
                )

        self.walls = walls
        return walls

    @staticmethod
    def _to_global_coords(
        wall: dict[str, Any],
        tile_offset: Tuple[float, float],
    ) -> dict[str, Any]:
        """Переводит локальный OBB тайла в глобальные пиксели страницы."""
        offset_x, offset_y = tile_offset
        bbox = wall.get("bbox", wall)

        try:
            global_bbox = {
                f"{axis}{point_index}": (
                    float(bbox[f"{axis}{point_index}"])
                    + (offset_x if axis == "x" else offset_y)
                )
                for point_index in range(1, 5)
                for axis in ("x", "y")
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Каждый OBB должен содержать числовые x1, y1 ... x4, y4"
            ) from exc

        if "bbox" not in wall:
            return global_bbox

        global_wall = dict(wall)
        global_wall["bbox"] = global_bbox
        return global_wall

    def scale_walls_coords(
        self,
        walls: list[dict[str, Any]],
        blueprint_scale: Tuple[int, int],
    ) -> list[dict[str, Any]]:
        """Convert wall coordinates from rendered-image pixels to real mm."""
        scale_ratio = self._get_blueprint_scale_ratio(blueprint_scale)
        if self.zoom is None or self.zoom <= 0:
            raise ValueError("zoom должен быть больше нуля")

        mm_per_pixel = (1 / self.zoom) * (25.4 / 72) * scale_ratio
        return self._scale_walls(
            walls,
            coefficient=mm_per_pixel,
            bbox_key="bbox",
        )

    def scale_pdf_walls_coords(
        self,
        walls: list[RegionOBB | RegionPolygon],
        blueprint_scale: Tuple[int, int],
    ) -> list[dict[str, Any]]:
        """Convert PDF geometry to millimetres while preserving its shape type."""
        scale_ratio = self._get_blueprint_scale_ratio(blueprint_scale)
        mm_per_pdf_point = (25.4 / 72) * scale_ratio

        def scale_ring(points) -> list[list[float]]:
            return [
                [float(x) * mm_per_pdf_point, float(y) * mm_per_pdf_point]
                for x, y in points
            ]

        converted = []
        for wall in walls:
            if not isinstance(wall, (RegionOBB, RegionPolygon)):
                raise TypeError(f"Unsupported wall geometry: {type(wall).__name__}")
            if wall.entry_index is None:
                raise ValueError("Wall geometry has no legend entry index")

            wall_data = {
                "legend_entry_id": wall.entry_index,
                "source_polygon_id": wall.global_id,
            }
            if isinstance(wall, RegionOBB):
                wall_data["obb_pdf"] = {
                    f"{axis}{index}": float(point[axis_index])
                    for index, point in enumerate(wall.polygon, start=1)
                    for axis_index, axis in enumerate(("x", "y"))
                }
                corners = scale_ring(wall.polygon)
                wall_data["obb"] = {
                    f"{axis}{index}": point[axis_index]
                    for index, point in enumerate(corners, start=1)
                    for axis_index, axis in enumerate(("x", "y"))
                }
            else:
                wall_data["polygon_pdf"] = [
                    [float(x), float(y)] for x, y in wall.polygon
                ]
                wall_data["holes_pdf"] = [
                    [[float(x), float(y)] for x, y in hole] for hole in wall.holes
                ]
                wall_data["polygon"] = scale_ring(wall.polygon)
                wall_data["holes"] = [scale_ring(hole) for hole in wall.holes]
                wall_data["width_mm"] = wall.temp_width * mm_per_pdf_point
                wall_data["area_mm2"] = wall.temp_area * mm_per_pdf_point ** 2
            converted.append(wall_data)

        return converted

    @staticmethod
    def _get_blueprint_scale_ratio(
        blueprint_scale: Tuple[int, int],
    ) -> float:
        scale_from, scale_to = blueprint_scale
        if scale_from <= 0 or scale_to <= 0:
            raise ValueError("Значения blueprint_scale должны быть больше нуля")
        return scale_to / scale_from

    @staticmethod
    def _scale_walls(
        walls: list[dict[str, Any]] | list[WallObb],
        coefficient: float,
        bbox_key: str,
        measurement_keys: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for wall in walls:
            wall_data = asdict(wall) if isinstance(wall, WallObb) else wall
            bbox = wall_data.get(bbox_key, wall_data)
            try:
                converted_bbox = {
                    f"{axis}{point_index}": (
                        float(bbox[f"{axis}{point_index}"]) * coefficient
                    )
                    for point_index in range(1, 5)
                    for axis in ("x", "y")
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "Каждый OBB должен содержать числовые x1, y1 ... x4, y4"
                ) from exc

            if bbox_key in wall_data:
                converted_wall = dict(wall_data)
                converted_wall["bbox"] = converted_bbox
                for key in measurement_keys:
                    if key in converted_wall:
                        converted_wall[key] = (
                            float(converted_wall[key]) * coefficient
                        )
            else:
                converted_wall = converted_bbox

            converted.append(converted_wall)

        return converted
    
    def get_walls(self):
        return self.walls

    
