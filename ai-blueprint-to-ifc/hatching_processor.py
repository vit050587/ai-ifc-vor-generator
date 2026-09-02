import json
from pathlib import Path
from tqdm import tqdm
from PIL import Image
from typing import List, Dict
import hashlib
import copy
from dino_service import DinoService
from pdf_prcoessor import PdfProcessor
from ollama_service import OllamaService
from drawing_statistics_analyzer import DrawingStatisticsAnalyzer
from rectangle_utils import get_two_points_bbox
from debug_manager import save_legend_rows
from dino_train_creator import save_dino_train_sample

from config import settings
from logger import setup_logger

logger = setup_logger(__name__)
class HatchingProcessor:
    def __init__(
        self,
        ollama_service: OllamaService,
        drawing_statistics: DrawingStatisticsAnalyzer,
        dino_service: DinoService,
        pdf_processor: PdfProcessor | None = None
    ):
        self.dino_service = dino_service
        self.drawing_statistics = drawing_statistics
        self.pdf_processor = pdf_processor
        self.ollama_service = ollama_service
        self.reset_to_default_legends()

        self.zoom = None

    def specify_legends(self, legends:list, load_deafult=True):
        self.legends = legends
        if legends:
            self.adjust_legends = False
        self._prepare_legends()
        if load_deafult:
            self.legends += self._load_walls_types("default")

    def reset_to_default_legends(self):
        self.legends = []
        self.adjust_legends = True
        self.legends += self._load_walls_types("fallback")
        self.legends += self._load_walls_types("default")

    def _load_walls_types(self, legends_type: str):
        legends = []
        with open(settings.LEGENDS_DIR / "map.json", "r", encoding="utf-8") as f:
            walls_types_map = json.load(f)[legends_type]
        for folder_name in walls_types_map:
            png_files = [p.name for p in (settings.LEGENDS_DIR / legends_type / folder_name).glob("*.png")]
            legend_symbols = []
            for png_name in png_files:
                image = Image.open(settings.LEGENDS_DIR / legends_type / folder_name / png_name)
                legend_symbols.append(image)
            full_description = walls_types_map[folder_name]
            legends.append(self._create_legend_row_for_hatching(legend_symbols, full_description))
        return legends

    def _create_legend_row_for_hatching(self, legend_symbols: List[Image.Image], full_description: str):
        legend_symbols = [{"image": img} for img in legend_symbols]
        return {"legend_symbols": legend_symbols, "full_description": full_description}

    def process(self, walls, zoom: float):
        self.zoom = zoom
        self._calculate_tensors_for_legends()

        requests = None
        if not self.adjust_legends:
            requests = self._form_dino_requests_walls(walls)

        for i, wall in enumerate(tqdm(walls, desc="Анализ штриховки", unit="wall")):
            wall_requests = requests[i] if requests is not None else None
            self._process_wall(wall, wall_requests)

        self.drawing_statistics.add_hatching_scores([wall["hatching"]["best"]["score"] if wall["hatching"]["best"] else 0 for wall in walls])

        save_legend_rows(self.legends)

        return walls

    def _process_wall(self, wall: Dict, requests: Dict | None):
        if not requests:
            cropped_wall = self._crop_wall(wall)
            plan_tensor, plan_mask_tensor = self.dino_service.prepare_image_and_mask(cropped_wall["image"], cropped_wall["plan_obb"])
            requests = self._form_dino_requests_legends(plan_tensor, plan_mask_tensor)
            requests = self._predict_dino_legend_request(requests)

        results = []

        i = 0
        for legend in self.legends:
            result, offset = self._get_best_symbol(i, requests, legend["legend_symbols"], legend["full_description"])
            i = offset
            if result is not None:
                results.append(result)

        if i != len(requests["ids"]):
            logger.error(f"Несовпадение количества запросов {i} != {len(requests['ids'])}")

        best_result = max(results, key=lambda result: result["score"]) if results else None

        if self.adjust_legends and best_result is not None and best_result["score"] < settings.NEW_LEGEND_CREATION_SCORE_THRESHOLD:
            new_symbol = self._crop_wall(wall, pixels_around=0)["image"]
            new_row = self._create_legend_row_for_hatching([new_symbol], str(hashlib.md5(new_symbol.tobytes()).hexdigest()))
            self.legends += [new_row]
            self._calculate_tensors_for_legends()

            best_result = self._get_best_symbol_with_tensors(plan_tensor, plan_mask_tensor, new_row["legend_symbols"], new_row["full_description"])
            
        # save_dino_train_sample(
        #     cropped_wall=self._crop_wall(wall),
        #     legend_image=self.legends,
        #     best_result=best_result,
        #     output_dir="dino_train"
        # )
        wall["hatching"] = {
            "best": best_result,
            "matches": results,
        }

        return wall

    def _form_dino_requests_walls(self, walls: List):
        requests = []

        plan_image_tensors, plan_mask_tensors, image2_tensors, image2_mask_tensors = ([],[],[],[])
        for wall in tqdm(walls, desc="Подготовка тензоров", unit="wall"):
            cropped_wall = self._crop_wall(wall)
            plan_tensor, plan_mask_tensor = self.dino_service.prepare_image_and_mask(cropped_wall["image"], cropped_wall["plan_obb"])
            request = self._form_dino_requests_legends(plan_tensor, plan_mask_tensor)

            plan_image_tensors += request["plan_image_tensor"]
            plan_mask_tensors += request["plan_mask_tensor"]
            image2_tensors += request["image2_tensor"]
            image2_mask_tensors += request["image2_mask_tensor"]

            requests.append(request)

        dino_results = self.dino_service.predict_pairs_in_tensors(
            image2_tensors,
            image2_mask_tensors,
            plan_image_tensors,
            plan_mask_tensors,
            tqdm_settings={"desc": "Анализ штриховки", "unit": "batch"}
        )

        current_offset = 0
        for request in requests:
            result_len = len(request["plan_image_tensor"])
            request["request_results"] = dino_results[current_offset: current_offset + result_len]
            current_offset += result_len
        return requests
    
    def _form_dino_requests_legends(self, plan_tensor, plan_mask_tensor):
        """Создает словарь запросов к dino"""
        requests = {"ids": [], "plan_image_tensor": [],"plan_mask_tensor":[],"image2_tensor":[],"image2_mask_tensor":[], "request_results":[]}

        i = 0
        for legend in self.legends:
            for symbol in legend["legend_symbols"]:
                symbol_image = symbol.get("image")
                if symbol_image is None:
                    continue
                requests["ids"].append(i)
                requests["plan_image_tensor"].append(plan_tensor)
                requests["plan_mask_tensor"].append(plan_mask_tensor)
                requests["image2_tensor"].append(symbol["tensor"])
                requests["image2_mask_tensor"].append(symbol["mask_tensor"])
                i += 1
        
        return requests

    def _predict_dino_legend_request(self, requests):
        requests["request_results"] = self.dino_service.predict_pairs_in_tensors(
            requests["image2_tensor"],
            requests["image2_mask_tensor"],
            requests["plan_image_tensor"], 
            requests["plan_mask_tensor"],
            )
        return requests
    
    def _get_best_symbol_with_tensors(self, plan_tensor, plan_mask_tensor, symbols, description):
        """Одиночный запрос к dino"""
        results = []
        for symbol in symbols:
            symbol_image = symbol.get("image")
            if symbol_image is None:
                continue
            
            prediction = self.dino_service.predict_pair_in_tensors(
                plan_image_tensor=plan_tensor,
                plan_mask_tensor=plan_mask_tensor,
                image2_tensor=symbol["tensor"],
                image2_mask_tensor=symbol["mask_tensor"]
            )
            result = {
                "legend_image": symbol_image,
                "text_designation": description,
                **prediction,
            }
            results.append(result)

        best_result = max(results, key=lambda result: result["score"]) if results else None
        return best_result
    
    def _calculate_tensors_for_legends(self):
        for legend in self.legends:
            for symbol in legend["legend_symbols"]:
                if not "tensor" in symbol or not "mask_tensor" in symbol:
                    symbol["tensor"], symbol["mask_tensor"] = self.dino_service.prepare_image_and_mask(symbol["image"])
    
    def _prepare_legends(self):
        for legend in self.legends:
            if not "full_description" in legend:
                legend["full_description"] = self._get_description(legend["legend_descriptions"])
            for description in legend["legend_descriptions"]:
                if not "image" in description:
                    _, description["image"] = self.pdf_processor.crop_pdf_rect(get_two_points_bbox(description["bbox"]), zoom=settings.HATCHING_ZOOM)
            for symbol in legend["legend_symbols"]:
                if not "image" in symbol:
                    _, symbol["image"] = self.pdf_processor.crop_pdf_rect(get_two_points_bbox(symbol["bbox"]), zoom=settings.HATCHING_ZOOM)

    def _get_best_symbol(self, offset, requests, symbols, description):
        results = []
        for symbol in symbols:
            symbol_image = symbol.get("image")
            if symbol_image is None:
                continue
            
            prediction = requests["request_results"][requests["ids"].index(offset)]
            result = {
                "legend_image": symbol_image,
                "text_designation": description,
                **prediction,
            }
            results.append(result)

            offset += 1
        best_result = max(results, key=lambda result: result["score"]) if results else None
        return best_result, offset

    
    def _get_description(self, descriptions: list):
        description_texts = []
        for description in descriptions:
            description_bbox = self._apply_description_bbox_horizontal_modifier(get_two_points_bbox(description["bbox"]))
            img_b64, _ = self.pdf_processor.crop_pdf_rect(description_bbox, zoom=settings.HATCHING_ZOOM)
            image_text_json = self.ollama_service.extract_from_drawing(img_b64, settings.OLLAMA_MODEL_NAME, "get_text_from_image")
            description_texts.append(image_text_json.get("text", ""))
        return " ".join(description_texts)

    @staticmethod
    def _apply_description_bbox_horizontal_modifier(bbox: dict[str, float]):
        bbox = copy.deepcopy(bbox)
        bbox_length = bbox["x1"] - bbox["x0"]
        bbox["x0"] = bbox["x0"] - bbox_length * settings.LEGEND_DESCRIPTION_HORIZONTAL_MODIFIER
        bbox["x1"] = bbox["x1"] + bbox_length * settings.LEGEND_DESCRIPTION_HORIZONTAL_MODIFIER
        return bbox

    def _crop_wall(self, wall, pixels_around=30):
        """Вырезает стену из пдф и возвращает рисунок стены с отступом в пикселях и координаты стены на рисунке"""
        if self.pdf_processor is None:
            raise ValueError("PdfProcessor is required to crop walls")

        target_zoom = (self.zoom or settings.WALL_DETECTION.zoom) * 2
        bbox_pdf = wall.get("bbox_pdf")
        if bbox_pdf is None:
            raise ValueError("Wall must contain bbox_pdf to crop at a different zoom")

        bbox = self.pdf_processor.pdf_obb_to_image_obb(
            bbox_pdf,
            zoom=target_zoom,
        )
        padding = pixels_around * 2
        rect = {
            "x0": min(bbox["x1"], bbox["x2"], bbox["x3"], bbox["x4"]),
            "y0": min(bbox["y1"], bbox["y2"], bbox["y3"], bbox["y4"]),
            "x1": max(bbox["x1"], bbox["x2"], bbox["x3"], bbox["x4"]),
            "y1": max(bbox["y1"], bbox["y2"], bbox["y3"], bbox["y4"]),
        }
        crop_x0 = max(0, int(rect["x0"] - padding))
        crop_y0 = max(0, int(rect["y0"] - padding))
        _, img = self.pdf_processor.crop_image(
            rect["x0"] - padding,
            rect["y0"] - padding,
            rect["x1"] + padding,
            rect["y1"] + padding,
            zoom=target_zoom,
        )

        points = [
            (
                float(bbox[f"x{point_index}"]) - crop_x0,
                float(bbox[f"y{point_index}"]) - crop_y0,
            )
            for point_index in range(1, 5)
        ]

        plan_obb = [
            coordinate
            for point in points
            for coordinate in point
        ]

        return {"image": img, "plan_obb": plan_obb}
