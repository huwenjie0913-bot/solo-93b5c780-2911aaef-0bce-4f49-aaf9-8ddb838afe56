"""Flask 应用工厂：蓝图注册、统一 4xx/5xx 错误序列化、启动时建表。"""

from flask import Flask, jsonify

from .api import bp
from .db import get_db_path, init_db
from .errors import APIError


def create_app(init_database=True):
    app = Flask(__name__)
    app.register_blueprint(bp)

    if init_database:
        init_db()

    @app.errorhandler(APIError)
    def _handle_api_error(err):
        return jsonify(err.to_response()[0]), err.to_response()[1]

    @app.errorhandler(404)
    def _handle_404(_e):
        return jsonify({"error": {"code": "NOT_FOUND", "message": "路由不存在"}}), 404

    @app.errorhandler(405)
    def _handle_405(_e):
        return jsonify({"error": {"code": "METHOD_NOT_ALLOWED",
                                  "message": "HTTP 方法不允许"}}), 405

    @app.errorhandler(500)
    def _handle_500(e):
        return jsonify({"error": {"code": "INTERNAL", "message": str(e)}}), 500

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "db": get_db_path()})

    return app
