import os
import platform
import stat
import subprocess
import sys
import re
import time
import threading
import queue
import base64
import tarfile
import urllib.request

import customtkinter as ctk

# ============================== CONFIG ======================================
LINK_FILE = "current_centaurilink_code.txt"  # saved next to this script, for convenience
STARTUP_TIMEOUT = 30                          # seconds to wait for cloudflared to hand out a URL
RESTART_DELAY = 5                             # seconds between a dropped tunnel and retrying
# =============================================================================

CLOUDFLARED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
LINK_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), LINK_FILE)

TUNNEL_PREFIX = "https://"
TUNNEL_SUFFIX = ".trycloudflare.com"

# ---------------------------------------------------------------- palette --
BG        = "#080D1A"
CARD      = "#0c1425"
CARD_HI   = "#111a2e"
CYAN      = "#00E5FF"
CYAN_DIM  = "#0aa8bd"
WHITE     = "#F0F4FF"
MUTED     = "#7A8BA8"
GOOD      = "#00FF88"
BAD       = "#FF6B6B"
AMBER     = "#FFB400"


def encode_tunnel_url(tunnel_url: str) -> str:
    """
    Strip the fixed https:// prefix and .trycloudflare.com suffix, then
    base64url-encode just the random subdomain. No network calls, no
    external service - purely local string manipulation.
    """
    subdomain = tunnel_url
    if subdomain.startswith(TUNNEL_PREFIX):
        subdomain = subdomain[len(TUNNEL_PREFIX):]
    if subdomain.endswith(TUNNEL_SUFFIX):
        subdomain = subdomain[: -len(TUNNEL_SUFFIX)]
    token = base64.urlsafe_b64encode(subdomain.encode("utf-8")).decode("ascii")
    return token.rstrip("=")


