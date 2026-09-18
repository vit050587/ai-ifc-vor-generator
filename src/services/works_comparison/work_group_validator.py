from tqdm import tqdm
from .ollama_service import OllamaService

import json

from .config import settings
from src.core.logger import setup_logger

logger = setup_logger(__name__)


class WorkGroupValidator:
    def __init__(self, ollama_service: OllamaService) -> None:
        self.ollama_service = ollama_service

    def validate(self, comparison: list[dict]):
        """К каждой проверенной позиции дописывает объект "validation_result" содержащий информацию о результатах валидации моделью"""
        selected_entries = [
            entry
            for entry in comparison
            if entry["status"] in settings.STATUSES_TO_VALIDATE
        ]
        selected_positions = sum(
            len(entry["ifcGroup"]["positions"])
            for entry in selected_entries
        )
        cache_before = self.ollama_service._get_tg_model_answer_cached.cache_info()
        self._validate_only_ifc(comparison)
        cache_after = self.ollama_service._get_tg_model_answer_cached.cache_info()
        return {
            "groupsSelectedForValidation": len(selected_entries),
            "positionsSelectedForValidation": selected_positions,
            "uniqueModelRequests": cache_after.misses - cache_before.misses,
            "cacheHits": cache_after.hits - cache_before.hits,
        }

    def _validate_only_ifc(self, comparison: list[dict]):
        groups_to_validate = [entry for entry in comparison if entry["status"] in settings.STATUSES_TO_VALIDATE]
        positions_to_validate = [
            position
            for group in groups_to_validate
            for position in group["ifcGroup"]["positions"]
        ]
        
        for position in tqdm(positions_to_validate, desc="Валидация", unit="position"):
            payload = self._compose_position_payload(position)

            if position.get("positionType") == "material":
                result, _ = self.ollama_service.get_tg_model_answer(
                    "validate_material",
                    {
                        "data": json.dumps(
                            payload,
                            ensure_ascii=False,
                            indent=2,
                        )
                    },
                )
            elif position.get("positionType") == "work":
                result, _ = self.ollama_service.get_tg_model_answer(
                    "validate_work",
                    {
                        "data": json.dumps(
                            payload,
                            ensure_ascii=False,
                            indent=2,
                        )
                    },
                )
            else:
                logger.warning("positionType was not specified")
                result, _ = self.ollama_service.get_tg_model_answer(
                    "validate_work",
                    {
                        "data": json.dumps(
                            payload,
                            ensure_ascii=False,
                            indent=2,
                        )
                    },
                )
            position["validation_result"] = result

    def _compose_position_payload(self, position: dict):
        properties_keys_to_delete = ["areaM2", "volumeM3", "lengthMm"]

        # исключение guid из объекта элемента
        element = {
            key: value
            for key, value in position["element"].items()
            if key != "guids"
        }
        properties = position["properties"]
        selection_parameters = position["selectionParameters"]
        selection_parameters_str =  self.get_str_for_payload(selection_parameters)

        properties = {k: v for k, v in properties.items() if k not in properties_keys_to_delete}

        properties_str = self.get_str_for_payload(properties)

        payload = {
            "inputElement": {
                "category": element.get("category", ""),
                "materialGroup": element.get("materialGroup"),
                "properties": properties_str,
            },
            "candidatePosition": {
                "name": position.get("name"),
                "unit": position.get("unit"),
            }
        }

        return payload

    def get_str_for_payload(self, json_obj):
        if not json_obj:
            return None
        return json.dumps(
                    json_obj,
                    ensure_ascii=False,
                    sort_keys=True,
                )
