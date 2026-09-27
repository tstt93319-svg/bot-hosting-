import os
import sys
import json
import time
import signal
import sqlite3
import subprocess
import threading
import ast
import shutil
import hashlib
import re
from pathlib import Path
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from concurrent.futures import ThreadPoolExecutor

import requests


# ============================================================
# KRUTIK CYBER EXPERT
# MULTI CLIENT TELEGRAM PYTHON HOSTING MANAGER
# VERSION 7.0
# ============================================================

APP_NAME = "KRUTIK CYBER EXPERT"
VERSION = "7.0"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_CHAT_ID_RAW = os.getenv("OWNER_CHAT_ID", "").strip()

try:
    OWNER_CHAT_ID = int(OWNER_CHAT_ID_RAW)
except Exception:
    OWNER_CHAT_ID = 0


BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(
    os.getenv(
        "DATA_DIR",
        str(BASE_DIR / "host_data")
    )
).expanduser()

CLIENTS_DIR = DATA_DIR / "clients"
DB_FILE = DATA_DIR / "hosting.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CLIENTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# RUNTIME
# ============================================================

processes = {}
process_lock = threading.RLock()

# Bot IDs intentionally stopped.
# Watcher checks this before auto restart.
stop_requested = set()

# One operation lock per bot.
bot_operation_locks = {}
bot_operation_locks_lock = threading.RLock()

# Owner input mode:
# None
# "grant"
# "revoke"
owner_input_mode = None
owner_input_lock = threading.RLock()

telegram_offset = 0

telegram_session = requests.Session()

MAX_LOG_CHARS = 12000


# ============================================================
# IMPORT -> PYPI PACKAGE MAP
# ============================================================

IMPORT_TO_PACKAGE = {
    "telegram": "python-telegram-bot==22.5",
    "telegram.ext": "python-telegram-bot==22.5",

    "openai": "openai>=1.50.0,<2",

    "requests": "requests>=2.31.0",
    "httpx": "httpx",
    "aiohttp": "aiohttp",

    "flask": "Flask",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",

    "bs4": "beautifulsoup4",
    "PIL": "Pillow",
    "cv2": "opencv-python",

    "dotenv": "python-dotenv",
    "yaml": "PyYAML",

    "Crypto": "pycryptodome",

    "numpy": "numpy",
    "pandas": "pandas",

    "qrcode": "qrcode",
    "schedule": "schedule",
    "rich": "rich",
    "colorama": "colorama",

    "selenium": "selenium",
    "jwt": "PyJWT",

    "google": "google-api-python-client",

    "discord": "discord.py",

    "psutil": "psutil",

    "dateutil": "python-dateutil",
    "sklearn": "scikit-learn",
    "matplotlib": "matplotlib",
    "bs4": "beautifulsoup4",
}


# ============================================================
# STANDARD LIBRARY
# ============================================================

STDLIB_MODULES = set(
    getattr(sys, "stdlib_module_names", set())
)

STDLIB_MODULES.update({
    "os",
    "sys",
    "re",
    "json",
    "time",
    "math",
    "random",
    "datetime",
    "calendar",
    "sqlite3",
    "subprocess",
    "threading",
    "signal",
    "pathlib",
    "typing",
    "asyncio",
    "logging",
    "traceback",
    "collections",
    "itertools",
    "functools",
    "statistics",
    "hashlib",
    "secrets",
    "uuid",
    "base64",
    "urllib",
    "http",
    "email",
    "socket",
    "ssl",
    "csv",
    "io",
    "tempfile",
    "shutil",
    "zipfile",
    "glob",
    "inspect",
    "dataclasses",
    "enum",
    "argparse",
    "configparser",
    "copy",
    "pickle",
    "struct",
    "string",
    "textwrap",
    "warnings",
    "platform",
    "timeit",
    "decimal",
    "fractions",
    "functools",
    "operator",
    "weakref",
    "queue",
    "concurrent",
    "multiprocessing",
    "unittest",
})


# ============================================================
# DATABASE
# ============================================================

db_lock = threading.RLock()


