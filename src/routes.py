import io
import os
import zipfile
import json
import pandas as pd
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user
from flask import render_template_string
from flask import (
    Blueprint, current_app, request,
    send_file, render_template,
    jsonify, redirect, Response
)
from src.services.session_manager import SessionManager
from src.services.mssk_lookup import get_mssk_code_map
from src.core.logger import setup_logger
from src.core.config import load_config
from src.schemas import (
    ErrorResponse, SessionFull, SessionListResponse,
    UploadResponse, StatusResponse, DeleteResponse,
    PreviewResponse, RestoreResponse, HealthResponse,
    SelectRowsResponse, FilterHeightResponse, NewRunResponse,
    RunSwitchResponse, RunsListResponse, ReferenceAcceptedResponse,
    ReferenceBuildResponse,
)

logger = setup_logger(__name__)

bp = Blueprint("main", __name__, url_prefix="/ifc-vor")

# ---------- Авторизация ----------

USERS = {
    "admin": {
        'password': "admin54321",
        'role': "expert"
    },
    "test": {
        'password': "test",
        'role': "base"
    },
    "test1": {
        'password': "test1",
        'role': "base"
    }
}


class User(UserMixin):
    def __init__(self, username, role):
        self.id = username
        self.username = username
        self.role = role


login_manager = LoginManager()


@login_manager.user_loader
def load_user(username):
    if username in USERS:
        return User(username, USERS[username]['role'])
    return None


def init_login_manager():
    from flask import current_app
    if not hasattr(current_app, 'login_manager'):
        login_manager.init_app(current_app)


@bp.route('/login', methods=['GET', 'POST'])
def login():
    init_login_manager()
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username in USERS and USERS[username]['password'] == password:
            login_user(User(username, USERS[username]['role']))
            next_url = request.args.get('next', '/ifc-vor/')
            return f'<meta http-equiv="refresh" content="0; url={next_url}">'
        return '<h2>Неверный логин или пароль</h2><a href="/ifc-vor/login">Попробовать снова</a>'

    return render_template_string('''
    <html><body style="font-family: Arial; max-width: 400px; margin: 50px auto;">
    <h2>Вход в систему</h2>
    <form method="post">
        <input name="username" placeholder="Логин" required
               style="width: 100%; padding: 8px; margin: 5px 0;"><br>
        <input type="password" name="password" placeholder="Пароль" required
               style="width: 100%; padding: 8px; margin: 5px 0;"><br>
        <button type="submit" style="padding: 10px 20px; background: #007bff; color: white;
                border: none; cursor: pointer; width: 100%;">Войти</button>
    </form>
    </body></html>
    ''')


@bp.route('/logout')
def logout():
    logout_user()
    return '<h2>Вы вышли</h2><a href="/ifc-vor/login">Войти снова</a>'


@bp.before_request
def protect():
    if request.endpoint in ('main.login', 'main.logout', 'flasgger.apispec', 'flasgger.static'):
        return None

    init_login_manager()

    if not current_user.is_authenticated:
        if request.path.startswith('/ifc-vor/api/'):
            return jsonify({"detail": "Требуется авторизация"}), 401
        return redirect('/ifc-vor/login?next=' + request.url)


# ---------- Менеджер сессий ----------

_manager: SessionManager | None = None


def _get_manager() -> SessionManager:
    global _manager
    if _manager is None:
        cfg = load_config()
        _manager = SessionManager(
            upload_folder=current_app.config["UPLOAD_FOLDER"],
            output_folder=current_app.config["OUTPUT_FOLDER"],
            sessions_file=os.path.abspath(current_app.config["SESSIONS_FILE"]),
            perechen_xlsx=cfg.DOCUMENTS_PATH,
            koefs_xlsx=cfg.KOEFS_PATH
        )
        logger.info("SessionManager инициализирован")
    return _manager


def _ok(schema_instance, status: int = 200) -> Response:
    return Response(
        schema_instance.model_dump_json(exclude_none=False, by_alias=True),
        status=status,
        mimetype="application/json",
    )


def _err(schema_instance, status: int) -> Response:
    return Response(
        schema_instance.model_dump_json(by_alias=True),
        status=status,
        mimetype="application/json",
    )


# ------- HTML -------

@bp.route("/", methods=["GET"])
def index():
    return render_template("index.html")


# ------- API -------

@bp.route("/api/health", methods=["GET"])
def health_check():
    """
    Проверка работоспособности сервиса.
    ---
    tags:
      - health
    responses:
      200:
        description: Сервис работает
        schema:
          type: object
          properties:
            status:
              type: string
              example: ok
            timestamp:
              type: string
              example: "2026-06-17T12:00:00"
    """
    return _ok(HealthResponse(
        status="ok",
        timestamp=pd.Timestamp.now().isoformat()
    ))


