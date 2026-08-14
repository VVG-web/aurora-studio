#!/usr/bin/env python3
"""
Dashboard server with open-file endpoint.
Serves the analyst dashboard and allows opening config files via OS "open" command.
"""
import os
import sys
import json
import argparse
import subprocess
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

# Раздаём корень проекта: и сам отчёт в Artifacts/, и файлы настроек в Settings/
# лежат внутри него, а путь до отчёта дашборд знает от генератора.
PACKAGE_ROOT = paths.PROJECT_ROOT
BASE_DIR = PACKAGE_ROOT

# Known config files (name -> absolute path)
CONFIG_FILES = {
    "roster": paths.ROSTER_PATH,
    "events": paths.EVENTS_PATH,
    "analyst_metrics": paths.data("analyst_metrics.json"),
    "confluence_activity": paths.data("confluence_activity.json"),
}


# Открываем настройки текстовым редактором, а не приложением по умолчанию:
# .csv в macOS уходит в Numbers, а он сохраняет в .numbers, и правка до дашборда
# не доезжает. Порядок можно переопределить переменной AURORA_EDITOR_APP.
EDITOR_APPS = [os.environ.get("AURORA_EDITOR_APP"), "Cursor", "Visual Studio Code",
               "Sublime Text", "TextEdit"]

# Пересборка после правки настроек: та же цепочка, что и у run.py --skip-fetch.
HERE = os.path.dirname(os.path.abspath(__file__))
REBUILD_CHAIN = [os.path.join(HERE, s) for s in
                 ("process_confluence.py", "make_analyst_metrics.py",
                  "update_analyst_metrics.py", "verify_weekly_by_person.py",
                  "make_extended.py")]


def reveal_in_folder(filepath):
    """Показать файл в файловом менеджере (Finder / Проводник)."""
    if sys.platform.startswith("darwin"):
        subprocess.Popen(["open", "-R", filepath],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif os.name == "nt":
        subprocess.Popen(["explorer", "/select,", filepath])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(filepath)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.dirname(filepath)


def open_in_editor(filepath):
    """Открыть файл в текстовом редакторе. Возвращает имя приложения."""
    if sys.platform.startswith("darwin"):
        for app in EDITOR_APPS:
            if not app:
                continue
            r = subprocess.run(["open", "-a", app, filepath],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return app
        subprocess.Popen(["open", filepath], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "приложение по умолчанию"
    if os.name == "nt":
        os.startfile(filepath)
        return "приложение по умолчанию"
    subprocess.Popen(["xdg-open", filepath], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "приложение по умолчанию"


class DashboardHandler(SimpleHTTPRequestHandler):
    """Custom handler that serves static files and handles open-file requests."""

    def __init__(self, *args, **kwargs):
        # Корень раздачи задаётся здесь: SimpleHTTPRequestHandler запоминает каталог
        # при создании обработчика, поэтому chdir внутри do_GET уже ни на что не влиял.
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        # Check for __open/ endpoint
        if self.path.startswith('/__open/'):
            self.handle_open_file()
            return

        if self.path.startswith('/__reveal/'):
            self.handle_reveal()
            return

        if self.path.startswith('/__rebuild'):
            self.handle_rebuild()
            return

        # Otherwise serve static files from BASE_DIR
        super().do_GET()

    def send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_reveal(self):
        """Показать файл настроек в Finder."""
        name = self.path[len('/__reveal/'):]
        filepath = CONFIG_FILES.get(name)
        if not filepath or not os.path.isfile(filepath):
            self.send_json(404, {"ok": False, "error": f"Unknown config: {name}"})
            return
        try:
            self.send_json(200, {"ok": True, "dir": reveal_in_folder(filepath), "path": filepath})
        except Exception as e:
            self.send_json(500, {"ok": False, "error": str(e)})

    def handle_rebuild(self):
        """Пересобрать метрики и дашборд после правки файлов настроек."""
        for script in REBUILD_CHAIN:
            r = subprocess.run([sys.executable, script],
                               cwd=BASE_DIR, capture_output=True, text=True)
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
                self.send_json(500, {"ok": False, "step": os.path.basename(script),
                                     "error": " / ".join(tail) or "ненулевой код возврата"})
                return
        self.send_json(200, {"ok": True, "steps": len(REBUILD_CHAIN)})
    
    def handle_open_file(self):
        """Handle /__open/<name> request."""
        # Extract filename from path
        filename = self.path[len('/__open/'):]
        
        # Look up in known config files (prevents path injection)
        if filename not in CONFIG_FILES:
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            response = json.dumps({"ok": False, "error": f"Unknown config: {filename}"})
            self.wfile.write(response.encode())
            return
        
        filepath = CONFIG_FILES[filename]
        
        # Check if file exists
        if not os.path.isfile(filepath):
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            response = json.dumps({"ok": False, "error": f"File not found: {filepath}"})
            self.wfile.write(response.encode())
            return
        
        try:
            app = open_in_editor(filepath)
            self.send_json(200, {"ok": True, "path": filepath, "app": app})
        except Exception as e:
            self.send_json(500, {"ok": False, "error": str(e)})
    
    def log_message(self, format, *args):
        """Log to stderr."""
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))


def main():
    parser = argparse.ArgumentParser(description='Serve dashboard with open-file support')
    parser.add_argument('--port', type=int, default=8000, help='Port to listen on (default: 8000)')
    parser.add_argument('--html', action='store_true', help='Open dashboard HTML in browser after starting')
    args = parser.parse_args()
    
    port = args.port
    
    # Отчёт лежит там, куда его положил генератор, — путь считаем от корня проекта,
    # который этот сервер и раздаёт.
    rel = os.path.relpath(paths.OUTPUT_PATH, PACKAGE_ROOT).replace(os.sep, "/")
    dashboard_url = f"http://localhost:{port}/{rel}"
    print(f"Starting server on port {port}...")
    print(f"Dashboard: {dashboard_url}")
    print(f"Config files: {', '.join(CONFIG_FILES.keys())}")
    print("Ctrl+C to stop")
    print()
    
    if args.html:
        # Open browser after a short delay
        import time
        import webbrowser
        time.sleep(0.5)
        webbrowser.open(dashboard_url)
    
    # Start server
    # Пересборка занимает секунды — с однопоточным сервером страница на это время
    # переставала отвечать.
    server = ThreadingHTTPServer(('localhost', port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()