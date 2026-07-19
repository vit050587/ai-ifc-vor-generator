import json
from pathlib import Path
from tqdm import tqdm
from PIL import Image
from typing import List
import hashlib

from dino_service import DinoService
from pdf_prcoessor import PdfProcessor
from ollama_service import OllamaService
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
        pdf_processor: PdfProcessor | None = None
    ):
        self.dino_service = DinoService(model_path=settings.DINO_HATCHING_MODEL)
        self.pdf_processor = pdf_processor
        self.legends = []
        self.ollama_service = ollama_service
        self.adjust_legends = True
        self.legends += self._load_walls_types("fallback")
        self.legends += self._load_walls_types("default")

        self.zoom = None

    def specify_legends(self, legends:list):
        self.legends = legends
        if legends:
            self.adjust_legends = False
        self._prepare_legends()
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

        for wall in tqdm(walls, desc="Анализ штриховки", unit="wall"):
            self._process_wall(wall)

        save_legend_rows(self.legends)

        return walls

    def _process_wall(self, wall):
        cropped_wall = self._crop_wall(wall)
        plan_tensor, plan_mask_tensor = self.dino_service.prepare_image_and_mask(cropped_wall["image"], cropped_wall["plan_obb"])
        results = []
        requests = self._form_dino_requests(plan_tensor, plan_mask_tensor)

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
        #     plan_image=cropped_wall["image"],
        #     plan_obb=cropped_wall["plan_obb"],
        #     legend_image=self.legends,
        #     best_result=best_result,
        #     output_dir="dino_train"
        # )
        wall["hatching"] = {
            "best": best_result,
            "matches": results,
        }

        return wall
    
    def _form_dino_requests(self, plan_tensor, plan_mask_tensor):
        """Создает словарь запросов и обращается к dino"""
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
   
        requests["request_results"] = self.dino_service.predict_pairs_in_tensors(
            [tensor for tensor in requests["image2_tensor"]],
            [tensor for tensor in requests["image2_mask_tensor"]],
            [tensor for tensor in requests["plan_image_tensor"]], 
            [tensor for tensor in requests["plan_mask_tensor"]],
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

    
    def _get_description(self, descriptions: dict):
        description_texts = []
        for description in descriptions:
            img_b64, _ = self.pdf_processor.crop_pdf_rect(get_two_points_bbox(description["bbox"]), zoom=settings.HATCHING_ZOOM)
            image_text_json = self.ollama_service.extract_from_drawing(img_b64, settings.OLLAMA_MODEL_NAME, "get_text_from_image")
            description_texts.append(image_text_json.get("text", ""))
        return " ".join(description_texts)

    def _crop_wall(self, wall, pixels_around=20):
        """Вырезает стену из пдф и возвращает рисунок стены с отступом в пикселях и координаты стены на рисунке"""
        if self.pdf_processor is None:
            raise ValueError("PdfProcessor is required to crop walls")

        bbox = wall["bbox"]
        rect = {
            "x0": min(bbox["x1"], bbox["x2"], bbox["x3"], bbox["x4"]),
            "y0": min(bbox["y1"], bbox["y2"], bbox["y3"], bbox["y4"]),
            "x1": max(bbox["x1"], bbox["x2"], bbox["x3"], bbox["x4"]),
            "y1": max(bbox["y1"], bbox["y2"], bbox["y3"], bbox["y4"]),
        }
        crop_x0 = max(0, int(rect["x0"] - pixels_around))
        crop_y0 = max(0, int(rect["y0"] - pixels_around))
        _, img = self.pdf_processor.crop_image(
            rect["x0"] - pixels_around,
            rect["y0"] - pixels_around,
            rect["x1"] + pixels_around,
            rect["y1"] + pixels_around,
            zoom= self.zoom or settings.BLUEPRINT.zoom
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
