import os
from flask import Flask
from flasgger import Swagger
from dotenv import load_dotenv
from flask_login import LoginManager

SWAGGER_TEMPLATE = {
    "swagger": "2.0",
    "info": {
        "title": "IFC VOR Generator API",
        "version": "1.0.0",
        "description": "API для генерации видов работ из IFC-файлов",
    },
    "basePath": "/",
    "schemes": ["http", "https"],
    "consumes": ["application/json", "multipart/form-data"],
    "produces": ["application/json"],
}

SWAGGER_CONFIG = {
    "headers": [],
    "specs": [
        {
            "endpoint": "apispec",
            "route": "/ifc-vor/apispec.json",
            "rule_filter": lambda rule: True,
            "model_filter": lambda tag: True,
        }
    ],
    "static_url_path": "/ifc-vor/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/ifc-vor/docs",
}


def create_app() -> Flask:
    load_dotenv()

    app = Flask(__name__)

    app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "1024")) * 1024 * 1024
    app.config["UPLOAD_FOLDER"] = os.getenv("UPLOAD_FOLDER", "uploads")
    app.config["OUTPUT_FOLDER"] = os.getenv("OUTPUT_FOLDER", "outputs")
    app.config["SESSIONS_FILE"] = os.getenv("SESSIONS_FILE", "outputs/sessions.json")
    app.config["PERECHEN_XLSX"] = os.getenv("PERECHEN_XLSX", "data/perechen_kr.xlsx")
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "iloverza123")

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["OUTPUT_FOLDER"], exist_ok=True)

    login_manager = LoginManager()
    login_manager.init_app(app)

    from .routes import USERS, User

    @login_manager.user_loader
    def load_user(username):
        if username in USERS:
            return User(username, USERS[username]['role'])
        return None

    @login_manager.unauthorized_handler
    def unauthorized():
        from flask import request, jsonify, redirect
        if request.path.startswith('/ifc-vor/api/'):
            return jsonify({"detail": "Требуется авторизация"}), 401
        return redirect('/ifc-vor/login?next=' + request.url)

    from .routes import bp as main_bp
    app.register_blueprint(main_bp)

    Swagger(app, template=SWAGGER_TEMPLATE, config=SWAGGER_CONFIG)

    return app