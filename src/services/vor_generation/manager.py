"""Жизненный цикл задачи формирования ВОР (вкладка «Формирование ВОР»).

Задача (job) — отдельная запись в общем реестре сессий
(``outputs/sessions.json``) с ``session_type='vor_generation'``; файлы — в
``outputs/<job_id>/``. На вход загружаются: IFC-модель и документы
(ПОС, ВОР застройщика, ПЗ, ПД). Результат — заполненный файл ВОР по типовому
шаблону.

Обработка выполняется в фоновом потоке:

1. выгрузка сырого дампа элементов IFC (``IFC_исходные_параметры.json``);
2. разбор документов (ВОР застройщика — детерминированно; ПОС/ПЗ/ПД — LLM);
3. сборка ВОР (:func:`builder.build_vor`);
4. сохранение результата и обновление статуса задачи.
"""

import copy
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.utils import secure_filename

from src.core.logger import setup_logger
from . import builder, customer_vor, documents

logger = setup_logger(__name__)

# Имя заполненного ВОР в каталоге задачи.
VOR_OUTPUT_FILENAME = "Сформированный_ВОР.xlsx"
# Имя ВОР заказчика, приведённого к структуре типового ВОР.
VOR_CUSTOMER_FILENAME = "ВОР_заказчика.xlsx"
# Имя JSON со статистикой сборки.
VOR_RESULT_JSON = "vor_generation.json"
# Имя сырого дампа IFC.
IFC_RAW_JSON = "IFC_исходные_параметры.json"

# Виды загружаемых документов.
DOC_KINDS = ("pos", "vor", "pz", "pd")
DOC_LABELS = {"pos": "ПОС", "vor": "ВОР", "pz": "ПЗ", "pd": "ПД"}

# Колонки QTO-периметра/толщины плит (мм) в сыром дампе IFC.
_SLAB_PERIMETER_KEY = "QTO::Qto_SlabBaseQuantities::Perimeter"
_SLAB_DEPTH_KEY = "QTO::Qto_SlabBaseQuantities::Depth"
_BBOX_PERIMETER_KEY = "QTO_bbox::Периметр_мм"
_BBOX_DEPTH_KEY = "QTO_bbox::Толщина_мм"


def _is_positive(value) -> bool:
    """True, если значение сырого дампа — положительное число."""
    try:
        return float(str(value).replace(",", ".")) > 0
    except (TypeError, ValueError):
        return False


def _enrich_slab_formwork(raw_json: Path, model) -> int:
    """Дополняет сырой дамп bbox-периметром/толщиной плит с неполным QTO.

    Опалубка фундаментной плиты считается по торцам (периметр контура ×
    толщина). У части плит QTO не содержит ``Perimeter``/``Depth`` (неполная
    выгрузка Revit) — для них периметр и толщина вычисляются из геометрии
    (bbox) тем же помощником, что и в конвейере ЦС/ИИ
    (``ifc_raw_dump._compute_formwork_bbox_quantities``). Значения (в мм)
    записываются в колонки ``QTO_bbox::Периметр_мм`` / ``QTO_bbox::Толщина_мм``
    и читаются :mod:`src.services.vor_generation.elements`.

    Возвращает число обогащённых элементов.
    """
    from src.services.ifc_raw_dump import _compute_formwork_bbox_quantities

    rows = json.loads(raw_json.read_text(encoding="utf-8"))
    by_gid = {str(row.get("GlobalId")): row for row in rows if row.get("GlobalId")}
    enriched = 0
    for element in model.by_type("IfcSlab"):
        row = by_gid.get(str(getattr(element, "GlobalId", "") or ""))
        if row is None:
            continue
        has_perimeter = _is_positive(row.get(_SLAB_PERIMETER_KEY))
        has_thickness = _is_positive(row.get(_SLAB_DEPTH_KEY))
        if has_perimeter and has_thickness:
            continue
        bbox = _compute_formwork_bbox_quantities(element)
        if not bbox:
            continue
        if not has_perimeter:
            row[_BBOX_PERIMETER_KEY] = bbox["perimeter_mm"]
        if not has_thickness:
            row[_BBOX_DEPTH_KEY] = bbox["depth_mm"]
        enriched += 1
    if enriched:
        raw_json.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        logger.info("Сырой дамп IFC: bbox-периметр/толщина плит добавлены для %d элементов", enriched)
    return enriched


