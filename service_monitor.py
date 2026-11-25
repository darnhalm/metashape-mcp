"""Run directly with Python or from Metashape's Tools > Run Script."""

import json
import os
import runpy
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from mcp_settings import load_settings, save_settings

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets


DEFAULT_URL = "http://127.0.0.1:8766/mcp"
PROBE = Path(__file__).resolve().with_name("mcp_status_probe.py")
STATES = {
    "checking": ("Проверка подключения", "#4973A4"),
    "online": ("На связи", "#08765B"),
    "offline": ("Нет подключения", "#A45A10"),
    "timeout": ("Нет ответа", "#A45A10"),
    "error": ("Ошибка проверки", "#AA3541"),
}


def suggested_root():
    """Folder of the project open in Metashape, else the folder of this MCP; None if missing."""
    candidate = None
    try:
        import Metashape
        document = Metashape.app.document
        if document is not None and document.path:
            candidate = Path(document.path).resolve().parent
    except Exception:  # outside Metashape or no document yet
        candidate = None
    if candidate is None:
        candidate = Path(__file__).resolve().parent
    return candidate if candidate.is_dir() else None


def root_covers(roots, path):
    """True when path equals or lies inside one of the allowed roots."""
    path = Path(path).resolve()
    for root in roots:
        try:
            path.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def start_sdk_process(process, script, arguments):
    uv = shutil.which("uv")
    if not uv and sys.platform == "darwin":
        uv = next((p for p in ("/opt/homebrew/bin/uv", "/usr/local/bin/uv")
                   if os.access(p, os.X_OK)), None)
    if not uv:
        raise RuntimeError("Для запуска MCP требуется программа uv.")
    env = QtCore.QProcessEnvironment.systemEnvironment()
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.remove(key)
    process.setProcessEnvironment(env)
    process.start(uv, ["run", "--script", str(script), *arguments])


