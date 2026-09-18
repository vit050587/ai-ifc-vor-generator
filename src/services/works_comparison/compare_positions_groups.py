from .config import settings

from src.core.logger import setup_logger

logger = setup_logger(__name__)

class ComparePositionsGroups:
    def __init__(self, project_groups: list[dict],  ifc_groups: list[dict]) -> None:
        self.project_groups = project_groups
        self.ifc_groups = ifc_groups

    def compare(self, normalized_merge_keys: list[str]):
        compared_groups = []

        project_by_key = {
            tuple(
                group[mk] for mk in normalized_merge_keys
            ): group
            for group in self.project_groups
        }

        ifc_by_key = {
            tuple(
                group[mk] for mk in normalized_merge_keys
            ): group
            for group in self.ifc_groups
        }

        keys = ifc_by_key.keys() | project_by_key.keys()

        for key in keys:
            project_group = project_by_key.get(key, None)
            ifc_group = ifc_by_key.get(key, None)

            not_empty_entry = project_group or ifc_group
            assert not_empty_entry is not None
            record = {mk: not_empty_entry[mk] for mk in normalized_merge_keys}

            if ifc_group is None:
                record["status"] = "only_project"
            elif project_group is None:
                record["status"] = "only_ifc"
            elif ifc_group["positionsWithoutQuantity"] > 0:
                record["status"] = "ifc_quantity_incomplete"
            elif project_group["positionsWithoutQuantity"] > 0:
                record["status"] = "project_quantity_incomplete"
            else:
                ifc_quantity = ifc_group["totalQuantity"]
                project_quantity = project_group["totalQuantity"]

                difference = ifc_quantity - project_quantity
                if project_quantity == 0:
                    percentage_difference = 0.0 if ifc_quantity == 0 else float("inf")
                else:
                    percentage_difference = abs(difference) / abs(project_quantity)

                record["difference"] = difference
                record["percentageDifference"] = percentage_difference

                if abs(difference) > settings.DIFFERENCE_TOLERANCE and percentage_difference > settings.DIFFERENCE_TOLERANCE_PERCENTAGE:
                    record["status"] = "quantity_mismatch"
                else:
                    record["status"] = "matched"

            record["projectGroup"] = project_group
            record["ifcGroup"] = ifc_group

            compared_groups.append(record)

        return compared_groups

