from __future__ import annotations
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class ErrorResponse(CamelModel):
    detail: str


class SessionFile(CamelModel):
    path: str
    filename: str
    size: int
    download_url: Optional[str] = None


class SessionFull(CamelModel):
    session_id: str
    created_at: str
    status: str
    source_type: Optional[str] = None
    processing_type: str = "KR"  # ← ДОБАВЛЕНО: тип обработки (KR/AR)
    ifc_file_name: Optional[str] = None
    pdf_file_name: Optional[str] = None
    excel_file_name: Optional[str] = None
    selected_rows: Optional[List[int]] = None
    construction_types: Dict[str, str] = {}
    building_height: Optional[float] = None
    files: List[SessionFile] = []
    error: Optional[str] = None
    progress: int = 0
    progress_message: str = ""
    has_results: bool = False
    is_reference_session: bool = False
    # Поля для поддержки множественных запусков
    runs: Optional[List[Dict[str, Any]]] = None
    current_run_id: Optional[str] = None


class SessionListResponse(CamelModel):
    sessions: List[SessionFull]
    total: int


class UploadResponse(CamelModel):
    session_id: str
    status: str
    source_type: str  # "ifc" или "pdf"
    processing_type: str = "KR"
    message: str


class ReferenceAcceptedResponse(CamelModel):
    session_id: str
    source_type: str
    status: str
    message: str


class ReferenceBuildResponse(CamelModel):
    session_id: str
    source_type: str
    ifc_elements_output: List[Dict[str, Any]]
    ifc_raw_elements_grouped: List[Dict[str, Any]]


class SelectRowsRequest(CamelModel):
    row_indices: List[int]
    all_rows: bool = False
    row_types: Dict[str, str] = {}
    row_materials: Dict[str, str] = {}
    building_height: Optional[float] = None
    processing_type: str = "KR"


class FilterHeightRequest(CamelModel):
    building_height: float


class StatusResponse(CamelModel):
    session_id: str
    status: str
    real_status: Optional[str] = None
    progress: int = 0
    progress_message: str = ""
    error: Optional[str] = None
    has_results: bool = False


class DeleteResponse(CamelModel):
    deleted: bool
    session_id: Optional[str] = None


class PreviewResponse(CamelModel):
    headers: List[str]
    rows: List[List[str]]
    total_rows: int
    saved_types: Optional[Dict[str, str]] = None
    is_preview: Optional[bool] = None
    preview_rows: Optional[int] = None
    building_height: Optional[float] = None  
    source_type: Optional[str] = None
    processing_type: str = "KR"
    has_blueprint_image: bool = False
    has_materials_md: bool = False
    has_ifc_elements_json: bool = False
    has_ifc_grouped_json: bool = False
    # Карта MSSK-кодов из data/elements_mssk_nested.json:
    # code → {name, order} (для группировки превью по колонке «Код мсск»)
    mssk_code_map: Optional[Dict[str, Any]] = None
    # Карта материалов: GlobalId → {name, order, code}
    # (имя группы по data/materials_mssk_nested.json из поля
    #  «Свойство::IfcMaterialLayer::Name» / пар MGE_Material*;
    #  «Прочее»/«Многослойные»; code — МССК-код материала для отображения
    #  в скобках). Используется в превью для группировки по материалу:
    #  АР — главный материал (L2), КР — монолитный/сборный ж/б (L3).
    materials_group_map: Optional[Dict[str, Any]] = None


class RestoreResponse(CamelModel):
    session_id: str
    status: str
    progress: int = 0
    progress_message: str = ""
    has_results: bool = False
    files: List[SessionFile] = []
    construction_types: Dict[str, str] = {}
    building_height: Optional[float] = None
    selected_rows_count: int = 0
    source_type: Optional[str] = None
    processing_type: str = "KR"
    runs: Optional[List[Dict[str, Any]]] = None
    current_run_id: Optional[str] = None


class SelectRowsResponse(CamelModel):
    session_id: str
    status: str
    selected_rows: int
    processing_type: str = "KR"
    message: str


class FilterHeightResponse(CamelModel):
    session_id: str
    status: str
    building_height: float
    message: str


class HealthResponse(CamelModel):
    status: str
    timestamp: str


# ========== НОВЫЕ СХЕМЫ ДЛЯ ПОДДЕРЖКИ МНОЖЕСТВЕННЫХ ЗАПУСКОВ ==========

class RunInfo(CamelModel):
    """Информация об одном запуске обработки"""
    run_id: str
    run_number: int
    status: str
    processing_type: str = "KR"
    selected_rows: Optional[List[int]] = None
    construction_types: Dict[str, str] = {}
    construction_materials: Dict[str, str] = {}
    building_height: Optional[float] = None
    grouped_data: Dict[str, Any] = {}
    files: List[SessionFile] = []
    created_at: str
    error: Optional[str] = None


class NewRunRequest(CamelModel):
    """Запрос на создание нового запуска"""
    row_indices: List[int]
    row_types: Dict[str, str] = {}
    row_materials: Dict[str, str] = {}
    building_height: Optional[float] = None
    grouped_data: Optional[Dict[str, Any]] = None
    processing_type: str = "KR"  # ← ДОБАВЛЕНО


class NewRunResponse(CamelModel):
    """Ответ при создании нового запуска"""
    session_id: str
    run_id: str
    run_number: int
    status: str
    processing_type: str = "KR"
    selected_rows: int
    message: str


class RunSwitchResponse(CamelModel):
    """Ответ при переключении на другой запуск"""
    session_id: str
    run_id: str
    run_number: Optional[int] = None
    processing_type: str = "KR"
    status: Optional[str] = None
    files: List[SessionFile] = []
    building_height: Optional[float] = None


class RunsListResponse(CamelModel):
    """Список всех запусков сессии"""
    runs: List[RunInfo]
    current_run_id: Optional[str] = None
    total: int


class PositionLinkItem(CamelModel):
    """Позиция цифрового сборника для группы элементов"""
    id: int
    name: str = ""


class PositionLinkVariant(CamelModel):
    """Вариант ссылок для группы: контекст (часть здания + геометрия).

    Один nameKey может иметь несколько вариантов (например, стены с тем же
    именем Revit в подземной и надземной части) — фронтенд выбирает вариант
    по контексту своей группы (part + geo).
    """
    part: str = ""
    geo: str = ""
    positions: List[PositionLinkItem] = []


class PositionLinksResponse(CamelModel):
    """Ссылки на позиции цифрового сборника по группам элементов.

    Ключ — имя элемента без цифрового ID (совпадает с nameKey групп
    в веб-интерфейсе), значение — варианты с контекстом (part, geo).
    ready=False означает, что файл position_links.json ещё строится.
    """
    session_id: str
    ready: bool = False
    position_links: Dict[str, List[PositionLinkVariant]] = {}