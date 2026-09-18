

from src.core.logger import setup_logger

logger = setup_logger(__name__)

class IfcPositionsExtractor:
    def __init__(self, ifc_result: list[dict]) -> None:
        self.ifc_result = ifc_result

    def run(self):
        positions = []

        for result in self.ifc_result:
            source_file = result.get("sourceFile")
            discipline = result.get("discipline")

            for group in result.get("elementGroups", []):
                group_id = group.get("groupId")
                element = group.get("element") or {}
                properties = group.get("properties") or {}

                for position in group.get("positions", []):
                    record = {
                        "code": position.get("code"),
                        "name": position.get("name"),
                        "unit": position.get("unit"),
                        "quantity": position.get("quantity"),
                        "positionType": position.get("positionType"),
                        "quantityStatus": position.get("quantityStatus"),
                        "selectionParameters": position.get("selectionParameters") or {},
                        "quantityCalculation": position.get("quantityCalculation"),
                        "sourceFile": source_file,
                        "discipline": discipline,
                        "groupId": group_id,
                        "element": element,
                        "properties": properties,
                    }
                    positions.append(record)

        return positions

        