def get_cloudflared_binary(status_cb):
    """Download the correct cloudflared binary for this OS/CPU if not present."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    os.makedirs(CLOUDFLARED_DIR, exist_ok=True)

    is_tgz = False
    if system == "windows":
        name = "cloudflared.exe"
        if "arm" in machine or "aarch64" in machine:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-arm64.exe"
        else:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    elif system == "darwin":
        name = "cloudflared"
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
        is_tgz = True
    else:  # linux (also covers Raspberry Pi OS)
        name = "cloudflared"
        if "aarch64" in machine or "arm64" in machine:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
        elif "arm" in machine:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm"
        else:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"

    binary_path = os.path.join(CLOUDFLARED_DIR, name)
    if os.path.exists(binary_path):
        return binary_path

    status_cb("Downloading cloudflared\u2026", AMBER)
    tmp_path = binary_path + (".tgz" if is_tgz else "")
    urllib.request.urlretrieve(url, tmp_path)

    if is_tgz:
        with tarfile.open(tmp_path) as tar:
            tar.extractall(CLOUDFLARED_DIR)
        os.remove(tmp_path)

    if system != "windows":
        st = os.stat(binary_path)
        os.chmod(binary_path, st.st_mode | stat.S_IEXEC)

    return binary_path


class CentauriLinkApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("CentauriLink")
        self.geometry("440x520")
        self.minsize(400, 480)
        self.configure(fg_color=BG)

        self.event_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.binary_path = None
        self.current_proc = None

        self._build_ui()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- UI ---
    def _build_ui(self):
        outer = ctk.CTkFrame(self, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=22, pady=22)

        # ---- brand header ----
        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill="x", pady=(4, 18))

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack()
        ctk.CTkLabel(title_row, text="\u25CF", text_color=CYAN,
                     font=ctk.CTkFont(size=14)).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(title_row, text="CentauriLink",
                     font=ctk.CTkFont(size=22, weight="bold"),
                     text_color=WHITE).pack(side="left")

        ctk.CTkLabel(header, text="Remote access for your Centauri Carbon",
                     font=ctk.CTkFont(size=12), text_color=MUTED).pack(pady=(4, 0))

        # ---- card ----
        card = ctk.CTkFrame(outer, fg_color=CARD, corner_radius=16,
                             border_width=1, border_color="#152036")
        card.pack(fill="both", expand=True)

        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=22, pady=22)

        ctk.CTkLabel(inner, text="PRINTER IP ADDRESS", anchor="w",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=MUTED).pack(fill="x")
        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x", pady=(6, 16))
        self.ip_entry = ctk.CTkEntry(
            row, placeholder_text="192.168.1.50", height=40, corner_radius=10,
            fg_color=CARD_HI, border_color="#1c2a44", border_width=1,
            text_color=WHITE,
        )
        self.ip_entry.pack(side="left", fill="x", expand=True)
        self.port_entry = ctk.CTkEntry(
            row, placeholder_text="80", height=40, width=64, corner_radius=10,
            fg_color=CARD_HI, border_color="#1c2a44", border_width=1,
            text_color=WHITE,
        )
        self.port_entry.insert(0, "80")
        self.port_entry.pack(side="left", padx=(8, 0))

        self.start_btn = ctk.CTkButton(
            inner, text="Start Tunnel", height=44, corner_radius=10,
            fg_color=CYAN, hover_color=CYAN_DIM, text_color=BG,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._toggle_tunnel,
        )
        self.start_btn.pack(fill="x")

        status_row = ctk.CTkFrame(inner, fg_color="transparent")
        status_row.pack(fill="x", pady=(14, 18))
        self.status_dot = ctk.CTkLabel(status_row, text="\u25CF", text_color=MUTED,
                                        font=ctk.CTkFont(size=11))
        self.status_dot.pack(side="left")
        self.status_label = ctk.CTkLabel(status_row, text="Stopped",
                                          font=ctk.CTkFont(size=12),
                                          text_color=MUTED)
        self.status_label.pack(side="left", padx=(6, 0))

        ctk.CTkLabel(inner, text="CENTAURILINK CODE", anchor="w",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=MUTED).pack(fill="x")
        code_row = ctk.CTkFrame(inner, fg_color="transparent")
        code_row.pack(fill="x", pady=(6, 0))
        self.code_entry = ctk.CTkEntry(
            code_row, placeholder_text="Not started yet", height=40, corner_radius=10,
            fg_color=CARD_HI, border_color="#1c2a44", border_width=1,
            text_color=CYAN, font=ctk.CTkFont(family="Consolas", size=13),
        )
        self.code_entry.pack(side="left", fill="x", expand=True)
        self.code_entry.configure(state="readonly")
        self.copy_btn = ctk.CTkButton(
            code_row, text="Copy", width=64, height=40, corner_radius=10,
            fg_color=CARD_HI, hover_color="#1c2a44", text_color=WHITE,
            border_width=1, border_color="#1c2a44",
            command=self._copy_code,
        )
        self.copy_btn.pack(side="left", padx=(8, 0))

        ctk.CTkLabel(
            inner,
            text="Paste this code into your CentauriLink site to view the printer.\nA new code is generated automatically if the tunnel reconnects.",
            font=ctk.CTkFont(size=11), text_color=MUTED, justify="left",
        ).pack(fill="x", pady=(14, 0))

    # ------------------------------------------------------------ helpers ---
    def _copy_code(self):
        code = self.code_entry.get()
        if code:
            self.clipboard_clear()
            self.clipboard_append(code)

    def _set_status(self, text, color):
        self.event_queue.put(("status", (text, color)))

    def _set_code(self, code):
        self.event_queue.put(("code", code))

    # -------------------------------------------------------- start/stop ---
    def _toggle_tunnel(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.stop_event.set()
            if self.current_proc:
                try:
                    self.current_proc.terminate()
                except Exception:
                    pass
            self.start_btn.configure(state="disabled")
        else:
            ip = self.ip_entry.get().strip()
            port = self.port_entry.get().strip() or "80"
            if not ip:
                self._set_status("Enter the printer's IP first", BAD)
                return

            self.stop_event.clear()
            self.start_btn.configure(text="Stop Tunnel")
            self._set_status("Starting\u2026", AMBER)
            self.worker_thread = threading.Thread(target=self._run, args=(ip, port), daemon=True)
            self.worker_thread.start()

    def _on_close(self):
        self.stop_event.set()
        if self.current_proc:
            try:
                self.current_proc.terminate()
            except Exception:
                pass
        self.destroy()

    # ------------------------------------------------------ worker logic ---
    def _run(self, ip, port):
        target = f"http://{ip}:{port}"
        try:
            if not self.binary_path:
                self.binary_path = get_cloudflared_binary(self._set_status)
        except Exception as e:
            self._set_status(f"Setup failed: {e}", BAD)
            self.event_queue.put(("stopped", None))
            return

        while not self.stop_event.is_set():
            self._set_status("Opening tunnel\u2026", AMBER)
            tunnel_url = self._start_and_wait_for_url(target)

            if self.stop_event.is_set():
                break

            if not tunnel_url:
                self._set_status("No response, retrying\u2026", BAD)
                self._sleep_interruptible(10)
                continue

            code = encode_tunnel_url(tunnel_url)

            try:
                with open(LINK_FILE_PATH, "w") as f:
                    f.write(code + "\n")
            except Exception:
                pass

            self._set_code(code)
            self._set_status("Live", GOOD)

            while self.current_proc and self.current_proc.poll() is None:
                if self.stop_event.is_set():
                    self.current_proc.terminate()
                    break
                time.sleep(0.5)

            if self.stop_event.is_set():
                break

            self._set_status("Reconnecting\u2026", AMBER)
            self._set_code("")
            self._sleep_interruptible(RESTART_DELAY)

        self.event_queue.put(("stopped", None))

    def _sleep_interruptible(self, seconds):
        for _ in range(seconds * 2):
            if self.stop_event.is_set():
                return
            time.sleep(0.5)

    def _start_and_wait_for_url(self, target_url):
        """
        Launches cloudflared and drains its stdout in this worker thread so
        the pipe never blocks, watching for the public URL to appear.
        """
        proc = subprocess.Popen(
            [self.binary_path, "tunnel", "--url", target_url, "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.current_proc = proc

        pattern = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
        tunnel_url = None
        deadline = time.time() + STARTUP_TIMEOUT

        while time.time() < deadline and not self.stop_event.is_set():
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            match = pattern.search(line)
            if match:
                tunnel_url = match.group(0)
                break

        return tunnel_url

    # -------------------------------------------------------- UI updates ---
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                if kind == "status":
                    text, color = payload
                    self.status_label.configure(text=text, text_color=color)
                    self.status_dot.configure(text_color=color)
                elif kind == "code":
                    self.code_entry.configure(state="normal")
                    self.code_entry.delete(0, "end")
                    self.code_entry.insert(0, payload)
                    self.code_entry.configure(state="readonly")
                elif kind == "stopped":
                    self.status_label.configure(text="Stopped", text_color=MUTED)
                    self.status_dot.configure(text_color=MUTED)
                    self.start_btn.configure(text="Start Tunnel", state="normal")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)


if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    app = CentauriLinkApp()
    app.mainloop()