

import json
from pathlib import Path
from tqdm import tqdm
from typing import Any, List, Dict
import statistics

from shapely.geometry import Polygon, box

from rectangle_utils import rectangles_to_yolo_obb, get_two_points_bbox
from pdf_prcoessor import PdfProcessor
from yolo_service import YoloService

from config import settings


class LegendLayoutProcessor:
    def __init__(self, pdf_processor: PdfProcessor):
        self.pdf_processor = pdf_processor
        self.yolo_service = YoloService(settings.YOLO_LEGEND_LAYOUT_MODEL)

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
                "legend_row": row,
                "legend_symbols": row_symbols,
                "legend_descriptions": row_descriptions,
            })

        return row_items

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
        for bbox in bboxes:
            _, img = self.pdf_processor.crop_pdf_rect(bbox, zoom=settings.LEGEND_ZOOM)
            layout_objects = self.yolo_service.detect(img, confidence=0.5, iou=0.50, imgsz=1472, classes=[0, 1, 2, 3], save_debug_dir=settings.DEBUG_LEGEND_LAYOUTS_DIR)
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
