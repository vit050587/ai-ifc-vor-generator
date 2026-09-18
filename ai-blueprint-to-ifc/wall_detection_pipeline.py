
from pdf_prcoessor import PdfProcessor
from config import WallDetectionProfile
from walls_processor import WallsProcessor
from ollama_service import OllamaService
from ollama_service_tg import OllamaServiceTg
from layout_processor import LayoutProcessor
from legend_layout_processor import LegendLayoutProcessor
from dino_service import DinoService
from hatching_detector import HatchingDetector
from hatching_processor import HatchingProcessor
from drawing_statistics_analyzer import DrawingStatisticsAnalyzer
from rectangle_utils import get_4p_bbox_area
from polygon_converter import RegionPolygon
from obb_converter import RegionOBB
from matrix_utils import get_polygon_area, get_polygon_thickness

from rectangle_utils import get_obb_dimensions

import debug_manager
import time
import torch

from typing import Tuple, Dict, List, Any

from logger import setup_logger
from config import settings

logger = setup_logger(__name__)

class WallDetectionPipeline:
    def __init__(self, pdf_path):
        self.pdf_path = pdf_path
        self.last_wall_id = 1

        debug_manager.delete_debug_folder()

        self.wall_detection = WallDetectionProfile(tile_overlap=20)
        self.ollama_service = OllamaService("prompts")
        self.ollama_service_tg = OllamaServiceTg("prompts")

        self.pdf_processor = PdfProcessor(pdf_path)
        self.dino_service = DinoService(model_path=settings.DINO_HATCHING_MODEL)
        self.layout_processor = LayoutProcessor(self.pdf_processor, self.ollama_service)
        self.legend_layout_processor = LegendLayoutProcessor(self.pdf_processor, self.dino_service)
        self.hatching_detector = HatchingDetector(self.wall_detection, self.pdf_processor)
        self.drawing_statistics = DrawingStatisticsAnalyzer(self.pdf_processor)
        self.hatching_processor = HatchingProcessor(self. ollama_service, self.drawing_statistics, self.dino_service, pdf_processor=self.pdf_processor, ollama_service_tg=self.ollama_service_tg)

        self.reference_scale = (1, 200)

    def run(self):
        start_time = time.time()
        
        debug_manager.save_run_settings()
        debug_manager.save_initial_blueprint(self.pdf_processor)

        result_object: dict[str, Any] = {"drawings": []}
        
        global_blueprint_scale = self._get_scale()
        if not global_blueprint_scale:
            self.layout_processor.parse_drawings_scales()

        legends = self.layout_processor.get_legends()
        drawings = self.layout_processor.get_drawings()
        if not drawings:
            drawings = [None]

        results = []
        all_walls_for_debug = []
        self.legend_row_items = None
        if legends:
            self.legend_layout_processor.parse_legend([legend["object"]["bbox"] for legend in legends], dpi=settings.DPI)
            self.legend_row_items = self.legend_layout_processor.get_legend_row_items(min_inside_ratio=settings.LEGEND_LAYOUT_MIN_INSIDE_RATIO, merge_similar=False)
            self.hatching_processor.specify_legends(self.legend_row_items, load_deafult=False)
        else:
            logger.info("Легенда не найдена")

        debug_matrixs = []

        drawings.sort(key=lambda drawing : get_4p_bbox_area(drawing["object"]["bbox"]) if drawing and drawing["object"]["bbox"] else 0, reverse=True)
        for i, drawing in enumerate(drawings):
            blueprint_scale = self._choose_drawing_scale(global_blueprint_scale, (drawing or {}).get("scale", None))

            walls_processor = WallsProcessor(self.pdf_path, self.wall_detection, self.pdf_processor, dpi=settings.DPI)
            tiles = walls_processor.get_tiles(i, drawings, self.layout_processor)
            walls_result = self.hatching_detector.get_walls(
                tiles,
                self.hatching_processor.legends,
                drawing_index=i,
            )

            if i == 0 and (walls_result["matrix_debug_object"] is None or self.validate_walls_result_with_retry(walls_result["matrix_debug_object"]["matrix"])):
                self.hatching_processor.reset_to_default_legends()
                self.legend_row_items = None
                walls_result = self.hatching_detector.get_walls(
                    tiles,
                    self.hatching_processor.legends,
                    drawing_index=i,
                )

            if settings.USE_TILES_CACHE:
                self.hatching_detector.memory.reduce_size(bytes_limit="15G")

            if walls_result["matrix_debug_object"]:
                debug_matrixs.append(walls_result["matrix_debug_object"])

            walls_polygons_and_obb: list[RegionOBB | RegionPolygon] = walls_result["walls"]
            walls_polygons_pdf = self.pdf_processor.image_geometry_to_pdf_geometry(
                walls_polygons_and_obb,
                dpi=settings.MATRIX_COMPRESSION_DPI,
            )

            walls_bboxes_mm = walls_processor.scale_pdf_walls_coords(
                walls_polygons_pdf,
                blueprint_scale,
            )
            self._add_legend_by_entry_id(walls_bboxes_mm)
            self._assign_ids(walls_bboxes_mm)
            all_walls_for_debug += walls_bboxes_mm

            result = {
                "walls": self._form_walls_result(walls_bboxes_mm),
            }
            results.append({"result": result})

        debug_manager.clear_legend_rows_folder()
        debug_manager.save_legend_rows(self.hatching_processor.legends)
        debug_manager.save_blueprint_masks_by_material(
            "full",
            debug_matrixs,
            self.pdf_processor,
            f"page_{self.pdf_path.stem}_hatching_masks.png",
            self.hatching_processor.legends,
            fill_opacity=0.5,
        )
        painted_image_debug, materials_colors_md_debug = debug_manager.save_blueprint_walls_by_material(
            "full",
            all_walls_for_debug, 
            self.pdf_processor, 
            f"page_{self.pdf_path.stem}_materials.png", 
            self.legend_row_items or [], 
            fill_opacity=0.5, 
            confidence=None, 
            zoom=settings.WALL_DETECTION.zoom,
            draw_obbs=True,
            draw_polygons=True
        )
        result_object["full_drawing"] = {"painted_image": painted_image_debug, "materials_colors_md": materials_colors_md_debug}
        result_object["drawings"] = results

        logger.info(f"Полное время обработки: {((time.time() - start_time)/60):.2f} мин")
        return result_object

    def validate_walls_result_with_retry(self, matrix: torch.Tensor) -> bool:
        ratio = matrix.any(dim=0).float().mean().item()

        if ratio > settings.FALLBACK_THRESHOLD:
            return False

        logger.info(
            f"Заполненность чертежа стенами слишком низкая ({ratio * 100:.1f}%). "
            "Выполняется повторная обработка без найденной легенды."
        )
        return True

    def _get_scale(self):
        blueprint_scale = self.layout_processor.get_blueprint_scale()
        if not blueprint_scale or blueprint_scale == (0, 0):
            return None

        return blueprint_scale

    def _choose_drawing_scale(self, global_blueprint_scale: Tuple[int, int] | None, blueprint_scale: Tuple[int, int] | None) -> Tuple[int, int]:
        result_scale = None
        if global_blueprint_scale:
            result_scale = global_blueprint_scale
            logger.info(f"Масштаб чертежа определен: {result_scale}.")
        elif blueprint_scale:
            result_scale = blueprint_scale
            logger.info(f"Масштаб чертежа определен: {result_scale}.")
        else:
            result_scale = self.reference_scale
            logger.warning(f"Масштаб чертежа не найден, используется: {result_scale}.")

        return result_scale

    def _add_legend_by_entry_id(self, walls_bboxes_mm: list[dict]):
        for wall in walls_bboxes_mm:
            wall["material"] = self.hatching_processor.legends[wall["legend_entry_id"]]["full_description"]

    def _assign_ids(self, walls: list[dict]):
        for wall in walls:
            wall["id"] = f"W{self.last_wall_id}"
            self.last_wall_id += 1

    @staticmethod
    def _form_walls_result(walls_bboxes):
        walls = []

        for index, detected_wall in enumerate(walls_bboxes, start=1):
            obb = detected_wall.get("obb", None)

            width_mm, length_m, angle_degrees, bbox_mm = (None, None, None, None)
            if obb:
                length_mm, width_mm, angle_degrees = get_obb_dimensions(obb)
                width_mm = round(width_mm, 1)
                length_m = round(length_mm / 1000, 3)
                angle_degrees = round(angle_degrees, 2)
                bbox_mm = {
                    f"{axis}{point_index}": round(
                        float(obb[f"{axis}{point_index}"]),
                        1,
                    )
                    for point_index in range(1, 5)
                    for axis in ("x", "y")
                }
            else:
                polygon: list[list[float]] | None = detected_wall.get("polygon", None)
                holes: list[list[list[float]]] = detected_wall.get("holes", [])
                if polygon:
                    angle_degrees, bbox_mm = (None, None)
                    width_mm = detected_wall["width_mm"]
                    length_mm = (detected_wall["area_mm2"] / width_mm) if width_mm else 0
                    width_mm = round(width_mm, 1)
                    length_m = round(length_mm / 1000, 3)

            result_wall = {
                "id": detected_wall["id"],
                "name": "Стена",
                "length_m": length_m,
                "width_mm": width_mm,
                "thickness_mm": width_mm,
                "angle_degrees": angle_degrees,
                "quantity": 1,
                "confidence": round(
                    float(detected_wall.get("confidence", 0)),
                    4,
                ),
                "bbox_mm": bbox_mm,
            }

            result_wall["material"] = detected_wall["material"]

            walls.append(result_wall)

        return walls
