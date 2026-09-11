#!/usr/bin/env python3
import http.server
import json
import os
import re
import shlex
import subprocess
import threading
import time
import signal
import sys
import tempfile
import urllib.request
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", 3326))
PUBLIC = os.path.join(BASE_DIR, "public")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CRON_TAG = "# rsyncgui-managed"
INTERVAL_FILE = os.path.join(BASE_DIR, "intervals.json")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
LOG_RETENTION_DAYS = 30

APP_VERSION = "1.0.1"
SERVICE_NAME = os.environ.get("RSYNCGUI_SERVICE", "rsyncgui")
GITHUB_RAW = "https://raw.githubusercontent.com/hirogura/rsyncgui/main/"
UPDATE_FILES = ["server.py", "public/index.html"]

MIME = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}

processes = {}
interval_processes = {}

os.makedirs(LOGS_DIR, exist_ok=True)


PROGRESS2_RE = re.compile(
    r"^\s*([\d,]+)\s+(\d{1,3})%\s+([\d.]+\S*B/s)\s+(\S+)"
)


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"pairs": [], "schedules": []}


def save_config(cfg):
    tmp_path = CONFIG_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, CONFIG_FILE)


def build_rsync_cmd(job):
    args = []

    options = job.get("options", {})
    for key, val in options.items():
        if key == "backup-dir":
            if val and str(val).strip():
                args.append(f"--backup-dir={val}")
        elif val is True:
            args.append(f"--{key}")
        elif val is not False and val != "" and val is not None:
            args.append(f"--{key}={val}")

    # フロントは backup-dir をトップレベルで送る場合があるため両対応
    top_backup = job.get("backup-dir", job.get("backup_dir", ""))
    if top_backup and str(top_backup).strip() and "backup-dir" not in options:
        args.append(f"--backup-dir={top_backup}")

    if job.get("dryRun") or job.get("dry_run"):
        args.append("--dry-run")

    args.append("--info=progress2")
    args.append("--no-inc-recursive")

    ssh_cfg = job.get("ssh")
    if ssh_cfg:
        ssh_parts = ["ssh"]
        if ssh_cfg.get("port") and ssh_cfg["port"] != "22":
            ssh_parts.append(f"-p {ssh_cfg['port']}")
        if ssh_cfg.get("key"):
            ssh_parts.append(f"-i {ssh_cfg['key']}")
        ssh_parts.append("-o StrictHostKeyChecking=no")
        args.append("-e")
        args.append(" ".join(ssh_parts))

    exclude = job.get("exclude", "")
    for line in exclude.split("\n"):
        line = line.strip()
        if line:
            args.append(f"--exclude={line}")

    source = job.get("source", "")
    target = job.get("target", "")

    if ssh_cfg and ssh_cfg.get("host"):
        user_prefix = f"{ssh_cfg['user']}@" if ssh_cfg.get("user") else ""
        if ":" not in target:
            target = f"{user_prefix}{ssh_cfg['host']}:{target}"

    args.append(source)
    args.append(target)

    use_sshpass = ssh_cfg and ssh_cfg.get("password") and ssh_cfg.get("host")
    password = ssh_cfg.get("password", "") if use_sshpass else ""
    return args, use_sshpass, password


def get_crontab():
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return ""


def set_crontab(content):
    proc = subprocess.run(["crontab", "-"], input=content, text=True, capture_output=True)
    return proc.returncode == 0


def _parse_time_str(time_str):
    """'HH:MM' を (hour, minute) に正規化。無効なら None。"""
    try:
        parts = str(time_str).strip().split(":")
        if len(parts) != 2:
            return None
        hour = int(parts[0])
        minute = int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, TypeError):
        pass
    return None


