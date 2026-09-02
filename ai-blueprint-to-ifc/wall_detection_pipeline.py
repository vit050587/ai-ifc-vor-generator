
from pdf_prcoessor import PdfProcessor
from config import WallDetectionProfile
from walls_processor import WallsProcessor
from ollama_service import OllamaService
from layout_processor import LayoutProcessor

class WallDetectionPipeline:
    def __init__(self,  pdf_path):
        self.pdf_path = pdf_path
        self.wall_detection = WallDetectionProfile(tile_overlap=10)
    
        self.pdf_processor = PdfProcessor(pdf_path)

    def run(self):
        ollama_service = OllamaService("prompts")
        layout_processor = LayoutProcessor(self.pdf_processor, ollama_service)
    
        drawings = layout_processor.get_drawings()
    
        if not drawings:
            return
    
        for i, drawing in enumerate(drawings):
            walls_processor = WallsProcessor(self.pdf_path, self.wall_detection, self.pdf_processor, dpi=900)
            tiles = walls_processor._get_tiles(i, drawings, layout_processor)
            print(len(tiles))