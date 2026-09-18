from .utils import group_positions, sum_positions_quantity

from .config import settings

from src.core.logger import setup_logger

logger = setup_logger(__name__)

class PositionsProcessor:
    def __init__(self) -> None:
        self.merge_keys = settings.IFC_COMPARISON_GROUP_KEYS
        self.prefix = "normalized_"


        
    def run(self, positions: list):
        list_result, normalized_merge_keys = group_positions(self.prefix, self.merge_keys, positions)

        for group in list_result:
            group.update(sum_positions_quantity(group["positions"]))

        return list_result, normalized_merge_keys

    





        
