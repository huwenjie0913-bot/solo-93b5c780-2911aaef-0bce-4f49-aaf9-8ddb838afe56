"""本地启动入口：python run.py（可用 LABEL_DB_PATH 环境变量指定数据库文件）。"""

from app import create_app

if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=False)
