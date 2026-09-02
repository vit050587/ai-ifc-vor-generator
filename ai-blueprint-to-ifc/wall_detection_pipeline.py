
from pdf_prcoessor import PdfProcessor
from config import WallDetectionProfile
from walls_processor import WallsProcessor
from ollama_service import OllamaService
from layout_processor import LayoutProcessor
from legend_layout_processor import LegendLayoutProcessor
from dino_service import DinoService
from hatching_detector import HatchingDetector
from hatching_processor import HatchingProcessor
from drawing_statistics_analyzer import DrawingStatisticsAnalyzer

from logger import setup_logger
from config import settings

logger = setup_logger(__name__)

class WallDetectionPipeline:
    def __init__(self,  pdf_path):
        self.pdf_path = pdf_path
        self.wall_detection = WallDetectionProfile(tile_overlap=20)
        self.ollama_service = OllamaService("prompts")

        self.pdf_processor = PdfProcessor(pdf_path)
        self.dino_service = DinoService(model_path=settings.DINO_HATCHING_MODEL)
        self.layout_processor = LayoutProcessor(self.pdf_processor, self.ollama_service)
        self.legend_layout_processor = LegendLayoutProcessor(self.pdf_processor, self.dino_service)
        self.hatching_detector = HatchingDetector(self.wall_detection)
        self.drawing_statistics = DrawingStatisticsAnalyzer(self.pdf_processor)
        self.hatching_processor = HatchingProcessor(self. ollama_service, self.drawing_statistics, self.dino_service, pdf_processor=self.pdf_processor)

    def run(self):
        global_blueprint_scale = self._get_scale()
        if not global_blueprint_scale:
            self.layout_processor.parse_drawings_scales()

        legends = self.layout_processor.get_legends()
        drawings = self.layout_processor.get_drawings()
        if not drawings:
            drawings = [None]

        results = []
        self.legend_row_items = None
        if legends:
            self.legend_layout_processor.parse_legend([legend["object"]["bbox"] for legend in legends])
            self.legend_row_items = self.legend_layout_processor.get_legend_row_items(min_inside_ratio=settings.LEGEND_LAYOUT_MIN_INSIDE_RATIO, merge_similar=False)
            self.hatching_processor.specify_legends(self.legend_row_items, load_deafult=False)
        else:
            logger.info("Легенда не найдена")

        drawings = self.layout_processor.get_drawings()
    
        if not drawings:
            return
    
        for i, drawing in enumerate(drawings):
            walls_processor = WallsProcessor(self.pdf_path, self.wall_detection, self.pdf_processor, dpi=settings.DPI)
            tiles = walls_processor.get_tiles(i, drawings, self.layout_processor)
            self.hatching_detector.get_walls(tiles, self.hatching_processor.legends)

            print(len(tiles))

    def _get_scale(self):
        blueprint_scale = self.layout_processor.get_blueprint_scale()
        if not blueprint_scale or blueprint_scale == (0, 0):
            return None
            
        return blueprint_scale