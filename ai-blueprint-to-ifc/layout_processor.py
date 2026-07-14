
import json
from pathlib import Path
from tqdm import tqdm

from pdf_prcoessor import PdfProcessor
from yolo_service import YoloService
from rectangle_utils import get_two_points_bbox
from debug_manager import save_layouts
from ollama_service import OllamaService
from typing import List, Dict, Tuple

from config import settings


class LayoutProcessor:
    def __init__(self, pdf_processor: PdfProcessor, ollama_service: OllamaService):
        self.pdf_processor = pdf_processor
        self.yolo_service = YoloService(settings.YOLO_LAYOUT_MODEL)
        self.ollama_service = ollama_service

        self.layouts = {}
        self._process_pdf()
        self.save_layouts_debug()

    def _process_pdf(self):
        """Заполняет layouts классами drawing_area, legend_block, title_block"""
        _, img = self.pdf_processor.pdf_to_base64(settings.LAYOUT_ZOOM)
        layout_objects = self.yolo_service.detect(img, confidence=0.58, iou=0.30, imgsz=1472, classes=[0, 1, 2], save_debug_dir=settings.DEBUG_LAYOUTS_DIR, class_iou_thresholds={1:0.01})
        layout_objects = self.pdf_processor.image_obbs_to_pdf_obbs(layout_objects, zoom=settings.LAYOUT_ZOOM)

        for layout_object in layout_objects:
            self.layouts.setdefault(layout_object["class_name"], []).append({"object": layout_object})

    def get_blueprint_scale(self) -> Tuple[int, int] | None:
        if not self.layouts.get("title_block"):
            return
        
        title_bbox = get_two_points_bbox(self.layouts["title_block"][0]["object"]["bbox"])
        img_b64, img = self.pdf_processor.crop_pdf_rect(title_bbox, zoom=settings.LAYOUT_ZOOM)
        scale_json = self.ollama_service.extract_from_drawing(img_b64, settings.OLLAMA_MODEL_NAME, "get_scale")
        return self._convert_ollama_response_scale(scale_json)
        
    
    def _convert_ollama_response_scale(self, scale_json) -> Tuple[int, int] | None:
        numerator = scale_json.get("scale", {}).get("numerator", None)
        denominator = scale_json.get("scale", {}).get("denominator", None)
        if isinstance(numerator, int) and isinstance(denominator, int):
            scale = (numerator, denominator)
            if not scale == (0, 0):
                return scale
        return None

    def get_legends(self) -> List[Dict] | None:
        return self.layouts.get("legend_block")

    def get_drawings(self) -> List[Dict] | None:
        return self.layouts.get("drawing_area")

    def save_layouts_debug(self):
        legends_images = self._get_images_from_objects_list(self.layouts.get("legend_block", []))
        titles_images = self._get_images_from_objects_list(self.layouts.get("title_block", []))
        drawings_images = self._get_images_from_objects_list(self.layouts.get("drawing_area", []))

        save_layouts(legends_images, titles_images, drawings_images)
    
    def parse_drawings_scales(self):
        for drawing in self.layouts.get("drawing_area", []):
            drawing_bbox = get_two_points_bbox(drawing["object"]["bbox"])
            img_b64, img = self.pdf_processor.crop_pdf_rect(drawing_bbox, zoom=settings.LAYOUT_ZOOM)
            scale_json = self.ollama_service.extract_from_drawing(img_b64, settings.OLLAMA_MODEL_NAME, "get_scale")
            drawing["scale"] = self._convert_ollama_response_scale(scale_json)

    def get_legend_images_for_train(self):
        return self._get_images_from_objects_list(self.layouts.get("legend_block", []))

    def _get_images_from_objects_list(self, converted_yolo_answer: list):
        object_bboxes = [answer["object"]["bbox"] for answer in converted_yolo_answer]

        images = self._get_images_from_bboxes_list(object_bboxes)
        return images

    def _get_images_from_bboxes_list(self, bboxes: list):
        return [
            self.pdf_processor.crop_pdf_rect(get_two_points_bbox(bbox), zoom=settings.LAYOUT_ZOOM)[1]
            for bbox in bboxes
        ]
        

        
        

