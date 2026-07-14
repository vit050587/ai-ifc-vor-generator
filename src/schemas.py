from __future__ import annotations
from typing import List, Optional, Dict
from pydantic import BaseModel, ConfigDict
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
    source_type: Optional[str] = None  # "ifc" или "pdf"
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


class SessionListResponse(CamelModel):
    sessions: List[SessionFull]
    total: int


class UploadResponse(CamelModel):
    session_id: str
    status: str
    source_type: str  # "ifc" или "pdf"
    message: str


class SelectRowsRequest(CamelModel):
    row_indices: List[int]
    all_rows: bool = False
    row_types: Dict[str, str] = {}
    building_height: Optional[float] = None


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
    has_blueprint_image: bool = False
    has_materials_md: bool = False


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


class SelectRowsResponse(CamelModel):
    session_id: str
    status: str
    selected_rows: int
    message: str


class FilterHeightResponse(CamelModel):
    session_id: str
    status: str
    building_height: float
    message: str


class HealthResponse(CamelModel):
    status: str
    timestamp: str
