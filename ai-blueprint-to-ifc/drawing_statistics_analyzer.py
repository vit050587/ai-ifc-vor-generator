from pdf_prcoessor import PdfProcessor

import statistics

import debug_manager

class DrawingStatisticsAnalyzer:
    def __init__(self, pdf_processor: PdfProcessor) -> None:
        self.pdf_processor = pdf_processor

        self.deleted_walls = {}
        self.hatching_scores: list[list[float]] = []

    def add_deleted_walls(self, walls: dict[str, list]):
        for key, values in walls.items():
            self.deleted_walls.setdefault(key, []).extend(values)

    def add_hatching_scores(self, scores: list[float]):
        self.hatching_scores.append(scores)

    def delete_last_hatching_scores(self):
        self.hatching_scores = self.hatching_scores[:-1]

    def get_average_best_hatching_confidence(self) -> float | None:
        scores = [
            score
            for drawing_scores in self.hatching_scores
            for score in drawing_scores
        ]
        if not scores:
            return None

        return statistics.mean(scores)

    def get_last_processing_confidence(self) -> float | None:
        if not self.hatching_scores:
            return None

        last_scores = self.hatching_scores[-1]
        if not last_scores:
            return None

        return statistics.mean(last_scores)

    def save_deleted_walls(self, legend_row_items):
        for wall_type, deleted_walls in self.deleted_walls.items():
            walls_with_score_labels = []
            for wall in deleted_walls:
                wall_with_score_label = dict(wall)
                score = wall["hatching"]["best"]["score"]
                wall_with_score_label["id"] = f"{round(score * 100, 1)}%"
                walls_with_score_labels.append(wall_with_score_label)

            debug_manager.save_blueprint_walls_by_material(
                "full",
                walls_with_score_labels,
                self.pdf_processor,
                f"page_{self.pdf_processor.pdf_path.stem}_deleted_walls_{wall_type}.png",
                legend_row_items,
                fill_opacity=0.5,
                save_md=False,
            )
