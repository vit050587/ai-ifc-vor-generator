from pathlib import Path
import json
from collections import Counter

from .positions_extractor import PositionsExtractor
from .positions_processor import PositionsProcessor
from .ifc_positions_extractor import IfcPositionsExtractor
from .ifc_positions_processor import IfcPositionsProcessor
from .compare_positions_groups import ComparePositionsGroups
from .ollama_service import OllamaService
from .work_group_validator import WorkGroupValidator
from .validation_result_former import ValidationResultFormer

from .config import settings

from src.core.logger import setup_logger

logger = setup_logger(__name__)

class Processor:
    def __init__(self, ifc_result: Path | list, project_result: Path | list) -> None:

        if isinstance(project_result, Path):
            with open(project_result, "r", encoding="utf-8") as f:
                project_result_json = json.load(f)
        elif isinstance(project_result, list):
            project_result_json = project_result

        if isinstance(ifc_result, Path):
            with open(ifc_result, "r", encoding="utf-8") as f:
                ifc_result_json = json.load(f)
        elif isinstance(ifc_result, list):
            ifc_result_json = ifc_result

        assert project_result_json is not None
        assert ifc_result_json is not None

        self.positions_extractor = PositionsExtractor(project_result_json)
        self.ifc_positions_extractor = IfcPositionsExtractor(ifc_result_json)
        self.positions_processor = PositionsProcessor()
        self.ifc_positions_processor = IfcPositionsProcessor()
        self.ollama_service = OllamaService(settings.OLLAMA_PROMPTS_PATH)
        self.work_group_validator = WorkGroupValidator(self.ollama_service)
        self.validation_result_former = ValidationResultFormer(settings.VALIDATION_RESULT_PATH)

    def run(self):
        logger.info("=== Обработка смет проекта ===")
        positions = self.positions_extractor.run()
        logger.info(f"Позиций смет найдено {len(positions)}")

        grouped_positions, normalized_merge_keys = self.positions_processor.run(positions)
        separated_groups = self.separate_ka_positions(grouped_positions)

        
        logger.info(f"Групп основных позиций: {len(separated_groups['positions'])}")
        logger.info(f"Групп КА: {len(separated_groups['kaPositions'])}")

        logger.info("=== Обработка ifc результата ===")

        ifc_positions = self.ifc_positions_extractor.run()
        logger.info(f"Позиций ifc найдено {len(ifc_positions)}")

        grouped_ifc_positions, normalized_merge_keys = self.ifc_positions_processor.run(ifc_positions)
        logger.info(f"Групп ifc позиций: {len(grouped_ifc_positions)}")

        self.compare_position_groups = ComparePositionsGroups(separated_groups["positions"], grouped_ifc_positions)
        compared_groups = self.compare_position_groups.compare(normalized_merge_keys)
        self._log_compare_results(compared_groups)

        validation_statistics = self.work_group_validator.validate(compared_groups)
        result = self.validation_result_former.form_and_save(
            compared_groups,
            validation_statistics,
        )

        return result

    def _is_ka(self, position: dict) -> bool:
        code = position.get("normalized_code") or ""
        return code.strip().lower().startswith("ка")

    def separate_ka_positions(self, grouped_positions: list[dict]):
        result = {
            "positions": [],
            "kaPositions": [],
        }

        for group in grouped_positions:
            if self._is_ka(group):
                result["kaPositions"].append(group)
            else:
                result["positions"].append(group)

        return result


    def _log_compare_results(self, compared_groups: list[dict]):
        status_counts = Counter(
            group.get("status", "unknown")
            for group in compared_groups
        )

        status_labels = {
            "matched": "Объёмы совпадают",
            "quantity_mismatch": "Объёмы различаются",
            "ifc_quantity_incomplete": "Неполный объём IFC",
            "project_quantity_incomplete": "Неполный объём заказчика",
            "only_project": "Только у заказчика",
            "only_ifc": "Только в IFC",
        }

        logger.info("=== Итог сравнения ===")
        logger.info("Всего групп: %s", len(compared_groups))
        matching_groups_count = sum(
            1
            for group in compared_groups
            if group.get("projectGroup") is not None
            and group.get("ifcGroup") is not None
        )
        logger.info(
            "Совпадающих групп по ключу: %s",
            matching_groups_count,
        )

        for status, label in status_labels.items():
            logger.info("%s: %s", label, status_counts.get(status, 0))

        known_statuses = set(status_labels)
        unknown_count = sum(
            count
            for status, count in status_counts.items()
            if status not in known_statuses
        )
        if unknown_count:
            logger.warning("Групп с неизвестным статусом: %s", unknown_count)

        
