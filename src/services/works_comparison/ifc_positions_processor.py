from .utils import group_positions, sum_positions_quantity

from .config import settings

from src.core.logger import setup_logger

logger = setup_logger(__name__)


class IfcPositionsProcessor:
    def __init__(self) -> None:
        self.merge_keys = settings.IFC_COMPARISON_GROUP_KEYS
        self.prefix = "normalized_"


        
    def run(self, positions: list):
        positions = self.filter_no_code(positions)

        list_result, normalized_merge_keys = group_positions(self.prefix, self.merge_keys, positions)

        for group in list_result:
            group.update(sum_positions_quantity(group["positions"]))

        return list_result, normalized_merge_keys

    def filter_no_code(self, positions: list):
        return [
            position
            for position in positions
            if str(position.get("code") or "").strip()
        ]