def pair_to_cron_cmd(pair):
    job = {
        "source": pair.get("source", ""),
        "target": pair.get("target", ""),
        "mode": pair.get("mode", "sync"),
        "options": pair.get("options", {}),
        "backup-dir": pair.get("backup-dir", pair.get("backup_dir", "")),
        "ssh": pair.get("ssh") if pair.get("sshEnabled") else None,
        "exclude": pair.get("exclude", ""),
        "dryRun": pair.get("dryRun", False),
    }
    args, use_sshpass, password = build_rsync_cmd(job)
    pair_id = pair.get("id", "?")
    safe_pair_id = shlex.quote(str(pair_id))
    cmd_parts = ["rsync"]
    i = 0
    while i < len(args):
        if args[i] == "-e" and i + 1 < len(args):
            cmd_parts.append(f"-e {shlex.quote(args[i+1])}")
            i += 2
        else:
            cmd_parts.append(shlex.quote(args[i]) if ' ' in args[i] else args[i])
            i += 1
    rsync_cmd = " ".join(cmd_parts)
    if use_sshpass:
        rsync_cmd = f"sshpass -f /tmp/rsyncgui_cron_pw_{pair_id} {rsync_cmd}"
    return f"/opt/rsyncgui/cron_runner.sh {safe_pair_id} {rsync_cmd}"


def sync_crontab(pairs):
    existing = get_crontab()
    lines = [l for l in existing.splitlines() if CRON_TAG not in l]

    for pair in pairs:
        sched = pair.get("schedule", {})
        if not sched.get("enabled"):
            continue
        sched_mode = sched.get("mode", "time")
        if sched_mode == "interval":
            continue
        times = sched.get("times", [])
        if not times:
            time_str = sched.get("time", "02:00")
            times = [time_str]
        days = sched.get("days", [])
        try:
            days = [int(d) for d in days if 0 <= int(d) <= 6]
        except (ValueError, TypeError):
            continue
        if not days:
            continue
        ssh_cfg = pair.get("ssh") if pair.get("sshEnabled") else None
        if ssh_cfg and ssh_cfg.get("password") and ssh_cfg.get("host"):
            pw_file = f"/tmp/rsyncgui_cron_pw_{pair.get('id', '?')}"
            try:
                with open(pw_file, "w") as f:
                    f.write(ssh_cfg["password"])
                os.chmod(pw_file, 0o600)
            except Exception:
                pass
        day_str = ",".join(str(d) for d in days)
        cmd = pair_to_cron_cmd(pair)
        for time_str in times:
            parsed = _parse_time_str(time_str)
            if not parsed:
                continue
            hour, minute = parsed
            cron_time = f"{minute} {hour} * * {day_str}"
            cron_line = f"{cron_time} {cmd} {CRON_TAG} id={pair.get('id', '?')}"
            lines.append(cron_line)

    new_crontab = "\n".join(lines) + "\n" if lines else ""
    return set_crontab(new_crontab)


def load_intervals():
    if os.path.exists(INTERVAL_FILE):
        with open(INTERVAL_FILE, "r") as f:
            return json.load(f)
    return []


def save_intervals(intervals):
    tmp_path = INTERVAL_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(intervals, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, INTERVAL_FILE)


def _log_ts(entry):
    ts = entry.get("timestamp", entry.get("ts", 0))
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0


def load_pair_logs(pair_id):
    log_file = os.path.join(LOGS_DIR, f"pair_{pair_id}.json")
    if not os.path.exists(log_file):
        return []
    try:
        with open(log_file, "r") as f:
            logs = json.load(f)
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        logs = [l for l in logs if _log_ts(l) > cutoff]
        return logs
    except Exception:
        return []


def save_pair_log(pair_id, entry):
    os.makedirs(LOGS_DIR, exist_ok=True)
    if "timestamp" not in entry and "ts" in entry:
        entry = {**entry, "timestamp": entry["ts"]}
    if "timestamp" not in entry:
        entry = {**entry, "timestamp": time.time()}
    log_file = os.path.join(LOGS_DIR, f"pair_{pair_id}.json")
    logs = load_pair_logs(pair_id)
    logs.append(entry)
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    logs = [l for l in logs if _log_ts(l) > cutoff]
    tmp_path = log_file + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(logs, f, ensure_ascii=False)
    os.replace(tmp_path, log_file)


