from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import customtkinter as ctk
import psutil
from tkinter import messagebox


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "services.json"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
LOG_LIMIT = 3000

COLORS = {
    "background": "#0B1018",
    "panel": "#111925",
    "panel_raised": "#172231",
    "border": "#243246",
    "text": "#E7EDF5",
    "muted": "#91A0B4",
    "green": "#31C48D",
    "red": "#F87171",
    "yellow": "#FBBF24",
    "blue": "#77A9FF",
}

STATUS_COLORS = {
    "Running": ("#31C48D", "#15392F"),
    "Stopped": ("#F87171", "#40242B"),
    "Starting": ("#FBBF24", "#44391E"),
    "Stopping": ("#FBBF24", "#44391E"),
}


def load_config(path: Path = CONFIG_PATH) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    return validate_services(raw)


def validate_services(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("services"), list):
        raise ValueError('The config must be a JSON object containing a "services" array.')
    if not raw["services"]:
        raise ValueError("Add at least one service to the services array.")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw["services"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Service #{index} must be an object.")
        service_id = item.get("id")
        name = item.get("name")
        command = item.get("command")
        cwd = item.get("cwd", ".")
        port = item.get("port", "")
        url = item.get("url", "")
        env = item.get("env", {})
        windows_command = item.get("windows_command")
        stop_command = item.get("stop_command")

        if not isinstance(service_id, str) or not ID_PATTERN.fullmatch(service_id):
            raise ValueError(f'Service #{index} has an invalid id. Use lowercase letters, numbers, "-" or "_".')
        if service_id in seen_ids:
            raise ValueError(f'Duplicate service id: "{service_id}".')
        seen_ids.add(service_id)
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f'Service "{service_id}" needs a name.')
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError(f'Service "{service_id}" command must be a non-empty array of strings.')
        if not isinstance(cwd, str) or not cwd.strip():
            raise ValueError(f'Service "{service_id}" cwd must be a non-empty path string.')
        if not isinstance(port, (str, int)):
            raise ValueError(f'Service "{service_id}" port must be a string or number.')
        if not isinstance(url, str):
            raise ValueError(f'Service "{service_id}" url must be a string.')
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError(f'Service "{service_id}" env must map strings to strings.')
        if windows_command is not None and (
            not isinstance(windows_command, list)
            or not windows_command
            or not all(isinstance(part, str) and part for part in windows_command)
        ):
            raise ValueError(f'Service "{service_id}" windows_command must be a non-empty array of strings.')
        if stop_command is not None and (
            not isinstance(stop_command, list)
            or not stop_command
            or not all(isinstance(part, str) and part for part in stop_command)
        ):
            raise ValueError(f'Service "{service_id}" stop_command must be a non-empty array of strings.')

        normalized.append({
            **item,
            "id": service_id,
            "name": name.strip(),
            "description": str(item.get("description", "")).strip(),
            "command": command,
            "windows_command": windows_command,
            "stop_command": stop_command,
            "cwd": cwd,
            "port": str(port).strip(),
            "url": url.strip(),
            "env": env,
        })
    return normalized


