from pdf_prcoessor import PdfProcessor
from yolo_service import YoloService
from pathlib import Path
from typing import Any, Tuple
from tqdm import tqdm

from config import settings
from rectangle_utils import rectangles_to_yolo_obb, get_two_points_bbox

class WallsProcessor:
    def __init__(self, pdf_path, pdf_processor: PdfProcessor | None = None, zoom: float | None = None):
        self.PDF_PATH = pdf_path
        if pdf_processor:
            self.pdf_proc = pdf_processor
        else:
            self.pdf_proc = PdfProcessor(self.PDF_PATH)
        
        self.yolo_service = YoloService(settings.YOLO_WALLS_MODEL)
        self.tiles_path = Path("blueprint_tiles")
        self.tiles_path.mkdir(parents=True, exist_ok=True)

        self.zoom = zoom or settings.BLUEPRINT.zoom

        self.walls = None

    def _get_blueprint_crops(self, drawing_bbox: list | None):
        blueprint = settings.BLUEPRINT
        tiles = self.pdf_proc.split_image_to_tiles(
            drawing_bbox,
            blueprint.tile_size,
            blueprint.tile_size,
            blueprint.tile_overlap,
            self.zoom,
        )

        for i, tile in enumerate(tqdm(tiles, desc="Обработка плиток", unit="tile")):
            image_path = self.tiles_path / f"page_{self.PDF_PATH.parent.name}_{self.PDF_PATH.stem}_tile_{i}.png"
            tile["image"].save(image_path)

        return tiles

    def get_walls_cords(self, drawing_bbox: list | None):
        """
        Возвращает стены в глобальных пиксельных координатах изображения PDF.
        """
        drawing_bbox_2points = get_two_points_bbox(drawing_bbox)
        tiles = self._get_blueprint_crops(drawing_bbox_2points)
        walls = []
        detection = settings.WALL_DETECTION

        for tile in tiles:
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
        """
        Переводит глобальные пиксельные координаты стен в реальные миллиметры.

        Исходный список не изменяется.
        """
        scale_from, scale_to = blueprint_scale

        if scale_from <= 0 or scale_to <= 0:
            raise ValueError(
                "Значения blueprint_scale должны быть больше нуля"
            )
        if self.zoom <= 0:
            raise ValueError("zoom должен быть больше нуля")

        # Пиксели рендера -> PDF points -> мм на листе -> реальные мм.
        mm_per_pixel = (
            (1 / self.zoom)
            * (25.4 / 72)
            * (scale_to / scale_from)
        )

        converted = []
        for wall in walls:
            bbox = wall.get("bbox", wall)
            try:
                converted_bbox = {
                    f"{axis}{point_index}": (
                        float(bbox[f"{axis}{point_index}"])
                        * mm_per_pixel
                    )
                    for point_index in range(1, 5)
                    for axis in ("x", "y")
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "Каждый OBB должен содержать числовые x1, y1 ... x4, y4"
                ) from exc

            if "bbox" in wall:
                converted_wall = dict(wall)
                converted_wall["bbox"] = converted_bbox
            else:
                converted_wall = converted_bbox

            converted.append(converted_wall)

        return converted
    
    def get_walls(self):
        return self.walls

    
