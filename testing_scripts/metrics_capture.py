#!/usr/bin/env python3
"""
Capture and analyze file-transfer performance metrics for the 602 final project baseline.

Metrics reported:
  1. Completion time
  2. Throughput (wire bytes / time)
  3. Goodput and overhead ratio: (throughput - goodput) / throughput * 100%
  4. Tail latency: P95(device transfer time) / (last_finish - first_finish)
  5. Packet loss / retransmission rate (from TCP analysis fields in Wireshark/tshark)
  6. CPU efficiency: system CPU-seconds / total useful bytes (MiB)

Requires Wireshark/tshark on PATH (or set TSHARK_PATH).
Network metrics come from a .pcap captured during the transfer.
CPU metrics come from psutil sampling while capture runs.

Typical workflow (recommended)
------------------------------
1. Edit experiment.json (your IP, 2 receiver IPs, file path, optional file_count).
2. Run everything (CPU + capture + localsend-cli send + analysis):

   python metrics_capture.py go experiment.json

For candidate=localsend, send_mode defaults to auto and uses:
   localsend-cli send <receiver_ip> <file_path>
to both phones in parallel. Other candidates use manual send (send_mode=manual).

devices.json example (optional, for multi-receiver fairness / tail latency):

{
  "start_epoch": 1716200000.0,
  "devices": [
    {"name": "phone_a", "ip": "192.168.1.10", "start_epoch": 1716200000.1, "finish_epoch": 1716200012.4, "bytes": 104857600},
    {"name": "phone_b", "ip": "192.168.1.11", "start_epoch": 1716200000.2, "finish_epoch": 1716200018.9, "bytes": 104857600}
  ]
}

If devices.json is omitted, completion time and fairness are derived from the pcap
using per-IP byte counts (best effort). For accurate tail latency in multi-device
tests, record finish times per device and supply devices.json.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import http.client
import json
import mimetypes
import os
import shutil
import signal
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]

try:
    import requests
    import urllib3
except ImportError:
    requests = None  # type: ignore[assignment]
    urllib3 = None  # type: ignore[assignment]


DEFAULT_CANDIDATES = ("localsend", "airdrop", "quickshare", "bittorrent", "hopswift")
LOCALSEND_SENDER_PORT = 53317


@dataclass
class ExperimentConfig:
    candidate: str
    my_ip: str
    receiver_ips: list[str]
    file_path: Path
    file_count: int = 1
    receiver_names: list[str] = field(default_factory=list)
    interface: str | None = None
    output_dir: Path = Path("runs/experiment")
    summary_csv: Path = Path("results_summary.csv")
    cpu_interval: float = 0.5
    tshark_path: str | None = None
    send_mode: str = "auto"
    localsend_port: int = 53317

    @property
    def uses_auto_send(self) -> bool:
        return self.send_mode == "auto" and self.candidate == "localsend"

    @property
    def single_file_size_bytes(self) -> int:
        return self.file_path.stat().st_size

    @property
    def bytes_per_receiver(self) -> int:
        """Total bytes each receiver should get (file_count × one file)."""
        return self.file_count * self.single_file_size_bytes

    @property
    def file_size_bytes(self) -> int:
        """Alias for bytes_per_receiver (used in devices.json and per-device metrics)."""
        return self.bytes_per_receiver

    @property
    def total_transfer_bytes(self) -> int:
        """All application data in this run (every receiver, every file)."""
        return self.bytes_per_receiver * len(self.receiver_ips)

    @property
    def capture_filter(self) -> str:
        hosts = " or ".join(f"host {ip}" for ip in self.receiver_ips)
        return f"host {self.my_ip} and ({hosts})"

    @property
    def display_filter(self) -> str:
        ips = [self.my_ip, *self.receiver_ips]
        return " or ".join(f"ip.addr == {ip}" for ip in ips)


def find_tshark(explicit: str | None = None) -> str:
    candidates: list[str] = []

    def add(path: str | None) -> None:
        if path and path not in candidates:
            candidates.append(path)

    add(explicit)
    add(os.environ.get("TSHARK_PATH"))
    add(shutil.which("tshark"))
    add(r"C:\Program Files\Wireshark\tshark.exe")
    add(r"C:\Program Files (x86)\Wireshark\tshark.exe")
    add("/mnt/c/Program Files/Wireshark/tshark.exe")
    add("/mnt/c/Program Files (x86)/Wireshark/tshark.exe")
    add("/usr/bin/tshark")
    add("/usr/local/bin/tshark")
    add("/Applications/Wireshark.app/Contents/MacOS/tshark")

    if os.name == "nt":
        try:
            import winreg

            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for key_path in (
                    r"SOFTWARE\Wireshark",
                    r"SOFTWARE\WOW6432Node\Wireshark",
                ):
                    try:
                        with winreg.OpenKey(hive, key_path) as key:
                            install_path, _ = winreg.QueryValueEx(key, "InstallPath")
                            add(str(Path(install_path) / "tshark.exe"))
                    except OSError:
                        pass
        except ImportError:
            pass

        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                add(str(Path(base) / "Wireshark" / "tshark.exe"))

        try:
            where = subprocess.run(
                ["where.exe", "tshark"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            for line in where.stdout.splitlines():
                add(line.strip())
        except OSError:
            pass

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate

    searched = "\n  ".join(c for c in candidates if c)
    raise FileNotFoundError(
        "tshark not found. Wireshark GUI includes tshark, but it is often not on PATH.\n"
        "Fix options:\n"
        "  1. Add to experiment.json: \"tshark_path\": \"C:/Program Files/Wireshark/tshark.exe\"\n"
        "  2. Or in PowerShell: $env:TSHARK_PATH = \"C:\\Program Files\\Wireshark\\tshark.exe\"\n"
        "  3. Or add Wireshark folder to your system PATH.\n"
        f"Searched:\n  {searched if searched else '(no candidates)'}"
    )


def wsl_to_windows_path(raw: str) -> str | None:
    if not raw.startswith("/mnt/"):
        return None
    parts = raw.replace("\\", "/").split("/")
    if len(parts) < 4 or parts[1] != "mnt" or len(parts[2]) != 1:
        return None
    rest = parts[3:]
    if not rest:
        return f"{parts[2].upper()}:\\"
    return f"{parts[2].upper()}:\\" + "\\".join(rest)


def is_windows_exe(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return normalized.endswith(".exe")


def path_for_tshark(path: Path, tshark_path: str | None) -> str:
    resolved = str(path.resolve())
    tshark = find_tshark(tshark_path)
    if is_windows_exe(tshark):
        converted = wsl_to_windows_path(resolved)
        if converted:
            return converted
    return resolved


def resolve_file_path(raw: str) -> Path:
    candidates = [Path(raw).expanduser()]
    win_path = wsl_to_windows_path(raw)
    if win_path:
        candidates.append(Path(win_path))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"file_path not found: {raw}")


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    last_print = time.time()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            now = time.time()
            if now - last_print >= 2:
                print(f"[localsend] Hashing file: {done / total * 100:5.1f}%", flush=True)
                last_print = now
    return digest.hexdigest()


def sha256_cache_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.sha256.json")


def cached_sha256_file(path: Path) -> str:
    stat = path.stat()
    cache_path = sha256_cache_path(path)
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                cache.get("path") == str(path)
                and cache.get("size") == stat.st_size
                and cache.get("mtime_ns") == stat.st_mtime_ns
                and isinstance(cache.get("sha256"), str)
            ):
                print(f"[localsend] Reusing cached SHA-256 from {cache_path}")
                return cache["sha256"]
        except (OSError, json.JSONDecodeError):
            pass

    print("[localsend] Computing SHA-256 once; later runs reuse the cache if the file is unchanged.")
    sha256 = sha256_file(path)
    payload = {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256,
    }
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[localsend] Saved SHA-256 cache to {cache_path}")
    return sha256


def localsend_fingerprint() -> str:
    try:
        from localsend_cli.crypto import ensure_cert, get_fingerprint

        ensure_cert()
        return str(get_fingerprint())
    except Exception:
        # The receiver accepts a stable fingerprint string; using a hash of the
        # host keeps this self-contained if localsend_cli internals change.
        return hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()


def localsend_device_info() -> dict[str, object]:
    """Sender metadata for prepare-upload (same shape as official LocalSend / localsend-cli)."""
    return {
        "alias": f"metrics-capture@{socket.gethostname()}",
        "version": "2.0",
        "deviceModel": platform_label(),
        "deviceType": "desktop",
        "fingerprint": localsend_fingerprint(),
        "port": LOCALSEND_SENDER_PORT,
        "protocol": "https",
        "download": False,
    }


def platform_label() -> str:
    if sys.platform.startswith("win"):
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("linux"):
        return "Linux"
    return sys.platform


class ProgressFile:
    """File-like body for requests; must expose __len__ so requests keeps Content-Length (not chunked)."""

    def __init__(self, path: Path, label: str, chunk_size: int = 8 * 1024 * 1024) -> None:
        self.path = path
        self.label = label
        self.chunk_size = chunk_size
        self.size = path.stat().st_size
        self.sent = 0
        self.started = time.time()
        self.last_print = self.started
        self.file = path.open("rb", buffering=chunk_size)

    def __len__(self) -> int:
        return self.size

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self.chunk_size
        chunk = self.file.read(min(size, self.chunk_size))
        if chunk:
            self.sent += len(chunk)
            now = time.time()
            if now - self.last_print >= 5:
                elapsed = max(now - self.started, 1e-9)
                speed = self.sent / elapsed
                print(
                    f"[localsend] {self.label}: {self.sent / self.size * 100:5.1f}% "
                    f"({format_bps(speed)})",
                    flush=True,
                )
                self.last_print = now
        return chunk

    def close(self) -> None:
        self.file.close()


def upload_file_http_client(
    ip: str,
    port: int,
    upload_path: str,
    file_path: Path,
    label: str,
    chunk_size: int = 1024 * 1024,
) -> tuple[int, str]:
    """Upload the file with a plain http.client loop.

    This avoids urllib3's request-body machinery for very large TLS uploads,
    which some LocalSend desktop receivers close mid-transfer.
    """
    size = file_path.stat().st_size
    context = ssl._create_unverified_context()
    conn = http.client.HTTPSConnection(ip, port, timeout=60, context=context)
    sent = 0
    started = time.time()
    last_print = started

    try:
        conn.putrequest("POST", upload_path, skip_accept_encoding=True)
        conn.putheader("Host", f"{ip}:{port}")
        conn.putheader("User-Agent", "metrics-capture-localsend/1.0")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        conn.endheaders()

        with file_path.open("rb", buffering=chunk_size) as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                now = time.time()
                if now - last_print >= 5:
                    elapsed = max(now - started, 1e-9)
                    print(
                        f"[localsend] {label}: {sent / size * 100:5.1f}% "
                        f"({format_bps(sent / elapsed)})",
                        flush=True,
                    )
                    last_print = now

        response = conn.getresponse()
        body = response.read(512).decode("utf-8", errors="replace")
        return response.status, body
    finally:
        conn.close()


def send_localsend_to_device(
    ip: str,
    name: str,
    file_path: Path,
    port: int,
    sha256: str,
) -> tuple[str, str, float, float]:
    if requests is None:
        raise RuntimeError("requests is required. Run: pip install -r requirements.txt")
    if urllib3 is not None:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    base_url = f"https://{ip}:{port}/api/localsend/v2"
    size = file_path.stat().st_size
    file_id = str(uuid.uuid4())[:8]
    modified = datetime.datetime.fromtimestamp(
        file_path.stat().st_mtime,
        tz=datetime.timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    accessed = datetime.datetime.fromtimestamp(
        file_path.stat().st_atime,
        tz=datetime.timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = {
        "info": localsend_device_info(),
        "files": {
            file_id: {
                "id": file_id,
                "fileName": file_path.name,
                "size": size,
                "fileType": mimetypes.guess_type(file_path.name)[0] or "application/octet-stream",
                "sha256": sha256,
                "metadata": {
                    "modified": modified,
                    "accessed": accessed,
                },
            }
        },
    }

    print(f"[localsend] Requesting auto-accept from {name} ({ip})...")
    start = time.time()
    session = requests.Session()
    session.verify = False

    try:
        response = session.post(f"{base_url}/prepare-upload", json=payload, timeout=60)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"prepare-upload to {name} ({ip}:{port}) failed: {exc}"
        ) from exc

    if response.status_code == 403:
        raise RuntimeError(f"{name} ({ip}) rejected the transfer")
    if response.status_code == 204:
        raise RuntimeError(f"{name} ({ip}) says there is nothing to transfer")
    if response.status_code != 200:
        raise RuntimeError(
            f"{name} ({ip}) prepare-upload failed: HTTP {response.status_code} {response.text[:120]}"
        )

    data = response.json()
    session_id = data["sessionId"]
    token = data["files"][file_id]
    upload_url = f"{base_url}/upload?sessionId={session_id}&fileId={file_id}&token={token}"

    print(f"[localsend] Accepted by {name}; uploading {size:,} bytes...")
    body = ProgressFile(file_path, name)
    try:
        upload = session.post(
            upload_url,
            data=body,
            timeout=(60, None),
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            },
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"{name} ({ip}) upload connection failed: {exc}") from exc
    finally:
        body.close()
    finish = time.time()
    if upload.status_code != 200:
        raise RuntimeError(f"{name} ({ip}) upload failed: HTTP {upload.status_code} {upload.text[:120]}")

    print(f"[localsend] Finished {name} ({ip}) in {finish - start:.1f}s")
    return ip, name, start, finish


def run_localsend_sync_send(cfg: ExperimentConfig, sha256: str) -> tuple[float, dict[str, float]]:
    print()
    print("Sending via LocalSend API to BOTH receivers in parallel.")
    print("No-manual mode: receivers must auto-accept incoming LocalSend transfers.")
    if cfg.file_count > 1:
        print(f"Batch: {cfg.file_count} files × {cfg.single_file_size_bytes / (1024**3):.2f} GiB "
              f"= {cfg.bytes_per_receiver / (1024**3):.2f} GiB per receiver.")

    start_epoch = time.time()
    finish_times: dict[str, float] = {}

    for index in range(1, cfg.file_count + 1):
        if cfg.file_count > 1:
            print(f"\n[localsend] File {index}/{cfg.file_count}: {cfg.file_path.name}")
        with ThreadPoolExecutor(max_workers=len(cfg.receiver_ips)) as pool:
            futures = [
                pool.submit(
                    send_localsend_to_device,
                    ip,
                    name,
                    cfg.file_path,
                    cfg.localsend_port,
                    sha256,
                )
                for name, ip in zip(cfg.receiver_names, cfg.receiver_ips)
            ]
            for future in as_completed(futures):
                ip, name, _, finish = future.result()
                finish_times[ip] = finish
                print(f"[localsend] {name} done with file {index}/{cfg.file_count}.")

    return start_epoch, finish_times


def run_manual_transfer(cfg: ExperimentConfig) -> tuple[float, dict[str, float]]:
    keys = [chr(ord("A") + i) for i in range(len(cfg.receiver_ips))]
    one_gib = cfg.single_file_size_bytes / (1024**3)
    per_rx_gib = cfg.bytes_per_receiver / (1024**3)
    print()
    print(f"Manual mode: use the {cfg.candidate} app.")
    if cfg.file_count > 1:
        print(
            f"Select all {cfg.file_count} files in LocalSend and send them together "
            f"({one_gib:.2f} GiB each, {per_rx_gib:.2f} GiB total per receiver)."
        )
        print(
            f"Size reference: {cfg.file_path.name} "
            f"(each selected file should be about this size)."
        )
    else:
        print(f"Send one file: {cfg.file_path.name} ({one_gib:.2f} GiB)")
    print("Receivers:")
    for key, name, ip in zip(keys, cfg.receiver_names, cfg.receiver_ips):
        print(f"  {key} = {name} ({ip})")
    print()
    print("Capturing background traffic. Please start the transfer in LocalSend now.")
    start_epoch = time.time()
    print()
    prompt_enter("Press Enter when the transfer has COMPLETELY FINISHED to stop Wireshark... ")
    
    finish_times: dict[str, float] = {}
    for ip in cfg.receiver_ips:
        finish_times[ip] = time.time()

    return start_epoch, finish_times


def run_tshark(args: list[str], *, check: bool = True, tshark_path: str | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [find_tshark(tshark_path), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def list_interfaces() -> list[str]:
    result = run_tshark(["-D"])
    interfaces: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: "1. \Device\NPF_{...} (Wi-Fi)"
        if ". " in line:
            interfaces.append(line.split(". ", 1)[1])
    return interfaces


def psutil_interface_for_ip(ip: str) -> str | None:
    if psutil is None:
        return None
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET and addr.address == ip:
                return iface
    return None


def resolve_tshark_interface(preferred: str | None, my_ip: str | None) -> str:
    raw = run_tshark(["-D"]).stdout
    interfaces: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if ". " in line:
            index, display = line.split(". ", 1)
            interfaces.append((index, display))

    if not interfaces:
        raise RuntimeError("No tshark capture interfaces found.")

    if preferred:
        if preferred.isdigit():
            return preferred
        for index, display in interfaces:
            if preferred == display or preferred in display:
                # On Windows, the display string includes " (Wi-Fi)" but -i is
                # most reliable with the numeric index from tshark -D.
                return index
        available = "\n  ".join(f"{index}: {display}" for index, display in interfaces)
        raise ValueError(f"Interface '{preferred}' not found. Available:\n  {available}")

    if my_ip:
        psutil_iface = psutil_interface_for_ip(my_ip)
        if psutil_iface:
            for index, display in interfaces:
                if psutil_iface in display:
                    return index

    for hint in ("Wi-Fi", "Ethernet", "en0"):
        for index, display in interfaces:
            if hint in display and "WSL" not in display and "Loopback" not in display:
                return index

    return interfaces[0][0]


def load_experiment_config(path: Path) -> ExperimentConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    receiver_ips = data.get("receiver_ips") or []
    if len(receiver_ips) != 1:
        raise ValueError("experiment.json must contain exactly 1 receiver_ips.")

    my_ip = data.get("my_ip")
    if not my_ip:
        raise ValueError("experiment.json must set my_ip (this laptop's IP).")

    raw_path = data.get("file_path", "")
    file_path = resolve_file_path(raw_path) if raw_path else Path("")

    receiver_names = data.get("receiver_names") or ["receiver_1"]
    if len(receiver_names) != 1:
        raise ValueError("receiver_names must contain exactly 1 names when provided.")

    candidate = data.get("candidate", "localsend")
    if candidate not in DEFAULT_CANDIDATES:
        raise ValueError(f"candidate must be one of: {', '.join(DEFAULT_CANDIDATES)}")

    send_mode = data.get("send_mode")
    if send_mode is None:
        send_mode = "auto" if candidate == "localsend" else "manual"
    if send_mode not in ("auto", "manual"):
        raise ValueError("send_mode must be 'auto' or 'manual'")

    file_count = int(data.get("file_count", 1))
    if file_count < 1:
        raise ValueError("file_count must be >= 1")

    return ExperimentConfig(
        candidate=candidate,
        my_ip=my_ip,
        receiver_ips=receiver_ips,
        receiver_names=receiver_names,
        file_path=file_path,
        file_count=file_count,
        interface=data.get("interface"),
        output_dir=Path(data.get("output_dir", "runs/experiment")),
        summary_csv=Path(data.get("summary_csv", "results_summary.csv")),
        cpu_interval=float(data.get("cpu_interval", 0.5)),
        tshark_path=data.get("tshark_path"),
        send_mode=send_mode,
        localsend_port=int(data.get("localsend_port", 53317)),
    )


def prompt_enter(message: str) -> float:
    input(message)
    return time.time()


def infer_finish_times_from_pcap(
    records: list[PacketRecord],
    cfg: ExperimentConfig,
    fallback_start_epoch: float,
) -> list[DeviceTiming]:
    per_ip_last: dict[str, float] = {ip: fallback_start_epoch for ip in cfg.receiver_ips}
    per_ip_first: dict[str, float] = {ip: float('inf') for ip in cfg.receiver_ips}
    per_ip_bytes: dict[str, int] = {ip: 0 for ip in cfg.receiver_ips}

    completed_ips = set()
    
    app_ts_path = Path(__file__).parent.resolve() / "app_timestamps.json"
    app_start = None
    app_end = None
    if app_ts_path.exists():
        try:
            ts_data = json.loads(app_ts_path.read_text(encoding="utf-8"))
            app_start = ts_data.get("start") / 1000.0
            app_end = ts_data.get("end") / 1000.0
        except Exception:
            pass
            
    for record in records:
        if app_start and app_end:
            if record.epoch < app_start or record.epoch > app_end:
                continue
            for ip in cfg.receiver_ips:
                if record.dst_ip == ip or record.src_ip == ip:
                    per_ip_last[ip] = app_end
                    per_ip_first[ip] = app_start
                    if record.payload_len > 0 and not record.is_retransmission:
                        if record.dst_ip == ip:
                            per_ip_bytes[ip] += record.payload_len
            continue

        # Ignore background traffic (mDNS, pings) that happened BEFORE you pressed Enter
        if record.epoch < fallback_start_epoch:
            continue
            
        for ip in cfg.receiver_ips:
            if ip in completed_ips:
                continue
                
            if record.dst_ip == ip or record.src_ip == ip:
                # If there is a silence gap of > 1.0 seconds after the transfer started, 
                # we assume the actual file burst has finished and this is just a background poll.
                if per_ip_last[ip] != fallback_start_epoch and (record.epoch - per_ip_last[ip] > 1.0):
                    completed_ips.add(ip)
                    continue
                    
                per_ip_last[ip] = max(per_ip_last[ip], record.epoch)
                per_ip_first[ip] = min(per_ip_first[ip], record.epoch)
                if record.payload_len > 0 and not record.is_retransmission:
                    if record.dst_ip == ip:
                        per_ip_bytes[ip] += record.payload_len

    devices: list[DeviceTiming] = []
    for name, ip in zip(cfg.receiver_names, cfg.receiver_ips):
        actual_start = per_ip_first[ip] if per_ip_first[ip] != float('inf') else fallback_start_epoch
        devices.append(
            DeviceTiming(
                name=name,
                ip=ip,
                start_epoch=actual_start,
                finish_epoch=per_ip_last[ip],
                bytes_transferred=cfg.file_size_bytes,
            )
        )
    return devices


def save_devices(path: Path, start_epoch: float, devices: list[DeviceTiming]) -> None:
    payload = {
        "start_epoch": start_epoch,
        "devices": [
            {
                "name": d.name,
                "ip": d.ip,
                "start_epoch": d.start_epoch,
                "finish_epoch": d.finish_epoch,
                "bytes": d.bytes_transferred,
            }
            for d in devices
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@dataclass
class PacketRecord:
    epoch: float
    frame_len: int
    payload_len: int
    is_retransmission: bool
    is_lost_segment: bool
    is_duplicate_ack: bool
    src_ip: str
    dst_ip: str
    protocol: str


@dataclass
class CpuSample:
    epoch: float
    cpu_percent_total: float
    cpu_percent_system: float


@dataclass
class DeviceTiming:
    name: str
    ip: str | None = None
    start_epoch: float | None = None
    finish_epoch: float | None = None
    bytes_transferred: int | None = None


@dataclass
class MetricsReport:
    candidate: str
    file_size_bytes: int | None
    completion_time_s: float
    throughput_bps: float
    goodput_bps: float
    overhead_ratio_percent: float
    tail_latency_ratio: float | None
    packet_loss_rate_percent: float | None
    retransmission_rate_percent: float
    cpu_efficiency_system_s_per_mib: float | None
    fairness_jain: float | None
    total_wire_bytes: int
    total_goodput_bytes: int
    total_packets: int
    retransmitted_packets: int
    lost_segment_packets: int
    duplicate_ack_packets: int
    file_count: int = 1
    device_speeds_bps: dict[str, float] = field(default_factory=dict)
    device_completion_times_s: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class CpuMonitor:
    def __init__(self, interval_s: float = 0.5) -> None:
        if psutil is None:
            raise RuntimeError("psutil is required. Install with: pip install psutil")
        self.interval_s = interval_s
        self.samples: list[CpuSample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.total_cpu_seconds = 0.0
        self.total_system_cpu_seconds = 0.0

    def start(self) -> None:
        psutil.cpu_percent(interval=None)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        last = time.time()
        while not self._stop.is_set():
            now = time.time()
            elapsed = now - last
            last = now
            total = psutil.cpu_percent(interval=None)
            system = psutil.cpu_times_percent(interval=None).system
            self.samples.append(CpuSample(epoch=now, cpu_percent_total=total, cpu_percent_system=system))
            self.total_cpu_seconds += (total / 100.0) * elapsed
            self.total_system_cpu_seconds += (system / 100.0) * elapsed
            time.sleep(self.interval_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "samples": [asdict(s) for s in self.samples],
                    "total_cpu_seconds": self.total_cpu_seconds,
                    "total_system_cpu_seconds": self.total_system_cpu_seconds,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> tuple[float, float]:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data["total_cpu_seconds"]), float(data.get("total_system_cpu_seconds", 0.0))


class CaptureSession:
    def __init__(
        self,
        interface: str,
        output_pcap: Path,
        display_filter: str | None = None,
        ring_buffer_mb: int | None = None,
        tshark_path: str | None = None,
    ) -> None:
        self.interface = interface
        self.output_pcap = output_pcap
        self.display_filter = display_filter
        self.ring_buffer_mb = ring_buffer_mb
        self.tshark_path = tshark_path
        self.process: subprocess.Popen[str] | None = None
        self.stderr = ""

    def _pcap_arg(self) -> str:
        return path_for_tshark(self.output_pcap, self.tshark_path)

    def start(self) -> None:
        self.output_pcap.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            find_tshark(self.tshark_path),
            "-i",
            self.interface,
            "-w",
            self._pcap_arg(),
            "-q",
        ]
        if self.ring_buffer_mb:
            cmd.extend(["-b", f"filessize:{self.ring_buffer_mb}"])
        kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            
        self.process = subprocess.Popen(cmd, **kwargs)
        time.sleep(1.0)
        if self.process.poll() is not None:
            stdout, stderr = self.process.communicate(timeout=5)
            self.stderr = stderr or stdout or ""
            raise RuntimeError(
                "tshark exited before capture started. "
                f"Command: {' '.join(cmd)}\n{self.stderr.strip()}"
            )
        print(f"Capturing packets to {self.output_pcap.resolve()}")

    def stop(self) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            if os.name == "nt":
                try:
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                    time.sleep(0.5)
                except (ValueError, OSError):
                    pass
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        try:
            stdout, stderr = self.process.communicate(timeout=5)
            self.stderr = stderr or stdout or self.stderr
        except (ValueError, subprocess.TimeoutExpired):
            pass
        self.process = None


def parse_pcap(pcap: Path, bpf_hint: str | None = None, tshark_path: str | None = None) -> list[PacketRecord]:
    fields = [
        "frame.time_epoch",
        "frame.len",
        "tcp.len",
        "udp.length",
        "tcp.analysis.retransmission",
        "tcp.analysis.lost_segment",
        "tcp.analysis.duplicate_ack",
        "ip.src",
        "ip.dst",
        "ipv6.src",
        "ipv6.dst",
        "_ws.col.Protocol",
    ]
    args = ["-r", path_for_tshark(pcap, tshark_path), "-T", "fields", "-E", "separator=|", "-E", "occurrence=f"]
    if bpf_hint:
        args.extend(["-Y", bpf_hint])
    args.extend(["-e", fields[0]])
    for f in fields[1:]:
        args.extend(["-e", f])

    result = run_tshark(args, tshark_path=tshark_path)
    records: list[PacketRecord] = []

    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        while len(parts) < len(fields):
            parts.append("")

        epoch = float(parts[0]) if parts[0] else 0.0
        frame_len = int(parts[1]) if parts[1] else 0
        tcp_len = int(parts[2]) if parts[2] else 0
        udp_len = int(parts[3]) if parts[3] else 0
        payload_len = tcp_len if tcp_len > 0 else max(udp_len - 8, 0) if udp_len > 8 else 0

        src = parts[7] or parts[9]
        dst = parts[8] or parts[10]
        protocol = parts[11] or "unknown"

        records.append(
            PacketRecord(
                epoch=epoch,
                frame_len=frame_len,
                payload_len=payload_len,
                is_retransmission=bool(parts[4]),
                is_lost_segment=bool(parts[5]),
                is_duplicate_ack=bool(parts[6]),
                src_ip=src,
                dst_ip=dst,
                protocol=protocol,
            )
        )

    return records


def load_devices(path: Path | None) -> tuple[float | None, list[DeviceTiming]]:
    if not path:
        return None, []
    data = json.loads(path.read_text(encoding="utf-8"))
    start_epoch = data.get("start_epoch")
    devices = []
    for item in data.get("devices", []):
        devices.append(
            DeviceTiming(
                name=item["name"],
                ip=item.get("ip"),
                start_epoch=item.get("start_epoch", start_epoch),
                finish_epoch=item.get("finish_epoch"),
                bytes_transferred=item.get("bytes"),
            )
        )
    return start_epoch, devices


def infer_sender_ip(records: list[PacketRecord]) -> str | None:
    sent: dict[str, int] = {}
    for r in records:
        if r.payload_len <= 0:
            continue
        sent[r.src_ip] = sent.get(r.src_ip, 0) + r.payload_len
    if not sent:
        return None
    return max(sent, key=sent.get)


def per_ip_goodput(records: list[PacketRecord], sender_ip: str | None) -> dict[str, int]:
    totals: dict[str, int] = {}
    for r in records:
        if r.payload_len <= 0 or r.is_retransmission:
            continue
        receiver = r.dst_ip if not sender_ip or r.src_ip == sender_ip else r.src_ip
        totals[receiver] = totals.get(receiver, 0) + r.payload_len
    return totals


def device_transfer_durations(devices: list[DeviceTiming]) -> list[float]:
    durations: list[float] = []
    for d in devices:
        if d.start_epoch is not None and d.finish_epoch is not None:
            durations.append(max(d.finish_epoch - d.start_epoch, 0.0))
    return durations


def compute_tail_latency(devices: list[DeviceTiming]) -> float | None:
    finishes = [d.finish_epoch for d in devices if d.finish_epoch is not None]
    if len(finishes) < 2:
        return None
    durations = device_transfer_durations(devices)
    if not durations:
        return None
    span = max(finishes) - min(finishes)
    if span <= 0:
        return None
    p95 = statistics.quantiles(durations, n=20)[18] if len(durations) >= 2 else durations[0]
    return p95 / span


def compute_fairness(speeds: dict[str, float]) -> float | None:
    values = [v for v in speeds.values() if v > 0]
    if len(values) < 2:
        return None
    n = len(values)
    s = sum(values)
    s2 = sum(v * v for v in values)
    if s2 <= 0:
        return None
    return (s * s) / (n * s2)


def compute_time_window(
    records: list[PacketRecord],
    devices: list[DeviceTiming],
    global_start: float | None,
) -> tuple[float, float, float]:
    """Return (first_epoch, last_epoch, completion_time_s) for metrics 1–2."""
    pcap_first = records[0].epoch
    pcap_last = records[-1].epoch

    finishes = [d.finish_epoch for d in devices if d.finish_epoch is not None]
    starts = [d.start_epoch for d in devices if d.start_epoch is not None]

    if finishes:
        if starts:
            first = min(starts)
        else:
            first = global_start if global_start is not None else pcap_first
        last = max(finishes)
        completion = max(last - first, 1e-9)
        return first, last, completion

    return pcap_first, pcap_last, max(pcap_last - pcap_first, 1e-9)


# CSV columns: 7 project metrics (metric 3 and 5 each use two columns) + optional extras.
SUMMARY_CSV_COLUMNS = [
    "candidate",
    "file_count",
    "completion_time_s",  # 1 (last receiver done − start)
    "device_1_name",
    "device_1_completion_time_s",
    "device_2_name",
    "device_2_completion_time_s",
    "throughput_bps",  # 2
    "goodput_bps",  # 3
    "overhead_ratio_percent",  # 3 (overhead %)
    "tail_latency_ratio",  # 4
    "packet_loss_rate_percent",  # 5
    "retransmission_rate_percent",  # 5
    "cpu_efficiency_system_s_per_mib",  # 6
    "fairness_jain",  # 7
    "total_wire_bytes",
    "total_goodput_bytes",
    "total_packets",
]


def analyze_records(
    records: list[PacketRecord],
    *,
    candidate: str,
    file_size_bytes: int | None,
    file_count: int = 1,
    devices: list[DeviceTiming],
    global_start: float | None,
    cpu_total_seconds: float | None,
    cpu_system_seconds: float | None,
) -> MetricsReport:
    notes: list[str] = []

    if not records:
        raise ValueError("No packets found in capture (check interface/filter/window).")

    records.sort(key=lambda r: r.epoch)
    first_epoch, last_epoch, completion_time = compute_time_window(records, devices, global_start)

    total_wire_bytes = sum(r.frame_len for r in records)
    total_goodput_bytes = sum(r.payload_len for r in records if not r.is_retransmission)
    total_packets = len(records)
    retransmitted = sum(1 for r in records if r.is_retransmission)
    lost = sum(1 for r in records if r.is_lost_segment)
    dup_acks = sum(1 for r in records if r.is_duplicate_ack)

    throughput_bps = total_wire_bytes / completion_time
    goodput_bps = total_goodput_bytes / completion_time
    overhead_ratio = 0.0
    if throughput_bps > 0:
        overhead_ratio = max((throughput_bps - goodput_bps) / throughput_bps * 100.0, 0.0)

    retransmission_rate = (retransmitted / total_packets * 100.0) if total_packets else 0.0
    packet_loss_rate = (lost / total_packets * 100.0) if total_packets else None

    sender_ip = infer_sender_ip(records)
    ip_goodput = per_ip_goodput(records, sender_ip)

    device_speeds: dict[str, float] = {}
    device_completion_times: dict[str, float] = {}
    if devices:
        for d in devices:
            bytes_x = d.bytes_transferred
            if bytes_x is None and d.ip and d.ip in ip_goodput:
                bytes_x = ip_goodput[d.ip]
            if bytes_x is None and file_size_bytes is not None:
                bytes_x = file_size_bytes
            start = d.start_epoch if d.start_epoch is not None else first_epoch
            finish = d.finish_epoch if d.finish_epoch is not None else last_epoch
            dur = max(finish - start, 1e-9)
            if d.start_epoch is not None and d.finish_epoch is not None:
                device_completion_times[d.name] = max(d.finish_epoch - d.start_epoch, 0.0)
            if bytes_x is not None:
                device_speeds[d.name] = bytes_x / dur
    elif ip_goodput:
        for ip, nbytes in ip_goodput.items():
            device_speeds[ip] = nbytes / completion_time
        notes.append("Fairness derived from per-IP goodput in pcap; supply devices.json for precise multi-device timing.")
    else:
        notes.append("Single-flow capture; fairness/tail latency need devices.json for multi-receiver tests.")

    fairness = compute_fairness(device_speeds)
    tail_latency = compute_tail_latency(devices) if devices else None

    expected_goodput = file_size_bytes
    if file_size_bytes is not None and devices:
        expected_goodput = file_size_bytes * len(devices)
    useful_bytes = expected_goodput if expected_goodput is not None else total_goodput_bytes
    cpu_efficiency = None
    cpu_for_metric = cpu_system_seconds if cpu_system_seconds is not None else cpu_total_seconds
    if cpu_for_metric is not None and useful_bytes > 0:
        mib = useful_bytes / (1024 * 1024)
        cpu_efficiency = cpu_for_metric / mib

    if expected_goodput is not None and total_goodput_bytes < expected_goodput * 0.5:
        notes.append(
            f"Captured goodput ({total_goodput_bytes} B) is much smaller than expected "
            f"({expected_goodput} B). Check capture filter/interface or extend capture window."
        )

    return MetricsReport(
        candidate=candidate,
        file_size_bytes=file_size_bytes,
        completion_time_s=completion_time,
        throughput_bps=throughput_bps,
        goodput_bps=goodput_bps,
        overhead_ratio_percent=overhead_ratio,
        tail_latency_ratio=tail_latency,
        packet_loss_rate_percent=packet_loss_rate,
        retransmission_rate_percent=retransmission_rate,
        cpu_efficiency_system_s_per_mib=cpu_efficiency,
        fairness_jain=fairness,
        total_wire_bytes=total_wire_bytes,
        total_goodput_bytes=total_goodput_bytes,
        total_packets=total_packets,
        retransmitted_packets=retransmitted,
        lost_segment_packets=lost,
        duplicate_ack_packets=dup_acks,
        file_count=file_count,
        device_speeds_bps=device_speeds,
        device_completion_times_s=device_completion_times,
        notes=notes,
    )


def format_bps(value: float) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.2f} MiB/s"
    if value >= 1024:
        return f"{value / 1024:.2f} KiB/s"
    return f"{value:.2f} B/s"


def print_report(report: MetricsReport) -> None:
    print()
    print(f"=== Metrics: {report.candidate} ===")
    if report.file_count > 1:
        per_rx = (report.file_size_bytes or 0) / (1024**3)
        print(f"   Batch: {report.file_count} files, {per_rx:.2f} GiB per receiver")
    print(f"1. Completion time:          {report.completion_time_s:.3f} s")
    if report.device_completion_times_s:
        print("   Per-device completion:")
        for name, dur in report.device_completion_times_s.items():
            print(f"     - {name}: {dur:.3f} s")
    print(f"2. Throughput:               {format_bps(report.throughput_bps)} ({report.throughput_bps:.0f} B/s)")
    print(f"3. Goodput:                  {format_bps(report.goodput_bps)} ({report.goodput_bps:.0f} B/s)")
    print(f"   Overhead ratio:           {report.overhead_ratio_percent:.2f} %")
    if report.tail_latency_ratio is not None:
        print(f"4. Tail latency ratio:       {report.tail_latency_ratio:.4f}")
    else:
        print("4. Tail latency ratio:       N/A (needs >=2 devices with finish times)")
    if report.packet_loss_rate_percent is not None:
        print(f"5. Packet loss rate:          {report.packet_loss_rate_percent:.4f} % (tcp.analysis.lost_segment)")
    else:
        print("5. Packet loss rate:          N/A")
    print(f"   Retransmission rate:      {report.retransmission_rate_percent:.4f} %")
    if report.cpu_efficiency_system_s_per_mib is not None:
        print(f"6. CPU efficiency:           {report.cpu_efficiency_system_s_per_mib:.4f} system CPU-s / MiB")
    else:
        print("6. CPU efficiency:           N/A (run with CPU monitor or provide --cpu-log)")
    if report.fairness_jain is not None:
        print(f"7. Fairness (Jain index):    {report.fairness_jain:.4f}")
    else:
        print("7. Fairness (Jain index):    N/A")
    print()
    print(f"Wire bytes: {report.total_wire_bytes:,} | Goodput bytes: {report.total_goodput_bytes:,} | Packets: {report.total_packets:,}")
    if report.device_speeds_bps:
        print("Per-device speeds:")
        for name, speed in sorted(report.device_speeds_bps.items()):
            print(f"  - {name}: {format_bps(speed)}")
    if report.notes:
        print("Notes:")
        for note in report.notes:
            print(f"  - {note}")
    print()


def save_report(report: MetricsReport, path: Path) -> None:
    path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")


def report_to_summary_row(report: MetricsReport) -> dict[str, str]:
    """Build one CSV row with all 7 project metrics (fixed column names)."""
    device_names = list(report.device_completion_times_s.keys())
    device_times = [report.device_completion_times_s[n] for n in device_names]
    row = {
        "candidate": report.candidate,
        "file_count": str(report.file_count),
        "completion_time_s": f"{report.completion_time_s:.6f}",
        "device_1_name": device_names[0] if len(device_names) > 0 else "",
        "device_1_completion_time_s": f"{device_times[0]:.6f}" if len(device_times) > 0 else "",
        "device_2_name": device_names[1] if len(device_names) > 1 else "",
        "device_2_completion_time_s": f"{device_times[1]:.6f}" if len(device_times) > 1 else "",
        "throughput_bps": f"{report.throughput_bps:.2f}",
        "goodput_bps": f"{report.goodput_bps:.2f}",
        "overhead_ratio_percent": f"{report.overhead_ratio_percent:.4f}",
        "tail_latency_ratio": "" if report.tail_latency_ratio is None else f"{report.tail_latency_ratio:.6f}",
        "packet_loss_rate_percent": "" if report.packet_loss_rate_percent is None else f"{report.packet_loss_rate_percent:.6f}",
        "retransmission_rate_percent": f"{report.retransmission_rate_percent:.6f}",
        "cpu_efficiency_system_s_per_mib": ""
        if report.cpu_efficiency_system_s_per_mib is None
        else f"{report.cpu_efficiency_system_s_per_mib:.6f}",
        "fairness_jain": "" if report.fairness_jain is None else f"{report.fairness_jain:.6f}",
        "total_wire_bytes": str(report.total_wire_bytes),
        "total_goodput_bytes": str(report.total_goodput_bytes),
        "total_packets": str(report.total_packets),
    }
    return row


def append_csv_summary(report: MetricsReport, csv_path: Path) -> None:
    row = report_to_summary_row(report)
    write_header = not csv_path.exists()
    if not write_header:
        with csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
        if header and header != SUMMARY_CSV_COLUMNS:
            raise ValueError(
                f"{csv_path} has an outdated header.\n"
                f"  expected: {SUMMARY_CSV_COLUMNS}\n"
                f"  found:    {header}\n"
                "Delete or rename the file, then re-run analyze."
            )
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_CSV_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def cmd_interfaces(_: argparse.Namespace) -> int:
    for iface in list_interfaces():
        print(iface)
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    session = CaptureSession(args.interface, Path(args.output), display_filter=args.filter)
    print(f"Capturing on '{args.interface}' -> {args.output}")
    print("Press Ctrl+C to stop.")
    session.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping capture...")
        session.stop()
    print("Done.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pcap_path = out_dir / "capture.pcap"
    cpu_path = out_dir / "cpu.json"
    devices_path = out_dir / "devices.json"
    report_path = out_dir / "metrics.json"

    monitor = CpuMonitor(interval_s=args.cpu_interval)
    capture = CaptureSession(args.interface, pcap_path, display_filter=args.filter)

    print(f"Candidate: {args.candidate}")
    print(f"Output directory: {out_dir}")
    print("Starting CPU monitor and tshark capture...")
    monitor.start()
    capture.start()
    start_epoch = time.time()

    print()
    print("Perform the file transfer now.")
    print("When all receivers finish, press Enter to stop capture and analyze.")
    if args.multi_device:
        print("For multi-device tests, edit devices.json after stopping (template will be written).")
    try:
        input()
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        stop_epoch = time.time()
        capture.stop()
        monitor.stop()
        monitor.save(cpu_path)

    if args.multi_device and not devices_path.exists():
        template = {
            "start_epoch": start_epoch,
            "devices": [
                {
                    "name": "device_1",
                    "ip": "192.168.x.x",
                    "start_epoch": start_epoch,
                    "finish_epoch": stop_epoch,
                    "bytes": args.file_size,
                }
            ],
        }
        devices_path.write_text(json.dumps(template, indent=2), encoding="utf-8")
        print(f"Wrote template {devices_path}. Update finish times/IPs, then re-run analyze.")

    global_start, devices = load_devices(devices_path if devices_path.exists() else None)
    if global_start is None:
        global_start = start_epoch

    if not pcap_path.exists():
        detail = capture.stderr.strip()
        raise RuntimeError(
            f"Capture file was not created: {pcap_path.resolve()}"
            + (f"\ntshark output:\n{detail}" if detail else "")
            + "\nRun PowerShell as Administrator and verify the interface name."
        )
    records = parse_pcap(pcap_path, bpf_hint=args.display_filter)
    report = analyze_records(
        records,
        candidate=args.candidate,
        file_size_bytes=args.file_size,
        devices=devices,
        global_start=global_start,
        cpu_total_seconds=monitor.total_cpu_seconds,
        cpu_system_seconds=monitor.total_system_cpu_seconds,
    )
    print_report(report)
    save_report(report, report_path)
    append_csv_summary(report, Path(args.summary_csv))
    print(f"Saved {report_path}")
    return 0


def cmd_go(args: argparse.Namespace) -> int:
    cfg = load_experiment_config(Path(args.config))
    
    # Map --test 1..9 to count and path
    test_counts = {1: 10, 2: 100, 3: 1000, 4: 10, 5: 100, 6: 1000, 7: 10, 8: 100, 9: 1000}
    if args.test:
        cfg.file_count = test_counts[args.test]
        # Assumes gen_testfiles generated data/1, data/2, etc.
        cfg.file_path = Path(__file__).parent.resolve() / "data" / str(args.test) / "testfile_0000000.bin"
        
        # Override output dir based on version
        version = args.version or "original"
        run_suffix = f"_run{args.run}" if args.run else ""
        cfg.output_dir = Path(f"runs/{version}_test{args.test}{run_suffix}")
        cfg.summary_csv = Path(f"summary_{version}_test{args.test}.csv")
        
    out_dir = cfg.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    pcap_path = out_dir / "capture.pcap"
    cpu_path = out_dir / "cpu.json"
    devices_path = out_dir / "devices.json"
    report_path = out_dir / "metrics.json"
    config_copy_path = out_dir / "experiment.json"

    iface = resolve_tshark_interface(cfg.interface, cfg.my_ip)
    tshark = find_tshark(cfg.tshark_path)
    one_gib = cfg.single_file_size_bytes / (1024**3)
    per_rx_gib = cfg.bytes_per_receiver / (1024**3)

    shutil.copy2(Path(args.config), config_copy_path)

    print("=== 602 FP experiment (all-in-one) ===")
    print(f"Candidate:     {cfg.candidate}")
    print(f"File:          {cfg.file_path}")
    if cfg.file_count > 1:
        print(f"File count:    {cfg.file_count} (select all in LocalSend, send together)")
        print(f"Per file:      {cfg.single_file_size_bytes:,} bytes ({one_gib:.2f} GiB)")
        print(f"Per receiver:  {cfg.bytes_per_receiver:,} bytes ({per_rx_gib:.2f} GiB)")
        print(f"Total data:    {cfg.total_transfer_bytes:,} bytes ({cfg.total_transfer_bytes / (1024**3):.2f} GiB)")
    else:
        print(f"File size:     {cfg.single_file_size_bytes:,} bytes ({one_gib:.2f} GiB)")
    print(f"My IP:         {cfg.my_ip}")
    for name, ip in zip(cfg.receiver_names, cfg.receiver_ips):
        print(f"Receiver:      {name} @ {ip}")
    print(f"Interface:     {iface}")
    print(f"tshark:        {tshark}")
    print(f"Capture filter:{cfg.capture_filter}")
    print(f"Send mode:     {cfg.send_mode}{' (LocalSend API streaming)' if cfg.uses_auto_send else ''}")
    print(f"Output dir:    {out_dir.resolve()}")
    print()
    if cfg.uses_auto_send:
        print("Auto-send enabled: script will stream the file to both phones.")
    else:
        print(f"Manual send: use the {cfg.candidate} app yourself.")
    print("Run PowerShell as Administrator so tshark can capture packets.")
    print()

    precomputed_sha256 = ""
    if cfg.uses_auto_send:
        print("[localsend] Preparing SHA-256 before CPU/capture starts...")
        precomputed_sha256 = cached_sha256_file(cfg.file_path)
        print("[localsend] SHA-256 ready.")
        print()

    monitor = CpuMonitor(interval_s=cfg.cpu_interval)
    capture = CaptureSession(iface, pcap_path, display_filter=cfg.capture_filter, tshark_path=cfg.tshark_path)

    print("Starting CPU monitor and packet capture...")
    monitor.start()
    capture.start()

    interrupted = False
    start_epoch = time.time()
    finish_times: dict[str, float] = {}
    try:
        if cfg.uses_auto_send:
            start_epoch, finish_times = run_localsend_sync_send(cfg, precomputed_sha256)
        else:
            start_epoch, finish_times = run_manual_transfer(cfg)
        print("\nStopping capture and CPU monitor...")
    except KeyboardInterrupt:
        print("\nInterrupted.")
        interrupted = True
    except RuntimeError as exc:
        print(f"\nTransfer failed: {exc}")
        interrupted = True
    finally:
        capture.stop()
        monitor.stop()
        monitor.save(cpu_path)

    if interrupted or len(finish_times) != len(cfg.receiver_ips):
        print("Experiment aborted before all receivers finished.")
        return 1
        
    app_ts_path = Path(__file__).parent.resolve() / "app_timestamps.json"
    app_start = None
    app_end = None
    if app_ts_path.exists():
        try:
            ts_data = json.loads(app_ts_path.read_text(encoding="utf-8"))
            app_start = ts_data.get("start") / 1000.0
            app_end = ts_data.get("end") / 1000.0
            if app_start and app_end:
                start_epoch = app_start
                for ip in cfg.receiver_ips:
                    finish_times[ip] = app_end
                print(f"[localsend] Overriding manual keystrokes with app timestamps! (Window: {app_end - app_start:.3f}s)")
        except Exception:
            pass

    devices = [
        DeviceTiming(
            name=name,
            ip=ip,
            start_epoch=start_epoch,
            finish_epoch=finish_times[ip],
            bytes_transferred=cfg.file_size_bytes,
        )
        for name, ip in zip(cfg.receiver_names, cfg.receiver_ips)
    ]
    save_devices(devices_path, start_epoch, devices)

    print("Analyzing capture...")
    if not pcap_path.exists():
        detail = capture.stderr.strip()
        raise RuntimeError(
            f"Capture file was not created: {pcap_path.resolve()}"
            + (f"\ntshark output:\n{detail}" if detail else "")
            + "\nRun PowerShell as Administrator and verify the interface name in experiment.json."
        )
    records = parse_pcap(pcap_path, bpf_hint=cfg.display_filter, tshark_path=cfg.tshark_path)
    if not records:
        raise ValueError("No packets captured. Check interface, IPs, and admin rights.")
        
    if app_start and app_end:
        records = [r for r in records if app_start <= r.epoch <= app_end]

    report = analyze_records(
        records,
        candidate=cfg.candidate,
        file_size_bytes=cfg.bytes_per_receiver,
        file_count=cfg.file_count,
        devices=devices,
        global_start=start_epoch,
        cpu_total_seconds=monitor.total_cpu_seconds,
        cpu_system_seconds=monitor.total_system_cpu_seconds,
    )
    print_report(report)
    save_report(report, report_path)
    append_csv_summary(report, cfg.summary_csv)

    print(f"Saved: {report_path}")
    print(f"Saved: {devices_path}")
    print(f"Saved: {cpu_path}")
    print(f"Saved: {pcap_path}")
    print(f"Appended: {cfg.summary_csv}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    global_start, devices = load_devices(Path(args.devices) if args.devices else None)
    cpu_total = cpu_system = None
    if args.cpu_log:
        cpu_total, cpu_system = CpuMonitor.load(Path(args.cpu_log))

    file_count = args.file_count
    file_size_bytes = args.file_size
    exp_path = Path(args.pcap).parent / "experiment.json"
    if exp_path.exists():
        cfg = load_experiment_config(exp_path)
        file_count = cfg.file_count
        file_size_bytes = cfg.bytes_per_receiver

    records = parse_pcap(Path(args.pcap), bpf_hint=args.display_filter)
    report = analyze_records(
        records,
        candidate=args.candidate,
        file_size_bytes=file_size_bytes,
        file_count=file_count,
        devices=devices,
        global_start=global_start,
        cpu_total_seconds=cpu_total,
        cpu_system_seconds=cpu_system,
    )
    print_report(report)
    if args.output:
        save_report(report, Path(args.output))
    if args.summary_csv:
        append_csv_summary(report, Path(args.summary_csv))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="602 FP baseline metrics capture/analyzer (Wireshark/tshark + CPU)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_go = sub.add_parser("go", help="All-in-one run from experiment.json")
    p_go.add_argument("config", help="Path to experiment.json")
    p_go.add_argument("--test", type=int, choices=range(1, 10), help="Test scenario 1-9")
    p_go.add_argument("--version", choices=["original", "batching"], default="original", help="Version prefix for output folders")
    p_go.add_argument("--run", type=int, default=1, help="Run iteration (e.g. 1, 2, 3)")
    p_go.set_defaults(func=cmd_go)

    p_if = sub.add_parser("interfaces", help="List tshark capture interfaces")
    p_if.set_defaults(func=cmd_interfaces)

    p_cap = sub.add_parser("capture", help="Capture packets only (stop with Ctrl+C)")
    p_cap.add_argument("--interface", "-i", required=True, help="Interface name from 'interfaces' command")
    p_cap.add_argument("--output", "-w", required=True, help="Output .pcap path")
    p_cap.add_argument("--filter", "-f", default=None, help="Capture BPF filter, e.g. 'host 192.168.1.10'")
    p_cap.set_defaults(func=cmd_capture)

    p_run = sub.add_parser("run", help="Capture during a live transfer and analyze")
    p_run.add_argument("--interface", "-i", required=True)
    p_run.add_argument("--output-dir", "-o", required=True)
    p_run.add_argument("--candidate", "-c", choices=DEFAULT_CANDIDATES, default="localsend")
    p_run.add_argument("--file-size", type=int, default=None, help="Application file size in bytes")
    p_run.add_argument("--filter", "-f", default=None, help="Capture BPF filter")
    p_run.add_argument("--display-filter", "-Y", default=None, help="Post-capture display filter for analysis")
    p_run.add_argument("--cpu-interval", type=float, default=0.5)
    p_run.add_argument("--multi-device", action="store_true", help="Write devices.json template for fairness/tail latency")
    p_run.add_argument("--summary-csv", default="results_summary.csv")
    p_run.set_defaults(func=cmd_run)

    p_an = sub.add_parser("analyze", help="Analyze an existing pcap (+ optional cpu/devices logs)")
    p_an.add_argument("--pcap", required=True)
    p_an.add_argument("--candidate", "-c", default="unknown")
    p_an.add_argument("--file-size", type=int, default=None, help="Bytes per receiver (file_count × one file)")
    p_an.add_argument("--file-count", type=int, default=1, help="Number of files sent per receiver in this run")
    p_an.add_argument("--cpu-log", default=None)
    p_an.add_argument("--devices", default=None)
    p_an.add_argument("--display-filter", "-Y", default=None)
    p_an.add_argument("--output", default=None)
    p_an.add_argument("--summary-csv", default=None)
    p_an.set_defaults(func=cmd_analyze)

    return parser


def main() -> int:
    try:
        parser = build_parser()
        args = parser.parse_args()
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"tshark failed: {exc.stderr or exc.stdout}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
