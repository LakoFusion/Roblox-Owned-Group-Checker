"""
Tor module - Quản lý Tor proxies
Hỗ trợ: Windows, Linux, macOS, Termux (Android)

Nằm trong package tor/ — cạnh tor.exe (Windows) nên tor_dir = thư mục này.
"""

import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import psutil


# Probe 1 port — True nếu có thể bind (chưa bị OS reserve / process khác chiếm).
def _is_port_available(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


# Tìm `count` port trống liên tiếp bắt đầu từ start_port, skip các port bị reserve.
def _find_available_ports(start_port, count):
    available = []
    p = start_port
    max_search = count * 10  # giới hạn tránh vòng lặp vô tận
    searched = 0
    while len(available) < count and searched < max_search:
        if _is_port_available(p):
            available.append(p)
        p += 1
        searched += 1
    if len(available) < count:
        raise RuntimeError(
            f"Không tìm đủ {count} port trống bắt đầu từ {start_port} "
            f"(đã quét {searched} port). Hãy đổi proxy_start_port trong config."
        )
    return available


# Detect OS: 'windows' | 'linux' | 'macos' | 'termux'.
def _detect_platform():
    system = platform.system().lower()
    if system == "linux":
        prefix = os.environ.get("PREFIX", "")
        if "com.termux" in prefix or os.path.isdir("/data/data/com.termux"):
            return "termux"
        return "linux"
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    if os.path.isdir("/data/data/com.termux"):
        return "termux"
    return system


# Quản lý cài đặt + start/stop Tor process.
class ServiceInstaller:
    def __init__(self, amount, proxy_start_port):
        self.amount = amount
        self.proxy_start_port = proxy_start_port
        self.platform = _detect_platform()
        self.tor_process = None
        self._setup_paths()
        # Dọn tiến trình tor của PROJECT còn sót từ phiên trước (theo path
        # binary) — giải phóng port cho _find_available_ports; KHÔNG đụng Tor
        # hệ thống (Tor Browser / service /usr/bin...).
        self._clean_stale_tor()

    # Dọn tiến trình tor CỦA PROJECT còn sót từ phiên trước (kill bị treo,
    # Ctrl+C ở Terminal...). Chỉ diệt process có exe nằm trong tor/ của project.
    # Bọc try quanh TỪNG lệnh .exe() vì trên Windows tiến trình hệ thống
    # (SYSTEM, antivirus...) ném AccessDenied ngay lúc đọc exe.
    def _clean_stale_tor(self):
        if not os.path.isdir(self.tor_dir):
            return
        tor_prefix = os.path.normcase(os.path.realpath(self.tor_dir)) + os.sep
        victims = []
        for proc in psutil.process_iter():
            try:
                exe = proc.exe()
                if not exe:
                    continue
                if os.path.normcase(os.path.realpath(exe)).startswith(tor_prefix):
                    victims.append(proc)
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
                PermissionError,
                OSError,
            ):
                continue
        if not victims:
            return
        print(f"[Tor] Tìm thấy {len(victims)} tiến trình Tor của project còn sót — dọn...")
        for proc in victims:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied, PermissionError):
                pass
        _, alive = psutil.wait_procs(victims, timeout=5)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, PermissionError):
                pass
        if alive:
            time.sleep(1)
        print(f"[Tor] Đã dọn {len(victims)} tiến trình Tor cũ — port thông thoáng.")

    # Thiết lập đường dẫn binary + config theo platform.
    def _setup_paths(self):
        # File này nằm trong tor/ cạnh tor.exe → tor_dir là CHÍNH thư mục này
        # (không còn + "tor" như khi module ở thư mục gốc).
        self.tor_dir = os.path.dirname(os.path.abspath(__file__))
        os.makedirs(self.tor_dir, exist_ok=True)

        # Trên Windows, config phải ở path ASCII vì Tor không đọc được Unicode.
        if self.platform == "windows":
            self.config_path = os.path.join(tempfile.gettempdir(), "tor_config")
        else:
            self.config_path = os.path.join(self.tor_dir, "config")

        if self.platform == "windows":
            self.tor_exe = os.path.join(self.tor_dir, "tor.exe")
        elif self.platform == "termux":
            self.tor_exe = os.path.join(
                os.environ.get("PREFIX", "/data/data/com.termux/files/usr"),
                "bin", "tor",
            )
        elif self.platform == "macos":
            brew_paths = ["/usr/local/bin/tor", "/opt/homebrew/bin/tor"]
            self.tor_exe = next((p for p in brew_paths if os.path.isfile(p)), None)
            if self.tor_exe is None:
                self.tor_exe = shutil.which("tor") or "/usr/local/bin/tor"
        else:
            self.tor_exe = shutil.which("tor") or "/usr/bin/tor"

    # Sinh file config với N HTTPTunnelPort. Tự động skip port bị OS reserve.
    def Generate_Config(self):
        if self.platform == "termux":
            data_dir = os.path.join(
                os.environ.get("PREFIX", "/data/data/com.termux/files/usr"),
                "var", "lib", "tor",
            )
        elif self.platform == "windows":
            # Path ASCII để Tor đọc được trên Windows.
            data_dir = os.path.join(tempfile.gettempdir(), "tor_data")
        else:
            data_dir = os.path.abspath(os.path.join(self.tor_dir, "data"))
        os.makedirs(data_dir, exist_ok=True)

        # Cần (1 SOCKSPort + N HTTPTunnelPort) port trống.
        ports = _find_available_ports(self.proxy_start_port, 1 + self.amount)
        self.socks_port = ports[0]
        self.tunnel_ports = ports[1:]

        skipped = ports[-1] - self.proxy_start_port + 1 - len(ports)
        if skipped > 0:
            print(f"[Tor] Đã skip {skipped} port bị OS reserve.")

        lines = [
            f"DataDirectory {data_dir}",
            f"SOCKSPort 127.0.0.1:{self.socks_port} IsolateSOCKSAuth",
            "BandwidthRate 1GB",
            "BandwidthBurst 1GB",
            "FetchDirInfoEarly 1",
            "FetchDirInfoExtraEarly 1",
            "NumEntryGuards 4",
            "Log NOTICE stdout",
        ]
        for port in self.tunnel_ports:
            lines.append(f"HTTPTunnelPort 127.0.0.1:{port} IsolateSOCKSAuth")

        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(
            f"[Tor] Config generated: {self.amount} proxies, "
            f"SOCKS={self.socks_port}, HTTPTunnel={self.tunnel_ports[0]}-{self.tunnel_ports[-1]}"
        )

    # Cài Tor tự động qua package manager phù hợp.
    def _install_tor(self):
        print("[Tor] Tor not found. Attempting to install...")
        if self.platform == "termux":
            self._run_install_cmd(["pkg", "install", "-y", "tor"])
            self.tor_exe = os.path.join(
                os.environ.get("PREFIX", "/data/data/com.termux/files/usr"),
                "bin", "tor",
            )
        elif self.platform == "linux":
            if shutil.which("apt-get"):
                self._run_install_cmd(["sudo", "apt-get", "update", "-y"])
                self._run_install_cmd(["sudo", "apt-get", "install", "-y", "tor"])
            elif shutil.which("dnf"):
                self._run_install_cmd(["sudo", "dnf", "install", "-y", "tor"])
            elif shutil.which("yum"):
                self._run_install_cmd(["sudo", "yum", "install", "-y", "tor"])
            elif shutil.which("pacman"):
                self._run_install_cmd(["sudo", "pacman", "-Sy", "--noconfirm", "tor"])
            elif shutil.which("apk"):
                self._run_install_cmd(["sudo", "apk", "add", "tor"])
            else:
                print("[Tor] ERROR: Cannot detect package manager. Please install Tor manually.")
                sys.exit(1)
            self.tor_exe = shutil.which("tor") or "/usr/bin/tor"
        elif self.platform == "macos":
            if shutil.which("brew"):
                self._run_install_cmd(["brew", "install", "tor"])
            else:
                print("[Tor] ERROR: Homebrew not found. Visit https://brew.sh")
                sys.exit(1)
            self.tor_exe = shutil.which("tor") or "/usr/local/bin/tor"
        elif self.platform == "windows":
            self._download_tor_windows()

    # Chạy lệnh cài + log gọn output.
    def _run_install_cmd(self, cmd):
        print(f"[Tor] Running: {' '.join(cmd)}")
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            if r.returncode != 0:
                stderr = r.stderr.decode(errors="replace").strip()
                print(f"[Tor] WARNING: Command exited with code {r.returncode}")
                if stderr:
                    print(f"[Tor] stderr: {stderr[:500]}")
        except subprocess.TimeoutExpired:
            print("[Tor] ERROR: Installation timed out.")
        except FileNotFoundError:
            print(f"[Tor] ERROR: Command not found: {cmd[0]}")

    # Hướng dẫn user tải Tor Expert Bundle thủ công cho Windows.
    def _download_tor_windows(self):
        print("[Tor] ERROR: tor.exe not found in tor/")
        print("[Tor] Download Tor Expert Bundle: https://www.torproject.org/download/tor/")
        print("[Tor] Extract and copy tor.exe into tor/")
        sys.exit(1)

    # Khởi động Tor process & đợi bootstrap 100%.
    def Start_Tor(self):
        self.Generate_Config()

        if not os.path.isfile(self.tor_exe):
            if self.platform == "windows":
                self._install_tor()
            elif shutil.which("tor"):
                self.tor_exe = shutil.which("tor")
            else:
                self._install_tor()

        if not os.path.isfile(self.tor_exe) and not shutil.which("tor"):
            print("[Tor] ERROR: Tor binary not found after installation attempt.")
            sys.exit(1)

        abs_config = os.path.abspath(self.config_path)
        print(f"[Tor] Platform: {self.platform}")
        print(f"[Tor] Binary: {self.tor_exe}")
        print(f"[Tor] Config: {abs_config}")
        print("[Tor] Starting Tor...")

        # Chạy trực tiếp (KHÔNG dùng -nt-service): -nt-service chỉ dành cho
        # Windows SCM, sẽ ngắt stdout khiến Popen không đọc được bootstrap.
        cmd = [self.tor_exe, "-f", abs_config]
        self.tor_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        print("[Tor] Waiting for bootstrap...")
        start = time.time()
        timeout = 300
        while True:
            if int(time.time() - start) > timeout:
                print(f"[Tor] ERROR: Bootstrap timed out after {timeout}s.")
                self._kill_process(self.tor_process)
                sys.exit(1)

            line = self.tor_process.stdout.readline()
            if not line:
                if self.tor_process.poll() is not None:
                    print(f"[Tor] ERROR: Tor exited with code {self.tor_process.returncode}")
                    sys.exit(1)
                time.sleep(0.1)
                continue

            decoded = line.decode(errors="replace").strip()
            if decoded:
                print(f"[Tor] {decoded}")
            if "Bootstrapped 100% (done): Done" in decoded:
                print("[Tor] Bootstrap complete!")
                return
            time.sleep(0.001)

    # Chỉ dừng process do chính instance này spawn (an toàn theo PID,
    # KHÔNG quét/kill toàn bộ Tor hệ thống như trước đây).
    def Stop_Tor(self):
        if getattr(self, "tor_process", None) is not None:
            self._kill_process(self.tor_process)
            self.tor_process = None
            print("[Tor] Stopped our Tor process.")
        else:
            print("[Tor] No own Tor process to stop (skip, keep system Tor).")

    # Kill 1 subprocess cụ thể (graceful → force).
    def _kill_process(self, process):
        if process and process.poll() is None:
            try:
                if self.platform == "windows":
                    process.terminate()
                else:
                    process.send_signal(signal.SIGTERM)
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass


# Giữ reference ServiceInstaller đang chạy để shutdown khi Ctrl+C.
_current_installer = None


# Public API: khởi tạo Tor với N HTTP tunnel proxies, trả về list URL string.
def Make_Proxies(amount, proxy_start_port):
    global _current_installer
    print(f"[Tor] Detected platform: {_detect_platform()}")
    _current_installer = ServiceInstaller(amount, proxy_start_port)
    _current_installer.Start_Tor()
    print("[Tor] Successfully initialized!")
    return [f"http://127.0.0.1:{port}" for port in _current_installer.tunnel_ports]


# Kill chỉ Tor process do chính chúng ta spawn (không đụng system Tor).
def Stop_Running_Tor():
    global _current_installer
    if _current_installer is None:
        return
    _current_installer._kill_process(_current_installer.tor_process)
    _current_installer = None
    print("[Tor] Stopped our Tor process.")