# 後方互換エイリアス（旧名で呼ぶ箇所用）
def get_logs(pair_id):
    return load_pair_logs(pair_id)


def save_log(pair_id, entry):
    return save_pair_log(pair_id, entry)


def cleanup_old_logs():
    if not os.path.exists(LOGS_DIR):
        return
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for fname in os.listdir(LOGS_DIR):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(LOGS_DIR, fname)
        try:
            with open(fpath, "r") as f:
                logs = json.load(f)
            filtered = [l for l in logs if _log_ts(l) > cutoff]
            if len(filtered) < len(logs):
                tmp_path = fpath + ".tmp"
                with open(tmp_path, "w") as f:
                    json.dump(filtered, f, ensure_ascii=False)
                os.replace(tmp_path, fpath)
        except Exception:
            pass


def ensure_remote_dir(job):
    ssh_cfg = job.get("ssh")
    if not ssh_cfg or not ssh_cfg.get("host"):
        target = job.get("target", "")
        if target:
            os.makedirs(target, exist_ok=True)
        return
    target = job.get("target", "")
    if not target:
        return
    user_prefix = f"{ssh_cfg['user']}@" if ssh_cfg.get("user") else ""
    host = ssh_cfg["host"]
    port = ssh_cfg.get("port", "22")
    key = ssh_cfg.get("key", "")
    password = ssh_cfg.get("password", "")
    remote_path = target.rstrip("/")
    mkdir_cmd = f"mkdir -p '{remote_path}'"
    ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no"]
    if port and port != "22":
        ssh_cmd += ["-p", port]
    if key:
        ssh_cmd += ["-i", key]
    ssh_cmd += [f"{user_prefix}{host}", mkdir_cmd]
    try:
        if password and ssh_cfg.get("host"):
            pw_file = tempfile.NamedTemporaryFile(mode='w', delete=False, prefix='rsyncgui_mkdirpw_')
            pw_file.write(password)
            pw_file.close()
            os.chmod(pw_file.name, 0o600)
            subprocess.run(["sshpass", "-f", pw_file.name] + ssh_cmd,
                           capture_output=True, timeout=15)
            os.unlink(pw_file.name)
        else:
            subprocess.run(ssh_cmd, capture_output=True, timeout=15)
    except Exception as e:
        print(f"[rsyncgui] ensure_remote_dir failed: {e}", flush=True)


def parse_progress_line(line):
    m = PROGRESS2_RE.match(line)
    if not m:
        return None
    bytes_str, percent_str, rate, eta = m.groups()
    try:
        transferred = int(bytes_str.replace(",", ""))
        percent = int(percent_str)
    except ValueError:
        return None
    return {"transferred": transferred, "percent": percent, "rate": rate, "eta": eta}


def make_job_entry(proc):
    return {
        "proc": proc,
        "log": [],
        "done": False,
        "exitCode": None,
        "progress": {"percent": 0, "transferred": 0, "rate": "", "eta": ""},
        "cancelled": False,
    }


def stream_output(job_id, proc):
    entry = processes.get(job_id)
    if entry is None:
        return
    try:
        for raw_line in proc.stdout:
            if processes.get(job_id) is None:
                break
            for line in raw_line.replace("\r", "\n").split("\n"):
                if not line:
                    continue
                progress = parse_progress_line(line)
                if progress:
                    processes[job_id]["progress"] = progress
                else:
                    processes[job_id]["log"].append({"type": "stdout", "text": line})
        for line in proc.stderr:
            if processes.get(job_id) is None:
                break
            processes[job_id]["log"].append({"type": "stderr", "text": line})
    except Exception as e:
        if processes.get(job_id) is not None:
            processes[job_id]["log"].append({"type": "error", "text": str(e)})


def _stop_interval_thread(pair_id):
    entry = interval_processes.get(pair_id)
    if entry:
        sf = entry.get("stop_flag")
        if isinstance(sf, dict):
            sf["stop"] = True