def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def now():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def init_db():
    with db_lock:
        conn = get_db()

        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT '',
                    last_seen TEXT DEFAULT ''
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS bots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_chat_id INTEGER NOT NULL,
                    name TEXT DEFAULT '',
                    filename TEXT DEFAULT '',
                    folder TEXT DEFAULT '',
                    status TEXT DEFAULT 'stopped',
                    pid INTEGER DEFAULT 0,
                    auto_restart INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS hosting_access (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS global_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT DEFAULT ''
                )
            """)

            conn.execute("""
                INSERT OR IGNORE INTO global_settings
                (key, value)
                VALUES ('global_client_lock', '0')
            """)

            conn.commit()

        finally:
            conn.close()


def is_global_client_lock():
    with db_lock:
        conn = get_db()

        try:
            row = conn.execute("""
                SELECT value
                FROM global_settings
                WHERE key=?
            """, (
                "global_client_lock",
            )).fetchone()

            if not row:
                return False

            return str(row["value"]) == "1"

        finally:
            conn.close()


def set_global_client_lock(locked):
    with db_lock:
        conn = get_db()

        try:
            conn.execute("""
                INSERT INTO global_settings
                (key, value)
                VALUES (?, ?)
                ON CONFLICT(key)
                DO UPDATE SET value=excluded.value
            """, (
                "global_client_lock",
                "1" if locked else "0"
            ))

            conn.commit()

        finally:
            conn.close()


# ============================================================
# TELEGRAM
# ============================================================

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def telegram(method, data=None, timeout=60):
    try:
        response = telegram_session.post(
            f"{TG_API}/{method}",
            data=data or {},
            timeout=timeout
        )

        if not response.ok:
            print(
                f"[TELEGRAM HTTP {response.status_code}] "
                f"{method}: {response.text[:500]}"
            )
            return None

        try:
            return response.json()
        except Exception:
            return None

    except Exception as e:
        print(
            f"[TELEGRAM ERROR] {method}: {e}"
        )
        return None


def send_message(
    chat_id,
    text,
    reply_markup=None
):
    data = {
        "chat_id": int(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if reply_markup is not None:
        data["reply_markup"] = json.dumps(
            reply_markup,
            ensure_ascii=False
        )

    return telegram(
        "sendMessage",
        data,
        timeout=30
    )


def edit_message(
    chat_id,
    message_id,
    text,
    reply_markup=None
):
    data = {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if reply_markup is not None:
        data["reply_markup"] = json.dumps(
            reply_markup,
            ensure_ascii=False
        )

    return telegram(
        "editMessageText",
        data,
        timeout=30
    )


def answer_callback(
    callback_id,
    text=""
):
    if not callback_id:
        return

    return telegram(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_id,
            "text": str(text)[:190],
            "show_alert": False
        },
        timeout=20
    )


def escape_html(value):
    if value is None:
        return ""

    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def send_long_message(
    chat_id,
    text,
    reply_markup=None
):
    limit = 3800

    if len(text) <= limit:
        return send_message(
            chat_id,
            text,
            reply_markup
        )

    chunks = [
        text[i:i + limit]
        for i in range(
            0,
            len(text),
            limit
        )
    ]

    for index, chunk in enumerate(chunks):

        send_message(
            chat_id,
            chunk,
            reply_markup
            if index == len(chunks) - 1
            else None
        )


# ============================================================
# USER
# ============================================================

def user_info(user):
    return {
        "chat_id": int(user.get("id", 0)),
        "username": user.get("username", "") or "",
        "first_name": user.get("first_name", "") or "",
        "last_name": user.get("last_name", "") or ""
    }


def display_username(row):
    if not row:
        return "Unknown"

    username = row["username"] or ""

    if username:
        return "@" + str(username)

    name = (
        f"{row['first_name'] or ''} "
        f"{row['last_name'] or ''}"
    ).strip()

    return name or str(row["chat_id"])


def is_owner(chat_id):
    try:
        return int(chat_id) == OWNER_CHAT_ID
    except Exception:
        return False


# ============================================================
# HOSTING ACCESS
# ============================================================

def has_hosting_access(chat_id):
    chat_id = int(chat_id)

    if is_owner(chat_id):
        return True

    if is_global_client_lock():
        return False

    with db_lock:
        conn = get_db()

        try:
            row = conn.execute("""
                SELECT enabled
                FROM hosting_access
                WHERE chat_id=?
            """, (
                chat_id,
            )).fetchone()

            return bool(
                row and
                int(row["enabled"]) == 1
            )

        finally:
            conn.close()


def grant_hosting_access_by_id(
    chat_id,
    username="",
    first_name="",
    last_name=""
):
    chat_id = int(chat_id)

    with db_lock:
        conn = get_db()

        try:
            conn.execute("""
                INSERT INTO hosting_access
                (
                    chat_id,
                    username,
                    first_name,
                    last_name,
                    enabled,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 1, ?, ?)

                ON CONFLICT(chat_id)
                DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    enabled=1,
                    updated_at=excluded.updated_at
            """, (
                chat_id,
                username,
                first_name,
                last_name,
                now(),
                now()
            ))

            conn.commit()

        finally:
            conn.close()


def revoke_hosting_access(chat_id):
    chat_id = int(chat_id)

    if is_owner(chat_id):
        return False

    with db_lock:
        conn = get_db()

        try:
            cursor = conn.execute("""
                UPDATE hosting_access
                SET enabled=0,
                    updated_at=?
                WHERE chat_id=?
            """, (
                now(),
                chat_id
            ))

            conn.commit()

            return cursor.rowcount > 0

        finally:
            conn.close()


def get_access_users():
    with db_lock:
        conn = get_db()

        try:
            return conn.execute("""
                SELECT *
                FROM hosting_access
                ORDER BY updated_at DESC
            """).fetchall()

        finally:
            conn.close()


# ============================================================
# CLIENT DATABASE
# ============================================================

def upsert_client(user):
    info = user_info(user)

    if not info["chat_id"]:
        return False

    created = False

    with db_lock:
        conn = get_db()

        try:
            existing = conn.execute("""
                SELECT chat_id
                FROM clients
                WHERE chat_id=?
            """, (
                info["chat_id"],
            )).fetchone()

            if existing is None:

                created = True

                conn.execute("""
                    INSERT INTO clients
                    (
                        chat_id,
                        username,
                        first_name,
                        last_name,
                        enabled,
                        created_at,
                        last_seen
                    )
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                """, (
                    info["chat_id"],
                    info["username"],
                    info["first_name"],
                    info["last_name"],
                    now(),
                    now()
                ))

            else:

                conn.execute("""
                    UPDATE clients
                    SET username=?,
                        first_name=?,
                        last_name=?,
                        last_seen=?
                    WHERE chat_id=?
                """, (
                    info["username"],
                    info["first_name"],
                    info["last_name"],
                    now(),
                    info["chat_id"]
                ))

            conn.commit()

        finally:
            conn.close()

    if created and not is_owner(
        info["chat_id"]
    ):
        notify_owner_new_client(info)

    return created


def get_client(chat_id):
    with db_lock:
        conn = get_db()

        try:
            return conn.execute("""
                SELECT *
                FROM clients
                WHERE chat_id=?
            """, (
                int(chat_id),
            )).fetchone()

        finally:
            conn.close()


def get_clients():
    with db_lock:
        conn = get_db()

        try:
            return conn.execute("""
                SELECT *
                FROM clients
                ORDER BY created_at DESC
            """).fetchall()

        finally:
            conn.close()


def set_client_enabled(
    chat_id,
    enabled
):
    chat_id = int(chat_id)

    with db_lock:
        conn = get_db()

        try:
            conn.execute("""
                UPDATE clients
                SET enabled=?
                WHERE chat_id=?
            """, (
                1 if enabled else 0,
                chat_id
            ))

            conn.commit()

        finally:
            conn.close()

    if not enabled:
        stop_all_bots_of_client(
            chat_id
        )


# ============================================================
# BOT DATABASE
# ============================================================

def create_bot(
    owner_chat_id,
    name,
    filename,
    folder
):
    with db_lock:
        conn = get_db()

        try:
            cursor = conn.execute("""
                INSERT INTO bots
                (
                    owner_chat_id,
                    name,
                    filename,
                    folder,
                    status,
                    pid,
                    auto_restart,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'stopped', 0, 1, ?, ?)
            """, (
                int(owner_chat_id),
                name,
                filename,
                folder,
                now(),
                now()
            ))

            conn.commit()

            return int(cursor.lastrowid)

        finally:
            conn.close()


def get_bot(bot_id):
    with db_lock:
        conn = get_db()

        try:
            return conn.execute("""
                SELECT *
                FROM bots
                WHERE id=?
            """, (
                int(bot_id),
            )).fetchone()

        finally:
            conn.close()


def get_bots(owner_chat_id=None):
    with db_lock:
        conn = get_db()

        try:

            if owner_chat_id is None:

                return conn.execute("""
                    SELECT *
                    FROM bots
                    ORDER BY id DESC
                """).fetchall()

            return conn.execute("""
                SELECT *
                FROM bots
                WHERE owner_chat_id=?
                ORDER BY id DESC
            """, (
                int(owner_chat_id),
            )).fetchall()

        finally:
            conn.close()


def update_bot(
    bot_id,
    **fields
):
    allowed = {
        "name",
        "filename",
        "folder",
        "status",
        "pid",
        "auto_restart",
        "updated_at"
    }

    clean = {}

    for key, value in fields.items():

        if key in allowed:
            clean[key] = value

    if not clean:
        return

    clean["updated_at"] = now()

    assignments = []
    values = []

    for key, value in clean.items():

        assignments.append(
            f"{key}=?"
        )

        values.append(value)

    values.append(int(bot_id))

    with db_lock:
        conn = get_db()

        try:
            conn.execute(
                f"""
                UPDATE bots
                SET {", ".join(assignments)}
                WHERE id=?
                """,
                values
            )

            conn.commit()

        finally:
            conn.close()


# ============================================================
# BOT OPERATION LOCK
# ============================================================

def get_bot_operation_lock(bot_id):
    bot_id = int(bot_id)

    with bot_operation_locks_lock:

        lock = bot_operation_locks.get(
            bot_id
        )

        if lock is None:

            lock = threading.RLock()

            bot_operation_locks[
                bot_id
            ] = lock

        return lock


# ============================================================
# FILE PATHS
# ============================================================

def client_folder(chat_id):
    folder = (
        CLIENTS_DIR /
        str(int(chat_id))
    )

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    return folder


def bot_script(row):
    return (
        Path(row["folder"]) /
        row["filename"]
    )


def bot_log_file(row):
    return (
        Path(row["folder"]) /
        "bot.log"
    )


def safe_filename(filename):
    filename = Path(
        str(filename)
    ).name

    filename = re.sub(
        r"[^A-Za-z0-9._-]",
        "_",
        filename
    )

    if not filename:
        filename = "main.py"

    if not filename.lower().endswith(".py"):
        filename += ".py"

    return filename


# ============================================================
# LOGGING
# ============================================================

def write_log(
    path,
    text
):
    try:

        path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        with path.open(
            "a",
            encoding="utf-8",
            errors="ignore"
        ) as file:

            file.write(
                str(text)
            )

    except Exception as e:

        print(
            f"[LOG ERROR] {e}"
        )


def read_log(
    row,
    max_chars=MAX_LOG_CHARS
):
    path = bot_log_file(
        row
    )

    if not path.exists():
        return "No logs available."

    try:

        data = path.read_text(
            encoding="utf-8",
            errors="ignore"
        )

        if len(data) > max_chars:

            data = (
                "… older logs trimmed …\n\n"
                + data[-max_chars:]
            )

        return data or "Log is empty."

    except Exception as e:

        return (
            f"Log error: {e}"
        )


# ============================================================
# IMPORT / DEPENDENCY DETECTION
# ============================================================

def detect_imports(script):
    modules = set()

    source = script.read_text(
        encoding="utf-8",
        errors="ignore"
    )

    tree = ast.parse(
        source,
        filename=str(script)
    )

    for node in ast.walk(tree):

        if isinstance(
            node,
            ast.Import
        ):

            for alias in node.names:

                modules.add(
                    alias.name.split(".")[0]
                )

        elif isinstance(
            node,
            ast.ImportFrom
        ):

            if node.module:

                modules.add(
                    node.module.split(".")[0]
                )

    return modules


def detect_packages(script):
    modules = detect_imports(
        script
    )

    packages = set()

    local_name = script.stem

    for module in modules:

        if not module:
            continue

        if module == local_name:
            continue

        if module in STDLIB_MODULES:
            continue

        package = IMPORT_TO_PACKAGE.get(
            module
        )

        if package:
            packages.add(package)

        else:
            # Best-effort fallback.
            # Unknown import may equal PyPI name.
            packages.add(module)

    return sorted(packages)


# ============================================================
# VENV
# ============================================================

def venv_python(folder):
    folder = Path(folder)

    if os.name == "nt":

        return (
            folder /
            ".venv" /
            "Scripts" /
            "python.exe"
        )

    return (
        folder /
        ".venv" /
        "bin" /
        "python"
    )


def ensure_venv(
    folder,
    log_file
):
    python = venv_python(
        folder
    )

    if python.exists():
        return python

    write_log(
        log_file,
        "\n[HOST] Creating virtual environment...\n"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "venv",
            str(
                Path(folder) /
                ".venv"
            )
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=300
    )

    write_log(
        log_file,
        result.stdout or ""
    )

    if result.returncode != 0:

        raise RuntimeError(
            "Virtual environment creation failed."
        )

    return python


def calculate_dependency_hash(
    script,
    packages
):
    try:
        source = script.read_bytes()
    except Exception:
        source = b""

    value = (
        source
        + b"\n"
        + "\n".join(
            packages
        ).encode(
            "utf-8"
        )
    )

    return hashlib.sha256(
        value
    ).hexdigest()


def install_dependencies(
    folder,
    script,
    log_file
):
    packages = detect_packages(
        script
    )

    marker = (
        Path(folder) /
        ".dependencies_hash"
    )

    current_hash = calculate_dependency_hash(
        script,
        packages
    )

    if marker.exists():

        try:

            old_hash = marker.read_text(
                encoding="utf-8"
            ).strip()

            if old_hash == current_hash:

                write_log(
                    log_file,
                    "[HOST] Dependencies already prepared.\n"
                )

                return

        except Exception:
            pass

    python = ensure_venv(
        folder,
        log_file
    )

    if not packages:

        write_log(
            log_file,
            "[HOST] No external packages detected.\n"
        )

        marker.write_text(
            current_hash,
            encoding="utf-8"
        )

        return

    write_log(
        log_file,
        (
            "\n[HOST] Dependencies:\n"
            +
            "\n".join(
                f"  - {package}"
                for package in packages
            )
            +
            "\n"
        )
    )

    write_log(
        log_file,
        "[HOST] Installing dependencies...\n"
    )

    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *packages
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=1200
    )

    write_log(
        log_file,
        result.stdout or ""
    )

    if result.returncode != 0:

        raise RuntimeError(
            "Dependency installation failed."
        )

    marker.write_text(
        current_hash,
        encoding="utf-8"
    )


# ============================================================
# PROCESS
# ============================================================

def process_alive(process):
    try:

        return (
            process is not None
            and process.poll() is None
        )

    except Exception:

        return False


def terminate_process(
    process
):
    if not process:
        return

    try:

        if os.name == "posix":

            try:

                os.killpg(
                    os.getpgid(
                        process.pid
                    ),
                    signal.SIGTERM
                )

            except ProcessLookupError:
                pass

            except Exception:

                try:
                    process.terminate()
                except Exception:
                    pass

        else:

            try:
                process.terminate()
            except Exception:
                pass

    except Exception as e:

        print(
            f"[TERMINATE ERROR] {e}"
        )


def kill_process(
    process
):
    if not process:
        return

    try:

        if os.name == "posix":

            try:

                os.killpg(
                    os.getpgid(
                        process.pid
                    ),
                    signal.SIGKILL
                )

            except ProcessLookupError:
                pass

            except Exception:

                try:
                    process.kill()
                except Exception:
                    pass

        else:

            try:
                process.kill()
            except Exception:
                pass

    except Exception as e:

        print(
            f"[KILL ERROR] {e}"
        )


# ============================================================
# START BOT
# ============================================================

def start_bot(
    bot_id
):
    bot_id = int(bot_id)

    operation_lock = get_bot_operation_lock(
        bot_id
    )

    with operation_lock:

        row = get_bot(
            bot_id
        )

        if not row:

            return False, "Bot not found."

        owner_id = int(
            row["owner_chat_id"]
        )

        client = get_client(
            owner_id
        )

        if client and not bool(
            client["enabled"]
        ):

            return (
                False,
                "Client hosting is disabled."
            )

        folder = Path(
            row["folder"]
        )

        script = bot_script(
            row
        )

        if not script.exists():

            update_bot(
                bot_id,
                status="error",
                pid=0
            )

            return (
                False,
                "Python file not found."
            )

        with process_lock:

            current = processes.get(
                bot_id
            )

            if current and process_alive(
                current
            ):

                return (
                    False,
                    "Bot is already running."
                )

            stop_requested.discard(
                bot_id
            )

        log_file = bot_log_file(
            row
        )

        write_log(
            log_file,
            (
                "\n"
                + "=" * 60
                + "\n"
                + f"[HOST] STARTING BOT #{bot_id}\n"
                + f"[HOST] Time: {now()}\n"
                + "=" * 60
                + "\n"
            )
        )

        try:

            source = script.read_text(
                encoding="utf-8",
                errors="ignore"
            )

            compile(
                source,
                str(script),
                "exec"
            )

            python = ensure_venv(
                folder,
                log_file
            )

            install_dependencies(
                folder,
                script,
                log_file
            )

            output = open(
                log_file,
                "a",
                encoding="utf-8",
                errors="ignore"
            )

            env = os.environ.copy()

            env["PYTHONUNBUFFERED"] = "1"

            command = [
                str(python),
                str(script)
            ]

            if os.name == "posix":

                process = subprocess.Popen(
                    command,
                    cwd=str(folder),
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True
                )

            else:

                process = subprocess.Popen(
                    command,
                    cwd=str(folder),
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env
                )

            # Parent no longer needs the descriptor.
            try:
                output.close()
            except Exception:
                pass

            with process_lock:

                processes[
                    bot_id
                ] = process

                stop_requested.discard(
                    bot_id
                )

            update_bot(
                bot_id,
                status="running",
                pid=process.pid
            )

            write_log(
                log_file,
                (
                    f"[HOST] PID: {process.pid}\n"
                    "[HOST] Bot started successfully.\n\n"
                )
            )

            threading.Thread(
                target=watch_process,
                args=(
                    bot_id,
                    process
                ),
                daemon=True
            ).start()

            return (
                True,
                f"Bot started. PID: {process.pid}"
            )

        except Exception as e:

            write_log(
                log_file,
                (
                    "\n[HOST ERROR]\n"
                    f"{repr(e)}\n"
                )
            )

            update_bot(
                bot_id,
                status="error",
                pid=0
            )

            return (
                False,
                str(e)
            )


# ============================================================
# WATCHER
# ============================================================

def watch_process(
    bot_id,
    process
):
    try:

        exit_code = process.wait()

    except Exception as e:

        print(
            f"[WATCHER ERROR] #{bot_id}: {e}"
        )

        exit_code = -1

    row = get_bot(
        bot_id
    )

    if row:

        write_log(
            bot_log_file(row),
            (
                "\n"
                f"[HOST] Process exited. "
                f"Code={exit_code}\n"
                f"[HOST] {now()}\n"
            )
        )

    with process_lock:

        same_process = (
            processes.get(bot_id)
            is process
        )

        if same_process:

            processes.pop(
                bot_id,
                None
            )

        intentional = (
            bot_id in stop_requested
        )

    row = get_bot(
        bot_id
    )

    if not row:

        with process_lock:
            stop_requested.discard(
                bot_id
            )

        return

    if intentional:

        update_bot(
            bot_id,
            status="stopped",
            pid=0
        )

        with process_lock:
            stop_requested.discard(
                bot_id
            )

        return

    client = get_client(
        int(row["owner_chat_id"])
    )

    if client and not bool(
        client["enabled"]
    ):

        update_bot(
            bot_id,
            status="stopped",
            pid=0
        )

        return

    if bool(row["auto_restart"]):

        update_bot(
            bot_id,
            status="restarting",
            pid=0
        )

        time.sleep(2)

        with process_lock:

            if bot_id in stop_requested:

                stop_requested.discard(
                    bot_id
                )

                update_bot(
                    bot_id,
                    status="stopped",
                    pid=0
                )

                return

        success, message = start_bot(
            bot_id
        )

        if not success:

            update_bot(
                bot_id,
                status="error",
                pid=0
            )

            print(
                f"[AUTO RESTART FAILED] "
                f"#{bot_id}: {message}"
            )

    else:

        update_bot(
            bot_id,
            status="stopped",
            pid=0
        )


# ============================================================
# STOP BOT
# ============================================================

def stop_bot(
    bot_id,
    intentional=True
):
    bot_id = int(bot_id)

    operation_lock = get_bot_operation_lock(
        bot_id
    )

    with operation_lock:

        row = get_bot(
            bot_id
        )

        if not row:

            return False, "Bot not found."

        if intentional:

            # IMPORTANT:
            # Do NOT modify auto_restart.
            # User preference is preserved.
            with process_lock:

                stop_requested.add(
                    bot_id
                )

        with process_lock:

            process = processes.get(
                bot_id
            )

        if process and process_alive(
            process
        ):

            terminate_process(
                process
            )

            deadline = (
                time.time() + 6
            )

            while (
                time.time() < deadline
            ):

                if not process_alive(
                    process
                ):
                    break

                time.sleep(
                    0.15
                )

            if process_alive(
                process
            ):

                kill_process(
                    process
                )

            try:

                process.wait(
                    timeout=3
                )

            except Exception:
                pass

        with process_lock:

            if processes.get(
                bot_id
            ) is process:

                processes.pop(
                    bot_id,
                    None
                )

        update_bot(
            bot_id,
            status="stopped",
            pid=0
        )

        if intentional:

            # Keep the flag briefly so watcher cannot race
            # and restart the process.
            def clear_stop_flag():
                time.sleep(3)

                with process_lock:
                    stop_requested.discard(
                        bot_id
                    )

            threading.Thread(
                target=clear_stop_flag,
                daemon=True
            ).start()

        return True, "Bot stopped."


# ============================================================
# RESTART BOT
# ============================================================

def restart_bot(
    bot_id
):
    bot_id = int(bot_id)

    operation_lock = get_bot_operation_lock(
        bot_id
    )

    with operation_lock:

        row = get_bot(
            bot_id
        )

        if not row:

            return False, "Bot not found."

        # Remember existing auto restart preference.
        old_auto_restart = bool(
            row["auto_restart"]
        )

        with process_lock:

            stop_requested.add(
                bot_id
            )

            process = processes.get(
                bot_id
            )

        if process and process_alive(
            process
        ):

            terminate_process(
                process
            )

            deadline = (
                time.time() + 6
            )

            while (
                time.time() < deadline
            ):

                if not process_alive(
                    process
                ):
                    break

                time.sleep(
                    0.15
                )

            if process_alive(
                process
            ):

                kill_process(
                    process
                )

            try:

                process.wait(
                    timeout=3
                )

            except Exception:
                pass

        with process_lock:

            processes.pop(
                bot_id,
                None
            )

            stop_requested.discard(
                bot_id
            )

        update_bot(
            bot_id,
            status="stopped",
            pid=0,
            auto_restart=(
                1
                if old_auto_restart
                else 0
            )
        )

        time.sleep(
            0.2
        )

        return start_bot(
            bot_id
        )


# ============================================================
# STOP ALL
# ============================================================

def stop_all_bots():
    rows = get_bots()

    bot_ids = [
        int(row["id"])
        for row in rows
    ]

    # Mark ALL before killing.
    # This prevents watcher race.
    with process_lock:

        for bot_id in bot_ids:

            stop_requested.add(
                bot_id
            )

    def stop_one(bot_id):
        try:

            return stop_bot(
                bot_id,
                intentional=True
            )

        except Exception as e:

            print(
                f"[STOP ALL ERROR] "
                f"#{bot_id}: {e}"
            )

            return False, str(e)

    if bot_ids:

        workers = min(
            16,
            max(1, len(bot_ids))
        )

        with ThreadPoolExecutor(
            max_workers=workers
        ) as executor:

            results = list(
                executor.map(
                    stop_one,
                    bot_ids
                )
            )

    else:

        results = []

    stopped = sum(
        1
        for result in results
        if result and result[0]
    )

    # Final database reconciliation.
    for bot_id in bot_ids:

        update_bot(
            bot_id,
            status="stopped",
            pid=0
        )

    with process_lock:

        for bot_id in bot_ids:

            process = processes.get(
                bot_id
            )

            if process and process_alive(
                process
            ):

                try:
                    kill_process(
                        process
                    )
                except Exception:
                    pass

            processes.pop(
                bot_id,
                None
            )

    # Clear later, not immediately.
    def cleanup_flags():

        time.sleep(4)

        with process_lock:

            for bot_id in bot_ids:

                stop_requested.discard(
                    bot_id
                )

    threading.Thread(
        target=cleanup_flags,
        daemon=True
    ).start()

    return stopped


def stop_all_bots_of_client(
    chat_id
):
    rows = get_bots(
        chat_id
    )

    count = 0

    for row in rows:

        success, _ = stop_bot(
            int(row["id"]),
            intentional=True
        )

        if success:
            count += 1

    return count


# ============================================================
# SAFE DELETE
# ============================================================

def safe_delete_folder(
    folder
):
    try:

        target = Path(
            folder
        ).resolve()

        base = CLIENTS_DIR.resolve()

        if target == base:
            raise RuntimeError(
                "Unsafe delete path."
            )

        if base not in target.parents:
            raise RuntimeError(
                "Unsafe delete path."
            )

        if target.exists():
            shutil.rmtree(
                target
            )

        return True

    except Exception as e:

        print(
            f"[DELETE FOLDER ERROR] {e}"
        )

        return False


# ============================================================
# DELETE BOT
# ============================================================

def delete_bot_permanently(
    bot_id
):
    bot_id = int(bot_id)

    operation_lock = get_bot_operation_lock(
        bot_id
    )

    with operation_lock:

        row = get_bot(
            bot_id
        )

        if not row:

            return False, "Bot not found."

        # Stop first.
        stop_bot(
            bot_id,
            intentional=True
        )

        folder = Path(
            row["folder"]
        )

        with process_lock:

            processes.pop(
                bot_id,
                None
            )

            stop_requested.discard(
                bot_id
            )

        with db_lock:

            conn = get_db()

            try:

                conn.execute(
                    "DELETE FROM bots WHERE id=?",
                    (bot_id,)
                )

                conn.commit()

            finally:

                conn.close()

        safe_delete_folder(
            folder
        )

        return (
            True,
            "Bot permanently deleted."
        )


# ============================================================
# DELETE CLIENT
# ============================================================

def delete_client_permanently(
    chat_id
):
    chat_id = int(chat_id)

    if is_owner(chat_id):

        return (
            False,
            "Owner cannot be deleted."
        )

    rows = get_bots(
        chat_id
    )

    for row in rows:

        delete_bot_permanently(
            int(row["id"])
        )

    with db_lock:

        conn = get_db()

        try:

            conn.execute(
                "DELETE FROM bots WHERE owner_chat_id=?",
                (chat_id,)
            )

            conn.execute(
                "DELETE FROM clients WHERE chat_id=?",
                (chat_id,)
            )

            conn.execute(
                "DELETE FROM hosting_access WHERE chat_id=?",
                (chat_id,)
            )

            conn.commit()

        finally:

            conn.close()

    folder = (
        CLIENTS_DIR /
        str(chat_id)
    )

    safe_delete_folder(
        folder
    )

    return (
        True,
        "Client permanently deleted."
    )


# ============================================================
# STATUS
# ============================================================

def bot_icon(row):

    status = str(
        row["status"]
    ).lower()

    if status == "running":
        return "🟢"

    if status == "restarting":
        return "🟡"

    if status == "error":
        return "🔴"

    return "⚪"


def bot_status_text(row):
    return (
        f"{bot_icon(row)} "
        f"{str(row['status']).upper()}"
    )


# ============================================================
# OWNER PANEL
# ============================================================

def owner_panel_keyboard():

    if is_global_client_lock():

        lock_button = {
            "text": "🔓 UNLOCK ALL CLIENTS",
            "callback_data": "unlockall"
        }

    else:

        lock_button = {
            "text": "🔒 LOCK ALL CLIENTS",
            "callback_data": "lockall"
        }

    return {
        "inline_keyboard": [
            [
                {
                    "text": "👥 Clients",
                    "callback_data": "clients"
                },
                {
                    "text": "🤖 All Bots",
                    "callback_data": "allbots"
                }
            ],
            [
                {
                    "text": "🔐 Hosting Access",
                    "callback_data": "access"
                }
            ],
            [
                {
                    "text": "➕ Grant Access",
                    "callback_data": "grantmenu"
                },
                {
                    "text": "🚫 Revoke Access",
                    "callback_data": "revokemenu"
                }
            ],
            [
                lock_button
            ],
            [
                {
                    "text": "🛑 STOP ALL BOTS",
                    "callback_data": "stopall"
                }
            ],
            [
                {
                    "text": "🔄 Refresh",
                    "callback_data": "panel"
                }
            ]
        ]
    }


def show_owner_panel(
    chat_id,
    message_id=None
):
    lock_status = (
        "🔒 GLOBAL CLIENT LOCK: ON"
        if is_global_client_lock()
        else "🟢 GLOBAL CLIENT LOCK: OFF"
    )

    clients = get_clients()
    bots = get_bots()

    running = sum(
        1
        for bot in bots
        if str(bot["status"]) == "running"
    )

    text = (
        f"👑 <b>{APP_NAME}</b>\n\n"
        f"Version: <code>{VERSION}</code>\n"
        f"{lock_status}\n\n"
        f"👥 Clients: <b>{len(clients)}</b>\n"
        f"🤖 Bots: <b>{len(bots)}</b>\n"
        f"🟢 Running: <b>{running}</b>\n"
    )

    keyboard = owner_panel_keyboard()

    if message_id:

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

    else:

        send_message(
            chat_id,
            text,
            keyboard
        )


# ============================================================
# CLIENT PANEL
# ============================================================

def client_panel_keyboard():

    return {
        "inline_keyboard": [
            [
                {
                    "text": "🤖 My Bots",
                    "callback_data": "mybots"
                }
            ],
            [
                {
                    "text": "📤 Upload .py",
                    "callback_data": "uploadhelp"
                }
            ],
            [
                {
                    "text": "📊 Status",
                    "callback_data": "mystatus"
                }
            ],
            [
                {
                    "text": "🔄 Refresh",
                    "callback_data": "panel"
                }
            ]
        ]
    }


def show_client_panel(
    chat_id,
    message_id=None
):
    if is_owner(chat_id):

        show_owner_panel(
            chat_id,
            message_id
        )

        return

    if is_global_client_lock():

        text = (
            f"🔒 <b>{APP_NAME}</b>\n\n"
            "⛔ <b>HOSTING LOCKED</b>\n\n"
            "Owner ne sabhi clients ke hosting "
            "controls temporarily lock kiye hain.\n\n"
            "🔓 Unlock hone ke baad hosting "
            "dobara use ho sakti hai."
        )

        keyboard = {
            "inline_keyboard": []
        }

        if message_id:

            edit_message(
                chat_id,
                message_id,
                text,
                keyboard
            )

        else:

            send_message(
                chat_id,
                text,
                keyboard
            )

        return

    if not has_hosting_access(
        chat_id
    ):

        text = (
            "🔐 <b>Access Denied</b>\n\n"
            "Owner ne abhi aapko hosting access "
            "grant nahi kiya hai."
        )

        if message_id:

            edit_message(
                chat_id,
                message_id,
                text,
                {
                    "inline_keyboard": []
                }
            )

        else:

            send_message(
                chat_id,
                text
            )

        return

    client = get_client(
        chat_id
    )

    bots = get_bots(
        chat_id
    )

    username = (
        display_username(client)
        if client
        else str(chat_id)
    )

    text = (
        f"👤 <b>{escape_html(username)}</b>\n\n"
        f"🤖 Bots: <b>{len(bots)}</b>\n\n"
        "Hosting Panel ready."
    )

    keyboard = client_panel_keyboard()

    if message_id:

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

    else:

        send_message(
            chat_id,
            text,
            keyboard
        )


# ============================================================
# OWNER CLIENT LIST
# ============================================================

def show_clients(
    owner_chat_id,
    message_id=None
):
    if not is_owner(
        owner_chat_id
    ):
        return

    clients = get_clients()

    text = (
        "👥 <b>CLIENTS</b>\n\n"
        "Client select karo:"
    )

    rows = []

    for client in clients:

        status = (
            "🟢"
            if client["enabled"]
            else "🔴"
        )

        name = display_username(
            client
        )

        bot_count = len(
            get_bots(
                int(client["chat_id"])
            )
        )

        rows.append([
            {
                "text": (
                    f"{status} "
                    f"{name[:22]} "
                    f"({bot_count} bots)"
                ),
                "callback_data":
                    f"client:{client['chat_id']}"
            }
        ])

    if not clients:

        text += (
            "\n\nNo clients registered."
        )

    rows.append([
        {
            "text": "⬅️ Owner Panel",
            "callback_data": "panel"
        }
    ])

    keyboard = {
        "inline_keyboard": rows
    }

    if message_id:

        edit_message(
            owner_chat_id,
            message_id,
            text,
            keyboard
        )

    else:

        send_message(
            owner_chat_id,
            text,
            keyboard
        )


# ============================================================
# OWNER CLIENT DETAIL
# ============================================================

def show_owner_client(
    owner_chat_id,
    client_id,
    message_id=None
):
    if not is_owner(
        owner_chat_id
    ):
        return

    client_id = int(
        client_id
    )

    client = get_client(
        client_id
    )

    if not client:

        send_message(
            owner_chat_id,
            "❌ Client not found."
        )

        return

    bots = get_bots(
        client_id
    )

    access = False

    with db_lock:

        conn = get_db()

        try:

            row = conn.execute("""
                SELECT enabled
                FROM hosting_access
                WHERE chat_id=?
            """, (
                client_id,
            )).fetchone()

            access = bool(
                row and
                row["enabled"]
            )

        finally:

            conn.close()

    client_status = (
        "🟢 ENABLED"
        if client["enabled"]
        else "🔴 DISABLED"
    )

    access_status = (
        "🟢 GRANTED"
        if access
        else "🔴 NOT GRANTED"
    )

    text = (
        "👤 <b>CLIENT CONTROL</b>\n\n"
        f"Username: "
        f"<b>{escape_html(display_username(client))}</b>\n"
        f"Chat ID: <code>{client_id}</code>\n"
        f"Client Status: <b>{client_status}</b>\n"
        f"Hosting Access: <b>{access_status}</b>\n"
        f"Bots: <b>{len(bots)}</b>\n\n"
        "🤖 <b>ONLY THIS CLIENT'S BOTS</b>\n"
    )

    if not bots:

        text += (
            "\nNo bots for this client."
        )

    else:

        for bot in bots:

            text += (
                f"\n{bot_status_text(bot)} "
                f"<b>#{bot['id']} "
                f"{escape_html(bot['name'])}</b>"
            )

    rows = []

    for bot in bots:

        rows.append([
            {
                "text": (
                    f"{bot_icon(bot)} "
                    f"#{bot['id']} "
                    f"{bot['name'][:25]}"
                ),
                "callback_data":
                    f"obot:{bot['id']}"
            }
        ])

    rows.append([
        {
            "text": (
                "🚫 Disable Client"
                if client["enabled"]
                else "✅ Enable Client"
            ),
            "callback_data":
                (
                    f"disable:{client_id}"
                    if client["enabled"]
                    else f"enable:{client_id}"
                )
        }
    ])

    rows.append([
        {
            "text": (
                "🚫 Revoke Hosting Access"
                if access
                else "✅ Grant Hosting Access"
            ),
            "callback_data":
                (
                    f"revoke:{client_id}"
                    if access
                    else f"grant:{client_id}"
                )
        }
    ])

    rows.append([
        {
            "text": "🛑 Stop This Client's Bots",
            "callback_data":
                f"stopclient:{client_id}"
        }
    ])

    rows.append([
        {
            "text": "🗑️ Delete Client Permanently",
            "callback_data":
                f"dclient:{client_id}"
        }
    ])

    rows.append([
        {
            "text": "⬅️ Clients",
            "callback_data": "clients"
        }
    ])

    keyboard = {
        "inline_keyboard": rows
    }

    if message_id:

        edit_message(
            owner_chat_id,
            message_id,
            text,
            keyboard
        )

    else:

        send_message(
            owner_chat_id,
            text,
            keyboard
        )


# ============================================================
# OWNER BOT DETAIL
# ============================================================

def show_owner_bot(
    owner_chat_id,
    bot_id,
    message_id=None
):
    if not is_owner(
        owner_chat_id
    ):
        return

    row = get_bot(
        bot_id
    )

    if not row:

        send_message(
            owner_chat_id,
            "❌ Bot not found."
        )

        return

    client = get_client(
        int(row["owner_chat_id"])
    )

    client_name = (
        display_username(client)
        if client
        else str(row["owner_chat_id"])
    )

    text = (
        "🤖 <b>BOT CONTROL</b>\n\n"
        f"Bot ID: <code>{row['id']}</code>\n"
        f"Name: <b>{escape_html(row['name'])}</b>\n"
        f"File: <code>{escape_html(row['filename'])}</code>\n"
        f"Client: <b>{escape_html(client_name)}</b>\n"
        f"Client ID: <code>{row['owner_chat_id']}</code>\n"
        f"Status: <b>{bot_status_text(row)}</b>\n"
        f"PID: <code>{row['pid'] or 0}</code>\n"
        f"Auto Restart: "
        f"<b>{'ON' if row['auto_restart'] else 'OFF'}</b>\n"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "▶️ Start",
                    "callback_data":
                        f"ob:s:{row['id']}"
                },
                {
                    "text": "⏹ Stop",
                    "callback_data":
                        f"ob:t:{row['id']}"
                }
            ],
            [
                {
                    "text": "🔄 Restart",
                    "callback_data":
                        f"ob:r:{row['id']}"
                },
                {
                    "text": "📜 Logs",
                    "callback_data":
                        f"ob:l:{row['id']}"
                }
            ],
            [
                {
                    "text": (
                        "🔴 Disable Auto Restart"
                        if row["auto_restart"]
                        else "🟢 Enable Auto Restart"
                    ),
                    "callback_data":
                        f"ob:a:{row['id']}"
                }
            ],
            [
                {
                    "text": "🗑️ Delete Permanently",
                    "callback_data":
                        f"ob:d:{row['id']}"
                }
            ],
            [
                {
                    "text": "⬅️ Back to Client",
                    "callback_data":
                        f"client:{row['owner_chat_id']}"
                }
            ]
        ]
    }

    if message_id:

        edit_message(
            owner_chat_id,
            message_id,
            text,
            keyboard
        )

    else:

        send_message(
            owner_chat_id,
            text,
            keyboard
        )


# ============================================================
# ALL BOTS
# ============================================================

def show_all_bots(
    owner_chat_id,
    message_id=None
):
    if not is_owner(
        owner_chat_id
    ):
        return

    bots = get_bots()

    text = (
        "🤖 <b>ALL BOTS</b>\n\n"
        f"Total: <b>{len(bots)}</b>\n"
    )

    rows = []

    for bot in bots:

        rows.append([
            {
                "text": (
                    f"{bot_icon(bot)} "
                    f"#{bot['id']} "
                    f"{bot['name'][:28]}"
                ),
                "callback_data":
                    f"obot:{bot['id']}"
            }
        ])

    rows.append([
        {
            "text": "⬅️ Owner Panel",
            "callback_data": "panel"
        }
    ])

    keyboard = {
        "inline_keyboard": rows
    }

    if message_id:

        edit_message(
            owner_chat_id,
            message_id,
            text,
            keyboard
        )

    else:

        send_message(
            owner_chat_id,
            text,
            keyboard
        )


# ============================================================
# CLIENT MY BOTS
# ============================================================

def show_my_bots(
    chat_id,
    message_id=None
):
    if is_global_client_lock():

        show_client_panel(
            chat_id,
            message_id
        )

        return

    if not has_hosting_access(
        chat_id
    ):

        send_message(
            chat_id,
            "❌ Hosting access denied."
        )

        return

    bots = get_bots(
        chat_id
    )

    text = (
        "🤖 <b>MY BOTS</b>\n\n"
    )

    rows = []

    for bot in bots:

        text += (
            f"{bot_status_text(bot)} "
            f"<b>#{bot['id']} "
            f"{escape_html(bot['name'])}</b>\n"
            f"📄 {escape_html(bot['filename'])}\n"
            f"PID: <code>{bot['pid'] or 0}</code>\n\n"
        )

        rows.append([
            {
                "text": (
                    f"{bot_icon(bot)} "
                    f"#{bot['id']} "
                    f"{bot['name'][:28]}"
                ),
                "callback_data":
                    f"cbot:{bot['id']}"
            }
        ])

    if not bots:

        text += (
            "No bots yet.\n\n"
            "📤 Upload a .py file to create one."
        )

    rows.append([
        {
            "text": "📤 Upload .py",
            "callback_data": "uploadhelp"
        }
    ])

    rows.append([
        {
            "text": "⬅️ Panel",
            "callback_data": "panel"
        }
    ])

    keyboard = {
        "inline_keyboard": rows
    }

    if message_id:

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

    else:

        send_message(
            chat_id,
            text,
            keyboard
        )


# ============================================================
# CLIENT BOT DETAIL
# ============================================================

def show_client_bot(
    chat_id,
    bot_id,
    message_id=None
):
    if is_global_client_lock():

        show_client_panel(
            chat_id,
            message_id
        )

        return

    if not has_hosting_access(
        chat_id
    ):

        send_message(
            chat_id,
            "❌ Hosting access denied."
        )

        return

    row = get_bot(
        bot_id
    )

    if not row:

        send_message(
            chat_id,
            "❌ Bot not found."
        )

        return

    if int(
        row["owner_chat_id"]
    ) != int(chat_id):

        send_message(
            chat_id,
            "❌ You can only control your own bots."
        )

        return

    text = (
        "🤖 <b>MY BOT</b>\n\n"
        f"ID: <code>{row['id']}</code>\n"
        f"Name: <b>{escape_html(row['name'])}</b>\n"
        f"File: <code>{escape_html(row['filename'])}</code>\n"
        f"Status: <b>{bot_status_text(row)}</b>\n"
        f"PID: <code>{row['pid'] or 0}</code>\n"
        f"Auto Restart: "
        f"<b>{'ON' if row['auto_restart'] else 'OFF'}</b>"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "▶️ Start",
                    "callback_data":
                        f"cb:s:{row['id']}"
                },
                {
                    "text": "⏹ Stop",
                    "callback_data":
                        f"cb:t:{row['id']}"
                }
            ],
            [
                {
                    "text": "🔄 Restart",
                    "callback_data":
                        f"cb:r:{row['id']}"
                },
                {
                    "text": "📜 Logs",
                    "callback_data":
                        f"cb:l:{row['id']}"
                }
            ],
            [
                {
                    "text": (
                        "🔴 Disable Auto Restart"
                        if row["auto_restart"]
                        else "🟢 Enable Auto Restart"
                    ),
                    "callback_data":
                        f"cb:a:{row['id']}"
                }
            ],
            [
                {
                    "text": "🗑️ Delete Permanently",
                    "callback_data":
                        f"cb:d:{row['id']}"
                }
            ],
            [
                {
                    "text": "⬅️ My Bots",
                    "callback_data": "mybots"
                }
            ]
        ]
    }

    if message_id:

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

    else:

        send_message(
            chat_id,
            text,
            keyboard
        )


# ============================================================
# ACCESS LIST
# ============================================================

def show_access(
    owner_chat_id,
    message_id=None
):
    if not is_owner(
        owner_chat_id
    ):
        return

    users = get_access_users()

    lock_text = (
        "🔒 GLOBAL LOCK ACTIVE"
        if is_global_client_lock()
        else "🟢 GLOBAL LOCK OFF"
    )

    text = (
        "🔐 <b>HOSTING ACCESS</b>\n\n"
        f"{lock_text}\n\n"
    )

    rows = []

    for user in users:

        status = (
            "🟢"
            if user["enabled"]
            else "🔴"
        )

        name = display_username(
            user
        )

        rows.append([
            {
                "text": (
                    f"{status} "
                    f"{name[:30]}"
                ),
                "callback_data":
                    f"accessuser:{user['chat_id']}"
            }
        ])

    if not users:

        text += (
            "No hosting access users."
        )

    rows.append([
        {
            "text": "⬅️ Owner Panel",
            "callback_data": "panel"
        }
    ])

    keyboard = {
        "inline_keyboard": rows
    }

    if message_id:

        edit_message(
            owner_chat_id,
            message_id,
            text,
            keyboard
        )

    else:

        send_message(
            owner_chat_id,
            text,
            keyboard
        )


# ============================================================
# NEW CLIENT NOTIFICATION
# ============================================================

def notify_owner_new_client(
    info
):
    if not OWNER_CHAT_ID:
        return

    username = (
        f"@{info['username']}"
        if info["username"]
        else "No username"
    )

    full_name = (
        f"{info['first_name']} "
        f"{info['last_name']}"
    ).strip()

    text = (
        "🆕 <b>NEW CLIENT</b>\n\n"
        f"👤 Username: "
        f"<b>{escape_html(username)}</b>\n"
        f"📝 Name: "
        f"<b>{escape_html(full_name or 'Unknown')}</b>\n"
        f"🆔 Chat ID: "
        f"<code>{info['chat_id']}</code>\n"
        f"🕒 Time: "
        f"<code>{now()}</code>\n\n"
        "⚠️ Hosting access is not granted automatically."
    )

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Grant Hosting",
                    "callback_data":
                        f"grant:{info['chat_id']}"
                }
            ],
            [
                {
                    "text": "👤 Open Client",
                    "callback_data":
                        f"client:{info['chat_id']}"
                }
            ]
        ]
    }

    send_message(
        OWNER_CHAT_ID,
        text,
        keyboard
    )


# ============================================================
# BOT ACTION EXECUTOR
# ============================================================

def execute_bot_action(
    chat_id,
    bot_id,
    action,
    owner_view
):
    bot_id = int(bot_id)

    row = get_bot(
        bot_id
    )

    if not row:

        send_message(
            chat_id,
            "❌ Bot not found."
        )

        return

    bot_owner = int(
        row["owner_chat_id"]
    )

    if owner_view:

        if not is_owner(chat_id):

            send_message(
                chat_id,
                "❌ Owner only."
            )

            return

    else:

        if is_global_client_lock():

            send_message(
                chat_id,
                "🔒 Hosting is locked by owner."
            )

            return

        if not has_hosting_access(
            chat_id
        ):

            send_message(
                chat_id,
                "❌ Hosting access denied."
            )

            return

        if bot_owner != int(chat_id):

            send_message(
                chat_id,
                "❌ You can only control your own bots."
            )

            return

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    if action == "s":

        def worker():

            success, message = start_bot(
                bot_id
            )

            send_message(
                chat_id,
                (
                    "▶️ <b>START</b>\n\n"
                    + escape_html(message)
                )
            )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

        return

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    if action == "t":

        def worker():

            success, message = stop_bot(
                bot_id,
                intentional=True
            )

            send_message(
                chat_id,
                (
                    "⏹ <b>STOP</b>\n\n"
                    + escape_html(message)
                )
            )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

        return

    # --------------------------------------------------------
    # RESTART
    # --------------------------------------------------------

    if action == "r":

        def worker():

            success, message = restart_bot(
                bot_id
            )

            send_message(
                chat_id,
                (
                    "🔄 <b>RESTART</b>\n\n"
                    + escape_html(message)
                )
            )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

        return

    # --------------------------------------------------------
    # LOGS
    # --------------------------------------------------------

    if action == "l":

        logs = read_log(
            row
        )

        send_long_message(
            chat_id,
            (
                f"📜 <b>LOGS - "
                f"{escape_html(row['name'])}</b>\n\n"
                f"<pre>{escape_html(logs)}</pre>"
            )
        )

        return

    # --------------------------------------------------------
    # AUTO RESTART
    # --------------------------------------------------------

    if action == "a":

        new_value = (
            0
            if bool(row["auto_restart"])
            else 1
        )

        update_bot(
            bot_id,
            auto_restart=new_value
        )

        send_message(
            chat_id,
            (
                "🔄 <b>AUTO RESTART UPDATED</b>\n\n"
                f"Status: "
                f"<b>{'ON' if new_value else 'OFF'}</b>"
            )
        )

        return

    # --------------------------------------------------------
    # DELETE
    # --------------------------------------------------------

    if action == "d":

        warning = (
            "⚠️ <b>PERMANENT DELETE</b>\n\n"
            f"Bot: <b>{escape_html(row['name'])}</b>\n"
            f"ID: <code>{bot_id}</code>\n\n"
            "This will permanently delete:\n"
            "• Python file\n"
            "• .venv\n"
            "• logs\n"
            "• bot folder\n"
            "• database record\n\n"
            "❗ <b>THIS CANNOT BE UNDONE.</b>"
        )

        if owner_view:

            confirm_callback = (
                f"confirmob:{bot_id}"
            )

            cancel_callback = (
                f"obot:{bot_id}"
            )

        else:

            confirm_callback = (
                f"confirmcb:{bot_id}"
            )

            cancel_callback = (
                f"cbot:{bot_id}"
            )

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text":
                            "⚠️ YES, DELETE PERMANENTLY",
                        "callback_data":
                            confirm_callback
                    }
                ],
                [
                    {
                        "text": "❌ Cancel",
                        "callback_data":
                            cancel_callback
                    }
                ]
            ]
        }

        send_message(
            chat_id,
            warning,
            keyboard
        )


# ============================================================
# UPLOAD HELP
# ============================================================

def upload_help(
    chat_id
):
    if is_global_client_lock():

        send_message(
            chat_id,
            "🔒 Hosting is locked by owner."
        )

        return

    if not has_hosting_access(
        chat_id
    ):

        send_message(
            chat_id,
            "❌ Hosting access denied."
        )

        return

    send_message(
        chat_id,
        (
            "📤 <b>UPLOAD PYTHON BOT</b>\n\n"
            "Sirf ek Python file bhejo.\n\n"
            "Example:\n"
            "<code>main.py</code>\n\n"
            "❌ ZIP nahi\n"
            "❌ requirements.txt ki zarurat nahi\n\n"
            "Host Python imports detect karke "
            "known dependencies automatically install karega."
        )
    )


# ============================================================
# DOWNLOAD TELEGRAM FILE
# ============================================================

def download_document(
    chat_id,
    document
):
    if is_global_client_lock():

        send_message(
            chat_id,
            "🔒 Hosting is locked by owner."
        )

        return

    if not has_hosting_access(
        chat_id
    ):

        send_message(
            chat_id,
            "❌ Hosting access denied."
        )

        return

    filename = safe_filename(
        document.get(
            "file_name",
            "main.py"
        )
    )

    if not filename.lower().endswith(
        ".py"
    ):

        send_message(
            chat_id,
            "❌ Sirf .py file allowed hai."
        )

        return

    file_id = document.get(
        "file_id"
    )

    if not file_id:

        send_message(
            chat_id,
            "❌ Telegram file ID missing."
        )

        return

    send_message(
        chat_id,
        "⏳ Python file download ho rahi hai..."
    )

    result = telegram(
        "getFile",
        {
            "file_id": file_id
        },
        timeout=30
    )

    if not result or not result.get(
        "ok"
    ):

        send_message(
            chat_id,
            "❌ Telegram file information nahi mili."
        )

        return

    file_path = (
        result
        .get("result", {})
        .get("file_path")
    )

    if not file_path:

        send_message(
            chat_id,
            "❌ Telegram file path missing."
        )

        return

    try:

        response = telegram_session.get(
            f"https://api.telegram.org/file/"
            f"bot{BOT_TOKEN}/{file_path}",
            timeout=120
        )

        response.raise_for_status()

    except Exception as e:

        send_message(
            chat_id,
            (
                "❌ Download failed:\n"
                f"<code>{escape_html(e)}</code>"
            )
        )

        return

    temp_folder = None
    bot_id = None

    try:

        client_dir = client_folder(
            chat_id
        )

        unique = hashlib.sha256(
            f"{time.time_ns()}".encode()
        ).hexdigest()[:12]

        temp_folder = (
            client_dir /
            f".upload_{unique}"
        )

        temp_folder.mkdir(
            parents=True,
            exist_ok=True
        )

        temp_script = (
            temp_folder /
            filename
        )

        temp_script.write_bytes(
            response.content
        )

        # Syntax validation first.
        source = temp_script.read_text(
            encoding="utf-8",
            errors="ignore"
        )

        compile(
            source,
            str(temp_script),
            "exec"
        )

        bot_name = Path(
            filename
        ).stem

        bot_id = create_bot(
            chat_id,
            bot_name,
            filename,
            str(temp_folder)
        )

        final_folder = (
            client_dir /
            f"bot_{bot_id}"
        )

        temp_folder.rename(
            final_folder
        )

        temp_folder = None

        update_bot(
            bot_id,
            folder=str(final_folder)
        )

        send_message(
            chat_id,
            (
                "✅ <b>BOT CREATED</b>\n\n"
                f"🤖 Bot ID: <code>{bot_id}</code>\n"
                f"📄 File: "
                f"<code>{escape_html(filename)}</code>\n\n"
                "Ab aap ise My Bots se start kar sakte ho."
            ),
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "▶️ Start Bot",
                            "callback_data":
                                f"cb:s:{bot_id}"
                        }
                    ],
                    [
                        {
                            "text": "🤖 My Bots",
                            "callback_data":
                                "mybots"
                        }
                    ]
                ]
            }
        )

    except SyntaxError as e:

        if temp_folder:

            shutil.rmtree(
                temp_folder,
                ignore_errors=True
            )

        send_message(
            chat_id,
            (
                "❌ <b>Python Syntax Error</b>\n\n"
                f"<code>{escape_html(e)}</code>"
            )
        )

    except Exception as e:

        if bot_id:

            try:
                delete_bot_permanently(
                    bot_id
                )
            except Exception:
                pass

        elif temp_folder:

            shutil.rmtree(
                temp_folder,
                ignore_errors=True
            )

        send_message(
            chat_id,
            (
                "❌ <b>Upload Error</b>\n\n"
                f"<code>{escape_html(e)}</code>"
            )
        )


# ============================================================
# OWNER INPUT
# ============================================================

def handle_owner_text_input(
    chat_id,
    text
):
    global owner_input_mode

    if not is_owner(
        chat_id
    ):
        return False

    with owner_input_lock:

        mode = owner_input_mode

        if not mode:
            return False

        if not text.isdigit():

            send_message(
                chat_id,
                "❌ Sirf numeric Telegram Chat ID bhejo."
            )

            return True

        user_id = int(
            text
        )

        if user_id <= 0:

            send_message(
                chat_id,
                "❌ Invalid Chat ID."
            )

            return True

        if mode == "grant":

            client = get_client(
                user_id
            )

            if client:

                grant_hosting_access_by_id(
                    user_id,
                    client["username"] or "",
                    client["first_name"] or "",
                    client["last_name"] or ""
                )

            else:

                grant_hosting_access_by_id(
                    user_id
                )

            owner_input_mode = None

            send_message(
                chat_id,
                (
                    "✅ <b>HOSTING ACCESS GRANTED</b>\n\n"
                    f"Chat ID: <code>{user_id}</code>"
                )
            )

            try:

                send_message(
                    user_id,
                    (
                        f"🎉 <b>{APP_NAME}</b>\n\n"
                        "✅ Owner ne hosting access grant "
                        "kar diya hai.\n\n"
                        "Use /panel."
                    )
                )

            except Exception:
                pass

            return True

        if mode == "revoke":

            result = revoke_hosting_access(
                user_id
            )

            owner_input_mode = None

            send_message(
                chat_id,
                (
                    "🚫 <b>ACCESS REVOKED</b>\n\n"
                    f"Chat ID: <code>{user_id}</code>"
                    if result
                    else
                    "❌ User access record not found."
                )
            )

            try:

                send_message(
                    user_id,
                    (
                        "🚫 <b>Hosting Access Revoked</b>\n\n"
                        "Owner ne aapka hosting access revoke "
                        "kar diya hai."
                    )
                )

            except Exception:
                pass

            return True

    return False


# ============================================================
# MESSAGE HANDLER
# ============================================================

def handle_message(
    message
):
    if not message:
        return

    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get(
        "id"
    )

    if chat_id is None:
        return

    user = message.get(
        "from",
        {}
    )

    text = (
        message.get("text")
        or ""
    ).strip()

    if user:

        upsert_client(
            user
        )

    # Owner numeric input.
    if is_owner(chat_id):

        if handle_owner_text_input(
            chat_id,
            text
        ):

            return

    # /start
    if text.startswith(
        "/start"
    ):

        if is_owner(chat_id):

            show_owner_panel(
                chat_id
            )

            return

        if is_global_client_lock():

            show_client_panel(
                chat_id
            )

            return

        if not has_hosting_access(
            chat_id
        ):

            send_message(
                chat_id,
                (
                    "🔐 <b>Hosting Access Required</b>\n\n"
                    "Aap registered ho, lekin owner ne "
                    "abhi hosting access grant nahi kiya."
                )
            )

            return

        show_client_panel(
            chat_id
        )

        return

    # /panel
    if text.startswith(
        "/panel"
    ):

        if is_owner(chat_id):

            show_owner_panel(
                chat_id
            )

        else:

            show_client_panel(
                chat_id
            )

        return

    # /clients
    if text.startswith(
        "/clients"
    ):

        if is_owner(chat_id):

            show_clients(
                chat_id
            )

        else:

            send_message(
                chat_id,
                "❌ Owner only."
            )

        return

    # /bots
    if text.startswith(
        "/bots"
    ):

        if is_owner(chat_id):

            show_all_bots(
                chat_id
            )

        else:

            show_my_bots(
                chat_id
            )

        return

    # /status
    if text.startswith(
        "/status"
    ):

        if is_owner(chat_id):

            bots = get_bots()

            running = sum(
                1
                for bot in bots
                if bot["status"] == "running"
            )

            send_message(
                chat_id,
                (
                    "📊 <b>HOST STATUS</b>\n\n"
                    f"Clients: <b>{len(get_clients())}</b>\n"
                    f"Bots: <b>{len(bots)}</b>\n"
                    f"Running: <b>{running}</b>\n"
                    f"Global Lock: "
                    f"<b>{'ON' if is_global_client_lock() else 'OFF'}</b>"
                )
            )

        else:

            if not has_hosting_access(
                chat_id
            ):

                send_message(
                    chat_id,
                    "❌ Hosting access denied."
                )

                return

            bots = get_bots(
                chat_id
            )

            running = sum(
                1
                for bot in bots
                if bot["status"] == "running"
            )

            send_message(
                chat_id,
                (
                    "📊 <b>MY STATUS</b>\n\n"
                    f"Bots: <b>{len(bots)}</b>\n"
                    f"Running: <b>{running}</b>"
                )
            )

        return

    # /help
    if text.startswith(
        "/help"
    ):

        if is_owner(chat_id):

            send_message(
                chat_id,
                (
                    "👑 <b>OWNER COMMANDS</b>\n\n"
                    "/panel\n"
                    "/clients\n"
                    "/bots\n"
                    "/status\n"
                    "/help"
                )
            )

        else:

            send_message(
                chat_id,
                (
                    "🤖 <b>CLIENT COMMANDS</b>\n\n"
                    "/panel\n"
                    "/bots\n"
                    "/status\n"
                    "/help"
                )
            )

        return

    # Document upload.
    document = message.get(
        "document"
    )

    if document:

        download_document(
            chat_id,
            document
        )

        return


# ============================================================
# CALLBACK HANDLER
# ============================================================

def handle_callback(
    query
):
    if not query:
        return

    callback_id = query.get(
        "id"
    )

    data = (
        query.get("data")
        or ""
    )

    message = query.get(
        "message"
    )

    if not message:

        answer_callback(
            callback_id
        )

        return

    chat = message.get(
        "chat",
        {}
    )

    try:

        chat_id = int(
            chat.get("id")
        )

        message_id = int(
            message.get("message_id")
        )

    except Exception:

        answer_callback(
            callback_id
        )

        return

    # ========================================================
    # PANEL
    # ========================================================

    if data == "panel":

        answer_callback(
            callback_id
        )

        if is_owner(chat_id):

            show_owner_panel(
                chat_id,
                message_id
            )

        else:

            show_client_panel(
                chat_id,
                message_id
            )

        return

    # ========================================================
    # CLIENTS
    # ========================================================

    if data == "clients":

        answer_callback(
            callback_id
        )

        if is_owner(chat_id):

            show_clients(
                chat_id,
                message_id
            )

        return

    # ========================================================
    # SELECT CLIENT
    # ========================================================

    if data.startswith(
        "client:"
    ):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:

            client_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        show_owner_client(
            chat_id,
            client_id,
            message_id
        )

        return

    # ========================================================
    # ALL BOTS
    # ========================================================

    if data == "allbots":

        answer_callback(
            callback_id
        )

        if is_owner(chat_id):

            show_all_bots(
                chat_id,
                message_id
            )

        return

    # ========================================================
    # MY BOTS
    # ========================================================

    if data == "mybots":

        answer_callback(
            callback_id
        )

        show_my_bots(
            chat_id,
            message_id
        )

        return

    # ========================================================
    # OWNER BOT DETAIL
    # ========================================================

    if data.startswith(
        "obot:"
    ):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:

            bot_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        show_owner_bot(
            chat_id,
            bot_id,
            message_id
        )

        return

    # ========================================================
    # CLIENT BOT DETAIL
    # ========================================================

    if data.startswith(
        "cbot:"
    ):

        answer_callback(
            callback_id
        )

        try:

            bot_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        show_client_bot(
            chat_id,
            bot_id,
            message_id
        )

        return

    # ========================================================
    # OWNER BOT ACTION
    # ========================================================

    if data.startswith(
        "ob:"
    ):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        parts = data.split(":")

        if len(parts) != 3:
            return

        action = parts[1]

        try:

            bot_id = int(
                parts[2]
            )

        except Exception:

            return

        execute_bot_action(
            chat_id,
            bot_id,
            action,
            True
        )

        return

    # ========================================================
    # CLIENT BOT ACTION
    # ========================================================

    if data.startswith(
        "cb:"
    ):

        answer_callback(
            callback_id
        )

        if is_global_client_lock():

            send_message(
                chat_id,
                "🔒 Hosting is locked by owner."
            )

            return

        parts = data.split(":")

        if len(parts) != 3:
            return

        action = parts[1]

        try:

            bot_id = int(
                parts[2]
            )

        except Exception:

            return

        execute_bot_action(
            chat_id,
            bot_id,
            action,
            False
        )

        return

    # ========================================================
    # UPLOAD HELP
    # ========================================================

    if data == "uploadhelp":

        answer_callback(
            callback_id
        )

        upload_help(
            chat_id
        )

        return

    # ========================================================
    # MYSTATUS
    # ========================================================

    if data == "mystatus":

        answer_callback(
            callback_id
        )

        if is_owner(chat_id):

            show_owner_panel(
                chat_id,
                message_id
            )

            return

        if not has_hosting_access(
            chat_id
        ):

            send_message(
                chat_id,
                "❌ Hosting access denied."
            )

            return

        bots = get_bots(
            chat_id
        )

        running = sum(
            1
            for bot in bots
            if bot["status"] == "running"
        )

        send_message(
            chat_id,
            (
                "📊 <b>MY STATUS</b>\n\n"
                f"Total Bots: <b>{len(bots)}</b>\n"
                f"Running: <b>{running}</b>\n"
                f"Stopped: "
                f"<b>{len(bots) - running}</b>"
            )
        )

        return

    # ========================================================
    # GLOBAL LOCK
    # ========================================================

    if data == "lockall":

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text":
                            "⚠️ YES, LOCK ALL CLIENTS",
                        "callback_data":
                            "confirm_lockall"
                    }
                ],
                [
                    {
                        "text": "❌ Cancel",
                        "callback_data":
                            "panel"
                    }
                ]
            ]
        }

        edit_message(
            chat_id,
            message_id,
            (
                "⚠️ <b>LOCK ALL CLIENTS</b>\n\n"
                "Sabhi clients:\n"
                "❌ Panel use nahi kar sakenge\n"
                "❌ Upload nahi kar sakenge\n"
                "❌ Start/Stop nahi kar sakenge\n"
                "❌ Restart nahi kar sakenge\n"
                "❌ Logs nahi dekh sakenge\n\n"
                "Running client bots automatically "
                "<b>stop nahi honge</b>.\n\n"
                "Owner ka access unaffected rahega."
            ),
            keyboard
        )

        return

    # ========================================================
    # CONFIRM LOCK
    # ========================================================

    if data == "confirm_lockall":

        answer_callback(
            callback_id,
            "All clients locked."
        )

        if not is_owner(chat_id):
            return

        set_global_client_lock(
            True
        )

        show_owner_panel(
            chat_id,
            message_id
        )

        return

    # ========================================================
    # UNLOCK
    # ========================================================

    if data == "unlockall":

        answer_callback(
            callback_id,
            "Clients unlocked."
        )

        if not is_owner(chat_id):
            return

        set_global_client_lock(
            False
        )

        show_owner_panel(
            chat_id,
            message_id
        )

        return

    # ========================================================
    # STOP ALL
    # ========================================================

    if data == "stopall":

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text":
                            "⚠️ YES, STOP ALL BOTS",
                        "callback_data":
                            "confirm_stopall"
                    }
                ],
                [
                    {
                        "text": "❌ Cancel",
                        "callback_data":
                            "panel"
                    }
                ]
            ]
        }

        edit_message(
            chat_id,
            message_id,
            (
                "⚠️ <b>STOP ALL BOTS</b>\n\n"
                "Sabhi running bots stop honge.\n"
                "Manual stop ke baad auto-restart watcher "
                "unhe automatically start nahi karega.\n\n"
                "Continue?"
            ),
            keyboard
        )

        return

    # ========================================================
    # CONFIRM STOP ALL
    # ========================================================

    if data == "confirm_stopall":

        answer_callback(
            callback_id,
            "Stopping all bots..."
        )

        if not is_owner(chat_id):
            return

        # Run in background so Telegram remains responsive.
        def worker():

            count = stop_all_bots()

            send_message(
                chat_id,
                (
                    "🛑 <b>STOP ALL COMPLETE</b>\n\n"
                    f"Stopped/processed: <b>{count}</b>\n\n"
                    "All bots have been stopped."
                ),
                {
                    "inline_keyboard": [
                        [
                            {
                                "text":
                                    "⬅️ Owner Panel",
                                "callback_data":
                                    "panel"
                            },
                            {
                                "text":
                                    "🤖 All Bots",
                                "callback_data":
                                    "allbots"
                            }
                        ]
                    ]
                }
            )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

        edit_message(
            chat_id,
            message_id,
            (
                "⏳ <b>STOP ALL IN PROGRESS</b>\n\n"
                "All bot processes terminate ho rahe hain..."
            )
        )

        return

    # ========================================================
    # ACCESS
    # ========================================================

    if data == "access":

        answer_callback(
            callback_id
        )

        if is_owner(chat_id):

            show_access(
                chat_id,
                message_id
            )

        return

    # ========================================================
    # ACCESS USER
    # ========================================================

    if data.startswith(
        "accessuser:"
    ):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:

            user_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        users = get_access_users()

        target = None

        for user in users:

            if int(
                user["chat_id"]
            ) == user_id:

                target = user
                break

        if not target:

            send_message(
                chat_id,
                "❌ Access user not found."
            )

            return

        enabled = bool(
            target["enabled"]
        )

        send_message(
            chat_id,
            (
                "🔐 <b>ACCESS USER</b>\n\n"
                f"User: "
                f"<b>{escape_html(display_username(target))}</b>\n"
                f"Chat ID: <code>{user_id}</code>\n"
                f"Status: "
                f"<b>{'ENABLED' if enabled else 'DISABLED'}</b>"
            ),
            {
                "inline_keyboard": [
                    [
                        {
                            "text": (
                                "🚫 Revoke Access"
                                if enabled
                                else
                                "✅ Grant Access"
                            ),
                            "callback_data":
                                (
                                    f"revoke:{user_id}"
                                    if enabled
                                    else
                                    f"grant:{user_id}"
                                )
                        }
                    ],
                    [
                        {
                            "text": "⬅️ Access List",
                            "callback_data":
                                "access"
                        }
                    ]
                ]
            }
        )

        return

    # ========================================================
    # GRANT MENU
    # ========================================================

    if data == "grantmenu":

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        with owner_input_lock:
            global owner_input_mode
            owner_input_mode = "grant"

        send_message(
            chat_id,
            (
                "➕ <b>GRANT HOSTING</b>\n\n"
                "Numeric Telegram Chat ID bhejo.\n\n"
                "Example:\n"
                "<code>123456789</code>"
            )
        )

        return

    # ========================================================
    # REVOKE MENU
    # ========================================================

    if data == "revokemenu":

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        with owner_input_lock:
            owner_input_mode = "revoke"

        send_message(
            chat_id,
            (
                "🚫 <b>REVOKE HOSTING</b>\n\n"
                "Numeric Telegram Chat ID bhejo."
            )
        )

        return

    # ========================================================
    # DIRECT GRANT
    # ========================================================

    if data.startswith(
        "grant:"
    ):

        answer_callback(
            callback_id,
            "Access granted."
        )

        if not is_owner(chat_id):
            return

        try:

            user_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        client = get_client(
            user_id
        )

        if client:

            grant_hosting_access_by_id(
                user_id,
                client["username"] or "",
                client["first_name"] or "",
                client["last_name"] or ""
            )

        else:

            grant_hosting_access_by_id(
                user_id
            )

        send_message(
            chat_id,
            (
                "✅ <b>HOSTING ACCESS GRANTED</b>\n\n"
                f"Chat ID: <code>{user_id}</code>"
            )
        )

        try:

            send_message(
                user_id,
                (
                    f"🎉 <b>{APP_NAME}</b>\n\n"
                    "✅ Owner ne hosting access grant "
                    "kar diya hai.\n\n"
                    "Use /panel."
                )
            )

        except Exception:
            pass

        return

    # ========================================================
    # REVOKE
    # ========================================================

    if data.startswith(
        "revoke:"
    ):

        answer_callback(
            callback_id,
            "Access revoked."
        )

        if not is_owner(chat_id):
            return

        try:

            user_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        result = revoke_hosting_access(
            user_id
        )

        send_message(
            chat_id,
            (
                "🚫 <b>ACCESS REVOKED</b>\n\n"
                f"Chat ID: <code>{user_id}</code>"
                if result
                else
                "❌ Access record not found."
            )
        )

        try:

            send_message(
                user_id,
                (
                    "🚫 <b>Hosting Access Revoked</b>\n\n"
                    "Owner ne aapka hosting access revoke "
                    "kar diya hai."
                )
            )

        except Exception:
            pass

        return

    # ========================================================
    # ENABLE CLIENT
    # ========================================================

    if data.startswith(
        "enable:"
    ):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:

            client_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        set_client_enabled(
            client_id,
            True
        )

        show_owner_client(
            chat_id,
            client_id,
            message_id
        )

        return

    # ========================================================
    # DISABLE CLIENT
    # ========================================================

    if data.startswith(
        "disable:"
    ):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:

            client_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        set_client_enabled(
            client_id,
            False
        )

        show_owner_client(
            chat_id,
            client_id,
            message_id
        )

        return

    # ========================================================
    # STOP CLIENT
    # ========================================================

    if data.startswith(
        "stopclient:"
    ):

        answer_callback(
            callback_id,
            "Stopping client bots..."
        )

        if not is_owner(chat_id):
            return

        try:

            client_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        def worker():

            count = stop_all_bots_of_client(
                client_id
            )

            send_message(
                chat_id,
                (
                    "🛑 <b>CLIENT BOTS STOPPED</b>\n\n"
                    f"Client ID: <code>{client_id}</code>\n"
                    f"Processed: <b>{count}</b>"
                )
            )

            show_owner_client(
                chat_id,
                client_id
            )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

        return

    # ========================================================
    # DELETE CLIENT
    # ========================================================

    if data.startswith(
        "dclient:"
    ):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:

            client_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        client = get_client(
            client_id
        )

        if not client:

            send_message(
                chat_id,
                "❌ Client not found."
            )

            return

        send_message(
            chat_id,
            (
                "⚠️ <b>PERMANENT CLIENT DELETE</b>\n\n"
                f"Client: "
                f"<b>{escape_html(display_username(client))}</b>\n"
                f"ID: <code>{client_id}</code>\n\n"
                "Permanently delete hoga:\n"
                "• All bots\n"
                "• Python files\n"
                "• .venv\n"
                "• Logs\n"
                "• Database records\n"
                "• Hosting access\n\n"
                "❗ <b>THIS CANNOT BE UNDONE.</b>"
            ),
            {
                "inline_keyboard": [
                    [
                        {
                            "text":
                                "⚠️ YES, DELETE CLIENT",
                            "callback_data":
                                f"confirmclient:{client_id}"
                        }
                    ],
                    [
                        {
                            "text": "❌ Cancel",
                            "callback_data":
                                f"client:{client_id}"
                        }
                    ]
                ]
            }
        )

        return

    # ========================================================
    # CONFIRM CLIENT DELETE
    # ========================================================

    if data.startswith(
        "confirmclient:"
    ):

        answer_callback(
            callback_id,
            "Deleting client..."
        )

        if not is_owner(chat_id):
            return

        try:

            client_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        def worker():

            success, result = (
                delete_client_permanently(
                    client_id
                )
            )

            send_message(
                chat_id,
                (
                    "✅ <b>CLIENT DELETED</b>\n\n"
                    "All client data permanently removed."
                    if success
                    else
                    f"❌ {escape_html(result)}"
                )
            )

            show_clients(
                chat_id
            )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

        return

    # ========================================================
    # OWNER BOT DELETE
    # ========================================================

    if data.startswith(
        "confirmob:"
    ):

        answer_callback(
            callback_id,
            "Deleting bot..."
        )

        if not is_owner(chat_id):
            return

        try:

            bot_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        row = get_bot(
            bot_id
        )

        owner_id = (
            int(row["owner_chat_id"])
            if row
            else 0
        )

        success, result = (
            delete_bot_permanently(
                bot_id
            )
        )

        send_message(
            chat_id,
            (
                "✅ <b>BOT DELETED PERMANENTLY</b>"
                if success
                else
                f"❌ {escape_html(result)}"
            )
        )

        if owner_id:

            show_owner_client(
                chat_id,
                owner_id
            )

        return

    # ========================================================
    # CLIENT BOT DELETE
    # ========================================================

    if data.startswith(
        "confirmcb:"
    ):

        answer_callback(
            callback_id,
            "Deleting bot..."
        )

        if is_global_client_lock():

            send_message(
                chat_id,
                "🔒 Hosting is locked."
            )

            return

        try:

            bot_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except Exception:

            return

        row = get_bot(
            bot_id
        )

        if not row:

            send_message(
                chat_id,
                "❌ Bot not found."
            )

            return

        if int(
            row["owner_chat_id"]
        ) != int(chat_id):

            send_message(
                chat_id,
                "❌ Permission denied."
            )

            return

        success, result = (
            delete_bot_permanently(
                bot_id
            )
        )

        send_message(
            chat_id,
            (
                "✅ <b>BOT DELETED PERMANENTLY</b>"
                if success
                else
                f"❌ {escape_html(result)}"
            )
        )

        show_my_bots(
            chat_id
        )

        return


# ============================================================
# RECOVERY
# ============================================================

def recover_bots():
    print(
        "[RECOVERY] Checking previously running bots..."
    )

    bots = get_bots()

    for row in bots:

        bot_id = int(
            row["id"]
        )

        # IMPORTANT:
        # Only bots whose DB state was RUNNING before
        # host restart are recovered.
        #
        # Manually stopped bots have status=stopped,
        # so they will NOT suddenly restart.
        if str(
            row["status"]
        ).lower() != "running":

            continue

        if not bool(
            row["auto_restart"]
        ):

            continue

        client = get_client(
            int(row["owner_chat_id"])
        )

        if client and not bool(
            client["enabled"]
        ):

            update_bot(
                bot_id,
                status="stopped",
                pid=0
            )

            continue

        def worker(
            target_bot_id
        ):

            success, message = start_bot(
                target_bot_id
            )

            print(
                f"[RECOVERY] "
                f"Bot #{target_bot_id}: "
                f"{success} - {message}"
            )

        threading.Thread(
            target=worker,
            args=(bot_id,),
            daemon=True
        ).start()

        time.sleep(
            0.2
        )


# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(
        self
    ):

        body = (
            f"{APP_NAME} "
            f"v{VERSION} - OK"
        ).encode(
            "utf-8"
        )

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(
            body
        )

    def log_message(
        self,
        format,
        *args
    ):
        return


def start_health_server():

    try:

        port = int(
            os.getenv(
                "PORT",
                "10000"
            )
        )

        server = HTTPServer(
            (
                "0.0.0.0",
                port
            ),
            HealthHandler
        )

        print(
            f"[HEALTH] Listening on port {port}"
        )

        server.serve_forever()

    except Exception as e:

        print(
            f"[HEALTH ERROR] {e}"
        )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def poll_updates():

    global telegram_offset

    print(
        "[POLL] Starting Telegram polling..."
    )

    while True:

        try:

            result = telegram(
                "getUpdates",
                {
                    "offset":
                        telegram_offset,
                    "timeout":
                        50,
                    "allowed_updates":
                        json.dumps([
                            "message",
                            "callback_query"
                        ])
                },
                timeout=65
            )

            if not result:

                time.sleep(
                    1
                )

                continue

            if not result.get(
                "ok"
            ):

                print(
                    "[POLL] Telegram API error."
                )

                time.sleep(
                    2
                )

                continue

            updates = result.get(
                "result",
                []
            )

            for update in updates:

                try:

                    update_id = int(
                        update.get(
                            "update_id",
                            0
                        )
                    )

                    telegram_offset = (
                        update_id + 1
                    )

                    if "message" in update:

                        handle_message(
                            update["message"]
                        )

                    elif "callback_query" in update:

                        handle_callback(
                            update["callback_query"]
                        )

                except Exception as e:

                    print(
                        "[UPDATE ERROR]",
                        repr(e)
                    )

                    try:

                        callback = update.get(
                            "callback_query"
                        )

                        if callback:

                            answer_callback(
                                callback.get("id"),
                                "An error occurred."
                            )

                    except Exception:
                        pass

        except Exception as e:

            print(
                "[POLL ERROR]",
                repr(e)
            )

            time.sleep(
                2
            )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 65)
    print(
        f"{APP_NAME} v{VERSION}"
    )
    print("=" * 65)

    if not BOT_TOKEN:

        print(
            "[FATAL] BOT_TOKEN is missing."
        )

        sys.exit(1)

    if not OWNER_CHAT_ID:

        print(
            "[FATAL] OWNER_CHAT_ID is missing."
        )

        sys.exit(1)

    init_db()

    print(
        f"[OWNER] {OWNER_CHAT_ID}"
    )

    print(
        f"[DATA] {DATA_DIR}"
    )

    print(
        f"[GLOBAL LOCK] "
        f"{is_global_client_lock()}"
    )

    # Render health.
    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    # Recover only previously RUNNING bots.
    threading.Thread(
        target=recover_bots,
        daemon=True
    ).start()

    print(
        "[HOST] Ready."
    )

    poll_updates()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
