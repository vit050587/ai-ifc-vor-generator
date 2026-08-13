

import json
from pathlib import Path
from tqdm import tqdm
from typing import Any, List, Dict
import statistics
import copy

from shapely.geometry import Polygon, box

from rectangle_utils import rectangles_to_yolo_obb, get_two_points_bbox
from pdf_prcoessor import PdfProcessor
from yolo_service import YoloService
from dino_service import DinoService
from PIL import Image

from config import settings
from logger import setup_logger

logger = setup_logger(__name__)


class LegendLayoutProcessor:
    def __init__(self, pdf_processor: PdfProcessor, dino_service: DinoService):
        self.pdf_processor = pdf_processor
        self.yolo_service = YoloService(settings.YOLO_LEGEND_LAYOUT_MODEL)
        self.dino_service = dino_service

        self.layouts = {}

    def get_legend_row_items(
        self,
        min_inside_ratio: float = 0.8,
    ) -> list[dict[str, Any]]:
        row_items = []
        rows = self.layouts.get("legend_row", [])
        symbols = self.layouts.get("legend_symbol", [])
        descriptions = self.layouts.get("legend_description", [])

        for row in rows:
            row_symbols = [
                symbol
                for symbol in symbols
                if self._is_bbox_inside(row, symbol, min_inside_ratio)
            ]
            row_descriptions = [
                description
                for description in descriptions
                if self._is_bbox_inside(row, description, min_inside_ratio)
            ]

            #Если нет символов либо описания то пропускаем
            if not (row_symbols and row_descriptions):
                continue

            row_items.append({
                "legend_rows": [row],
                "legend_symbols": row_symbols,
                "legend_descriptions": row_descriptions,
            })

        row_items = self.merge_similar_legend_rows(row_items)
        return row_items

    def merge_similar_legend_rows(self, row_items: list[dict]):
        merged_row_items = copy.deepcopy(row_items)

        if not merged_row_items:
            return []

        merge_number = 0
        merged = True
        while merged:
            for row_item_first in merged_row_items:
                merged = False
                symbols_first = self._get_symbols_from_legend_row(row_item_first)
                for row_item_second in merged_row_items:
                    if row_item_first is row_item_second:
                        continue

                    symbols_second = self._get_symbols_from_legend_row(row_item_second)
                    if self._compare_symbols_lists(symbols_first, symbols_second, settings.LEGEND_ROWS_SIMILARITY_THRESHOLD):
                        merged_row_items.append(self._merge_legend_rows(row_item_first, row_item_second))
                        merged_row_items.remove(row_item_first)
                        merged_row_items.remove(row_item_second)
                        merged = True
                        merge_number += 1
                        break
                if merged:
                    break

        if merge_number:
            logger.info(f"Объединено {merge_number} строк легенды. Количство строк легенды: {len(merged_row_items)}.")

        return merged_row_items

    def _get_symbols_from_legend_row(self, row_item):
        symbols = []
        for symbol in row_item.get("legend_symbols", []):
            _, symbol_image = self.pdf_processor.crop_pdf_rect(get_two_points_bbox(symbol["bbox"]), zoom=settings.HATCHING_ZOOM)
            symbols.append(symbol_image)
        return symbols
    

    def _compare_symbols_lists(self, first_symbols:list[Image.Image], second_symbols:list[Image.Image], threshold:float):
        for symbol_first in first_symbols:
            for symbol_second in second_symbols:
                if self.dino_service.predict_pair(plan_image=symbol_first, plan_obb=None, image2=symbol_second)["score"] > threshold:
                    return True
        return False

    @staticmethod
    def _merge_legend_rows(first_row: dict[str, list], second_row: dict[str, list]):
        result = {}

        for key, value in first_row.items():
            if isinstance(value, list):
                result.setdefault(key, []).extend(value)

        for key, value in second_row.items():
            if isinstance(value, list):
                result.setdefault(key, []).extend(value)

        return result

    @staticmethod
    def _is_bbox_inside(
        container: dict[str, Any],
        item: dict[str, Any],
        min_inside_ratio: float,
    ) -> bool:
        container_polygon = LegendLayoutProcessor._bbox_to_polygon(container)
        item_polygon = LegendLayoutProcessor._bbox_to_polygon(item)

        if item_polygon.area <= 0:
            return False

        inside_ratio = container_polygon.intersection(item_polygon).area / item_polygon.area
        return inside_ratio >= min_inside_ratio

    @staticmethod
    def _bbox_to_polygon(item: dict[str, Any]) -> Polygon:
        bbox = item.get("bbox", item)

        if all(key in bbox for key in ("x0", "y0", "x1", "y1")):
            return box(
                float(bbox["x0"]),
                float(bbox["y0"]),
                float(bbox["x1"]),
                float(bbox["y1"]),
            )

        return Polygon([
            (float(bbox[f"x{index}"]), float(bbox[f"y{index}"]))
            for index in range(1, 5)
        ])

    def parse_legend(self, bboxes: List[dict]):
        bboxes = [get_two_points_bbox(bbox) for bbox in bboxes]
        for i, bbox in enumerate(bboxes):
            _, img = self.pdf_processor.crop_pdf_rect(bbox, zoom=settings.LEGEND_ZOOM)
            layout_objects = self.yolo_service.detect(img, confidence=0.5, iou=0.50, imgsz=1472, classes=[0, 1, 2, 3], save_debug_dir=settings.DEBUG_LEGEND_LAYOUTS_DIR, save_debug_name=f"{i}.png")
            layout_objects = self.pdf_processor.cropped_image_obbs_to_pdf_obbs(bbox, layout_objects, zoom=settings.LEGEND_ZOOM)

            for layout_object in layout_objects:
                self.layouts.setdefault(layout_object["class_name"], []).append(layout_object)
        return self.layouts
    
    def get_average_confidence(self) -> Dict[str, Any]:
        result_object = {}

        average_confidences = []
        for layout_type in self.layouts:
            confidences = []
            for layout_object in self.layouts[layout_type]:
                confidences.append(layout_object["confidence"])
            if not confidences:
                continue
            confidence = statistics.mean(confidences)
            result_object.setdefault(layout_type, {})["average_confidence"] = confidence
            average_confidences.append(confidence)
        if not average_confidences:
                return result_object

        result_object["overall_average_confidence"] = statistics.mean(average_confidences)
        return result_object