def start_interval_scheduler(pairs):
    active_ids = set()
    for pair in pairs:
        sched = pair.get("schedule", {}) if isinstance(pair.get("schedule", {}), dict) else {}
        if sched.get("enabled") and sched.get("mode") == "interval":
            active_ids.add(pair.get("id"))
    for pair_id in list(interval_processes.keys()):
        if pair_id not in active_ids:
            _stop_interval_thread(pair_id)
            del interval_processes[pair_id]

    for pair in pairs:
        sched = pair.get("schedule", {}) if isinstance(pair.get("schedule", {}), dict) else {}
        if not sched.get("enabled") or sched.get("mode") != "interval":
            continue
        pair_id = pair.get("id")
        try:
            interval_val = int(sched.get("interval", 60))
        except (ValueError, TypeError):
            interval_val = 60
        interval_val = max(1, interval_val)
        unit = sched.get("intervalUnit", "s")
        if unit not in ("s", "m", "h"):
            unit = "s"
        if pair_id in interval_processes:
            existing = interval_processes[pair_id]
            if existing.get("interval") == interval_val and existing.get("unit") == unit:
                continue
            _stop_interval_thread(pair_id)
            del interval_processes[pair_id]
        interval_seconds = interval_val * {"s": 1, "m": 60, "h": 3600}[unit]
        interval_seconds = max(1, interval_seconds)
        stop_flag = {"stop": False}
        pair_snapshot = json.loads(json.dumps(pair))
        interval_processes[pair_id] = {"interval": interval_val, "unit": unit,
                                       "stop_flag": stop_flag, "thread": None}

        def run_interval(pid, secs, pair_data, sf):
            while not sf["stop"]:
                time.sleep(1)
                if sf["stop"]:
                    break
                job = {
                    "source": pair_data.get("source", ""),
                    "target": pair_data.get("target", ""),
                    "mode": pair_data.get("mode", "sync"),
                    "options": pair_data.get("options", {}),
                    "backup-dir": pair_data.get("backup-dir", pair_data.get("backup_dir", "")),
                    "ssh": pair_data.get("ssh") if pair_data.get("sshEnabled") else None,
                    "exclude": pair_data.get("exclude", ""),
                    "dryRun": pair_data.get("dryRun", False),
                }
                args, use_sshpass, password = build_rsync_cmd(job)
                job_id = f"int_{pid}_{int(time.time() * 1000)}"
                pw_file = None
                try:
                    ensure_remote_dir(job)
                    if use_sshpass:
                        pw_file = tempfile.NamedTemporaryFile(mode='w', delete=False, prefix='rsyncgui_intpw_')
                        pw_file.write(password)
                        pw_file.close()
                        os.chmod(pw_file.name, 0o600)
                        cmd_list = ["sshpass", "-f", pw_file.name, "rsync"] + args
                    else:
                        cmd_list = ["rsync"] + args
                    print(f"[rsyncgui] interval run: pid={pid} cmd={' '.join(cmd_list)}", flush=True)
                    proc = subprocess.Popen(
                        cmd_list,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        preexec_fn=os.setsid,
                    )
                    processes[job_id] = make_job_entry(proc)
                    stream_output(job_id, proc)
                    proc.wait()
                    processes[job_id]["done"] = True
                    processes[job_id]["exitCode"] = proc.returncode
                    save_pair_log(pid, {
                        "timestamp": time.time(),
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "done" if proc.returncode == 0 else "error",
                        "command": " ".join(cmd_list),
                        "exitCode": proc.returncode,
                        "log": "",
                    })
                    print(f"[rsyncgui] interval done: pid={pid} exit={proc.returncode}", flush=True)
                except Exception as e:
                    entry = processes.get(job_id)
                    if entry is not None:
                        entry["log"].append({"type": "error", "text": str(e)})
                        entry["done"] = True
                        entry["exitCode"] = -1
                    print(f"[rsyncgui] interval error: pid={pid} err={e}", flush=True)
                finally:
                    if pw_file and os.path.exists(pw_file.name):
                        os.unlink(pw_file.name)
                for _ in range(secs):
                    if sf["stop"]:
                        break
                    time.sleep(1)

        t = threading.Thread(target=run_interval, args=(pair_id, interval_seconds, pair_snapshot, stop_flag), daemon=True)
        t.start()
        interval_processes[pair_id]["thread"] = t


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = 30

    def log_message(self, format, *args):
        pass

    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type in (BrokenPipeError, ConnectionResetError, TimeoutError):
            print(f"[rsyncgui] connection closed early: {client_address}")
        else:
            super().handle_error(request, client_address)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _file_response(self, filepath):
        ext = os.path.splitext(filepath)[1]
        mime = MIME.get(ext, "application/octet-stream")
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            if ext in (".html", ".css", ".js", ".json", ".svg"):
                self.send_header("Content-Type", f"{mime}; charset=utf-8")
            else:
                self.send_header("Content-Type", mime)
            self._cors()
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except (FileNotFoundError, IsADirectoryError):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
        except OSError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/api/version":
            self._json_response({"version": APP_VERSION})
        elif path == "/api/browse":
            browse_path = params.get("path", ["/"])[0]
            self._browse(browse_path)
        elif path == "/api/du":
            du_path = params.get("path", [""])[0]
            self._du(du_path)
        elif path.startswith("/api/status/"):
            job_id = path.split("/")[-1]
            self._status(job_id)
        elif path == "/api/config":
            self._get_config()
        elif path == "/api/cron":
            self._get_cron()
        elif path.startswith("/api/logs/"):
            pair_id = path.split("/")[-1]
            self._get_pair_logs(pair_id)
        elif path == "/api/logs":
            pair_id = params.get("pairId", [""])[0]
            self._get_logs(pair_id)
        else:
            if path == "/":
                path = "/index.html"
            # パストラバーサル対策: PUBLIC 配下のみ配信
            rel = os.path.normpath(path.lstrip("/"))
            filepath = os.path.join(PUBLIC, rel)
            if os.path.commonpath([PUBLIC, os.path.abspath(filepath)]) != PUBLIC:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return
            self._file_response(filepath)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            content_length = 0
        body = self.rfile.read(content_length)

        if parsed.path == "/api/rsync":
            self._run_rsync(body)
        elif parsed.path == "/api/config":
            self._save_config(body)
        elif parsed.path == "/api/schedule/save":
            self._save_schedule(body)
        elif parsed.path.startswith("/api/cancel/"):
            job_id = parsed.path.split("/")[-1]
            self._cancel(job_id)
        elif parsed.path == "/api/cancel-all":
            self._cancel_all()
        elif parsed.path == "/api/mkdir":
            self._mkdir(body)
        elif parsed.path == "/api/restart":
            self._restart()
        elif parsed.path == "/api/admin/restart":
            self._admin_restart()
        elif parsed.path == "/api/admin/update":
            self._admin_update()
        else:
            self.send_response(404)
            self.end_headers()

    def _browse(self, browse_path):
        browse_path = browse_path or "/"
        try:
            entries = os.scandir(browse_path)
            items = []
            for e in entries:
                if e.name.startswith("."):
                    continue
                if e.is_dir():
                    items.append({
                        "name": e.name,
                        "type": "dir",
                        "path": os.path.join(browse_path, e.name),
                    })
            items.sort(key=lambda x: x["name"])
            parent = os.path.dirname(browse_path.rstrip("/"))
            if not parent:
                parent = "/"
            self._json_response({"current": browse_path, "parent": parent, "items": items})
        except Exception as ex:
            self._json_response({"error": str(ex)}, 500)

    def _du(self, du_path):
        if not du_path:
            self._json_response({"error": "path is required"}, 400)
            return
        try:
            result = subprocess.run(
                ["du", "-sb", du_path], capture_output=True, text=True, timeout=30
            )
            size = 0
            if result.returncode == 0 and result.stdout:
                size = int(result.stdout.split()[0])
            self._json_response({"path": du_path, "bytes": size})
        except Exception as ex:
            self._json_response({"error": str(ex)}, 500)

    def _get_config(self):
        cfg = load_config()
        self._json_response(cfg)

    def _save_config(self, body):
        try:
            cfg = json.loads(body)
            save_config(cfg)
            self._json_response({"ok": True})
        except Exception as ex:
            self._json_response({"error": str(ex)}, 500)

    def _save_schedule(self, body):
        try:
            data = json.loads(body)
            pairs = data.get("pairs", [])
            for p in pairs:
                p.pop("progress", None)
                p.pop("status", None)
                p.pop("jobId", None)
            sync_crontab(pairs)
            start_interval_scheduler(pairs)
            cfg = load_config()
            cfg["pairs"] = pairs
            save_config(cfg)
            self._json_response({"ok": True})
        except Exception as ex:
            self._json_response({"error": str(ex)}, 500)

    def _get_pair_logs(self, pair_id):
        logs = load_pair_logs(pair_id)
        self._json_response({"logs": logs})

    def _get_cron(self):
        content = get_crontab()
        lines = [l for l in content.splitlines() if CRON_TAG in l]

        cfg = load_config()
        interval_entries = []
        for pair in cfg.get("pairs", []):
            sched = pair.get("schedule", {})
            if sched.get("enabled") and sched.get("mode") == "interval":
                interval_val = sched.get("interval", 60)
                interval_unit = sched.get("intervalUnit", "s")
                unit_labels = {"s": "秒", "m": "分", "h": "時間"}
                interval_entries.append({
                    "id": pair.get("id", "?"),
                    "source": pair.get("source", ""),
                    "target": pair.get("target", ""),
                    "interval": interval_val,
                    "intervalUnit": interval_unit,
                    "intervalLabel": f"{interval_val}{unit_labels.get(interval_unit, '秒')}ごと"
                })

        self._json_response({"entries": lines, "intervals": interval_entries})

    def _get_logs(self, pair_id):
        if not pair_id:
            self._json_response({"error": "pairId is required"}, 400)
            return
        logs = get_logs(pair_id)
        self._json_response({"logs": logs})

    def _mkdir(self, body):
        try:
            data = json.loads(body)
            dir_path = data.get("path", "")
            ssh_cfg = data.get("ssh")
            if not dir_path:
                self._json_response({"error": "path is required"}, 400)
                return
            if ssh_cfg and ssh_cfg.get("host"):
                ssh_parts = ["ssh"]
                if ssh_cfg.get("port") and ssh_cfg["port"] != "22":
                    ssh_parts.append(f"-p {ssh_cfg['port']}")
                if ssh_cfg.get("key"):
                    ssh_parts.append(f"-i {ssh_cfg['key']}")
                ssh_parts.append("-o StrictHostKeyChecking=no")
                user_prefix = f"{ssh_cfg['user']}@" if ssh_cfg.get("user") else ""
                remote_cmd = f"mkdir -p '{dir_path}'"
                cmd = ssh_parts + [f"{user_prefix}{ssh_cfg['host']}", remote_cmd]
                use_sshpass = ssh_cfg.get("password") and ssh_cfg.get("host")
                if use_sshpass:
                    pw_file = tempfile.NamedTemporaryFile(mode='w', delete=False, prefix='rsyncgui_mkdirpw_')
                    pw_file.write(ssh_cfg["password"])
                    pw_file.close()
                    os.chmod(pw_file.name, 0o600)
                    try:
                        result = subprocess.run(
                            ["sshpass", "-f", pw_file.name] + cmd,
                            capture_output=True, text=True, timeout=30
                        )
                    finally:
                        if os.path.exists(pw_file.name):
                            os.unlink(pw_file.name)
                else:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode != 0:
                    self._json_response({"error": result.stderr.strip() or "SSH mkdir failed"}, 500)
                    return
            else:
                os.makedirs(dir_path, exist_ok=True)
            self._json_response({"ok": True})
        except Exception as ex:
            self._json_response({"error": str(ex)}, 500)

    def _run_rsync(self, body):
        try:
            job = json.loads(body)
            if not job.get("source") or not job.get("target"):
                self._json_response({"error": "source and target are required"}, 400)
                return
            args, use_sshpass, password = build_rsync_cmd(job)
            job_id = f"job_{int(time.time() * 1000)}"

            cmd = "rsync " + " ".join(args)
            if use_sshpass:
                cmd = f"sshpass -f /tmp/rsyncgui_pw_{job_id} rsync " + " ".join(args)

            # ポーリング競合対策: レスポンス前にプレースホルダを登録
            processes[job_id] = make_job_entry(None)
            self._json_response({"id": job_id, "command": cmd})

            def run():
                pw_file = None
                try:
                    if use_sshpass:
                        pw_file = tempfile.NamedTemporaryFile(mode='w', delete=False, prefix='rsyncgui_pw_')
                        pw_file.write(password)
                        pw_file.close()
                        os.chmod(pw_file.name, 0o600)
                        cmd_list = ["sshpass", "-f", pw_file.name, "rsync"] + args
                    else:
                        cmd_list = ["rsync"] + args

                    proc = subprocess.Popen(
                        cmd_list,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        preexec_fn=os.setsid,
                    )
                    processes[job_id]["proc"] = proc
                    if processes[job_id].get("cancelled"):
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                    stream_output(job_id, proc)
                    proc.wait()
                    processes[job_id]["done"] = True
                    if processes[job_id].get("cancelled"):
                        processes[job_id]["exitCode"] = proc.returncode if proc.returncode is not None else -15
                    else:
                        processes[job_id]["exitCode"] = proc.returncode
                    pair_id = job.get("pairId", "unknown")
                    log_texts = []
                    for entry in processes[job_id]["log"]:
                        log_texts.append(entry.get("text", ""))
                    save_pair_log(pair_id, {
                        "timestamp": time.time(),
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "exitCode": processes[job_id]["exitCode"],
                        "command": cmd,
                        "log": "\n".join(log_texts),
                    })
                except Exception as e:
                    if job_id in processes:
                        processes[job_id]["log"].append({"type": "error", "text": str(e)})
                        processes[job_id]["done"] = True
                        processes[job_id]["exitCode"] = -1
                finally:
                    if pw_file and os.path.exists(pw_file.name):
                        os.unlink(pw_file.name)

            threading.Thread(target=run, daemon=True).start()
        except Exception as ex:
            self._json_response({"error": str(ex)}, 500)

    def _cancel(self, job_id):
        p = processes.get(job_id)
        if not p:
            self._json_response({"error": "not found"}, 404)
            return
        proc = p.get("proc")
        if p.get("done"):
            self._json_response({"ok": False, "error": "already finished"})
            return
        if not proc:
            # 起動直後（Popen前）の取り消し
            p["cancelled"] = True
            p["done"] = True
            p["exitCode"] = -15
            p["log"].append({"type": "error", "text": "（ユーザー操作により中断されました）"})
            self._json_response({"ok": True})
            return
        try:
            p["cancelled"] = True
            pgid = os.getpgid(proc.pid)
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            def force_kill_if_needed():
                time.sleep(1)
                try:
                    if proc.poll() is None:
                        os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    if proc.poll() is None:
                        os.kill(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    subprocess.run(
                        ["pkill", "-9", "-P", str(proc.pid)],
                        capture_output=True, timeout=3
                    )
                except Exception:
                    pass
            threading.Thread(target=force_kill_if_needed, daemon=True).start()
            p["log"].append({"type": "error", "text": "（ユーザー操作により中断されました）"})
            self._json_response({"ok": True})
        except ProcessLookupError:
            self._json_response({"ok": False, "error": "process already exited"})
        except Exception as ex:
            self._json_response({"error": str(ex)}, 500)

    def _cancel_all(self):
        stopped = 0
        for job_id, p in list(processes.items()):
            proc = p.get("proc")
            if not proc or p.get("done"):
                continue
            try:
                p["cancelled"] = True
                pgid = os.getpgid(proc.pid)
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.kill(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    subprocess.run(
                        ["pkill", "-9", "-P", str(proc.pid)],
                        capture_output=True, timeout=3
                    )
                except Exception:
                    pass
                p["log"].append({"type": "error", "text": "（全停止により中断されました）"})
                p["done"] = True
                p["exitCode"] = -9
                stopped += 1
            except Exception:
                pass
        self._json_response({"ok": True, "stopped": stopped})

    def _restart(self):
        self._json_response({"ok": True, "message": "再起動します"})
        def do_restart():
            time.sleep(0.5)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=do_restart, daemon=True).start()

    def _admin_restart(self):
        self._json_response({"ok": True})
        def do_restart():
            time.sleep(0.8)
            subprocess.run(["systemctl", "restart", SERVICE_NAME],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=do_restart, daemon=True).start()

    def _admin_update(self):
        try:
            fetched = {}
            for rel in UPDATE_FILES:
                req = urllib.request.Request(GITHUB_RAW + rel,
                                             headers={"User-Agent": "rsyncgui-updater"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    fetched[rel] = resp.read().decode("utf-8")
            if not fetched["server.py"].startswith("#!") or "</html>" not in fetched["public/index.html"]:
                raise ValueError("ダウンロードしたファイルが不正です")
            changed = False
            for rel, new_text in fetched.items():
                try:
                    with open(os.path.join(BASE_DIR, rel), encoding="utf-8") as f:
                        cur_text = f.read()
                except Exception:
                    cur_text = ""
                if cur_text != new_text:
                    changed = True
                    break
            if not changed:
                self._json_response({"ok": True, "updated": False})
                return
            compile(fetched["server.py"], "server.py", "exec")
            new_paths = {}
            for rel, new_text in fetched.items():
                tmp_path = os.path.join(BASE_DIR, rel + ".new")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(new_text)
                new_paths[rel] = tmp_path
            for rel, tmp_path in new_paths.items():
                os.replace(tmp_path, os.path.join(BASE_DIR, rel))
            try:
                os.chmod(os.path.join(BASE_DIR, "server.py"), 0o755)
            except OSError:
                pass
        except Exception as e:
            self._json_response({"ok": False, "error": str(e)}, 500)
            return
        self._json_response({"ok": True, "updated": True})
        def do_restart():
            time.sleep(1.0)
            subprocess.run(["systemctl", "restart", SERVICE_NAME],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=do_restart, daemon=True).start()

    def _status(self, job_id):
        p = processes.get(job_id)
        if not p:
            self._json_response({"error": "not found"}, 404)
            return
        log = p["log"][:]
        p["log"] = []
        self._json_response({
            "done": p["done"],
            "exitCode": p["exitCode"],
            "log": log,
            "progress": p.get("progress", {"percent": 0, "transferred": 0, "rate": "", "eta": ""}),
            "cancelled": p.get("cancelled", False),
        })


def kill_orphaned_rsync():
    """起動時の残留rsync掃除。cron実行中の正規ジョブを殺さないよう何もしない。
    旧版では無条件に `rsync.*--info=progress2` を SIGTERM しており、
    cron実行中ジョブや他ユーザーのrsyncまで殺す危険があったため無効化。"""
    return


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    server = http.server.ThreadingHTTPServer((host, PORT), Handler)
    server.daemon_threads = True
    print(f"rsyncGUI running at http://{host}:{PORT}")

    kill_orphaned_rsync()

    cfg = load_config()
    pairs = cfg.get("pairs", [])
    start_interval_scheduler(pairs)
    if pairs:
        try:
            sync_crontab(pairs)
            print("[rsyncgui] crontab restored from config.json")
        except Exception as e:
            print(f"[rsyncgui] crontab restore failed: {e}")

    def shutdown(sig, frame):
        print("\nShutting down...")
        for pid in list(interval_processes.keys()):
            sf = interval_processes[pid].get("stop_flag")
            if isinstance(sf, dict):
                sf["stop"] = True
        def force_exit():
            time.sleep(3)
            os._exit(0)
        threading.Thread(target=force_exit, daemon=True).start()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