@bp.route("/api/upload_ifc", methods=["POST"])
def upload_ifc():
    """
    Загрузка файла (IFC или PDF) и запуск фоновой обработки.
    ---
    tags:
      - upload
    consumes:
      - multipart/form-data
    parameters:
      - name: file
        in: formData
        type: file
        required: true
        description: IFC-файл (.ifc) или PDF-чертёж (.pdf), максимум 500 МБ
      - name: processingType
        in: formData
        type: string
        required: false
        default: "KR"
        description: Тип обработки — KR (конструктивные решения) или AR (архитектурные решения)
    responses:
      200:
        description: Файл принят, сессия создана, обработка запущена
        schema:
          type: object
          properties:
            sessionId:
              type: string
              example: "3fa85f64-5717-4562-b3fc-2c963f66afa6"
            status:
              type: string
              example: ifc_processing
            sourceType:
              type: string
              example: "ifc"
            message:
              type: string
              example: "IFC файл принят, начата обработка"
      400:
        description: Ошибка валидации
        schema:
          type: object
          properties:
            detail:
              type: string
      500:
        description: Внутренняя ошибка сервера
        schema:
          type: object
          properties:
            detail:
              type: string
    """
    if "file" not in request.files:
        return _err(ErrorResponse(detail="Файл не передан"), 400)

    f = request.files["file"]
    if not f.filename:
        return _err(ErrorResponse(detail="Пустое имя файла"), 400)

    filename_lower = f.filename.lower()
    is_ifc = filename_lower.endswith(".ifc")
    is_pdf = filename_lower.endswith(".pdf")

    if not is_ifc and not is_pdf:
        return _err(ErrorResponse(detail="Поддерживаются файлы .ifc и .pdf"), 400)

    f.seek(0, 2)
    size = f.tell()
    f.seek(0)

    max_size = 500 * 1024 * 1024
    if size > max_size:
        return _err(ErrorResponse(detail=f"Файл слишком большой. Максимум {max_size // (1024*1024)} МБ"), 413)

    # Получаем тип обработки (КР или АР)
    processing_type = request.form.get("processingType", "KR").upper()
    if processing_type not in ("KR", "AR"):
        processing_type = "KR"
    
    logger.info(f"Загрузка файла {f.filename}, тип обработки: {processing_type}")

    try:
        if is_ifc:
            result = _get_manager().process_ifc(f, f.filename, processing_type)
            result["source_type"] = "ifc"
        else:
            result = _get_manager().process_pdf(f, f.filename, processing_type)
            result["source_type"] = "pdf"
        return _ok(UploadResponse(**result))
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 400)
    except Exception as e:
        logger.error(f"Ошибка загрузки файла: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Внутренняя ошибка: {str(e)}"), 500)


@bp.route("/api/reference", methods=["POST"])
def build_reference():
    """
    Запуск построения JSON-справочников из IFC или PDF.
    ---
    tags:
      - reference
    consumes:
      - multipart/form-data
    parameters:
      - name: file
        in: formData
        type: file
        required: true
        description: IFC-файл (.ifc) или PDF-чертёж (.pdf), максимум 500 МБ
      - name: processingType
        in: formData
        type: string
        required: false
        default: "KR"
        description: Тип обработки — KR (конструктивные решения) или AR (архитектурные решения)
    responses:
      202:
        description: Файл принят, построение запущено
        schema:
          type: object
          properties:
            sessionId:
              type: string
            sourceType:
              type: string
              enum: [ifc, pdf]
            status:
              type: string
              example: reference_processing
            message:
              type: string
      400:
        description: Файл отсутствует или имеет неподдерживаемый формат
      413:
        description: Файл превышает допустимый размер
      500:
        description: Внутренняя ошибка сервера
    """
    if "file" not in request.files:
        return _err(ErrorResponse(detail="Файл не передан"), 400)

    uploaded_file = request.files["file"]
    if not uploaded_file.filename:
        return _err(ErrorResponse(detail="Пустое имя файла"), 400)

    extension = os.path.splitext(uploaded_file.filename)[1].lower()
    if extension not in (".ifc", ".pdf"):
        return _err(ErrorResponse(detail="Поддерживаются файлы .ifc и .pdf"), 400)

    uploaded_file.seek(0, 2)
    size = uploaded_file.tell()
    uploaded_file.seek(0)

    max_size = 500 * 1024 * 1024
    if size > max_size:
        return _err(
            ErrorResponse(
                detail=f"Файл слишком большой. Максимум {max_size // (1024 * 1024)} МБ"
            ),
            413,
        )

    # Получаем тип обработки для справочников
    processing_type = request.form.get("processingType", "KR").upper()
    if processing_type not in ("KR", "AR"):
        processing_type = "KR"
    
    logger.info(f"Запуск построения справочников: {uploaded_file.filename}, тип: {processing_type}")

    try:
        result = _get_manager().build_reference(
            uploaded_file, uploaded_file.filename, processing_type
        )
        response = _ok(ReferenceAcceptedResponse(**result), status=202)
        response.headers["Location"] = (
            f"/ifc-vor/api/session/{result['session_id']}/reference"
        )
        response.headers["Retry-After"] = "2"
        return response
    except ValueError as exc:
        return _err(ErrorResponse(detail=str(exc)), 400)
    except Exception as exc:
        logger.error(f"Ошибка запуска построения справочника: {exc}", exc_info=True)
        return _err(ErrorResponse(detail=f"Внутренняя ошибка: {exc}"), 500)


