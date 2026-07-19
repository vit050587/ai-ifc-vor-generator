from ollama_service import OllamaService
from dino_service import DinoService
from pathlib import Path
from draw_geometry import render_rectangles_fast, render_rectangles_on_image
import json
import statistics
from utils import image_to_base64, find_intersecting_rectangles
from pdf_prcoessor import PdfProcessor
from walls_processor import WallsProcessor
from transformer_service import TransformerService
from result_former import save_result
from rectangle_utils import get_obb_dimensions
from typing import Tuple, Dict, List, Any
import copy
import os
from PIL import Image, ImageDraw
from rectangle_utils import merge_overlapping_obb, trim_overlapping_obb, remove_small_area_walls
from rich.pretty import pprint
from hatching_processor import HatchingProcessor
import debug_manager
from layout_processor import LayoutProcessor
from legend_layout_processor import LegendLayoutProcessor

from logger import setup_logger
from config import settings

logger = setup_logger(__name__)

class Processor:
    def __init__(self, pdf_path):
        Image.MAX_IMAGE_PIXELS = None

        self.PDF_PATH = pdf_path

        debug_manager.delete_debug_folder()

        self.ollama_service = OllamaService("prompts")
        self.pdf_processor = PdfProcessor(self.PDF_PATH)
        self.transformers_service = TransformerService(settings.PROMPTS_DIR)
        self.hatching_processor = HatchingProcessor(self.ollama_service, pdf_processor=self.pdf_processor)
        self.layout_processor = LayoutProcessor(self.pdf_processor, self.ollama_service)
        self.legend_layout_processor = LegendLayoutProcessor(self.pdf_processor)

        self.reference_scale = (1, 200)
    def process(self) -> Dict[str, Any]:
        debug_manager.save_run_settings()
        debug_manager.save_initial_blueprint(self.pdf_processor)

        global_blueprint_scale = self._get_scale()
        if not global_blueprint_scale:
            self.layout_processor.parse_drawings_scales()

        legends = self.layout_processor.get_legends()
        drawings = self.layout_processor.get_drawings()
        if not drawings:
            drawings = [None]

        results = []
        legend_row_items = None
        if legends:
            self.legend_layout_processor.parse_legend([legend["object"]["bbox"] for legend in legends])
            legend_row_items = self.legend_layout_processor.get_legend_row_items(min_inside_ratio=settings.LEGEND_LAYOUT_MIN_INSIDE_RATIO)
            self.hatching_processor.specify_legends(legend_row_items)
        else:
            logger.info("Легенда не найдена")

        result_object: dict[str, Any] = {"drawings": []}
        all_walls_bboxes_pix = []
        walls_processors = []
        for i, drawing in enumerate(drawings):
            blueprint_scale = self._choose_drawing_scale(global_blueprint_scale, (drawing or {}).get("scale", None))

            # Вычисляем приближение для обрабатываемого чертежа
            zoom_for_drawing = settings.BLUEPRINT.zoom * ((blueprint_scale[1] / blueprint_scale[0]) / (self.reference_scale[1] / self.reference_scale[0]))
            if zoom_for_drawing < 1 or zoom_for_drawing > 30:
                logger.warning(f"Неверный коэффициент приближения {zoom_for_drawing} используется {settings.BLUEPRINT.zoom}")
                zoom_for_drawing = settings.BLUEPRINT.zoom
                
            self.walls_processor = WallsProcessor(self.PDF_PATH, self.pdf_processor, zoom_for_drawing)
            walls_processors.append(self.walls_processor)

            drawing_bbox = drawing["object"]["bbox"] if drawing else None

            folder_name = str(i)
            walls_bboxes_pix = self._process_walls(drawing_bbox, folder_name, zoom_for_drawing)

            debug_manager.save_walls_highlighted(folder_name, walls_bboxes_pix, self.pdf_processor)

            self.hatching_processor.process(walls_bboxes_pix, zoom_for_drawing)

            walls_bboxes_pix = self._prepare_walls(walls_bboxes_pix)
            all_walls_bboxes_pix += walls_bboxes_pix

            painted_image_debug, materials_colors_md_debug = debug_manager.save_blueprint_walls_by_material(folder_name, walls_bboxes_pix, self.pdf_processor, f"page_{self.PDF_PATH.stem}_materials.png", legend_row_items or [], fill_opacity=0.5)
            walls_bboxes_mm = self.walls_processor.scale_walls_coords(walls_bboxes_pix, blueprint_scale)

            result = {
                "walls": self._form_walls_result(walls_bboxes_mm),
            }

            debug_manager.save_walls_result(folder_name, result)

            results.append(result)
            result_object["drawings"].append({"painted_image": painted_image_debug, "materials_colors_md": materials_colors_md_debug, "result": result})
            
        painted_image_debug, materials_colors_md_debug = debug_manager.save_blueprint_walls_by_material("full", all_walls_bboxes_pix, self.pdf_processor, f"page_{self.PDF_PATH.stem}_materials.png", legend_row_items or [], fill_opacity=0.5)
        result_object["full_drawing"] = {"painted_image": painted_image_debug, "materials_colors_md": materials_colors_md_debug}

        blueprint_processing_confidence = self._get_overall_confidence(walls_processors, all_walls_bboxes_pix)
        logger.info(f"Итоговый confidence обработки для чертежа: {blueprint_processing_confidence}")
        result_object["confidence"] = blueprint_processing_confidence

        debug_manager.save_result(result_object)

        save_result(results)

        return result_object
    
    def _get_overall_confidence(self, walls_processors: List[WallsProcessor], walls: list[dict[str, Any]]) -> float | None:
        confidences = []

        average_walls_confidence = self._get_average_walls_confidence(walls_processors)
        average_hatching_confidence = self._get_average_hatching_confidence(walls)
        average_layout_confidence = self.layout_processor.get_average_confidence().get("overall_average_confidence", None)
        average_legend_layout_confidence = self.legend_layout_processor.get_average_confidence().get("overall_average_confidence", None)

        confidences = [average_walls_confidence, average_hatching_confidence, average_layout_confidence, average_legend_layout_confidence]
        confidences = [confidence for confidence in confidences if isinstance(confidence, float)]

        return statistics.mean(confidences) if confidences else None
    
    def _get_average_walls_confidence(self, walls_processors: List[WallsProcessor]) -> float | None:
        walls = []
        for wall_processor in walls_processors:
            walls_current = wall_processor.get_walls()
            if walls_current:
                walls += walls_current
        
        if not walls:
            return None
        
        confidences = [wall["confidence"] for wall in walls]
        if not confidences:
            return None
        
        return statistics.mean(confidences)
    
    def _get_average_hatching_confidence(self, walls: list[dict[str, Any]]) -> float | None:
        hatchings_scores = [wall["hatching"]["best"]["score"] for wall in walls]
        if not hatchings_scores:
            return None
        
        return statistics.mean(hatchings_scores)
    
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
    
    def _get_scale(self):
        blueprint_scale = self.layout_processor.get_blueprint_scale()
        if not blueprint_scale or blueprint_scale == (0, 0):
            return None
            
        return blueprint_scale
    
    
    def _prepare_walls(self, walls):
        walls, statistics = Processor._delete_wrong_walls(walls)
        for reason, number in statistics["deleted"].items():
            logger.info(f"Удалено {number} стен по причине {reason}.")

        self._assign_designations_to_walls(walls)

        return walls

    def _assign_designations_to_walls(self, walls):
        for i, wall in enumerate(walls):
            wall["id"] = f"W{i}"
    
    def _process_walls(self, drawing_bbox, folder_name, zoom: float):
        walls_on_blueprint = self.walls_processor.get_walls_cords(drawing_bbox)
        walls_on_blueprint_number = len(walls_on_blueprint)
        if not walls_on_blueprint_number:
            return []
        
        self.save_blueprint_with_walls(folder_name, {"red": walls_on_blueprint}, f"page_{self.PDF_PATH.stem}_walls.png", zoom)
        merged_walls = merge_overlapping_obb(
            walls_on_blueprint,
            **settings.WALL_MERGE.model_dump(),
        )
        merged_walls_number = len(merged_walls)
        merged_deleted_number = walls_on_blueprint_number - merged_walls_number
        logger.info(
            f"Объединено {merged_deleted_number} стен "
            f"({100 * merged_deleted_number / walls_on_blueprint_number:.2f}%)"
        )
        self.save_blueprint_with_walls(folder_name, {"red": merged_walls}, f"page_{self.PDF_PATH.stem}_merged_walls.png", zoom)
        trimed_walls = trim_overlapping_obb(
            merged_walls,
            **settings.WALL_TRIM.model_dump(),
        )
        trimed_walls = remove_small_area_walls(trimed_walls, settings.MAX_WALL_AREA_FOR_DELETE)
        trimed_number = len(trimed_walls)
        if not trimed_number:
            return []
        
        trimed_changed_number = sum(1 for w in trimed_walls if "trimmed_count" in w) # Считаем те стены где появились метаданные обрезки
        self.save_blueprint_with_walls(folder_name, {"red": trimed_walls}, f"page_{self.PDF_PATH.stem}_trimed_walls.png", zoom)
        trim_deleted_number = merged_walls_number - trimed_number
        logger.info(
            f"Удалено {trim_deleted_number} стен. Обрезано "
            f"{100 * trimed_changed_number / trimed_number:.2f}%"
        )
        
        merged_walls_for_render = [w for w in trimed_walls if "merged_count" in w]
        unmerged_walls_for_render = [w for w in trimed_walls if not "merged_count" in w]
        self.save_blueprint_with_walls(folder_name, {"blue": merged_walls_for_render, "red": unmerged_walls_for_render}, f"page_{self.PDF_PATH.stem}_result.png", zoom)

        # self._save_for_train_dino(trimed_walls)
        self._add_pdf_bbox_to_walls(trimed_walls, zoom)
        return trimed_walls

    def _add_pdf_bbox_to_walls(self, walls, zoom):
        """Добавляет к стенам их координаты на pdf в соответствии с bbox"""
        walls_pdf = self.pdf_processor.image_obbs_to_pdf_obbs(
            walls,
            zoom=zoom,
        )

        for wall, wall_pdf in zip(walls, walls_pdf):
            wall["bbox_pdf"] = wall_pdf["bbox"]
        
    
    def _save_for_train_dino(self, trimed_walls):
        Path("walls").mkdir(parents=True, exist_ok=True)
        Path("walls_highlited").mkdir(parents=True, exist_ok=True)
        with open("train.jsonl", "w", encoding="utf-8") as train_file:
            for i, wall in enumerate(trimed_walls):
                bbox = wall["bbox"]
                rect = {
                    "x0": min(bbox["x1"], bbox["x2"], bbox["x3"], bbox["x4"]),
                    "y0": min(bbox["y1"], bbox["y2"], bbox["y3"], bbox["y4"]),
                    "x1": max(bbox["x1"], bbox["x2"], bbox["x3"], bbox["x4"]),
                    "y1": max(bbox["y1"], bbox["y2"], bbox["y3"], bbox["y4"]),
                }
                crop_x0 = max(0, int(rect["x0"] - 20))
                crop_y0 = max(0, int(rect["y0"] - 20))
                _, img = self.pdf_processor.crop_image(
                    rect["x0"] - 20,
                    rect["y0"] - 20,
                    rect["x1"] + 20,
                    rect["y1"] + 20,
                )

                image_name = f"page_1_{i}.png"
                img.save(f"walls/{image_name}")
                highlighted_img = img.copy()
                draw = ImageDraw.Draw(highlighted_img)
                points = [
                    (
                        float(bbox[f"x{point_index}"]) - crop_x0,
                        float(bbox[f"y{point_index}"]) - crop_y0,
                    )
                    for point_index in range(1, 5)
                ]
                draw.line(points + [points[0]], fill="red", width=1)
                highlighted_img.save(f"walls_highlited/{image_name}")

                train_row = {
                    "plan_image": image_name,
                    "plan_obb": [
                        coordinate
                        for point in points
                        for coordinate in point
                    ],
                    "wall_type": "monolit_jb_1",
                }
                train_file.write(json.dumps(train_row, ensure_ascii=False) + "\n")
        
    
    def save_blueprint_with_walls(
        self,
        folder_name: str | Path,
        walls: dict[str, list[dict]],
        file_name: str | Path,
        zoom: float
    ):
        """
        Сохраняет стены на чертеже.

        Для разных цветов передайте:
            {"red": walls_1, "blue": walls_2}
        """
        output_dir = settings.DEBUG_DIR / folder_name / settings.DEBUG_IMAGES_DIR
        output_path = output_dir / file_name

        # Перещитываем координаты в глобальные pdf
        for color in walls:
            self._add_pdf_bbox_to_walls(walls[color], zoom)
        self.pdf_processor.render_obb_rectangles(
            walls,
            width=2,
            save_path=output_path,
            zoom=zoom
        )

    @staticmethod
    def _form_walls_result(walls_bboxes):
        walls = []

        for index, detected_wall in enumerate(walls_bboxes, start=1):
            bbox = detected_wall["bbox"]
            length_mm, width_mm, angle_degrees = get_obb_dimensions(bbox)

            result_wall = {
                "id": detected_wall["id"],
                "name": "Стена",
                "length_m": round(length_mm / 1000, 3),
                "width_mm": round(width_mm, 1),
                "thickness_mm": round(width_mm, 1),
                "angle_degrees": round(angle_degrees, 2),
                "quantity": 1,
                "confidence": round(
                    float(detected_wall.get("confidence", 0)),
                    4,
                ),
                "bbox_mm": {
                    f"{axis}{point_index}": round(
                        float(bbox[f"{axis}{point_index}"]),
                        1,
                    )
                    for point_index in range(1, 5)
                    for axis in ("x", "y")
                },
            }

            if "hatching" in detected_wall:
                result_wall["hatching"] = detected_wall["hatching"]
                best_hatching = detected_wall["hatching"].get("best")
                if best_hatching:
                    result_wall["material"] = best_hatching.get("text_designation", "")

            walls.append(result_wall)

        return walls
    
    @staticmethod
    def _delete_wrong_walls(walls):
        statistics = {"deleted": {}}
        walls_number = len(walls)
        walls = [wall for wall in walls if wall["hatching"]["best"]["text_designation"] != "wrong_wall_type"]
        statistics["deleted"]["wrong_walls"] = walls_number - len(walls)
        walls_number = len(walls)
        walls = [wall for wall in walls if wall["hatching"]["best"]["score"] > settings.HATCHING_SCORE_THRESHOLD]
        statistics["deleted"]["wrong_confidence"] = walls_number - len(walls)
        return walls, statistics

