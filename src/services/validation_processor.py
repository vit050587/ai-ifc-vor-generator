from src.services.works_comparison.processor import Processor

class ValidationProcessor:
    def __init__(self) -> None:
        pass
    def validate(self, estimates: list[dict], ifc_result: list[dict], reasoning: bool = True):
        processor = Processor(project_result=estimates, ifc_result=ifc_result)

        result = processor.run()
        return result