@bp.route("/api/session/<session_id>/reference", methods=["GET"])
def get_reference_result(session_id: str):
    """
    Получение результата построения JSON-справочников.
    ---
    tags:
      - reference
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
    responses:
      200:
        description: Оба JSON-справочника готовы
        schema:
          type: object
          properties:
            sessionId:
              type: string
            sourceType:
              type: string
            ifcElementsOutput:
              type: array
              items:
                type: object
            ifcRawElementsGrouped:
              type: array
              items:
                type: object
      202:
        description: Обработка ещё выполняется
      404:
        description: Сессия не найдена
      409:
        description: Сессия создана не справочной ручкой
      422:
        description: Ошибка фоновой обработки
    """
    if not session_id or len(session_id) < 8:
        return _err(ErrorResponse(detail="Некорректный ID сессии"), 400)

    manager = _get_manager()
    session = manager.get(session_id)
    if not session:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    if not session.get("is_reference_session"):
        return _err(
            ErrorResponse(detail="Сессия не относится к построению справочников"),
            409,
        )
    if session.get("status") == "error":
        return _err(
            ErrorResponse(
                detail=session.get("error") or "Ошибка построения справочников"
            ),
            422,
        )

    try:
        result = manager.get_reference_result(session_id)
        if result is None:
            response = _ok(
                ReferenceAcceptedResponse(
                    session_id=session_id,
                    source_type=session.get("source_type", ""),
                    status=session.get("status", "reference_processing"),
                    message=session.get(
                        "progress_message",
                        "Формирование JSON-справочников выполняется",
                    ),
                ),
                status=202,
            )
            response.headers["Retry-After"] = "2"
            return response
        return _ok(ReferenceBuildResponse(**result))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(f"Не удалось получить JSON-справочники: {exc}", exc_info=True)
        return _err(ErrorResponse(detail=str(exc)), 422)
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)


@bp.route("/api/sessions", methods=["GET"])
def list_sessions():
    """
    Список всех сессий обработки.
    ---
    tags:
      - sessions
    responses:
      200:
        description: Массив сессий
        schema:
          type: object
          properties:
            sessions:
              type: array
              items:
                $ref: '#/definitions/SessionFull'
            total:
              type: integer
    definitions:
      SessionFile:
        type: object
        properties:
          path:
            type: string
          filename:
            type: string
          size:
            type: integer
          downloadUrl:
            type: string
      SessionFull:
        type: object
        properties:
          sessionId:
            type: string
          createdAt:
            type: string
          status:
            type: string
          ifcFileName:
            type: string
          excelFileName:
            type: string
          buildingHeight:
            type: number
          files:
            type: array
            items:
              $ref: '#/definitions/SessionFile'
          progress:
            type: integer
          progressMessage:
            type: string
          hasResults:
            type: boolean
          error:
            type: string
    """
    try:
        raw = _get_manager().list_sessions()
        sessions = [SessionFull(**s) for s in raw]
        return _ok(SessionListResponse(sessions=sessions, total=len(sessions)))
    except Exception as e:
        logger.error(f"Ошибка списка сессий: {e}", exc_info=True)
        return _err(ErrorResponse(detail=str(e)), 500)


