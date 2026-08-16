"""
Entry point. Run with: python run.py
Or in production, with a proper WSGI server, e.g.:
    gunicorn -w 1 -b 0.0.0.0:5000 run:app
(Use exactly 1 worker - the background scanner thread and token cache
are per-process, so multiple workers would each try to log in/scan
independently.)
"""
from app.web import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