def find_docker_cli() -> str | None:
    located = shutil.which("docker")
    if located:
        return located
    candidates = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe")
    if program_files:
        candidates.append(Path(program_files) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe")
    return next((str(path) for path in candidates if path.is_file()), None)


def expand_command_parts(parts: list[str]) -> list[str]:
    docker_cli = find_docker_cli()
    expanded = []
    for part in parts:
        if part.lower() in {"docker", "docker.exe"} and docker_cli:
            expanded.append(docker_cli)
        else:
            expanded.append(part.replace("{python}", sys.executable))
    return expanded


def add_docker_cli_to_path(environment: dict[str, str]) -> None:
    docker_cli = find_docker_cli()
    if not docker_cli:
        return
    docker_bin = str(Path(docker_cli).parent)
    path_entries = environment.get("PATH", "").split(os.pathsep)
    normalized_entries = {os.path.normcase(os.path.normpath(entry)) for entry in path_entries if entry}
    if os.path.normcase(os.path.normpath(docker_bin)) not in normalized_entries:
        environment["PATH"] = docker_bin + (os.pathsep + environment["PATH"] if environment.get("PATH") else "")


def service_command(config: dict[str, Any]) -> list[str]:
    if os.name == "nt" and config.get("windows_command"):
        command = list(config["windows_command"])
    else:
        command = list(config["command"])
    return expand_command_parts(command)


class ServiceManager:
    """Owns every child process and reports UI-safe events through a queue."""

    def __init__(self, services: list[dict[str, Any]]) -> None:
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = threading.RLock()
        self._services = {service["id"]: service for service in services}
        self._runtime = {
            service_id: {
                "status": "Stopped",
                "process": None,
                "pid": None,
                "generation": 0,
                "stop_requested": False,
                "return_code": None,
                "process_created": None,
            }
            for service_id in self._services
        }

    @property
    def services(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(service) for service in self._services.values()]

    def update_services(self, services: list[dict[str, Any]]) -> None:
        with self._lock:
            if any(runtime["status"] != "Stopped" for runtime in self._runtime.values()):
                raise RuntimeError("Stop all services before applying a new configuration.")
            self._services = {service["id"]: service for service in services}
            self._runtime = {
                service_id: {
                    "status": "Stopped",
                    "process": None,
                    "pid": None,
                    "generation": 0,
                    "stop_requested": False,
                    "return_code": None,
                    "process_created": None,
                }
                for service_id in self._services
            }

    def status(self, service_id: str) -> dict[str, Any]:
        with self._lock:
            runtime = self._runtime[service_id]
            return {
                "status": runtime["status"],
                "pid": runtime["pid"],
                "return_code": runtime["return_code"],
            }

    def has_active_services(self) -> bool:
        with self._lock:
            return any(runtime["status"] != "Stopped" for runtime in self._runtime.values())

    def start_all(self) -> None:
        for service_id in self._services:
            self.start_service(service_id)

    def stop_all(self) -> None:
        for service_id in list(self._services):
            self.stop_service(service_id)

    def start_service(self, service_id: str) -> None:
        with self._lock:
            runtime = self._runtime[service_id]
            if runtime["status"] != "Stopped":
                return
            runtime["generation"] += 1
            generation = runtime["generation"]
            runtime["status"] = "Starting"
            runtime["stop_requested"] = False
            runtime["return_code"] = None
            service = dict(self._services[service_id])
        self._status_event(service_id)
        threading.Thread(
            target=self._spawn_service,
            args=(service_id, generation, service),
            daemon=True,
            name=f"start-{service_id}",
        ).start()

    def stop_service(self, service_id: str) -> None:
        with self._lock:
            runtime = self._runtime[service_id]
            if runtime["status"] in {"Stopped", "Stopping"}:
                return
            runtime["stop_requested"] = True
            runtime["status"] = "Stopping"
            process = runtime["process"]
            generation = runtime["generation"]
            created = runtime["process_created"]
            service = dict(self._services[service_id])
        self._status_event(service_id)
        if process is not None:
            threading.Thread(
                target=self._stop_service_worker,
                args=(service_id, generation, process, created, service),
                daemon=True,
                name=f"stop-{service_id}",
            ).start()

    def restart_service(self, service_id: str) -> None:
        self.stop_service(service_id)

        def restart_after_stop() -> None:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.status(service_id)["status"] == "Stopped":
                    self.start_service(service_id)
                    return
                time.sleep(0.1)
            self._log(service_id, "Restart timed out while waiting for the process to stop.", "system")

        threading.Thread(target=restart_after_stop, daemon=True, name=f"restart-{service_id}").start()

    def shutdown(self, timeout: float = 8.0) -> None:
        self.stop_all()
        deadline = time.monotonic() + timeout
        while self.has_active_services() and time.monotonic() < deadline:
            time.sleep(0.1)

    def _spawn_service(self, service_id: str, generation: int, config: dict[str, Any]) -> None:
        command = service_command(config)
        cwd = Path(os.path.expandvars(config["cwd"]))
        if not cwd.is_absolute():
            cwd = (APP_DIR / cwd).resolve()
        if not cwd.is_dir():
            raise_error = FileNotFoundError(f"Working directory does not exist: {cwd}")
            self._start_failed(service_id, generation, raise_error)
            return

        command = [os.path.expandvars(part) for part in command]
        environment = os.environ.copy()
        environment.update({key: os.path.expandvars(value) for key, value in config.get("env", {}).items()})
        add_docker_cli_to_path(environment)
        creation_flags = 0
        popen_options: dict[str, Any] = {}
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creation_flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_options["start_new_session"] = True

        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
                **popen_options,
            )
        except Exception as exc:
            self._start_failed(service_id, generation, exc)
            return

        process_created = None
        try:
            process_created = psutil.Process(process.pid).create_time()
        except (psutil.Error, OSError):
            pass

        with self._lock:
            runtime = self._runtime.get(service_id)
            if runtime is None or runtime["generation"] != generation:
                orphaned = True
                stop_requested = True
            else:
                orphaned = False
                stop_requested = runtime["stop_requested"]
                runtime["process"] = process
                runtime["pid"] = process.pid
                runtime["process_created"] = process_created
                runtime["status"] = "Stopping" if stop_requested else "Running"

        if orphaned:
            try:
                process.terminate()
            except OSError:
                pass
            return

        self._status_event(service_id)
        self._log(service_id, f"Started PID {process.pid}: {subprocess.list2cmdline(command)}", "system")

        for stream_name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            threading.Thread(
                target=self._read_stream,
                args=(service_id, stream_name, stream),
                daemon=True,
                name=f"log-{service_id}-{stream_name}",
            ).start()

        threading.Thread(
            target=self._watch_process,
            args=(service_id, generation, process),
            daemon=True,
            name=f"watch-{service_id}",
        ).start()

        if stop_requested:
            threading.Thread(
                target=self._stop_service_worker,
                args=(service_id, generation, process, process_created, config),
                daemon=True,
                name=f"stop-{service_id}",
            ).start()

    def _stop_service_worker(
        self,
        service_id: str,
        generation: int,
        process: subprocess.Popen[str],
        process_created: float | None,
        config: dict[str, Any],
    ) -> None:
        stop_command = config.get("stop_command")
        if stop_command:
            command = [os.path.expandvars(part) for part in expand_command_parts(stop_command)]
            cwd = Path(os.path.expandvars(config["cwd"]))
            if not cwd.is_absolute():
                cwd = (APP_DIR / cwd).resolve()
            environment = os.environ.copy()
            environment.update({key: os.path.expandvars(value) for key, value in config.get("env", {}).items()})
            add_docker_cli_to_path(environment)
            try:
                result = subprocess.run(
                    command,
                    cwd=str(cwd),
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=25,
                    creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
                )
                for line in (result.stdout or "").splitlines():
                    self._log(service_id, line, "stdout")
                for line in (result.stderr or "").splitlines():
                    self._log(service_id, line, "stderr")
                if result.returncode:
                    self._log(service_id, f"Stop command exited with code {result.returncode}.", "system")
            except Exception as error:
                self._log(service_id, f"Stop command failed: {error}", "stderr")
        self._terminate_tree(service_id, generation, process, process_created)

    def _start_failed(self, service_id: str, generation: int, error: Exception) -> None:
        self._log(service_id, f"Could not start service: {error}", "stderr")
        with self._lock:
            runtime = self._runtime.get(service_id)
            if runtime is None or runtime["generation"] != generation:
                return
            runtime["status"] = "Stopped"
            runtime["process"] = None
            runtime["pid"] = None
            runtime["process_created"] = None
            runtime["stop_requested"] = False
            runtime["return_code"] = None
        self._status_event(service_id)

    def _read_stream(self, service_id: str, stream_name: str, stream: Any) -> None:
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                self._log(service_id, line.rstrip("\r\n"), stream_name)
        except (OSError, ValueError) as exc:
            self._log(service_id, f"Log stream closed: {exc}", "system")
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _watch_process(self, service_id: str, generation: int, process: subprocess.Popen[str]) -> None:
        return_code = process.wait()
        with self._lock:
            runtime = self._runtime.get(service_id)
            if runtime is None or runtime["generation"] != generation:
                return
            runtime["status"] = "Stopped"
            runtime["process"] = None
            runtime["pid"] = None
            runtime["process_created"] = None
            runtime["stop_requested"] = False
            runtime["return_code"] = return_code
        if return_code == 0:
            self._log(service_id, "Process exited successfully.", "system")
        else:
            self._log(service_id, f"Process exited with code {return_code}.", "system")
        self._status_event(service_id)

    def _terminate_tree(
        self,
        service_id: str,
        generation: int,
        process: subprocess.Popen[str],
        expected_created: float | None,
    ) -> None:
        try:
            root = psutil.Process(process.pid)
            if expected_created is not None and abs(root.create_time() - expected_created) > 0.01:
                self._log(service_id, "Skipped termination because the process id has been reused.", "system")
                return
            children = root.children(recursive=True)
            victims = list(reversed(children)) + [root]
            for victim in victims:
                try:
                    victim.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            _, alive = psutil.wait_procs(victims, timeout=5)
            for victim in alive:
                try:
                    victim.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if alive:
                psutil.wait_procs(alive, timeout=2)
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, OSError) as exc:
            self._log(service_id, f"Process-tree stop fallback: {exc}", "system")

        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            except OSError as exc:
                self._log(service_id, f"Could not finish stopping process: {exc}", "stderr")

    def _status_event(self, service_id: str) -> None:
        with self._lock:
            runtime = self._runtime.get(service_id)
            if runtime is None:
                return
            event = {
                "type": "status",
                "service_id": service_id,
                "status": runtime["status"],
                "pid": runtime["pid"],
                "return_code": runtime["return_code"],
            }
        self.events.put(event)

    def _log(self, service_id: str, message: str, stream: str) -> None:
        service = self._services.get(service_id, {})
        self.events.put({
            "type": "log",
            "service_id": service_id,
            "service_name": service.get("name", service_id),
            "stream": stream,
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": message,
        })