@bp.route("/api/session/<session_id>/status", methods=["GET"])
def get_status(session_id: str):
    """
    Краткий статус сессии (для поллинга).
    ---
    tags:
      - sessions
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Статус сессии
        schema:
          type: object
          properties:
            sessionId:
              type: string
            status:
              type: string
              enum: [ifc_processing, selecting_rows, processing, completed, error]
            realStatus:
              type: string
            progress:
              type: integer
            progressMessage:
              type: string
            error:
              type: string
            hasResults:
              type: boolean
      400:
        description: Некорректный ID сессии
      404:
        description: Сессия не найдена
    """
    if not session_id or len(session_id) < 8:
        return _err(ErrorResponse(detail="Некорректный ID сессии"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    status_mapping = {
        "ifc_processing": "processing",
        "pdf_processing": "processing",
        "reference_processing": "processing",
        "ifc_processed": "selecting_rows",
        "selecting_rows": "selecting_rows",
        "filtering_type": "filtering_type",
        "filtering_height": "filtering_height",
        "processing": "processing",
        "completed": "completed",
        "error": "error",
    }

    return _ok(StatusResponse(
        session_id=s["session_id"],
        status=status_mapping.get(s["status"], s["status"]),
        real_status=s["status"],
        progress=s.get("progress", 0),
        progress_message=s.get("progress_message", ""),
        error=s.get("error"),
        has_results=s.get("has_results", False),
    ))


@bp.route("/api/session/<session_id>", methods=["GET"])
def get_session(session_id: str):
    """
    Полные данные сессии.
    ---
    tags:
      - sessions
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Данные сессии
        schema:
          $ref: '#/definitions/SessionFull'
      400:
        description: Некорректный ID
      404:
        description: Сессия не найдена
    """
    if not session_id or len(session_id) < 8:
        return _err(ErrorResponse(detail="Некорректный ID сессии"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    return _ok(SessionFull(**s))


@bp.route("/api/session/<session_id>", methods=["DELETE"])
def delete_session(session_id: str):
    """
    Удаление сессии и всех связанных файлов.
    ---
    tags:
      - sessions
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Сессия удалена
        schema:
          type: object
          properties:
            deleted:
              type: boolean
            sessionId:
              type: string
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    ok = _get_manager().delete(session_id)
    if not ok:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    return _ok(DeleteResponse(deleted=True, session_id=session_id))


@bp.route("/api/session/<session_id>/restore", methods=["POST"])
def restore_session(session_id: str):
    """
    Восстановить данные сессии для продолжения работы.
    ---
    tags:
      - sessions
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Данные для восстановления интерфейса
        schema:
          type: object
          properties:
            sessionId:
              type: string
            status:
              type: string
            progress:
              type: integer
            progressMessage:
              type: string
            hasResults:
              type: boolean
            buildingHeight:
              type: number
            selectedRowsCount:
              type: integer
            files:
              type: array
              items:
                $ref: '#/definitions/SessionFile'
            processingType:
              type: string
              example: "KR"
      400:
        description: Некорректный ID
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    # Получаем файлы текущего запуска
    current_files = []
    current_run_id = s.get("current_run_id")
    if current_run_id:
        runs = s.get("runs", [])
        for run in runs:
            if run.get("run_id") == current_run_id:
                current_files = run.get("files", [])
                break
    
    if not current_files:
        current_files = s.get("files", [])

    return _ok(RestoreResponse(
        session_id=s["session_id"],
        status=s["status"],
        progress=s.get("progress", 0),
        progress_message=s.get("progress_message", ""),
        has_results=s.get("has_results", False),
        files=current_files,
        construction_types=s.get("construction_types", {}),
        building_height=s.get("building_height"),
        selected_rows_count=len(s.get("selected_rows", []) or []),
        source_type=s.get("source_type"),
        runs=s.get("runs", []),
        current_run_id=s.get("current_run_id"),
        processing_type=s.get("processing_type", "KR"),
    ))


@bp.route("/api/session/<session_id>/preview", methods=["GET"])
def preview_excel(session_id: str):
    """
    Предпросмотр Excel-таблицы после обработки IFC (для выбора строк).
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Заголовки и строки таблицы
        schema:
          type: object
          properties:
            headers:
              type: array
              items:
                type: string
            rows:
              type: array
              items:
                type: array
                items:
                  type: string
            totalRows:
              type: integer
            savedTypes:
              type: object
            processingType:
              type: string
      404:
        description: Сессия или файл не найдены
      500:
        description: Ошибка чтения файла
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    # Ищем Excel в original/ директории — ТОЛЬКО исправленный или объединенный
    excel_path = None
    session_dir = os.path.join(_get_manager().output_folder, session_id)
    original_dir = os.path.join(session_dir, 'original')
    search_dir = original_dir if os.path.exists(original_dir) else session_dir
    
    if os.path.exists(search_dir):
        for f in os.listdir(search_dir):
            if f.endswith('.xlsx'):
                # ПРОПУСКАЕМ сгруппированные и сокращённые
                if 'сгруппированный' in f.lower() or 'сокращенный' in f.lower():
                    continue
                if 'ДЛЯ_СМЕТЧИКА' in f:
                    excel_path = os.path.join(search_dir, f)
                    logger.info(f"Найден Excel для preview: {f}")
                    break
    
    # Fallback: старый путь из сессии
    if not excel_path or not os.path.exists(excel_path):
        excel_path = s.get("excel_file_path")
        logger.info(f"Использую fallback Excel: {excel_path}")
    
    if not excel_path or not os.path.exists(excel_path):
        return _err(ErrorResponse(detail="Excel файл не найден"), 404)

    try:
        if excel_path.endswith(".csv"):
            df = pd.read_csv(excel_path)
        else:
            df = pd.read_excel(excel_path)
    except Exception as e:
        return _err(ErrorResponse(detail=f"Ошибка чтения файла: {str(e)}"), 500)

    headers = df.columns.tolist()
    rows = df.fillna("-").astype(str).values.tolist()
    saved_types = s.get("construction_types", {})

    # Проверяем наличие чертежа и условных обозначений
    has_blueprint_image = False
    has_materials_md = False
    has_ifc_elements_json = False
    has_ifc_grouped_json = False

    source_type = s.get("source_type")
    
    for f in os.listdir(search_dir) if os.path.exists(search_dir) else []:
        if f.startswith("blueprint_painted") and f.endswith(".png"):
            has_blueprint_image = True
        if f == "materials_colors.md":
            has_materials_md = True

    # JSON/XLSX-справочники лежат в корне директории сессии
    if os.path.exists(session_dir):
        has_ifc_elements_json = os.path.exists(os.path.join(session_dir, "ifc_elements_output.json"))
        has_ifc_grouped_json = os.path.exists(os.path.join(session_dir, "ifc_raw_elements_grouped.json"))

    # После чтения основного Excel
    building_height = None
    try:
        xls = pd.ExcelFile(excel_path)
        
        # Ищем лист с высотой
        height_sheet = None
        for sheet_name in xls.sheet_names:
            if 'высота' in sheet_name.lower():
                height_sheet = sheet_name
                break
        
        if height_sheet:
            df_height = pd.read_excel(excel_path, sheet_name=height_sheet)
            logger.info(f"Лист высоты: {height_sheet}, колонки: {df_height.columns.tolist()}")
            
            # Ищем значение высоты
            for col in df_height.columns:
                col_lower = str(col).lower()
                if 'значение' in col_lower or 'высота' in col_lower or 'height' in col_lower:
                    for _, row_data in df_height.iterrows():
                        try:
                            val = float(row_data[col])
                            if val > 0:
                                building_height = val
                                logger.info(f"Найдена высота: {building_height} м")
                                break
                        except (ValueError, TypeError):
                            pass
                    if building_height:
                        break
            
            # Если не нашли по колонкам — ищем в первой строке
            if not building_height:
                for _, row_data in df_height.iterrows():
                    for col in df_height.columns:
                        try:
                            val = float(row_data[col])
                            if 1 < val < 10000:
                                building_height = val
                                logger.info(f"Найдена высота (по значению): {building_height} м")
                                break
                        except (ValueError, TypeError):
                            pass
                    if building_height:
                        break
    except Exception as e:
        logger.warning(f"Не удалось прочитать высоту: {e}")

    # Карта материалов для группировки превью по материалу (GlobalId → {name, order, code}).
    # Строится для АР (главный материал из IfcMaterialLayer::Name) и для КР
    # (код материала MGE_MaterialCode / IfcMaterialLayer::Name:
    #  «Железобетон сборный (СТ 00 15 01)», «Бетон монолитный (СТ 00 10 02)» и т.п.).
    materials_group_map = None
    if s.get("processing_type", "KR") in ("AR", "KR"):
        try:
            from src.services.materials_lookup import (
                build_materials_lookup,
                resolve_material_group_with_code,
            )
            mat_lookup, _ = build_materials_lookup()
            raw_map = _get_manager()._load_material_layer_map(session_id)
            if raw_map and 'GlobalId' in df.columns:
                materials_group_map = {}
                for gid in df['GlobalId'].dropna().astype(str).unique():
                    raw_val = raw_map.get(gid, '')
                    name, order, code = resolve_material_group_with_code(raw_val, mat_lookup)
                    materials_group_map[gid] = {"name": name, "order": order, "code": code}
        except Exception as e:
            logger.warning(f"Не удалось построить карту материалов для превью: {e}", exc_info=True)
            materials_group_map = None

    return _ok(PreviewResponse(
        headers=headers,
        rows=rows,
        total_rows=len(df),
        saved_types=saved_types,
        building_height=building_height,
        source_type=source_type,
        has_blueprint_image=has_blueprint_image,
        has_materials_md=has_materials_md,
        has_ifc_elements_json=has_ifc_elements_json,
        has_ifc_grouped_json=has_ifc_grouped_json,
        processing_type=s.get("processing_type", "KR"),
        mssk_code_map=get_mssk_code_map(),
        materials_group_map=materials_group_map,
    ))


@bp.route("/api/session/<session_id>/preview_result/<path:filename>", methods=["GET"])
def preview_result(session_id: str, filename: str):
    """
    Предпросмотр финального файла результатов (xlsx/csv/json).
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
      - name: filename
        in: path
        type: string
        required: true
        description: Имя файла
    responses:
      200:
        description: Содержимое файла
        schema:
          type: object
          properties:
            headers:
              type: array
              items:
                type: string
            rows:
              type: array
              items:
                type: array
                items:
                  type: string
            totalRows:
              type: integer
            isPreview:
              type: boolean
      400:
        description: Некорректное имя файла или формат не поддерживается
      404:
        description: Файл не найден
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return _err(ErrorResponse(detail="Некорректное имя файла"), 400)

    path = _get_manager().file_path(session_id, filename)
    if not path or not os.path.exists(path):
        # Fallback: ищем файл в корне директории сессии
        session_dir = os.path.join(_get_manager().output_folder, session_id)
        fallback_path = os.path.join(session_dir, filename)
        if os.path.exists(fallback_path) and os.path.isfile(fallback_path):
            path = fallback_path
    if not path or not os.path.exists(path):
        return _err(ErrorResponse(detail="Файл не найден"), 404)

    try:
        MAX_PREVIEW = 100

        if path.endswith(".csv"):
            df = pd.read_csv(path)
        elif path.endswith(".xlsx"):
            df = pd.read_excel(path)
        elif path.endswith(".json"):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            # Массив объектов (например, «все элементы» или «группы элементов»)
            if isinstance(data, list):
                if data and isinstance(data[0], dict):
                    headers = list(data[0].keys())
                else:
                    headers = ["№", "Значение"]
                rows = []
                for i, item in enumerate(data[:MAX_PREVIEW], 1):
                    if isinstance(item, dict):
                        rows.append([str(item.get(h, "-")) for h in headers])
                    else:
                        rows.append([str(i), str(item)])
                return _ok(PreviewResponse(
                    headers=headers,
                    rows=rows,
                    total_rows=len(data),
                    preview_rows=min(len(data), MAX_PREVIEW),
                    is_preview=len(data) > MAX_PREVIEW,
                ))

            # Словарь «ключ — значение»
            return _ok(PreviewResponse(
                headers=["Ключ", "Значение"],
                rows=[[str(k), str(v)] for k, v in data.items()],
                total_rows=len(data),
                is_preview=True,
            ))
        else:
            return _err(ErrorResponse(detail="Предпросмотр недоступен для этого типа файла"), 400)

        headers = df.columns.tolist()
        rows = df.fillna("-").astype(str).values.tolist()

        return _ok(PreviewResponse(
            headers=headers,
            rows=rows[:MAX_PREVIEW],
            total_rows=len(df),
            preview_rows=min(len(rows), MAX_PREVIEW),
            is_preview=len(rows) > MAX_PREVIEW,
        ))

    except Exception as e:
        return _err(ErrorResponse(detail=f"Ошибка чтения файла: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/blueprint_image", methods=["GET"])
def get_blueprint_image(session_id: str):
    """
    Получить изображение чертежа с отмеченными элементами (для PDF-сессий).
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    produces:
      - image/png
    responses:
      200:
        description: PNG-изображение чертежа
      404:
        description: Сессия или изображение не найдены
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    image_path = None
    
    # Ищем в original/ директории
    session_dir = os.path.join(_get_manager().output_folder, session_id)
    original_dir = os.path.join(session_dir, 'original')
    
    search_dirs = []
    if os.path.exists(original_dir):
        search_dirs.append(original_dir)
    search_dirs.append(session_dir)
    
    for search_dir in search_dirs:
        for fname in os.listdir(search_dir):
            if fname.startswith("blueprint_painted") and fname.endswith(".png"):
                image_path = os.path.join(search_dir, fname)
                break
        if image_path:
            break

    if not image_path or not os.path.exists(image_path):
        logger.warning(f"Изображение чертежа не найдено для сессии {session_id}. image_path={image_path}")
        return _err(ErrorResponse(detail="Изображение чертежа не найдено"), 404)

    return send_file(os.path.abspath(image_path), mimetype="image/png")


@bp.route("/api/session/<session_id>/materials_md", methods=["GET"])
def get_materials_md(session_id: str):
    """
    Получить условные обозначения материалов (markdown) для PDF-сессии.
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Markdown-таблица условных обозначений
        schema:
          type: object
          properties:
            markdown:
              type: string
      404:
        description: Сессия или файл не найдены
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    md_path = _get_manager().file_path(session_id, "materials_colors.md")

    # Fallback: ищем в директории сессии
    if not md_path or not os.path.exists(md_path):
        session_dir = os.path.join(_get_manager().output_folder, session_id)
        original_dir = os.path.join(session_dir, 'original')
        
        for search_dir in [original_dir, session_dir]:
            if os.path.exists(search_dir):
                fallback_path = os.path.join(search_dir, "materials_colors.md")
                if os.path.exists(fallback_path):
                    md_path = fallback_path
                    break

    if not md_path or not os.path.exists(md_path):
        return _err(ErrorResponse(detail="Условные обозначения не найдены"), 404)

    try:
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        return jsonify({"markdown": content})
    except Exception as e:
        return _err(ErrorResponse(detail=f"Ошибка чтения файла: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/download/<path:filename>", methods=["GET"])
def download_file(session_id: str, filename: str):
    """
    Скачать конкретный файл результата.
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
      - name: filename
        in: path
        type: string
        required: true
    produces:
      - application/octet-stream
    responses:
      200:
        description: Файл для скачивания
      400:
        description: Некорректное имя файла
      404:
        description: Файл не найден
    """
    if not filename or ".." in filename:
        return _err(ErrorResponse(detail="Некорректное имя файла"), 400)

    path = _get_manager().file_path(session_id, filename)
    if not path or not os.path.exists(path):
        # Fallback: ищем файл в корне директории сессии
        session_dir = os.path.join(_get_manager().output_folder, session_id)
        fallback_path = os.path.join(session_dir, filename)
        if os.path.exists(fallback_path) and os.path.isfile(fallback_path):
            path = fallback_path
    if not path or not os.path.exists(path):
        return _err(ErrorResponse(detail="Файл не найден"), 404)

    directory, name = os.path.split(path)
    return send_file(path, as_attachment=True, download_name=name, mimetype="application/octet-stream")


@bp.route("/api/session/<session_id>/download_all", methods=["GET"])
def download_all(session_id: str):
    """
    Скачать все файлы результатов сессии одним ZIP-архивом.
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
    produces:
      - application/zip
    responses:
      200:
        description: ZIP-архив со всеми файлами сессии
      404:
        description: Сессия не найдена или нет файлов
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    s = _get_manager().get(session_id)
    if not s:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)

    # Получаем файлы текущего запуска
    files = []
    current_run_id = s.get("current_run_id")
    if current_run_id:
        runs = s.get("runs", [])
        for run in runs:
            if run.get("run_id") == current_run_id:
                files = run.get("files", [])
                break
    
    if not files:
        files = s.get("files", [])
    
    if not files:
        return _err(ErrorResponse(detail="Нет файлов для скачивания"), 404)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            fp = f.get("path")
            fname = f.get("filename", "file")
            if fp and os.path.exists(fp):
                try:
                    zf.write(fp, arcname=fname)
                except Exception as e:
                    logger.warning(f"Ошибка добавления файла в архив {fname}: {e}")

    if buf.tell() == 0:
        return _err(ErrorResponse(detail="Не удалось создать архив"), 500)

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"session_{session_id[:8]}.zip",
    )


@bp.route("/api/session/<session_id>/select_rows", methods=["POST"])
def select_rows(session_id: str):
    """
    Выбор строк и запуск обработки.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии (должна быть в статусе selecting_rows)
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - rowIndices
          properties:
            rowIndices:
              type: array
              items:
                type: integer
              description: Индексы выбранных строк (0-based)
            allRows:
              type: boolean
              description: Выбрать все строки
              default: false
            rowTypes:
              type: object
              description: "Часть здания для каждой строки: {row_index: 'Надземная'|'Подземная'|'Цоколь'}"
            rowMaterials:
              type: object
              description: "Материал для каждой строки: {row_index: 'Бетон'|'Цемент'|'Кирпич'|'Дерево'|...}"
            buildingHeight:
              type: number
              description: Высота здания в метрах (1-10000)
            groupedData:
              type: object
            processingType:
              type: string
              enum: ["KR", "AR"]
              default: "KR"
    responses:
      200:
        description: Обработка запущена
      400:
        description: Ошибка валидации
      404:
        description: Сессия не найдена
      500:
        description: Ошибка сервера
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    data = request.get_json()
    if not data:
        return _err(ErrorResponse(detail="Тело запроса должно быть JSON"), 400)

    row_indices = data.get("rowIndices", data.get("row_indices", []))
    all_rows = data.get("allRows", data.get("all_rows", False))
    row_types = data.get("rowTypes", data.get("row_types", {}))
    row_materials = data.get("rowMaterials", data.get("row_materials", {}))
    building_height = data.get("buildingHeight", data.get("building_height"))
    grouped_data = data.get("groupedData", data.get("grouped_data", {}))
    processing_type = data.get("processingType", "KR").upper()

    # Валидация processing_type
    if processing_type not in ("KR", "AR"):
        processing_type = "KR"

    if not all_rows and (not row_indices or len(row_indices) == 0):
        return _err(ErrorResponse(detail="Выберите хотя бы одну строку"), 400)

    if building_height is not None:
        try:
            building_height = float(building_height)
            if building_height <= 0:
                return _err(ErrorResponse(detail="Высота должна быть положительным числом"), 400)
            if building_height > 10000:
                return _err(ErrorResponse(detail="Слишком большая высота здания"), 400)
        except (ValueError, TypeError):
            return _err(ErrorResponse(detail="Некорректное значение высоты"), 400)

    # Валидация материалов
    if row_materials:
        validated_materials = {}
        for key, value in row_materials.items():
            try:
                int_key = int(key)
                if not isinstance(value, str):
                    return _err(ErrorResponse(detail=f"Некорректное значение материала для строки {key}"), 400)
                validated_materials[str(int_key)] = value.strip()
            except (ValueError, TypeError):
                return _err(ErrorResponse(detail=f"Некорректный индекс строки в материалах: {key}"), 400)
        row_materials = validated_materials

    # Валидация частей здания
    if row_types:
        validated_types = {}
        valid_parts = {"Надземная", "Подземная", "Цоколь"}
        for key, value in row_types.items():
            try:
                int_key = int(key)
                if value not in valid_parts:
                    return _err(ErrorResponse(detail=f"Некорректная часть здания для строки {key}: {value}"), 400)
                validated_types[str(int_key)] = value
            except (ValueError, TypeError):
                return _err(ErrorResponse(detail=f"Некорректный индекс строки в частях здания: {key}"), 400)
        row_types = validated_types

    logger.info(f"select_rows: session={session_id}, строк={len(row_indices)}, тип={processing_type}")

    try:
        result = _get_manager().select_rows(
            session_id, 
            row_indices, 
            all_rows, 
            row_types, 
            row_materials,
            building_height, 
            grouped_data,
            processing_type
        )
        return _ok(SelectRowsResponse(**result))
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 400)
    except Exception as e:
        logger.error(f"Ошибка выбора строк: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


# ========== НОВЫЕ ЭНДПОИНТЫ ДЛЯ ПОВТОРНЫХ ЗАПУСКОВ ==========

@bp.route("/api/session/<session_id>/new_run", methods=["POST"])
def new_run(session_id: str):
    """
    Создать новый запуск обработки с другими выбранными строками.
    Исходный IFC/PDF файл не обрабатывается заново.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - rowIndices
          properties:
            rowIndices:
              type: array
              items:
                type: integer
            rowTypes:
              type: object
            rowMaterials:
              type: object
            buildingHeight:
              type: number
            groupedData:
              type: object
            processingType:
              type: string
              enum: ["KR", "AR"]
              default: "KR"
    responses:
      200:
        description: Новый запуск создан
      400:
        description: Ошибка валидации
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    data = request.get_json()
    if not data:
        return _err(ErrorResponse(detail="Тело запроса должно быть JSON"), 400)

    row_indices = data.get("rowIndices", data.get("row_indices", []))
    row_types = data.get("rowTypes", data.get("row_types", {}))
    row_materials = data.get("rowMaterials", data.get("row_materials", {}))
    building_height = data.get("buildingHeight", data.get("building_height"))
    grouped_data = data.get("groupedData", data.get("grouped_data", {}))
    processing_type = data.get("processingType", "KR").upper()

    # Валидация processing_type
    if processing_type not in ("KR", "AR"):
        processing_type = "KR"

    if not row_indices or len(row_indices) == 0:
        return _err(ErrorResponse(detail="Выберите хотя бы одну строку"), 400)

    if building_height is not None:
        try:
            building_height = float(building_height)
            if building_height <= 0:
                return _err(ErrorResponse(detail="Высота должна быть положительным числом"), 400)
            if building_height > 10000:
                return _err(ErrorResponse(detail="Слишком большая высота здания"), 400)
        except (ValueError, TypeError):
            return _err(ErrorResponse(detail="Некорректное значение высоты"), 400)

    logger.info(f"new_run: session={session_id}, строк={len(row_indices)}, тип={processing_type}")

    try:
        result = _get_manager().new_run(
            session_id,
            row_indices,
            row_types or {},
            row_materials or {},
            building_height,
            grouped_data or {},
            processing_type
        )
        return _ok(NewRunResponse(**result))
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 400)
    except Exception as e:
        logger.error(f"Ошибка создания нового запуска: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/runs", methods=["GET"])
def list_runs(session_id: str):
    """
    Получить список всех запусков сессии.
    ---
    tags:
      - sessions
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
    responses:
      200:
        description: Список запусков
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    runs = _get_manager().list_runs(session_id)
    current_run_id = None
    
    s = _get_manager().get(session_id)
    if s:
        current_run_id = s.get("current_run_id")
    
    # Логируем для отладки
    logger.info(f"Runs для сессии {session_id}:")
    logger.info(f"  всего={len(runs)}, current_run_id={current_run_id}")
    for run in runs:
        logger.info(f"  Run: {json.dumps(run, ensure_ascii=False, default=str)[:200]}")
    
    return _ok(RunsListResponse(
        runs=runs,
        current_run_id=current_run_id,
        total=len(runs)
    ))


@bp.route("/api/session/<session_id>/switch_run/<run_id>", methods=["POST"])
def switch_run(session_id: str, run_id: str):
    """
    Переключиться на другой запуск.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
      - name: run_id
        in: path
        type: string
        required: true
    responses:
      200:
        description: Переключение выполнено
      404:
        description: Сессия или запуск не найдены
    """
    if not session_id or not run_id:
        return _err(ErrorResponse(detail="ID сессии и запуска обязательны"), 400)

    try:
        result = _get_manager().switch_run(session_id, run_id)
        return _ok(RunSwitchResponse(**result))
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 404)
    except Exception as e:
        logger.error(f"Ошибка переключения запуска: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/filter_height", methods=["POST"])
def filter_by_height(session_id: str):
    """
    Фильтрация элементов по высоте здания.
    ---
    tags:
      - processing
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            buildingHeight:
              type: number
    responses:
      200:
        description: Фильтрация выполнена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    data = request.get_json()
    if not data:
        return _err(ErrorResponse(detail="Тело запроса должно быть JSON"), 400)

    building_height = data.get("buildingHeight", data.get("building_height"))
    if building_height is None:
        return _err(ErrorResponse(detail="Укажите высоту здания"), 400)

    try:
        height = float(building_height)
        if height <= 0:
            return _err(ErrorResponse(detail="Высота должна быть положительным числом"), 400)
        if height > 10000:
            return _err(ErrorResponse(detail="Слишком большая высота здания"), 400)
    except (ValueError, TypeError):
        return _err(ErrorResponse(detail="Некорректное значение высоты"), 400)

    try:
        result = _get_manager().filter_by_height(session_id, height)
        return _ok(FilterHeightResponse(**result))
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 400)
    except Exception as e:
        logger.error(f"Ошибка фильтрации по высоте: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/3d_model", methods=["POST"])
def create_3d_model(session_id: str):
    """
    Создание 3D модели (GLB) по запросу пользователя.
    Файл формируется только для IFC-сессий и только при вызове этой ручки.
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Статус создания 3D модели
        schema:
          type: object
          properties:
            status:
              type: string
              enum: [creating, ready]
            filename:
              type: string
            downloadUrl:
              type: string
      400:
        description: 3D модель недоступна для этой сессии
      404:
        description: Сессия не найдена
      500:
        description: Внутренняя ошибка
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    try:
        result = _get_manager().ensure_3d_model(session_id)
        return jsonify(result), 200
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except ValueError as e:
        return _err(ErrorResponse(detail=str(e)), 400)
    except Exception as e:
        logger.error(f"Ошибка создания 3D модели: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)


@bp.route("/api/session/<session_id>/3d_model/status", methods=["GET"])
def get_3d_model_status(session_id: str):
    """
    Статус создания 3D модели (GLB).
    ---
    tags:
      - files
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        description: ID сессии
    responses:
      200:
        description: Статус генерации 3D модели
        schema:
          type: object
          properties:
            status:
              type: string
              enum: [none, creating, ready, error]
            filename:
              type: string
            error:
              type: string
      404:
        description: Сессия не найдена
    """
    if not session_id:
        return _err(ErrorResponse(detail="ID сессии не указан"), 400)

    try:
        result = _get_manager().get_3d_model_status(session_id)
        return jsonify(result), 200
    except KeyError:
        return _err(ErrorResponse(detail="Сессия не найдена"), 404)
    except Exception as e:
        logger.error(f"Ошибка получения статуса 3D модели: {e}", exc_info=True)
        return _err(ErrorResponse(detail=f"Ошибка сервера: {str(e)}"), 500)