class ServiceMonitor(QtWidgets.QWidget):
    def __init__(self, url):
        super().__init__()
        self.url = url
        self.last_result = None
        self.pending = False
        self.closing = False
        self.worker_diagnostic = ""
        self.log_path = None
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.setWindowTitle("Metashape MCP — состояние сервиса")
        self.resize(720, 760)
        self.setMinimumSize(560, 620)
        self.setStyleSheet("""
            QWidget { background: #F5F6F8; color: #202C39; font-size: 14px; }
            QLabel#title { font-size: 22px; font-weight: 600; }
            QLabel#state { font-size: 20px; font-weight: 600; }
            QLabel#muted { color: #536477; font-size: 13px; }
            QLabel#section { font-size: 15px; font-weight: 600; }
            QLabel#warning { color: #8A4B08; font-size: 12px; }
            QPlainTextEdit { background: white; border: 1px solid #CBD3DA;
                             border-radius: 6px; padding: 8px; }
            QPushButton { background: white; border: 1px solid #B9C4CE;
                          border-radius: 5px; padding: 6px 12px; }
            QPushButton:pressed { background: #E4EBF1; }
            QPushButton:disabled { color: #687583; background: #EEF1F3; }
        """)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(8)
        title = QtWidgets.QLabel("Metashape MCP")
        title.setObjectName("title")
        layout.addWidget(title)
        endpoint = QtWidgets.QLabel(url)
        endpoint.setTextFormat(QtCore.Qt.PlainText)
        endpoint.setObjectName("muted")
        endpoint.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(endpoint)
        layout.addSpacing(12)
        roots_heading = QtWidgets.QLabel("Разрешённые рабочие папки")
        roots_heading.setObjectName("section")
        layout.addWidget(roots_heading)
        self.allowed_roots = QtWidgets.QListWidget()
        self.allowed_roots.setMaximumHeight(92)
        self.allowed_roots.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.allowed_roots)
        roots_buttons = QtWidgets.QHBoxLayout()
        self.allow_project_root = QtWidgets.QPushButton("Разрешить папку проекта")
        self.allow_project_root.setVisible(False)
        self.add_root = QtWidgets.QPushButton("Добавить папку…")
        self.remove_root = QtWidgets.QPushButton("Убрать выбранные")
        roots_buttons.addWidget(self.allow_project_root)
        roots_buttons.addWidget(self.add_root)
        roots_buttons.addWidget(self.remove_root)
        roots_buttons.addStretch()
        layout.addLayout(roots_buttons)
        roots_note = QtWidgets.QLabel(
            "Открытие проектов, добавление фотографий и экспорт разрешены только внутри этих папок."
        )
        roots_note.setObjectName("muted")
        roots_note.setWordWrap(True)
        layout.addWidget(roots_note)
        layout.addSpacing(12)
        script_heading = QtWidgets.QLabel("Пользовательские скрипты")
        script_heading.setObjectName("section")
        layout.addWidget(script_heading)
        self.allow_scripts = QtWidgets.QCheckBox(
            "Разрешить запуск пользовательских Python-скриптов через MCP"
        )
        layout.addWidget(self.allow_scripts)
        script_directory_row = QtWidgets.QHBoxLayout()
        self.script_directory = QtWidgets.QLineEdit()
        self.script_directory.setReadOnly(True)
        self.script_directory.setPlaceholderText("Папка не выбрана")
        script_directory_row.addWidget(self.script_directory, 1)
        self.choose_script_directory = QtWidgets.QPushButton("Выбрать папку…")
        script_directory_row.addWidget(self.choose_script_directory)
        layout.addLayout(script_directory_row)
        script_warning = QtWidgets.QLabel(
            "Скрипты выполняются внутри Metashape с правами текущего пользователя. "
            "Сохранение защищает проект, но не отменяет изменения внешних файлов или сети."
        )
        script_warning.setObjectName("warning")
        script_warning.setWordWrap(True)
        layout.addWidget(script_warning)
        self.allow_reload = QtWidgets.QCheckBox(
            "Разрешить перезагрузку сервиса через MCP (для разработки)")
        layout.addWidget(self.allow_reload)
        reload_warning = QtWidgets.QLabel(
            "Команда reload_service загружает текущий код из папки MCP внутрь Metashape. "
            "Кто может менять файлы в этой папке и вызывать MCP, сможет выполнить свой код. "
            "Включайте только на время разработки.")
        reload_warning.setObjectName("warning")
        reload_warning.setWordWrap(True)
        layout.addWidget(reload_warning)
        layout.addSpacing(12)
        self.state = QtWidgets.QLabel()
        self.state.setObjectName("state")
        self.detail = QtWidgets.QLabel()
        self.detail.setTextFormat(QtCore.Qt.PlainText)
        self.detail.setWordWrap(True)
        layout.addWidget(self.state)
        layout.addWidget(self.detail)
        layout.addSpacing(10)
        heading = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel("Журнал подключения")
        label.setObjectName("section")
        heading.addWidget(label)
        heading.addStretch()
        self.checked = QtWidgets.QLabel()
        self.checked.setObjectName("muted")
        heading.addWidget(self.checked)
        layout.addLayout(heading)
        self.history = QtWidgets.QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.history.document().setMaximumBlockCount(400)
        layout.addWidget(self.history, 1)
        buttons = QtWidgets.QHBoxLayout()
        self.open_log = QtWidgets.QPushButton("Открыть файл журнала")
        self.open_log.clicked.connect(self.show_log)
        buttons.addWidget(self.open_log)
        buttons.addStretch()
        self.refresh = QtWidgets.QPushButton("Проверить сейчас")
        self.refresh.clicked.connect(self.check)
        buttons.addWidget(self.refresh)
        self.reload_service = QtWidgets.QPushButton("Перезагрузить сервис")
        self.reload_service.setToolTip(
            "Загрузить текущую версию кода MCP без перезапуска Metashape. "
            "Недоступно, пока выполняется задание.")
        self.reload_service.clicked.connect(self.request_reload)
        buttons.addWidget(self.reload_service)
        layout.addLayout(buttons)

        self.worker = QtCore.QProcess(self)
        self.worker.readyReadStandardOutput.connect(self.read_result)
        self.worker.readyReadStandardError.connect(self.read_diagnostic)
        self.worker.errorOccurred.connect(self.worker_error)
        self.worker.finished.connect(self.worker_finished)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.check)
        self.settings_path = None
        self.settings = None
        self.prepare_settings()
        self.allow_project_root.clicked.connect(self.allow_suggested_root)
        self.timer.timeout.connect(self.refresh_suggested_root)
        self.add_root.clicked.connect(self.select_allowed_root)
        self.remove_root.clicked.connect(self.remove_allowed_roots)
        self.allow_scripts.toggled.connect(self.change_script_permission)
        self.allow_reload.toggled.connect(self.change_reload_permission)
        self.choose_script_directory.clicked.connect(self.select_script_directory)
        self.prepare_log()
        self.display("checking", "Подключение к MCP-серверу…")
        self.timer.start()
        QtCore.QTimer.singleShot(0, self.check)

    def prepare_settings(self):
        data = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.GenericDataLocation)
        self.settings_path = Path(data) / "Metashape MCP" / "settings.json"
        self.settings = load_settings(self.settings_path)
        self.allow_scripts.setChecked(bool(self.settings["allow_user_scripts"]))
        self.allow_reload.blockSignals(True)
        self.allow_reload.setChecked(bool(self.settings["allow_mcp_reload"]))
        self.allow_reload.blockSignals(False)
        self.script_directory.setText(self.settings["user_scripts_directory"])
        self.refresh_allowed_roots()

    def refresh_allowed_roots(self):
        self.allowed_roots.clear()
        self.allowed_roots.addItems(self.settings["allowed_roots"])
        self.refresh_suggested_root()

    def refresh_suggested_root(self):
        """Offer the open project's folder (or this MCP folder) for one-click approval."""
        suggested = suggested_root()
        covered = suggested is None or root_covers(self.settings["allowed_roots"], suggested)
        self.allow_project_root.setVisible(not covered)
        if not covered:
            self.allow_project_root.setText(f"Разрешить {suggested.name or suggested}")
            self.allow_project_root.setToolTip(str(suggested))

    def allow_suggested_root(self):
        suggested = suggested_root()
        if suggested is not None:
            self.add_allowed_root(str(suggested))

    def select_allowed_root(self):
        suggested = suggested_root()
        start = (str(suggested) if suggested is not None else
                 self.settings["allowed_roots"][-1] if self.settings["allowed_roots"] else str(Path.home()))
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "Разрешённая рабочая папка", start)
        if selected:
            self.add_allowed_root(selected)

    def add_allowed_root(self, selected):
        resolved = str(Path(selected).resolve())
        if resolved not in self.settings["allowed_roots"]:
            self.settings["allowed_roots"].append(resolved)
            save_settings(self.settings_path, self.settings)
            self.refresh_allowed_roots()
            self.record(f"Добавлена разрешённая рабочая папка: {resolved}")

    def remove_allowed_roots(self):
        selected = {item.text() for item in self.allowed_roots.selectedItems()}
        if not selected:
            return
        self.settings["allowed_roots"] = [
            value for value in self.settings["allowed_roots"] if value not in selected
        ]
        save_settings(self.settings_path, self.settings)
        self.refresh_allowed_roots()
        self.record("Удалены выбранные разрешённые рабочие папки.")

    def change_script_permission(self, enabled):
        if enabled:
            answer = QtWidgets.QMessageBox.warning(
                self, "Разрешить пользовательские скрипты?",
                "Скрипты получают доступ к Metashape и файлам с правами текущего пользователя. "
                "Изменения вне проекта могут быть необратимыми.\n\nПродолжить?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                self.allow_scripts.blockSignals(True)
                self.allow_scripts.setChecked(False)
                self.allow_scripts.blockSignals(False)
                return
        self.settings["allow_user_scripts"] = bool(enabled)
        save_settings(self.settings_path, self.settings)
        self.record("Запуск пользовательских скриптов " + ("разрешён." if enabled else "запрещён."))

    def change_reload_permission(self, enabled):
        if enabled:
            answer = QtWidgets.QMessageBox.warning(
                self, "Разрешить перезагрузку через MCP?",
                "MCP-клиент сможет сам перезагружать сервис, то есть выполнять внутри Metashape "
                "текущий код из папки MCP. Это нужно только при разработке.\n\nПродолжить?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                self.allow_reload.blockSignals(True)
                self.allow_reload.setChecked(False)
                self.allow_reload.blockSignals(False)
                return
        self.settings["allow_mcp_reload"] = bool(enabled)
        save_settings(self.settings_path, self.settings)
        self.record("Перезагрузка сервиса через MCP " + ("разрешена." if enabled else "запрещена."))

    def select_script_directory(self):
        selected = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Папка пользовательских скриптов",
            self.settings["user_scripts_directory"] or str(Path.home()),
        )
        if not selected:
            return
        resolved = str(Path(selected).resolve())
        if resolved != self.settings["user_scripts_directory"]:
            self.settings["user_scripts_directory"] = resolved
            self.settings["approved_script_hashes"] = {}
            save_settings(self.settings_path, self.settings)
            self.script_directory.setText(resolved)
            self.record("Выбрана папка пользовательских скриптов; прежние разрешения сброшены.")

    def prepare_log(self):
        data = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.GenericDataLocation)
        folder = Path(data) / "Metashape MCP" / "logs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = folder / f"monitor-{stamp}-{os.getpid()}.log"
            path.touch(exist_ok=False)
            self.log_path = path
            self.open_log.setToolTip(str(path))
            self.record(f"Адрес сервиса: {self.url}")
        except OSError as error:
            self.open_log.setEnabled(False)
            self.history.appendPlainText(f"Не удалось создать журнал: {error}")

    def record(self, message):
        line = f"{datetime.now().astimezone().isoformat(timespec='seconds')}  {message}"
        self.history.appendPlainText(line)
        if self.log_path:
            try:
                with self.log_path.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
            except OSError as error:
                self.history.appendPlainText(f"Ошибка записи журнала: {error}")

    def display(self, state, detail):
        if state != "checking":
            self.checked.setText("Проверено в " + datetime.now().strftime("%H:%M:%S"))
        if self.last_result == (state, detail):
            return
        self.last_result = state, detail
        label, color = STATES[state]
        self.state.setText("●  " + label)
        self.state.setStyleSheet(f"color: {color}")
        self.detail.setText(detail)
        self.record(f"{label}. {detail}")

    def show_log(self):
        if self.log_path and not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.log_path))):
            self.record(f"Откройте файл журнала: {self.log_path}")

    def check(self):
        if self.pending or self.closing:
            return
        if self.worker.state() == QtCore.QProcess.NotRunning:
            try:
                endpoint = urlsplit(self.url)
                if endpoint.scheme not in ("http", "https") or not endpoint.hostname:
                    raise ValueError
                if endpoint.port == 0:
                    raise ValueError
            except ValueError:
                self.display("error", "Проверьте HTTP-адрес в METASHAPE_MCP_URL.")
                return
            self.worker_diagnostic = ""
            try:
                start_sdk_process(self.worker, PROBE, [self.url])
            except RuntimeError as error:
                self.display("error", str(error))
                return
        self.pending = True
        self.refresh.setEnabled(False)
        self.worker.write(b"check\n")

    def read_result(self):
        while self.worker.canReadLine():
            raw = bytes(self.worker.readLine())
            try:
                reply = json.loads(raw)
                self.display(reply["state"], reply["detail"])
            except (KeyError, ValueError, TypeError):
                self.display("error", "Получен некорректный ответ от процесса проверки.")
            self.pending = False
            self.refresh.setEnabled(True)

    def read_diagnostic(self):
        data = bytes(self.worker.readAllStandardError()).decode("utf-8", errors="replace")
        self.worker_diagnostic = (self.worker_diagnostic + data)[-2000:]

    def worker_error(self, error):
        if not self.closing and error == QtCore.QProcess.FailedToStart:
            self.display("error", self.worker.errorString())
            self.pending = False
            self.refresh.setEnabled(True)

    def worker_finished(self, *_):
        if self.closing:
            QtCore.QTimer.singleShot(0, self.close)
        else:
            self.read_diagnostic()
            self.display("error", self.worker_diagnostic.strip() or "Процесс проверки завершился.")
            self.pending = False
            self.refresh.setEnabled(True)

    def request_reload(self):
        """Re-run the launcher after this click returns; it replaces the service and this window."""
        launcher = Path(__file__).resolve().with_name("start_metashape_mcp.py")
        self.record("Запрошена перезагрузка сервиса из окна мониторинга.")

        def run():
            try:
                runpy.run_path(str(launcher), run_name="__main__")
            except Exception as error:
                QtWidgets.QMessageBox.warning(
                    None, "Metashape MCP", f"Сервис не перезагружен: {error}")

        QtCore.QTimer.singleShot(0, run)

    def closeEvent(self, event):
        if not self.closing:
            self.closing = True
            self.timer.stop()
            self.record("Окно мониторинга закрыто.")
            self.worker.closeWriteChannel()
        if self.worker.state() != QtCore.QProcess.NotRunning:
            event.ignore()
            self.refresh.setEnabled(False)
            QtCore.QTimer.singleShot(1500, self.worker.kill)
            return
        super().closeEvent(event)


def show_service_monitor():
    app = QtWidgets.QApplication.instance()
    window = getattr(app, "_metashape_mcp_monitor", None)
    if window is None or window.closing:
        window = ServiceMonitor(os.environ.get("METASHAPE_MCP_URL", DEFAULT_URL))
        app._metashape_mcp_monitor = window
    window.show()
    window.raise_()
    window.activateWindow()
    return window


if __name__ == "__main__":
    existing_app = QtWidgets.QApplication.instance()
    app = existing_app or QtWidgets.QApplication(sys.argv)
    show_service_monitor()
    if existing_app is None:
        if hasattr(app, "exec"):
            app.exec()
        else:
            app.exec_()