class ControlPanel(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.title("StackPilot | Local Control Panel")
        self.geometry("1440x920")
        self.minsize(1120, 760)
        self.configure(fg_color=COLORS["background"])

        try:
            self.services = load_config()
        except ValueError as exc:
            messagebox.showerror("Configuration error", f"{exc}\n\nEdit {CONFIG_PATH} and relaunch.", parent=self)
            self.destroy()
            raise

        self.manager = ServiceManager(self.services)
        self.log_history: deque[dict[str, str]] = deque(maxlen=LOG_LIMIT)
        self.cards: dict[str, dict[str, Any]] = {}
        self.settings_window: ctk.CTkToplevel | None = None
        self._opening_frontend = False
        self.log_filter_value = "All Services"
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(80, self._drain_events)

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        shell = ctk.CTkFrame(self, fg_color=COLORS["background"], corner_radius=0)
        shell.grid(row=0, column=0, sticky="nsew")
        shell.grid_columnconfigure(1, weight=1)
        shell.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(shell, width=236, fg_color="#0E1520", corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)

        brand = ctk.CTkFrame(sidebar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=22, pady=(26, 30))
        ctk.CTkLabel(brand, text="✦", text_color=COLORS["blue"], font=ctk.CTkFont(size=22, weight="bold")).pack(side="left")
        ctk.CTkLabel(brand, text="StackPilot", text_color=COLORS["text"], font=ctk.CTkFont(size=18, weight="bold")).pack(side="left", padx=(9, 0))
        ctk.CTkLabel(brand, text="LOCAL DEV", text_color=COLORS["muted"], font=ctk.CTkFont(size=9, weight="bold")).pack(side="left", padx=(8, 0), pady=(5, 0))

        ctk.CTkLabel(sidebar, text="WORKSPACE", anchor="w", text_color="#66778E", font=ctk.CTkFont(size=10, weight="bold")).grid(row=1, column=0, sticky="ew", padx=23, pady=(0, 10))
        project_card = ctk.CTkFrame(sidebar, fg_color=COLORS["panel"], corner_radius=12, border_width=1, border_color=COLORS["border"])
        project_card.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 26))
        project_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(project_card, text="▣  To-do List", anchor="w", text_color=COLORS["text"], font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, sticky="ew", padx=13, pady=(12, 4))
        ctk.CTkLabel(project_card, text="Local service workspace", anchor="w", text_color=COLORS["muted"], font=ctk.CTkFont(size=10)).grid(row=1, column=0, sticky="ew", padx=13, pady=(0, 12))

        ctk.CTkLabel(sidebar, text="SERVICES", anchor="w", text_color="#66778E", font=ctk.CTkFont(size=10, weight="bold")).grid(row=3, column=0, sticky="ew", padx=23, pady=(0, 10))
        self.sidebar_services = ctk.CTkFrame(sidebar, fg_color="transparent")
        self.sidebar_services.grid(row=4, column=0, sticky="new", padx=12)

        sidebar_footer = ctk.CTkFrame(sidebar, fg_color="transparent")
        sidebar_footer.grid(row=5, column=0, sticky="sew", padx=18, pady=18)
        sidebar_footer.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(sidebar_footer, text="Processes stay attached to this panel.\nClosing the app stops managed services.", justify="left", anchor="w", text_color="#718198", font=ctk.CTkFont(size=10), wraplength=190).grid(row=0, column=0, sticky="ew")

        content = ctk.CTkFrame(shell, fg_color=COLORS["background"], corner_radius=0)
        content.grid(row=0, column=1, sticky="nsew", padx=(26, 28), pady=(24, 22))
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(2, weight=1)
        content.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(content, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 22))
        header.grid_columnconfigure(0, weight=1)
        title_stack = ctk.CTkFrame(header, fg_color="transparent")
        title_stack.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(title_stack, text="Development workspace", text_color=COLORS["text"], font=ctk.CTkFont(size=27, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(title_stack, text="Start, monitor, and stop your local stack from one place.", text_color=COLORS["muted"], font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(5, 0))

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.grid(row=0, column=1, sticky="e")
        self.settings_button = ctk.CTkButton(actions, text="⚙  Settings", width=110, height=39, fg_color=COLORS["panel_raised"], hover_color="#243449", text_color=COLORS["text"], command=self._open_settings)
        self.settings_button.pack(side="left", padx=(0, 9))
        self.stop_all_button = ctk.CTkButton(actions, text="■  Stop All", width=112, height=39, fg_color="#48252D", hover_color="#63303A", text_color="#FFDDE1", command=self.manager.stop_all)
        self.stop_all_button.pack(side="left", padx=(0, 9))
        self.run_all_button = ctk.CTkButton(actions, text="▶  Run All", width=120, height=39, fg_color="#16835F", hover_color="#1C9C70", text_color="#FFFFFF", font=ctk.CTkFont(size=12, weight="bold"), command=self.manager.start_all)
        self.run_all_button.pack(side="left")
        self.open_button = ctk.CTkButton(actions, text="↗  Open To-do List", width=150, height=39, fg_color="#315FA7", hover_color="#3B73C6", text_color="#FFFFFF", font=ctk.CTkFont(size=11, weight="bold"), command=self._open_todo_list)
        self.open_button.pack(side="left", padx=(9, 0))

        section_header = ctk.CTkFrame(content, fg_color="transparent")
        section_header.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        section_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(section_header, text="Services", text_color=COLORS["text"], font=ctk.CTkFont(size=16, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(section_header, text="Process status updates live", text_color=COLORS["muted"], font=ctk.CTkFont(size=10)).grid(row=0, column=1, sticky="e")

        self.service_grid = ctk.CTkFrame(content, fg_color="transparent")
        self.service_grid.grid(row=2, column=0, sticky="nsew", pady=(0, 19))
        self._rebuild_service_cards()

        log_panel = ctk.CTkFrame(content, fg_color=COLORS["panel"], corner_radius=16, border_width=1, border_color=COLORS["border"])
        log_panel.grid(row=3, column=0, sticky="nsew")
        log_panel.grid_columnconfigure(0, weight=1)
        log_panel.grid_rowconfigure(1, weight=1)

        log_header = ctk.CTkFrame(log_panel, fg_color="transparent")
        log_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(13, 9))
        log_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(log_header, text="Live output", text_color=COLORS["text"], font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, sticky="w")
        self.clear_logs_button = ctk.CTkButton(log_header, text="Clear", width=56, height=27, fg_color="transparent", hover_color="#243246", text_color=COLORS["muted"], command=self._clear_logs)
        self.clear_logs_button.grid(row=0, column=1, padx=(0, 8))
        self.log_filter = ctk.CTkOptionMenu(log_header, values=self._log_filter_values(), width=150, height=29, fg_color=COLORS["panel_raised"], button_color="#243246", button_hover_color="#344761", command=self._change_log_filter)
        self.log_filter.set("All Services")
        self.log_filter.grid(row=0, column=2)

        self.console = ctk.CTkTextbox(log_panel, fg_color="#080D14", text_color="#CBD5E1", font=ctk.CTkFont(family="Cascadia Code", size=11), wrap="none", corner_radius=10, border_width=1, border_color="#1B2838")
        self.console.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.console.configure(state="disabled")

    def _rebuild_service_cards(self) -> None:
        for child in self.service_grid.winfo_children():
            child.destroy()
        for child in self.sidebar_services.winfo_children():
            child.destroy()
        self.cards.clear()

        for column in range(2):
            self.service_grid.grid_columnconfigure(column, weight=1, uniform="service-card")
        for row in range((len(self.services) + 1) // 2):
            self.service_grid.grid_rowconfigure(row, weight=1, uniform="service-row")

        for index, service in enumerate(self.services):
            row, column = divmod(index, 2)
            card = ctk.CTkFrame(self.service_grid, fg_color=COLORS["panel"], corner_radius=16, border_width=1, border_color=COLORS["border"])
            card.grid(row=row, column=column, sticky="nsew", padx=(0 if column == 0 else 9, 9 if column == 0 else 0), pady=(0, 10))
            card.grid_columnconfigure(0, weight=1)

            top = ctk.CTkFrame(card, fg_color="transparent")
            top.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 7))
            top.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(top, text=service["name"], text_color=COLORS["text"], font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, sticky="w")
            badge = ctk.CTkLabel(top, text="Stopped", text_color=STATUS_COLORS["Stopped"][0], fg_color=STATUS_COLORS["Stopped"][1], corner_radius=9, font=ctk.CTkFont(size=10, weight="bold"), padx=10, pady=4)
            badge.grid(row=0, column=1, sticky="e")

            description = service.get("description") or "Local service"
            ctk.CTkLabel(card, text=description, text_color=COLORS["muted"], font=ctk.CTkFont(size=10), anchor="w", justify="left", wraplength=450).grid(row=1, column=0, sticky="ew", padx=16)
            port = f"Port {service['port']}" if service.get("port") else "Port not specified"
            pid_label = ctk.CTkLabel(card, text=f"{port}   ·   PID —", text_color="#7E8DA2", font=ctk.CTkFont(size=10), anchor="w")
            pid_label.grid(row=2, column=0, sticky="ew", padx=16, pady=(7, 2))
            command_preview = " ".join(service_command(service))
            ctk.CTkLabel(card, text=command_preview, text_color="#8293AA", font=ctk.CTkFont(family="Cascadia Code", size=9), anchor="w", justify="left", wraplength=450).grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 9))

            controls = ctk.CTkFrame(card, fg_color="transparent")
            controls.grid(row=4, column=0, sticky="ew", padx=13, pady=(0, 13))
            for control_column in range(3):
                controls.grid_columnconfigure(control_column, weight=1)
            service_id = service["id"]
            ctk.CTkButton(controls, text="Start", height=31, fg_color="#17684F", hover_color="#1B8060", command=lambda sid=service_id: self.manager.start_service(sid)).grid(row=0, column=0, sticky="ew", padx=3)
            ctk.CTkButton(controls, text="Stop", height=31, fg_color="#48252D", hover_color="#63303A", command=lambda sid=service_id: self.manager.stop_service(sid)).grid(row=0, column=1, sticky="ew", padx=3)
            ctk.CTkButton(controls, text="Restart", height=31, fg_color=COLORS["panel_raised"], hover_color="#2A3A50", command=lambda sid=service_id: self.manager.restart_service(sid)).grid(row=0, column=2, sticky="ew", padx=3)

            self.cards[service_id] = {"badge": badge, "pid": pid_label}
            self._add_sidebar_service(service)

        if hasattr(self, "log_filter"):
            values = self._log_filter_values()
            self.log_filter.configure(values=values)
            if self.log_filter_value not in values:
                self.log_filter_value = "All Services"
            self.log_filter.set(self.log_filter_value)

    def _add_sidebar_service(self, service: dict[str, Any]) -> None:
        row = ctk.CTkFrame(self.sidebar_services, fg_color="transparent")
        row.pack(fill="x", pady=2)
        row.grid_columnconfigure(1, weight=1)
        dot = ctk.CTkLabel(row, text="●", text_color=STATUS_COLORS["Stopped"][0], width=16, font=ctk.CTkFont(size=9))
        dot.grid(row=0, column=0, sticky="w", padx=(5, 7), pady=7)
        ctk.CTkLabel(row, text=service["name"], anchor="w", text_color="#B8C4D3", font=ctk.CTkFont(size=11)).grid(row=0, column=1, sticky="ew", pady=7)
        self.cards[service["id"]]["dot"] = dot

    def _log_filter_values(self) -> list[str]:
        return ["All Services", *[service["name"] for service in self.services]]

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.manager.events.get_nowait()
                if event["type"] == "status":
                    self._update_status(event)
                elif event["type"] == "log":
                    self._add_log(event)
                elif event["type"] == "open_frontend":
                    self._opening_frontend = False
                    self.open_button.configure(state="normal", text="↗  Open To-do List")
                    if webbrowser.open(event["url"], new=2):
                        self.manager._log("frontend", f"Opened {event['url']} in your browser.", "system")
                    else:
                        self.manager._log("frontend", f"Could not open a browser automatically. Open {event['url']}.", "system")
                elif event["type"] == "frontend_timeout":
                    self._opening_frontend = False
                    self.open_button.configure(state="normal", text="↗  Open To-do List")
                    self.manager._log("frontend", f"Frontend did not respond at {event['url']}. Check the service log.", "stderr")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._drain_events)

    def _update_status(self, event: dict[str, Any]) -> None:
        card = self.cards.get(event["service_id"])
        if not card:
            return
        status = event["status"]
        foreground, background = STATUS_COLORS.get(status, STATUS_COLORS["Stopped"])
        card["badge"].configure(text=status, text_color=foreground, fg_color=background)
        pid = event.get("pid")
        service = next((item for item in self.services if item["id"] == event["service_id"]), {})
        port = f"Port {service['port']}" if service.get("port") else "Port not specified"
        card["pid"].configure(text=f"{port}   ·   PID {pid if pid else '—'}")
        card["dot"].configure(text_color=foreground)

    def _add_log(self, event: dict[str, str]) -> None:
        self.log_history.append(event)
        if self.log_filter_value == "All Services" or self.log_filter_value == event["service_name"]:
            self._write_log_line(event)

    def _write_log_line(self, event: dict[str, str]) -> None:
        prefix = f"{event['time']}  {event['service_name']:<10}  {event['stream'].upper():<6}  "
        self.console.configure(state="normal")
        self.console.insert("end", prefix + event["message"] + "\n")
        line_count = int(self.console.index("end-1c").split(".")[0])
        if line_count > LOG_LIMIT:
            self.console.delete("1.0", f"{line_count - LOG_LIMIT + 1}.0")
        self.console.see("end")
        self.console.configure(state="disabled")

    def _change_log_filter(self, value: str) -> None:
        self.log_filter_value = value
        self._render_logs()

    def _render_logs(self) -> None:
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        for event in self.log_history:
            if self.log_filter_value == "All Services" or self.log_filter_value == event["service_name"]:
                prefix = f"{event['time']}  {event['service_name']:<10}  {event['stream'].upper():<6}  "
                self.console.insert("end", prefix + event["message"] + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def _clear_logs(self) -> None:
        self.log_history.clear()
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _open_todo_list(self) -> None:
        if self._opening_frontend:
            return
        frontend = next((service for service in self.services if service["id"] == "frontend"), None)
        if frontend is None:
            messagebox.showerror("Frontend not configured", 'Add a service with id "frontend" to services.json.', parent=self)
            return

        url = frontend.get("url") or f"http://127.0.0.1:{frontend.get('port') or 5173}"
        self._opening_frontend = True
        self.open_button.configure(state="disabled", text="Starting To-do List…")
        self.manager._log("frontend", "Starting the stack and waiting for the frontend…", "system")
        self.manager.start_all()

        def wait_until_ready() -> None:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(url, timeout=1) as response:
                        if response.status < 500:
                            self.manager.events.put({"type": "open_frontend", "url": url})
                            return
                except Exception:
                    time.sleep(0.5)
            self.manager.events.put({"type": "frontend_timeout", "url": url})

        threading.Thread(target=wait_until_ready, daemon=True, name="open-todo-list").start()

    def _open_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.focus()
            return
        window = ctk.CTkToplevel(self)
        self.settings_window = window
        window.title("Service configuration")
        window.geometry("760x690")
        window.minsize(650, 520)
        window.transient(self)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(window, text="Service configuration", anchor="w", text_color=COLORS["text"], font=ctk.CTkFont(size=19, weight="bold")).grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 4))
        ctk.CTkLabel(window, text=f"Edit the JSON saved in {CONFIG_PATH.name}. Stop services before applying changes.", anchor="w", text_color=COLORS["muted"], font=ctk.CTkFont(size=11)).grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 10))
        editor = ctk.CTkTextbox(window, fg_color="#080D14", text_color="#D8E2EF", font=ctk.CTkFont(family="Cascadia Code", size=11), wrap="none", corner_radius=10)
        editor.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 14))
        editor.insert("1.0", json.dumps({"services": self.services}, indent=2))

        footer = ctk.CTkFrame(window, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 18))
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(footer, text="Commands are argument arrays; no shell is used for process launch.", text_color=COLORS["muted"], font=ctk.CTkFont(size=10)).grid(row=0, column=0, sticky="w")

        def save_configuration() -> None:
            if self.manager.has_active_services():
                messagebox.showwarning("Services are active", "Stop all services before saving a new configuration.", parent=window)
                return
            try:
                services = validate_services(json.loads(editor.get("1.0", "end")))
                CONFIG_PATH.write_text(json.dumps({"services": services}, indent=2) + "\n", encoding="utf-8")
                self.manager.update_services(services)
                self.services = services
                self._rebuild_service_cards()
            except (json.JSONDecodeError, ValueError, OSError, RuntimeError) as exc:
                messagebox.showerror("Could not save configuration", str(exc), parent=window)
                return
            window.destroy()
            self.settings_window = None

        ctk.CTkButton(footer, text="Cancel", width=90, height=36, fg_color=COLORS["panel_raised"], hover_color="#243449", command=window.destroy).grid(row=0, column=1, padx=(0, 8))
        ctk.CTkButton(footer, text="Save configuration", width=150, height=36, fg_color="#17684F", hover_color="#1B8060", command=save_configuration).grid(row=0, column=2)

    def _close(self) -> None:
        if self.manager.has_active_services():
            self.manager.shutdown(timeout=8)
        self.destroy()


def main() -> None:
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    app = ControlPanel()
    if app.winfo_exists():
        app.mainloop()


if __name__ == "__main__":
    main()
