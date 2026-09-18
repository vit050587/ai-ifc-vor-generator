
import copy
from src.core.logger import setup_logger

logger = setup_logger(__name__)

class PositionsExtractor:
    def __init__(self, estimates_result: list) -> None:
        self.estimates_result = copy.deepcopy(estimates_result)

    def filter_estimates(self):
        for source in self.estimates_result:
            result_data = source["result"].get("resultData", [])

            source["result"]["resultData"] = [
                estimate
                for estimate in result_data
                if estimate.get("form") != "Форма 13"
            ]

    def run(self):
        positions = []

        self.filter_estimates()

        for source in self.estimates_result:
            source_file = source["sourceFile"]
            for estimate in source["result"]["resultData"]:
                estimate_number = estimate["estimateNumber"]
                work_description = estimate["workDescription"]
                for section in estimate["sections"]:
                    section_name = section["sectionName"]
                    for position in section["positions"]:
                        position_number = position["number"]
                        for row in position["rows"]:
                            rate = row.get("rateCodeAndResourceCodes") or {}
                            record = {
                                "sourceFiles": [source_file],
                                "estimateNumber": estimate_number,
                                "workDescription": work_description,
                                "sectionName": section_name,
                                "positionNumber": position_number,
                                "rowNumber": row.get("rowNumber"),
                                "code": rate.get("code"),
                                "note": rate.get("note"),
                                "workAndCostName": row.get("workAndCostName"),
                                "unit": row.get("unit"),
                                "quantity": row.get("quantity"),
                            }
                            positions.append(record)

        start_positions_count = len(positions)
        positions = self.delete_duplicated_positions(positions)
        logger.info(f"Удалено {start_positions_count - len(positions)} позиций")

        return positions

    def delete_duplicated_positions(self, positions: list[dict]) -> list[dict]:
        result = []
        positions_by_key = {}

        unique_fields = (
            "estimateNumber",
            "sectionName",
            "positionNumber",
            "rowNumber",
            "code",
            "workAndCostName",
            "unit",
            "quantity",
        )

        for position in positions:
            key = tuple(position.get(field) for field in unique_fields)

            existing_position = positions_by_key.get(key)
            if existing_position is not None:
                existing_sources = existing_position["sourceFiles"]
                for source_file in position["sourceFiles"]:
                    if source_file not in existing_sources:
                        existing_sources.append(source_file)
                continue

            positions_by_key[key] = position
            result.append(position)

        return result

        