class VorGenerationManager:
    """Менеджер задач формирования ВОР."""

    ACTIVE = {"processing"}

    def __init__(self, sessions):
        self.sessions = sessions
        self.root = Path(sessions.output_folder)
        self.lock = sessions._state_lock
        self.jobs = {}
        with self.lock:
            for sid, session in sessions._sessions.items():
                if session.get("session_type") == "vor_generation":
                    self.jobs[sid] = session["vor_generation"]
            for sid, job in list(self.jobs.items()):
                if job["status"] in self.ACTIVE:
                    job.update(status="error", message="Обработка прервана перезапуском сервиса. Загрузите файлы заново.",
                               error="Сервис перезапущен")
                self._update(sid)

    # -----------------------------------------------------------------
    def _write(self, job_id: str, name: str, payload) -> None:
        path = self.root / job_id / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _update(self, job_id: str, **fields) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job.update(fields)
            job.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            files = []
            for name in (VOR_OUTPUT_FILENAME, VOR_CUSTOMER_FILENAME, VOR_RESULT_JSON, IFC_RAW_JSON):
                path = self.root / job_id / name
                if path.is_file():
                    files.append({"path": str(path), "filename": name, "size": path.stat().st_size})
            self.sessions._sessions[job_id] = {
                "session_id": job_id,
                "session_type": "vor_generation",
                "source_type": "vor_generation",
                "created_at": job["created_at"],
                "status": job["status"],
                "processing_type": "KR",
                "ifc_file_name": job["ifc"].get("name") if job.get("ifc") else "",
                "progress": job.get("progress", 0),
                "progress_message": job.get("message", ""),
                "error": job.get("error"),
                "files": files,
                "has_results": job["status"] == "completed",
                "vor_generation": job,
            }
            self.sessions._save()

    # -----------------------------------------------------------------
    def _stage(self, job_id: str, progress=None, message="", **fields) -> None:
        self._update(job_id, message=message, **({"progress": progress} if progress is not None else {}), **fields)

    def _save_files(self, job_id: str, files: dict) -> dict:
        saved = {}
        for kind, file in files.items():
            if not file or not file.filename:
                continue
            name = secure_filename(file.filename) or "file"
            path = self.root / job_id / f"{kind}_{name}"
            file.save(path)
            saved[kind] = {"name": file.filename, "path": str(path)}
        return saved

    # -----------------------------------------------------------------
    def create(self, files: dict):
        """Создаёт задачу: сохраняет файлы и запускает фоновую обработку.

        ``files`` — словарь ``{kind: FileStorage}``, где ``kind`` ∈
        ``{'ifc', 'pos', 'vor', 'pz', 'pd'}``.
        """
        ifc = files.get("ifc")
        if not ifc or not ifc.filename or Path(ifc.filename).suffix.lower() != ".ifc":
            raise ValueError("Загрузите IFC-модель (.ifc)")
        docs = {kind: file for kind in DOC_KINDS if (file := files.get(kind)) and file.filename}
        for kind, file in docs.items():
            if Path(file.filename).suffix.lower() not in (".pdf", ".xlsx", ".xls"):
                raise ValueError(f"{DOC_LABELS[kind]}: поддерживаются файлы PDF, XLSX, XLS")

        job_id = str(uuid.uuid4())
        (self.root / job_id).mkdir(parents=True, exist_ok=True)
        saved = self._save_files(job_id, {"ifc": ifc, **docs})
        ifc_info = saved["ifc"]
        doc_info = {kind: saved[kind] for kind in DOC_KINDS if kind in saved}

        with self.lock:
            self.jobs[job_id] = {
                "id": job_id,
                "ifc": ifc_info,
                "documents": doc_info,
                "status": "processing",
                "progress": 0,
                "message": "Файлы приняты, начата обработка",
                "error": None,
            }
            self._update(job_id)
        threading.Thread(target=self._run, args=(job_id,), daemon=True,
                         name=f"VOR-Generation-{job_id[:8]}").start()
        return self.get(job_id)

    # -----------------------------------------------------------------
    def _run(self, job_id: str) -> None:
        try:
            job = self.get(job_id)
            job_dir = self.root / job_id
            ifc_path = job["ifc"]["path"]
            doc_paths = {kind: info["path"] for kind, info in job.get("documents", {}).items()}

            # 1. Сырой дамп элементов IFC.
            self._stage(job_id, 5, "Выгрузка параметров элементов из модели IFC")
            import ifcopenshell
            from src.services.ifc_raw_dump import dump_ifc_elements_raw
            model = ifcopenshell.open(ifc_path)
            dump_ifc_elements_raw(ifc_path, str(job_dir), model=model)
            raw_json = job_dir / IFC_RAW_JSON
            if not raw_json.is_file():
                raise RuntimeError("Не удалось выгрузить параметры элементов IFC")
            # Периметр/толщина плит для расчёта опалубки фундаментных плит
            # (у части плит QTO неполный — добавляется bbox-оценка).
            try:
                _enrich_slab_formwork(raw_json, model)
            except Exception as exc:  # pragma: no cover — не роняем задачу
                logger.warning("Сырой дамп: bbox-периметр/толщина плит не добавлены: %s", exc)
            del model

            # 2. Разбор документов.
            self._stage(job_id, 35, "Разбор загруженных документов")
            docs = documents.load_documents(
                doc_paths, use_llm=True,
                progress=lambda message: self._stage(job_id, None, message),
            )

            # 3. ВОР заказчика — в структуру типового ВОР (группировка заказчика
            #    по блокам/секциям/этажам «схлопывается» к подразделам шаблона).
            customer_stats = None
            vor_path = doc_paths.get("vor")
            if vor_path:
                self._stage(job_id, 55, "Приведение ВОР заказчика к типовому виду")
                try:
                    customer_stats = customer_vor.build_customer_vor(
                        vor_path, str(job_dir / VOR_CUSTOMER_FILENAME),
                        elements_json_path=str(raw_json),
                        progress=lambda message: self._stage(job_id, None, message),
                    )
                except Exception as exc:  # pragma: no cover — не роняем задачу
                    logger.warning("ВОР заказчика: приведение к типовому виду не выполнено: %s", exc)

            # 4. Сборка ВОР.
            self._stage(job_id, 70, "Формирование ВОР по типовому шаблону")
            output_path = job_dir / VOR_OUTPUT_FILENAME
            stats = builder.build_vor(
                elements_json_path=str(raw_json),
                docs=docs,
                output_path=str(output_path),
                ifc_name=job["ifc"]["name"],
                progress=lambda message: self._stage(job_id, None, message),
            )
            stats["customer"] = customer_stats

            self._write(job_id, VOR_RESULT_JSON, stats)
            self._stage(job_id, 100, "ВОР сформирован", status="completed", result=stats)
        except Exception as exc:
            logger.exception("Формирование ВОР не выполнено: %s", job_id)
            self._update(job_id, status="error", error=str(exc), message="Ошибка формирования ВОР")

    # -----------------------------------------------------------------
    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs or job_id not in self.sessions._sessions:
                raise KeyError("Задача формирования ВОР не найдена")
            job = copy.deepcopy(self.jobs[job_id])
        job["has_result"] = (self.root / job_id / VOR_OUTPUT_FILENAME).is_file()
        job["has_customer_vor"] = (self.root / job_id / VOR_CUSTOMER_FILENAME).is_file()
        return job

    def list(self) -> list:
        with self.lock:
            return [copy.deepcopy(job) for sid, job in reversed(list(self.jobs.items()))
                    if sid in self.sessions._sessions]

    def artifact(self, job_id: str, filename: str):
        job = self.get(job_id)
        allowed = {VOR_OUTPUT_FILENAME, VOR_CUSTOMER_FILENAME, VOR_RESULT_JSON, IFC_RAW_JSON}
        if filename not in allowed:
            raise KeyError("Файл не найден")
        if filename in (VOR_OUTPUT_FILENAME, VOR_CUSTOMER_FILENAME) and job["status"] != "completed":
            raise ValueError("Результат ещё не готов")
        path = self.root / job_id / filename
        if not path.is_file():
            raise KeyError("Файл не найден")
        return path
