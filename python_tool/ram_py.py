from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

import psutil
import requests
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


APP_NAME = "Roblox Account Manager Python"
ROBLOX_PLAYER_NAME = "robloxplayerbeta"


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = base_dir()
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def strip_cookie(raw: str) -> str:
    value = (raw or "").strip().strip('"').strip("'")
    match = re.search(r"\.ROBLOSECURITY\s*=\s*([^;\s]+)", value, flags=re.I)
    if match:
        return match.group(1).strip()
    return value


def extract_place_id(text: str) -> int:
    text = (text or "").strip()
    match = re.search(r"/games/(\d+)", text, flags=re.I)
    if match:
        return safe_int(match.group(1))
    match = re.search(r"\b(\d{3,})\b", text)
    return safe_int(match.group(1)) if match else 0


def extract_private_server_link_code(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""

    try:
        parsed = urlparse(text)
        query = parse_qs(parsed.query)
        if query.get("privateServerLinkCode"):
            return query["privateServerLinkCode"][0]
    except Exception:
        pass

    match = re.search(r"privateServerLinkCode=([^&\s]+)", text, flags=re.I)
    return match.group(1) if match else ""


def parse_browser_tracker(command_line: str) -> str:
    if not command_line:
        return ""
    match = re.search(
        r"(?:\s-b\s+(\d+)|browsertrackerid[:=](\d+)|browserTrackerId=(\d+))",
        command_line,
        flags=re.I,
    )
    if not match:
        return ""
    return next((group for group in match.groups() if group), "")


@dataclass
class LastLaunch:
    place_id: int = 0
    job_id: str = ""
    follow_user: bool = False
    join_vip: bool = False
    at: float = 0.0


@dataclass
class Account:
    security_token: str = ""
    username: str = ""
    user_id: int = 0
    alias: str = ""
    description: str = ""
    group: str = "Default"
    valid: bool = False
    last_use: str = ""
    browser_tracker_id: str = ""
    fields: Dict[str, str] = field(default_factory=dict)
    last_launch: LastLaunch = field(default_factory=LastLaunch)
    last_auto_rejoin_attempt: float = 0.0
    auto_rejoin_pending: bool = False

    @property
    def display_name(self) -> str:
        return self.alias or self.username or str(self.user_id) or "Unknown"

    @property
    def has_last_launch(self) -> bool:
        return self.last_launch.place_id > 0

    def ensure_tracker(self) -> str:
        if not self.browser_tracker_id:
            self.browser_tracker_id = f"{random.randint(100000, 175000)}{random.randint(100000, 900000)}"
        return self.browser_tracker_id

    def remember_launch(self, place_id: int, job_id: str, follow_user: bool, join_vip: bool) -> None:
        self.last_launch = LastLaunch(
            place_id=place_id,
            job_id=job_id or "",
            follow_user=follow_user,
            join_vip=join_vip,
            at=time.time(),
        )
        self.last_use = now_iso()

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("auto_rejoin_pending", None)
        return data

    @staticmethod
    def from_dict(data: Any) -> "Account":
        if isinstance(data, str):
            return Account(security_token=strip_cookie(data))
        if not isinstance(data, dict):
            return Account()

        launch_data = data.get("last_launch") or data.get("LastLaunch") or {}
        last_launch = LastLaunch(
            place_id=safe_int(launch_data.get("place_id") or launch_data.get("PlaceID") or launch_data.get("PlaceId")),
            job_id=str(launch_data.get("job_id") or launch_data.get("JobID") or ""),
            follow_user=bool(launch_data.get("follow_user") or launch_data.get("FollowUser") or False),
            join_vip=bool(launch_data.get("join_vip") or launch_data.get("JoinVIP") or False),
            at=float(launch_data.get("at") or 0.0),
        )

        return Account(
            security_token=strip_cookie(
                data.get("security_token")
                or data.get("SecurityToken")
                or data.get("cookie")
                or data.get("Cookie")
                or ""
            ),
            username=str(data.get("username") or data.get("Username") or ""),
            user_id=safe_int(data.get("user_id") or data.get("UserID") or data.get("UserId")),
            alias=str(data.get("alias") or data.get("Alias") or ""),
            description=str(data.get("description") or data.get("Description") or ""),
            group=str(data.get("group") or data.get("Group") or "Default"),
            valid=bool(data.get("valid") if "valid" in data else data.get("Valid", False)),
            last_use=str(data.get("last_use") or data.get("LastUse") or ""),
            browser_tracker_id=str(data.get("browser_tracker_id") or data.get("BrowserTrackerID") or ""),
            fields=dict(data.get("fields") or data.get("Fields") or {}),
            last_launch=last_launch,
            last_auto_rejoin_attempt=float(data.get("last_auto_rejoin_attempt") or 0.0),
        )


DEFAULT_SETTINGS: Dict[str, Any] = {
    "watcher_enabled": True,
    "auto_rejoin": True,
    "auto_rejoin_delay": 15,
    "exit_if_no_connection": True,
    "no_connection_timeout": 30,
    "memory_low_enabled": False,
    "memory_low_mb": 200,
    "scan_interval": 2,
    "launch_delay": 9,
    "auto_close_last_process": True,
    "shuffle_page_count": 3,
}


class DataStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.accounts_path = DATA_DIR / "accounts.json"
        self.settings_path = DATA_DIR / "settings.json"
        self.favorites_path = DATA_DIR / "favorite_games.json"
        self.recent_path = DATA_DIR / "recent_games.json"
        self.accounts: List[Account] = []
        self.settings: Dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.favorites: List[Dict[str, Any]] = []
        self.recent: List[Dict[str, Any]] = []

    def load(self) -> None:
        with self.lock:
            self.accounts = [Account.from_dict(item) for item in self._load_json(self.accounts_path, [])]
            loaded_settings = self._load_json(self.settings_path, {})
            if isinstance(loaded_settings, dict):
                self.settings.update(loaded_settings)
            self.favorites = list(self._load_json(self.favorites_path, []))
            self.recent = list(self._load_json(self.recent_path, []))

    def save_accounts(self) -> None:
        with self.lock:
            self._save_json(self.accounts_path, [account.to_dict() for account in self.accounts])

    def save_settings(self) -> None:
        with self.lock:
            self._save_json(self.settings_path, self.settings)

    def save_games(self) -> None:
        with self.lock:
            self._save_json(self.favorites_path, self.favorites)
            self._save_json(self.recent_path, self.recent)

    def add_recent(self, place_id: int, job_id: str = "", name: str = "") -> None:
        with self.lock:
            item = {"name": name or str(place_id), "place_id": place_id, "job_id": job_id or ""}
            self.recent = [
                game
                for game in self.recent
                if safe_int(game.get("place_id")) != place_id or str(game.get("job_id") or "") != item["job_id"]
            ]
            self.recent.insert(0, item)
            self.recent = self.recent[:25]
            self.save_games()

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    @staticmethod
    def _save_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp_path.replace(path)


class RobloxAPI:
    MAIN = "https://www.roblox.com"
    AUTH = "https://auth.roblox.com"
    GAMES = "https://games.roblox.com"
    USERS = "https://users.roblox.com"
    PRESENCE = "https://presence.roblox.com"
    REFERER = "https://www.roblox.com/games/4924922222/Brookhaven-RP"

    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log

    def session_for(self, account: Account) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "RobloxAccountManagerPython/1.0",
                "Accept": "application/json, text/plain, */*",
                "Referer": self.REFERER,
            }
        )
        session.cookies.set(".ROBLOSECURITY", account.security_token, domain=".roblox.com", path="/")
        return session

    def validate_account(self, account: Account) -> Account:
        session = self.session_for(account)
        response = session.get(f"{self.MAIN}/my/account/json", timeout=20)
        if response.status_code != 200:
            account.valid = False
            raise RuntimeError(f"Account validation failed: HTTP {response.status_code} {response.text[:200]}")

        data = response.json()
        account.username = str(data.get("Name") or data.get("name") or account.username)
        account.user_id = safe_int(data.get("UserId") or data.get("userId") or account.user_id)
        account.valid = True
        account.last_use = now_iso()
        return account

    def get_csrf_token(self, account: Account) -> str:
        session = self.session_for(account)
        response = session.post(f"{self.AUTH}/v1/authentication-ticket/", timeout=20)
        token = response.headers.get("x-csrf-token")
        if response.status_code == 403 and token:
            return token
        if token:
            return token
        raise RuntimeError(f"Could not get X-CSRF token: HTTP {response.status_code} {response.text[:200]}")

    def get_auth_ticket(self, account: Account) -> str:
        token = self.get_csrf_token(account)
        session = self.session_for(account)
        response = session.post(
            f"{self.AUTH}/v1/authentication-ticket/",
            headers={"X-CSRF-TOKEN": token, "Referer": self.REFERER},
            timeout=20,
        )
        ticket = response.headers.get("rbx-authentication-ticket")
        if ticket:
            account.last_use = now_iso()
            return ticket
        raise RuntimeError(f"Could not get authentication ticket: HTTP {response.status_code} {response.text[:200]}")

    def get_user_id(self, username_or_id: str) -> int:
        value = (username_or_id or "").strip()
        if value.isdigit():
            return int(value)

        response = requests.post(
            f"{self.USERS}/v1/usernames/users",
            json={"usernames": [value], "excludeBannedUsers": False},
            timeout=20,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Could not resolve username: HTTP {response.status_code} {response.text[:200]}")
        data = response.json().get("data") or []
        if not data:
            raise RuntimeError(f"Username not found: {value}")
        return safe_int(data[0].get("id"))

    def get_presence(self, user_id: int) -> Dict[str, Any]:
        response = requests.post(f"{self.PRESENCE}/v1/presence/users", json={"userIds": [user_id]}, timeout=20)
        if response.status_code != 200:
            return {}
        users = response.json().get("userPresences") or []
        return users[0] if users else {}

    def get_random_job_id(self, place_id: int, page_count: int = 3, choose_lowest: bool = False) -> str:
        valid_servers: List[str] = []
        cursor = ""
        pages = max(page_count, 1)

        for _ in range(pages):
            params = {"sortOrder": "Asc", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = requests.get(f"{self.GAMES}/v1/games/{place_id}/servers/public", params=params, timeout=20)
            if response.status_code != 200:
                break
            data = response.json()
            for server in data.get("data") or []:
                playing = safe_int(server.get("playing"))
                max_players = safe_int(server.get("maxPlayers"))
                server_id = str(server.get("id") or "")
                if server_id and 0 < playing < max_players and max_players > 1:
                    valid_servers.append(server_id)
            cursor = str(data.get("nextPageCursor") or "")
            if not cursor or choose_lowest:
                break

        if not valid_servers:
            return ""
        return valid_servers[0] if choose_lowest else random.choice(valid_servers)

    def resolve_private_server(self, account: Account, place_id: int, job_id: str) -> Tuple[str, str]:
        link_code = extract_private_server_link_code(job_id)
        access_code = job_id or ""
        if not link_code:
            return access_code, ""

        session = self.session_for(account)
        response = session.get(
            f"{self.MAIN}/games/{place_id}",
            params={"privateServerLinkCode": link_code},
            headers={"Referer": self.REFERER},
            timeout=20,
            allow_redirects=True,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Could not resolve private server link: HTTP {response.status_code}")

        match = re.search(
            r"Roblox\.GameLauncher\.joinPrivateGame\(\d+,\s*'([A-Za-z0-9\-]+)'",
            response.text,
        )
        if not match:
            raise RuntimeError("Could not find private server access code in Roblox response")
        return match.group(1), link_code

    def launch_account(
        self,
        account: Account,
        place_id: int,
        job_id: str = "",
        follow_user: bool = False,
        join_vip: bool = False,
        shuffle: bool = False,
        shuffle_pages: int = 3,
    ) -> str:
        if place_id <= 0:
            raise RuntimeError("Place/User ID is empty")
        if not account.security_token:
            raise RuntimeError(f"{account.display_name} has no cookie")

        tracker = account.ensure_tracker()
        account.remember_launch(place_id, job_id, follow_user, join_vip)

        if shuffle and not follow_user and not join_vip and not job_id:
            job_id = self.get_random_job_id(place_id, page_count=shuffle_pages)
            account.remember_launch(place_id, job_id, follow_user, join_vip)

        ticket = self.get_auth_ticket(account)
        launch_time = int(time.time() * 1000)

        if join_vip:
            access_code, link_code = self.resolve_private_server(account, place_id, job_id)
            launcher_url = (
                "https://assetgame.roblox.com/game/PlaceLauncher.ashx?"
                f"request=RequestPrivateGame&placeId={place_id}&accessCode={access_code}&linkCode={link_code}"
            )
        elif follow_user:
            launcher_url = (
                "https://assetgame.roblox.com/game/PlaceLauncher.ashx?"
                f"request=RequestFollowUser&userId={place_id}"
            )
        else:
            request_name = "RequestGameJob" if job_id else "RequestGame"
            launcher_url = (
                "https://assetgame.roblox.com/game/PlaceLauncher.ashx?"
                f"request={request_name}&browserTrackerId={tracker}&placeId={place_id}"
            )
            if job_id:
                launcher_url += f"&gameId={job_id}"
            launcher_url += "&isPlayTogetherGame=false"

        uri = (
            f"roblox-player:1+launchmode:play+gameinfo:{ticket}"
            f"+launchtime:{launch_time}"
            f"+placelauncherurl:{quote(launcher_url, safe='')}"
            f"+browsertrackerid:{tracker}"
            "+robloxLocale:en_us+gameLocale:en_us+channel:+LaunchExp:InApp"
        )

        self.log(f"Launching {account.display_name} -> {place_id}")
        if sys.platform == "win32":
            os.startfile(uri)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", uri])
        return "Success"


def iter_roblox_processes() -> Iterable[psutil.Process]:
    for proc in psutil.process_iter(["pid", "name", "create_time"]):
        try:
            name = str(proc.info.get("name") or "").lower()
            if name.startswith(ROBLOX_PLAYER_NAME):
                yield proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def process_command_line(proc: psutil.Process) -> str:
    try:
        return " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def close_existing_processes(tracker: str, log: Callable[[str], None]) -> None:
    if not tracker:
        return
    for proc in iter_roblox_processes():
        try:
            command_line = process_command_line(proc)
            if parse_browser_tracker(command_line) != tracker:
                continue
            log(f"Closing previous Roblox process {proc.pid} for tracker {tracker}")
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except psutil.TimeoutExpired:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            log(f"Could not close Roblox process: {exc}")


@dataclass
class ProcessState:
    pid: int
    tracker: str
    account: Optional[Account]
    create_time: float
    log_path: Optional[Path] = None
    log_pos: int = 0
    connected: bool = False
    disconnected_at: float = 0.0
    exit_reason: str = ""


class RobloxWatcher:
    def __init__(
        self,
        get_accounts: Callable[[], List[Account]],
        get_settings: Callable[[], Dict[str, Any]],
        launch_last: Callable[[Account, str], None],
        log: Callable[[str], None],
    ) -> None:
        self.get_accounts = get_accounts
        self.get_settings = get_settings
        self.launch_last = launch_last
        self.log = log
        self.states: Dict[int, ProcessState] = {}
        self.suppressed_until: Dict[str, float] = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="RobloxWatcher", daemon=True)
        self.thread.start()
        self.log("Watcher started")

    def stop(self) -> None:
        self.stop_event.set()
        self.log("Watcher stopped")

    def suppress_auto_rejoin(self, tracker: str, seconds: int = 45) -> None:
        if not tracker:
            return
        with self.lock:
            self.suppressed_until[tracker] = time.time() + seconds

    def is_suppressed(self, tracker: str) -> bool:
        if not tracker:
            return False
        with self.lock:
            until = self.suppressed_until.get(tracker, 0)
            if until > time.time():
                return True
            self.suppressed_until.pop(tracker, None)
            return False

    def _run(self) -> None:
        while not self.stop_event.is_set():
            settings = self.get_settings()
            if settings.get("watcher_enabled", True):
                try:
                    self.scan()
                except Exception as exc:
                    self.log(f"Watcher scan error: {exc}")
            interval = max(float(settings.get("scan_interval", 2)), 1.0)
            self.stop_event.wait(interval)

    def scan(self) -> None:
        current_pids: set[int] = set()
        assigned_logs = {str(state.log_path) for state in self.states.values() if state.log_path}

        for proc in iter_roblox_processes():
            try:
                pid = proc.pid
                current_pids.add(pid)
                command_line = process_command_line(proc)
                tracker = parse_browser_tracker(command_line)
                if not tracker:
                    continue

                state = self.states.get(pid)
                if not state:
                    account = self.find_account_by_tracker(tracker)
                    create_time = float(proc.info.get("create_time") or time.time())
                    state = ProcessState(pid=pid, tracker=tracker, account=account, create_time=create_time)
                    self.states[pid] = state
                    owner = account.display_name if account else "unknown account"
                    self.log(f"Watching Roblox PID {pid} ({owner})")

                self.assign_log_file(state, assigned_logs)
                self.read_log_file(state)
                self.apply_process_rules(proc, state)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        for pid, state in list(self.states.items()):
            if pid in current_pids:
                continue
            self.states.pop(pid, None)
            reason = state.exit_reason or "Roblox process exited"
            self.log(f"Roblox PID {pid} exited: {reason}")
            if state.account:
                self.queue_auto_rejoin(state.account, state.tracker, reason)

    def find_account_by_tracker(self, tracker: str) -> Optional[Account]:
        return next((account for account in self.get_accounts() if account.browser_tracker_id == tracker), None)

    def assign_log_file(self, state: ProcessState, assigned_logs: set[str]) -> None:
        if state.log_path and state.log_path.exists():
            return
        logs_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "logs"
        if not logs_dir.exists():
            return

        candidates: List[Path] = []
        cutoff = state.create_time - 90
        for path in logs_dir.glob("*_Player_*.log"):
            try:
                if str(path) in assigned_logs:
                    continue
                if path.stat().st_mtime >= cutoff:
                    candidates.append(path)
            except OSError:
                continue

        if not candidates:
            return

        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        state.log_path = candidates[0]
        state.log_pos = 0
        assigned_logs.add(str(state.log_path))
        self.log(f"Attached log for PID {state.pid}: {state.log_path.name}")

    def read_log_file(self, state: ProcessState) -> None:
        if not state.log_path or not state.log_path.exists():
            return

        try:
            with state.log_path.open("r", encoding="utf-8", errors="ignore") as handle:
                handle.seek(state.log_pos)
                data = handle.read()
                state.log_pos = handle.tell()
        except OSError:
            return

        if not data:
            return

        for line in data.splitlines():
            if "Joining game" in line and "place" in line:
                state.connected = True
                state.disconnected_at = 0.0
            elif "[FLog::Network] Sending disconnect with reason:" in line:
                state.connected = False
                state.disconnected_at = time.time()
                self.log(f"Roblox PID {state.pid} disconnected")

    def apply_process_rules(self, proc: psutil.Process, state: ProcessState) -> None:
        settings = self.get_settings()
        try:
            age = time.time() - float(proc.info.get("create_time") or time.time())
            if age > 30 and settings.get("memory_low_enabled", False):
                rss_mb = proc.memory_info().rss / 1024 / 1024
                threshold = max(safe_int(settings.get("memory_low_mb"), 200), 50)
                if rss_mb < threshold:
                    self.kill_process(proc, state, f"Low memory ({rss_mb:.0f} MB < {threshold} MB)")
                    return

            if settings.get("exit_if_no_connection", True) and state.disconnected_at:
                timeout = max(safe_int(settings.get("no_connection_timeout"), 30), 5)
                disconnected_for = time.time() - state.disconnected_at
                if disconnected_for >= timeout:
                    self.kill_process(proc, state, f"Lost connection for {disconnected_for:.0f} seconds")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return

    def kill_process(self, proc: psutil.Process, state: ProcessState, reason: str) -> None:
        state.exit_reason = reason
        self.log(f"Killing Roblox PID {state.pid}: {reason}")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except psutil.TimeoutExpired:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            self.log(f"Could not kill Roblox PID {state.pid}: {exc}")

    def has_running_tracker(self, tracker: str) -> bool:
        for proc in iter_roblox_processes():
            if parse_browser_tracker(process_command_line(proc)) == tracker:
                return True
        return False

    def queue_auto_rejoin(self, account: Account, tracker: str, reason: str) -> None:
        settings = self.get_settings()
        if not settings.get("auto_rejoin", True) or not account.has_last_launch:
            return
        if self.is_suppressed(tracker):
            self.log(f"Auto rejoin suppressed for {account.display_name}")
            return
        if account.auto_rejoin_pending:
            return

        account.auto_rejoin_pending = True
        source_time = account.last_launch.at

        def worker() -> None:
            try:
                delay = max(safe_int(self.get_settings().get("auto_rejoin_delay"), 15), 5)
                self.log(f"Auto rejoin queued for {account.display_name} in {delay}s ({reason})")
                time.sleep(delay)

                while self.get_settings().get("watcher_enabled", True) and self.get_settings().get("auto_rejoin", True):
                    if self.internet_available():
                        break
                    self.log(f"Waiting for internet before rejoining {account.display_name}")
                    time.sleep(5)

                if not self.get_settings().get("auto_rejoin", True):
                    return
                if account.last_launch.at != source_time:
                    self.log(f"Auto rejoin skipped for {account.display_name}: launched again manually")
                    return
                if self.has_running_tracker(tracker):
                    self.log(f"Auto rejoin skipped for {account.display_name}: Roblox already running")
                    return

                account.last_auto_rejoin_attempt = time.time()
                self.launch_last(account, reason)
            except Exception as exc:
                self.log(f"Auto rejoin failed for {account.display_name}: {exc}")
            finally:
                account.auto_rejoin_pending = False

        threading.Thread(target=worker, name=f"AutoRejoin-{tracker}", daemon=True).start()

    @staticmethod
    def internet_available() -> bool:
        try:
            requests.get("https://www.roblox.com", timeout=5)
            return True
        except requests.RequestException:
            return False


class RAMApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1100x700")
        self.store = DataStore()
        self.store.load()
        self.api = RobloxAPI(self.log)
        self.launch_stop_event = threading.Event()
        self.watcher = RobloxWatcher(
            get_accounts=lambda: self.store.accounts,
            get_settings=self.current_settings,
            launch_last=self.launch_last_from_watcher,
            log=self.log,
        )

        self.var_place = tk.StringVar()
        self.var_job = tk.StringVar()
        self.var_vip = tk.BooleanVar(value=False)
        self.var_follow = tk.BooleanVar(value=False)
        self.var_shuffle = tk.BooleanVar(value=False)

        self.var_watcher_enabled = tk.BooleanVar(value=bool(self.store.settings.get("watcher_enabled", True)))
        self.var_auto_rejoin = tk.BooleanVar(value=bool(self.store.settings.get("auto_rejoin", True)))
        self.var_exit_no_conn = tk.BooleanVar(value=bool(self.store.settings.get("exit_if_no_connection", True)))
        self.var_mem_low = tk.BooleanVar(value=bool(self.store.settings.get("memory_low_enabled", False)))
        self.var_auto_close = tk.BooleanVar(value=bool(self.store.settings.get("auto_close_last_process", True)))
        self.var_rejoin_delay = tk.IntVar(value=safe_int(self.store.settings.get("auto_rejoin_delay"), 15))
        self.var_no_conn_timeout = tk.IntVar(value=safe_int(self.store.settings.get("no_connection_timeout"), 30))
        self.var_mem_low_mb = tk.IntVar(value=safe_int(self.store.settings.get("memory_low_mb"), 200))
        self.var_scan_interval = tk.IntVar(value=safe_int(self.store.settings.get("scan_interval"), 2))
        self.var_launch_delay = tk.IntVar(value=safe_int(self.store.settings.get("launch_delay"), 9))
        self.var_shuffle_pages = tk.IntVar(value=safe_int(self.store.settings.get("shuffle_page_count"), 3))

        self.build_ui()
        self.refresh_accounts()
        self.refresh_games()
        self.apply_settings(save=False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def build_ui(self) -> None:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.accounts_tab = ttk.Frame(notebook, padding=8)
        self.games_tab = ttk.Frame(notebook, padding=8)
        self.watcher_tab = ttk.Frame(notebook, padding=8)
        self.log_tab = ttk.Frame(notebook, padding=8)

        notebook.add(self.accounts_tab, text="Accounts")
        notebook.add(self.games_tab, text="Games")
        notebook.add(self.watcher_tab, text="Watcher")
        notebook.add(self.log_tab, text="Log")

        self.build_accounts_tab()
        self.build_games_tab()
        self.build_watcher_tab()
        self.build_log_tab()

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.status, anchor=tk.W).pack(fill=tk.X, padx=8, pady=(0, 6))

    def build_accounts_tab(self) -> None:
        pane = ttk.PanedWindow(self.accounts_tab, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=3)
        pane.add(right, weight=2)

        columns = ("alias", "username", "user_id", "group", "valid", "last_use")
        self.account_tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="extended")
        headings = {
            "alias": "Alias",
            "username": "Username",
            "user_id": "User ID",
            "group": "Group",
            "valid": "Valid",
            "last_use": "Last Use",
        }
        widths = {"alias": 140, "username": 160, "user_id": 95, "group": 90, "valid": 60, "last_use": 175}
        for column in columns:
            self.account_tree.heading(column, text=headings[column])
            self.account_tree.column(column, width=widths[column], anchor=tk.W)

        scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.account_tree.yview)
        self.account_tree.configure(yscrollcommand=scroll.set)
        self.account_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        account_buttons = ttk.Frame(left)
        account_buttons.pack(fill=tk.X, pady=(8, 0))
        for text, command in [
            ("Add Cookie", self.add_cookie),
            ("Import", self.import_accounts),
            ("Export", self.export_accounts),
            ("Edit Alias", self.edit_alias),
            ("Validate", self.validate_selected),
            ("Remove", self.remove_selected),
        ]:
            ttk.Button(account_buttons, text=text, command=command).pack(side=tk.LEFT, padx=(0, 6))

        launch = ttk.LabelFrame(right, text="Launch", padding=10)
        launch.pack(fill=tk.X)
        launch.columnconfigure(1, weight=1)

        ttk.Label(launch, text="Place ID / URL / User").grid(row=0, column=0, sticky=tk.W, pady=3)
        ttk.Entry(launch, textvariable=self.var_place).grid(row=0, column=1, sticky=tk.EW, pady=3)
        ttk.Label(launch, text="Job ID / VIP link").grid(row=1, column=0, sticky=tk.W, pady=3)
        ttk.Entry(launch, textvariable=self.var_job).grid(row=1, column=1, sticky=tk.EW, pady=3)

        ttk.Checkbutton(launch, text="VIP/private server", variable=self.var_vip).grid(row=2, column=0, columnspan=2, sticky=tk.W)
        ttk.Checkbutton(launch, text="Follow user", variable=self.var_follow).grid(row=3, column=0, columnspan=2, sticky=tk.W)
        ttk.Checkbutton(launch, text="Shuffle public server", variable=self.var_shuffle).grid(row=4, column=0, columnspan=2, sticky=tk.W)

        controls = ttk.Frame(launch)
        controls.grid(row=5, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))
        ttk.Button(controls, text="Launch Selected", command=self.launch_selected).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(controls, text="Stop Queue", command=self.stop_launch_queue).pack(side=tk.LEFT)

        settings = ttk.LabelFrame(right, text="Queue", padding=10)
        settings.pack(fill=tk.X, pady=(10, 0))
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="Delay between accounts").grid(row=0, column=0, sticky=tk.W)
        ttk.Spinbox(settings, from_=0, to=120, textvariable=self.var_launch_delay, width=8).grid(row=0, column=1, sticky=tk.W)
        ttk.Label(settings, text="Shuffle pages").grid(row=1, column=0, sticky=tk.W)
        ttk.Spinbox(settings, from_=1, to=10, textvariable=self.var_shuffle_pages, width=8).grid(row=1, column=1, sticky=tk.W)
        ttk.Button(settings, text="Apply Settings", command=self.apply_settings).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))

        info = ttk.LabelFrame(right, text="Notes", padding=10)
        info.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        ttk.Label(
            info,
            text=(
                "This Python build stores account cookies only in python_tool/data.\n"
                "Use your own accounts only. Auto rejoin works for Roblox sessions launched by this tool."
            ),
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

    def build_games_tab(self) -> None:
        pane = ttk.PanedWindow(self.games_tab, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)

        fav_frame = ttk.LabelFrame(pane, text="Favorites", padding=8)
        recent_frame = ttk.LabelFrame(pane, text="Recent", padding=8)
        pane.add(fav_frame, weight=1)
        pane.add(recent_frame, weight=1)

        self.favorite_tree = self.make_game_tree(fav_frame)
        self.recent_tree = self.make_game_tree(recent_frame)
        self.favorite_tree.bind("<Double-1>", lambda _event: self.load_selected_game(self.favorite_tree))
        self.recent_tree.bind("<Double-1>", lambda _event: self.load_selected_game(self.recent_tree))

        fav_buttons = ttk.Frame(fav_frame)
        fav_buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(fav_buttons, text="Add Current", command=self.add_current_favorite).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(fav_buttons, text="Load", command=lambda: self.load_selected_game(self.favorite_tree)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(fav_buttons, text="Remove", command=self.remove_favorite).pack(side=tk.LEFT)

        recent_buttons = ttk.Frame(recent_frame)
        recent_buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(recent_buttons, text="Load", command=lambda: self.load_selected_game(self.recent_tree)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(recent_buttons, text="Clear", command=self.clear_recent).pack(side=tk.LEFT)

    def make_game_tree(self, parent: ttk.Frame) -> ttk.Treeview:
        columns = ("name", "place_id", "job_id")
        tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse")
        for column, heading, width in [
            ("name", "Name", 180),
            ("place_id", "Place ID", 100),
            ("job_id", "Job/VIP", 220),
        ]:
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor=tk.W)
        tree.pack(fill=tk.BOTH, expand=True)
        return tree

    def build_watcher_tab(self) -> None:
        frame = ttk.LabelFrame(self.watcher_tab, text="Roblox Watcher", padding=12)
        frame.pack(anchor=tk.NW, fill=tk.X)
        frame.columnconfigure(1, weight=1)

        ttk.Checkbutton(frame, text="Enable Roblox watcher", variable=self.var_watcher_enabled, command=self.apply_settings).grid(
            row=0, column=0, columnspan=3, sticky=tk.W, pady=2
        )
        ttk.Checkbutton(frame, text="Auto rejoin when Roblox closes", variable=self.var_auto_rejoin, command=self.apply_settings).grid(
            row=1, column=0, columnspan=3, sticky=tk.W, pady=2
        )
        ttk.Label(frame, text="Auto rejoin delay").grid(row=2, column=0, sticky=tk.W, pady=2)
        ttk.Spinbox(frame, from_=5, to=3600, textvariable=self.var_rejoin_delay, width=8).grid(row=2, column=1, sticky=tk.W, pady=2)
        ttk.Label(frame, text="seconds").grid(row=2, column=2, sticky=tk.W, pady=2)

        ttk.Checkbutton(frame, text="Exit Roblox after lost connection", variable=self.var_exit_no_conn, command=self.apply_settings).grid(
            row=3, column=0, columnspan=3, sticky=tk.W, pady=2
        )
        ttk.Label(frame, text="No connection timeout").grid(row=4, column=0, sticky=tk.W, pady=2)
        ttk.Spinbox(frame, from_=5, to=600, textvariable=self.var_no_conn_timeout, width=8).grid(row=4, column=1, sticky=tk.W, pady=2)
        ttk.Label(frame, text="seconds").grid(row=4, column=2, sticky=tk.W, pady=2)

        ttk.Checkbutton(frame, text="Restart if Roblox memory is below", variable=self.var_mem_low, command=self.apply_settings).grid(
            row=5, column=0, columnspan=3, sticky=tk.W, pady=2
        )
        ttk.Label(frame, text="Memory threshold").grid(row=6, column=0, sticky=tk.W, pady=2)
        ttk.Spinbox(frame, from_=50, to=4096, textvariable=self.var_mem_low_mb, width=8).grid(row=6, column=1, sticky=tk.W, pady=2)
        ttk.Label(frame, text="MB").grid(row=6, column=2, sticky=tk.W, pady=2)

        ttk.Checkbutton(frame, text="Close previous process before relaunching same account", variable=self.var_auto_close, command=self.apply_settings).grid(
            row=7, column=0, columnspan=3, sticky=tk.W, pady=2
        )
        ttk.Label(frame, text="Scan interval").grid(row=8, column=0, sticky=tk.W, pady=2)
        ttk.Spinbox(frame, from_=1, to=30, textvariable=self.var_scan_interval, width=8).grid(row=8, column=1, sticky=tk.W, pady=2)
        ttk.Label(frame, text="seconds").grid(row=8, column=2, sticky=tk.W, pady=2)

        ttk.Button(frame, text="Apply Settings", command=self.apply_settings).grid(row=9, column=0, sticky=tk.W, pady=(10, 0))

    def build_log_tab(self) -> None:
        self.log_text = tk.Text(self.log_tab, height=20, wrap=tk.WORD, state=tk.DISABLED)
        scroll = ttk.Scrollbar(self.log_tab, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def current_settings(self) -> Dict[str, Any]:
        with self.store.lock:
            return dict(self.store.settings)

    def apply_settings(self, save: bool = True) -> None:
        with self.store.lock:
            self.store.settings.update(
                {
                    "watcher_enabled": bool(self.var_watcher_enabled.get()),
                    "auto_rejoin": bool(self.var_auto_rejoin.get()),
                    "auto_rejoin_delay": max(safe_int(self.var_rejoin_delay.get()), 5),
                    "exit_if_no_connection": bool(self.var_exit_no_conn.get()),
                    "no_connection_timeout": max(safe_int(self.var_no_conn_timeout.get()), 5),
                    "memory_low_enabled": bool(self.var_mem_low.get()),
                    "memory_low_mb": max(safe_int(self.var_mem_low_mb.get()), 50),
                    "scan_interval": max(safe_int(self.var_scan_interval.get()), 1),
                    "launch_delay": max(safe_int(self.var_launch_delay.get()), 0),
                    "auto_close_last_process": bool(self.var_auto_close.get()),
                    "shuffle_page_count": max(safe_int(self.var_shuffle_pages.get()), 1),
                }
            )
            if save:
                self.store.save_settings()

        if self.store.settings.get("watcher_enabled", True):
            self.watcher.start()
        else:
            self.watcher.stop()
        self.status.set("Settings saved")

    def refresh_accounts(self) -> None:
        self.account_tree.delete(*self.account_tree.get_children())
        for index, account in enumerate(self.store.accounts):
            self.account_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    account.alias,
                    account.username,
                    account.user_id or "",
                    account.group,
                    "Yes" if account.valid else "No",
                    account.last_use,
                ),
            )

    def refresh_games(self) -> None:
        self.favorite_tree.delete(*self.favorite_tree.get_children())
        for index, game in enumerate(self.store.favorites):
            self.favorite_tree.insert(
                "",
                tk.END,
                iid=f"f{index}",
                values=(game.get("name") or "", game.get("place_id") or "", game.get("job_id") or ""),
            )

        self.recent_tree.delete(*self.recent_tree.get_children())
        for index, game in enumerate(self.store.recent):
            self.recent_tree.insert(
                "",
                tk.END,
                iid=f"r{index}",
                values=(game.get("name") or "", game.get("place_id") or "", game.get("job_id") or ""),
            )

    def selected_accounts(self) -> List[Account]:
        accounts: List[Account] = []
        for iid in self.account_tree.selection():
            index = safe_int(iid, -1)
            if 0 <= index < len(self.store.accounts):
                accounts.append(self.store.accounts[index])
        return accounts

    def add_cookie(self) -> None:
        raw = simpledialog.askstring("Add Cookie", "Paste .ROBLOSECURITY cookie:", show="*")
        if not raw:
            return
        account = Account(security_token=strip_cookie(raw))
        if not account.security_token:
            messagebox.showerror(APP_NAME, "Cookie is empty")
            return

        def work() -> Tuple[Account, Optional[Exception]]:
            try:
                self.api.validate_account(account)
                return account, None
            except Exception as exc:
                return account, exc

        def done(result: Tuple[Account, Optional[Exception]]) -> None:
            new_account, error = result
            if error and not messagebox.askyesno(APP_NAME, f"Could not validate this cookie.\n{error}\n\nSave anyway?"):
                return
            self.store.accounts.append(new_account)
            self.store.save_accounts()
            self.refresh_accounts()
            self.log(f"Added account {new_account.display_name}")

        self.run_background(work, done)

    def import_accounts(self) -> None:
        path = filedialog.askopenfilename(
            title="Import accounts",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            items = data.get("accounts") if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise ValueError("Expected a JSON list of accounts")
            added = 0
            existing_tokens = {account.security_token for account in self.store.accounts}
            for item in items:
                account = Account.from_dict(item)
                if not account.security_token or account.security_token in existing_tokens:
                    continue
                self.store.accounts.append(account)
                existing_tokens.add(account.security_token)
                added += 1
            self.store.save_accounts()
            self.refresh_accounts()
            self.log(f"Imported {added} account(s)")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Import failed:\n{exc}")

    def export_accounts(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Export accounts",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps([account.to_dict() for account in self.store.accounts], indent=2),
                encoding="utf-8",
            )
            self.log(f"Exported accounts to {path}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Export failed:\n{exc}")

    def edit_alias(self) -> None:
        accounts = self.selected_accounts()
        if not accounts:
            return
        account = accounts[0]
        alias = simpledialog.askstring("Edit Alias", "Alias:", initialvalue=account.alias)
        if alias is None:
            return
        account.alias = alias[:50]
        self.store.save_accounts()
        self.refresh_accounts()

    def validate_selected(self) -> None:
        accounts = self.selected_accounts() or self.store.accounts
        if not accounts:
            return

        def work() -> Tuple[int, int]:
            ok = 0
            failed = 0
            for account in accounts:
                try:
                    self.api.validate_account(account)
                    ok += 1
                    self.log(f"Validated {account.display_name}")
                except Exception as exc:
                    account.valid = False
                    failed += 1
                    self.log(f"Validation failed for {account.display_name}: {exc}")
            self.store.save_accounts()
            return ok, failed

        def done(result: Tuple[int, int]) -> None:
            self.refresh_accounts()
            ok, failed = result
            messagebox.showinfo(APP_NAME, f"Validated: {ok}\nFailed: {failed}")

        self.run_background(work, done)

    def remove_selected(self) -> None:
        selected = sorted((safe_int(iid, -1) for iid in self.account_tree.selection()), reverse=True)
        selected = [index for index in selected if 0 <= index < len(self.store.accounts)]
        if not selected:
            return
        if not messagebox.askyesno(APP_NAME, f"Remove {len(selected)} account(s)?"):
            return
        for index in selected:
            self.store.accounts.pop(index)
        self.store.save_accounts()
        self.refresh_accounts()

    def build_launch_target(self) -> Dict[str, Any]:
        place_text = self.var_place.get().strip()
        job_id = self.var_job.get().strip()
        follow_user = bool(self.var_follow.get())
        join_vip = bool(self.var_vip.get())

        if not place_text and "/games/" in job_id:
            place_text = job_id
            self.var_place.set(place_text)

        if "privateServerLinkCode=" in place_text and extract_place_id(place_text):
            job_id = place_text
            join_vip = True
            self.var_job.set(job_id)
            self.var_vip.set(True)
        elif "privateServerLinkCode=" in job_id:
            join_vip = True
            self.var_vip.set(True)
        elif job_id.upper().startswith("VIP:"):
            job_id = job_id[4:].strip()
            join_vip = True
            self.var_job.set(job_id)
            self.var_vip.set(True)

        if follow_user:
            return {
                "place_text": place_text,
                "place_id": 0,
                "job_id": "",
                "follow_user": True,
                "join_vip": False,
                "shuffle": False,
            }

        place_id = extract_place_id(place_text)
        if place_id <= 0:
            raise RuntimeError("Place ID is empty or invalid")
        return {
            "place_text": place_text,
            "place_id": place_id,
            "job_id": job_id,
            "follow_user": False,
            "join_vip": join_vip,
            "shuffle": bool(self.var_shuffle.get()),
        }

    def launch_selected(self) -> None:
        accounts = self.selected_accounts()
        if not accounts:
            messagebox.showwarning(APP_NAME, "Select at least one account")
            return
        try:
            target = self.build_launch_target()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        self.apply_settings()
        self.launch_stop_event.clear()

        def worker() -> None:
            launched = 0
            for account in accounts:
                if self.launch_stop_event.is_set():
                    break
                try:
                    self.launch_account_with_target(account, target)
                    launched += 1
                except Exception as exc:
                    self.log(f"Launch failed for {account.display_name}: {exc}")

                delay = max(safe_int(self.current_settings().get("launch_delay"), 0), 0)
                if delay > 0 and launched < len(accounts):
                    for _ in range(delay):
                        if self.launch_stop_event.is_set():
                            break
                        time.sleep(1)

            self.store.save_accounts()
            self.root.after(0, self.refresh_accounts)
            self.root.after(0, self.refresh_games)
            self.root.after(0, lambda: self.status.set(f"Launch queue finished ({launched}/{len(accounts)})"))

        threading.Thread(target=worker, name="LaunchQueue", daemon=True).start()

    def launch_account_with_target(self, account: Account, target: Dict[str, Any]) -> None:
        place_id = safe_int(target.get("place_id"))
        follow_user = bool(target.get("follow_user"))
        job_id = str(target.get("job_id") or "")
        if follow_user:
            place_id = self.api.get_user_id(str(target.get("place_text") or ""))
            presence = self.api.get_presence(place_id)
            if presence and safe_int(presence.get("userPresenceType")) != 2:
                self.log(f"Follow target {place_id} does not look in-game; launching anyway")

        settings = self.current_settings()
        if settings.get("auto_close_last_process", True):
            tracker = account.ensure_tracker()
            self.watcher.suppress_auto_rejoin(tracker)
            close_existing_processes(tracker, self.log)

        self.api.launch_account(
            account=account,
            place_id=place_id,
            job_id=job_id,
            follow_user=follow_user,
            join_vip=bool(target.get("join_vip")),
            shuffle=bool(target.get("shuffle")),
            shuffle_pages=max(safe_int(settings.get("shuffle_page_count"), 3), 1),
        )
        if not follow_user:
            self.store.add_recent(place_id, job_id)
        self.store.save_accounts()
        self.root.after(0, self.refresh_accounts)

    def stop_launch_queue(self) -> None:
        self.launch_stop_event.set()
        self.status.set("Stopping launch queue")

    def launch_last_from_watcher(self, account: Account, reason: str) -> None:
        launch = account.last_launch
        self.log(f"Auto rejoining {account.display_name}: {reason}")
        self.api.launch_account(
            account=account,
            place_id=launch.place_id,
            job_id=launch.job_id,
            follow_user=launch.follow_user,
            join_vip=launch.join_vip,
            shuffle=False,
            shuffle_pages=max(safe_int(self.current_settings().get("shuffle_page_count"), 3), 1),
        )
        self.store.save_accounts()
        self.root.after(0, self.refresh_accounts)

    def add_current_favorite(self) -> None:
        try:
            target = self.build_launch_target()
            if target["follow_user"]:
                messagebox.showwarning(APP_NAME, "Favorites are for games, not follow-user launches")
                return
            place_id = safe_int(target["place_id"])
            job_id = str(target.get("job_id") or "")
            name = simpledialog.askstring("Favorite", "Name:", initialvalue=str(place_id))
            if not name:
                return
            self.store.favorites.append({"name": name, "place_id": place_id, "job_id": job_id})
            self.store.save_games()
            self.refresh_games()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def selected_game_data(self, tree: ttk.Treeview) -> Optional[Dict[str, Any]]:
        selected = tree.selection()
        if not selected:
            return None
        values = tree.item(selected[0], "values")
        if len(values) < 3:
            return None
        return {"name": values[0], "place_id": safe_int(values[1]), "job_id": str(values[2] or "")}

    def load_selected_game(self, tree: ttk.Treeview) -> None:
        game = self.selected_game_data(tree)
        if not game:
            return
        self.var_place.set(str(game["place_id"]))
        self.var_job.set(game["job_id"])
        self.var_vip.set(bool(game["job_id"] and "privateServerLinkCode" in game["job_id"]))
        self.status.set(f"Loaded game {game['name']}")

    def remove_favorite(self) -> None:
        selected = self.favorite_tree.selection()
        if not selected:
            return
        index = safe_int(selected[0].lstrip("f"), -1)
        if 0 <= index < len(self.store.favorites):
            self.store.favorites.pop(index)
            self.store.save_games()
            self.refresh_games()

    def clear_recent(self) -> None:
        if not messagebox.askyesno(APP_NAME, "Clear recent games?"):
            return
        self.store.recent.clear()
        self.store.save_games()
        self.refresh_games()

    def run_background(self, work: Callable[[], Any], done: Optional[Callable[[Any], None]] = None) -> None:
        def runner() -> None:
            try:
                result = work()
                if done:
                    self.root.after(0, lambda value=result: done(value))
            except Exception as exc:
                self.root.after(0, lambda error=exc: messagebox.showerror(APP_NAME, str(error)))

        threading.Thread(target=runner, daemon=True).start()

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(line)

        def append() -> None:
            if not hasattr(self, "log_text"):
                return
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, line + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
            self.status.set(message)

        if hasattr(self, "root"):
            self.root.after(0, append)

    def on_close(self) -> None:
        self.apply_settings(save=True)
        self.store.save_accounts()
        self.store.save_games()
        self.watcher.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    RAMApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
