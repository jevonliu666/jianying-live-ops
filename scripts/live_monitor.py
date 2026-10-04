"""实时画面监控模块：窗口捕获 + MJPEG 流 + 分步快照 + 用户回传（确认/取点）。

设计目标：让操作者在浏览器里同步看到剪映自动化的每一步执行过程。

- WindowLocator : 定位目标窗口矩形（优先剪映窗口，找不到则回退全屏）
- ScreenMonitor : 后台线程用 mss 抓帧，支持在画面上叠加"即将点击"标记
- LiveServer    : 内置 HTTP 服务
      GET  /             监控页面（viewer/index.html）
      GET  /stream       MJPEG 实时流（<img> 直接可用）
      GET  /api/state    当前状态 JSON（步骤日志/横幅/待确认/待取点）
      GET  /api/frame    最新一帧 JPEG
      GET  /api/steps/N  第 N 步的快照
      POST /api/confirm  用户在页面点击"确认"（解除 wait_confirm 阻塞）
      POST /api/click    用户在画面上取点（解除 wait_point 阻塞，返回屏幕坐标）

依赖：mss、Pillow、uiautomation（窗口定位，可选降级）。
"""

import io
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mss
from PIL import Image, ImageDraw

# ---------------------------------------------------------------- 窗口定位

class WindowLocator:
    """定位目标窗口（默认剪映），返回 mss 风格的矩形 dict；找不到返回 None。"""

    def __init__(self, title_regex=r"剪映|Jianying|CapCut", class_name=None):
        self.title_regex = title_regex
        self.class_name = class_name

    def locate(self):
        try:
            import uiautomation as uia
            if self.class_name:
                wt = uia.WindowControl(searchDepth=1, ClassName=self.class_name,
                                       RegexName=self.title_regex)
            else:
                wt = uia.WindowControl(searchDepth=1, RegexName=self.title_regex)
            if wt.Exists(0.3):
                r = wt.BoundingRectangle
                if r.width() > 200 and r.height() > 200:
                    return {"left": r.left, "top": r.top,
                            "width": r.width(), "height": r.height(),
                            "title": wt.Name or "?"}
        except Exception:
            pass
        return None

    @staticmethod
    def fullscreen():
        with mss.mss() as sct:
            m = sct.monitors[1]  # 主屏
            return {"left": m["left"], "top": m["top"],
                    "width": m["width"], "height": m["height"],
                    "title": "(未找到窗口, 全屏)"}

# ---------------------------------------------------------------- 抓帧线程

class ScreenMonitor(threading.Thread):
    """按固定帧率抓取目标区域，保留最新一帧；支持叠加点击标记。"""

    def __init__(self, locator, fps=5.0, follow_window=True):
        super().__init__(daemon=True, name="ScreenMonitor")
        self.locator = locator
        self.fps = fps
        self.follow_window = follow_window
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._frame = None            # PIL.Image
        self._rect = None             # 当前抓取的矩形
        self._marker = None           # (x, y, label, expire_ts) 屏幕坐标
        self._fixed_rect = None       # 锁定的矩形（calibration 期间用）
        self.frame_seq = 0

    # ---- 标记（屏幕坐标） ----
    def set_marker(self, x, y, label="", ttl=1.2):
        self._marker = (x, y, label, time.time() + ttl)

    def clear_marker(self):
        self._marker = None

    def lock_rect(self, rect=None):
        """锁定/解锁抓取区域（避免取点时窗口还在移动）。"""
        self._fixed_rect = rect

    def _current_rect(self):
        if self._fixed_rect:
            return self._fixed_rect
        if self.follow_window:
            return self.locator.locate() or self.locator.fullscreen()
        return self.locator.fullscreen()

    def run(self):
        interval = 1.0 / max(self.fps, 0.5)
        with mss.mss() as sct:
            while not self._stop_evt.is_set():
                t0 = time.time()
                try:
                    rect = self._current_rect()
                    raw = sct.grab({"left": rect["left"], "top": rect["top"],
                                    "width": rect["width"], "height": rect["height"]})
                    im = Image.frombytes("RGB", raw.size, raw.rgb)
                    marker = self._marker
                    if marker and time.time() < marker[3]:
                        self._draw_marker(im, marker, rect)
                    elif marker:
                        self._marker = None
                    with self._lock:
                        self._frame = im
                        self._rect = rect
                        self.frame_seq += 1
                except Exception:
                    time.sleep(0.5)
                dt = time.time() - t0
                if dt < interval:
                    time.sleep(interval - dt)

    @staticmethod
    def _draw_marker(im, marker, rect):
        x, y, label, _ = marker
        cx, cy = x - rect["left"], y - rect["top"]
        if not (0 <= cx < im.width and 0 <= cy < im.height):
            return
        d = ImageDraw.Draw(im)
        r = 26
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 40, 40), width=5)
        d.line([cx - r - 12, cy, cx + r + 12, cy], fill=(255, 40, 40), width=3)
        d.line([cx, cy - r - 12, cx, cy + r + 12], fill=(255, 40, 40), width=3)
        if label:
            d.text((cx + r + 8, cy - 12), label, fill=(255, 40, 40))

    def latest(self):
        with self._lock:
            return self._frame, self._rect

    def latest_jpeg(self, quality=70):
        with self._lock:
            if self._frame is None:
                return None
            buf = io.BytesIO()
            self._frame.save(buf, "JPEG", quality=quality)
            return buf.getvalue()

    def snapshot(self, path, quality=88):
        with self._lock:
            if self._frame is None:
                return None
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._frame.save(path, "JPEG", quality=quality)
            return path

    def stop(self):
        self._stop_evt.set()

# ---------------------------------------------------------------- HTTP 服务

_VIEWER_HTML_FALLBACK = """<!doctype html><meta charset=utf-8><title>jy live ops</title>
<body style="background:#111;color:#eee;font-family:sans-serif">
<h3>viewer/index.html 缺失，仅提供基础流</h3><img src="/stream" style="max-width:100%">
<script>setInterval(async()=>{const s=await(await fetch('/api/state')).json();
document.title=`steps:${s.steps.length}`},1000)</script>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "JyLiveOps/1.0"

    # 静音日志
    def log_message(self, *a):
        pass

    @property
    def mon(self):
        return self.server.monitor

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._serve_viewer()
        elif path == "/stream":
            self._serve_mjpeg()
        elif path == "/api/state":
            self._json(self.mon.state_dict())
        elif path == "/api/frame":
            jpg = self.mon.capture.latest_jpeg()
            self._bytes(jpg or b"", "image/jpeg")
        elif path.startswith("/api/steps/"):
            self._serve_step_file(path)
        else:
            self.send_error(404)

    def _serve_viewer(self):
        html = self.mon.viewer_html
        self._bytes(html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_mjpeg(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=jylive")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        interval = 1.0 / max(self.mon.fps, 0.5)
        while True:
            jpg = self.mon.capture.latest_jpeg()
            if jpg is not None:
                try:
                    self.wfile.write(b"--jylive\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
            time.sleep(interval)

    def _serve_step_file(self, path):
        m = re.match(r"/api/steps/(\d+)$", path)
        if not m:
            self.send_error(404)
            return
        idx = int(m.group(1))
        steps = self.mon.steps
        if 0 <= idx < len(steps) and steps[idx].get("snapshot"):
            p = steps[idx]["snapshot"]
            if os.path.exists(p):
                with open(p, "rb") as f:
                    self._bytes(f.read(), "image/jpeg")
                return
        self.send_error(404)

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = b""
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(ln) if ln else b""
        except Exception:
            pass
        if path == "/api/confirm":
            self.mon._confirm_value = True
            self.mon._confirm_evt.set()
            self._json({"ok": True})
        elif path == "/api/click":
            try:
                data = json.loads(body.decode("utf-8") or "{}")
                x, y = int(data["x"]), int(data["y"])
                self.mon._click_value = (x, y)
                self.mon._click_evt.set()
                self._json({"ok": True, "x": x, "y": y})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        else:
            self.send_error(404)

    # ---------- helpers ----------
    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._bytes(data, "application/json; charset=utf-8", code)

    def _bytes(self, data, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class LiveServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------- 门面

class Monitor:
    """实时监控门面：抓帧 + HTTP 服务 + 步骤日志 + 用户回传。"""

    def __init__(self, port=7865, window_regex=r"剪映|Jianying|CapCut", fps=5.0,
                 run_dir=None, follow_window=True, viewer_path=None):
        self.port = port
        self.fps = fps
        self.locator = WindowLocator(window_regex)
        self.capture = ScreenMonitor(self.locator, fps=fps, follow_window=follow_window)
        self.run_dir = run_dir or os.path.abspath(
            os.path.join("live_ops_runs", time.strftime("%Y%m%d_%H%M%S")))
        os.makedirs(self.run_dir, exist_ok=True)
        self.steps = []                # [{idx,title,detail,status,t_start,t_end,snapshot}]
        self._cur_step = None
        self._banner = None            # {"text":..., "show_confirm":bool}
        self._awaiting_click = False
        self._confirm_evt = threading.Event()
        self._click_evt = threading.Event()
        self._confirm_value = None
        self._click_value = None
        self._server = None
        self._viewer_path = viewer_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer", "index.html")

    # ---- 生命周期 ----
    def start(self):
        self.capture.start()
        self._server = LiveServer(("127.0.0.1", self.port), _Handler)
        self._server.monitor = self
        threading.Thread(target=self._server.serve_forever,
                         daemon=True, name="LiveServer").start()
        # 等第一帧就绪（保证 begin_step/end_step 的快照从第一步就有画面）
        deadline = time.time() + 5
        while self.capture.frame_seq < 1 and time.time() < deadline:
            time.sleep(0.1)
        return self.url

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def stop(self):
        self.capture.stop()
        if self._server:
            self._server.shutdown()

    @property
    def viewer_html(self):
        try:
            with open(self._viewer_path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return _VIEWER_HTML_FALLBACK

    # ---- 状态 ----
    def state_dict(self):
        frame, rect = self.capture.latest()
        return {
            "url": self.url,
            "fps": self.fps,
            "frame_seq": self.capture.frame_seq,
            "window": rect,
            "banner": self._banner,
            "awaiting_click": self._awaiting_click,
            "steps": [{k: v for k, v in s.items() if k != "snapshot_abs"} for s in self.steps],
            "run_dir": self.run_dir,
            "ts": time.time(),
        }

    # ---- 步骤 ----
    def begin_step(self, title, detail=""):
        idx = len(self.steps)
        self._cur_step = {"idx": idx, "title": title, "detail": detail,
                          "status": "running", "t_start": time.time(),
                          "t_end": None, "snapshot": None}
        self.steps.append(self._cur_step)
        return idx

    def end_step(self, status="ok", detail=None, snapshot=True):
        s = self._cur_step
        if not s:
            return
        if detail is not None:
            s["detail"] = (s["detail"] + " | " if s["detail"] else "") + detail
        s["status"] = status
        s["t_end"] = time.time()
        if snapshot:
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", s["title"])[:40]
            p = os.path.join(self.run_dir, f"step_{s['idx']:02d}_{safe}.jpg")
            if self.capture.snapshot(p):
                s["snapshot"] = p
        self._cur_step = None

    def note(self, text):
        """附加一条无快照的日志步骤。"""
        self.steps.append({"idx": len(self.steps), "title": text, "detail": "",
                           "status": "note", "t_start": time.time(),
                           "t_end": time.time(), "snapshot": None})

    # ---- 横幅与用户回传 ----
    def set_banner(self, text=None, show_confirm=False):
        self._banner = {"text": text, "show_confirm": show_confirm} if text else None

    def wait_confirm(self, prompt, timeout=None):
        """在页面上弹出提示并阻塞，直到用户点"确认"或超时。返回是否被确认。"""
        self._confirm_evt.clear()
        self._confirm_value = None
        self.set_banner(prompt, show_confirm=True)
        ok = self._confirm_evt.wait(timeout)
        self.set_banner(None)
        return bool(ok and self._confirm_value)

    def wait_point(self, prompt, timeout=None):
        """请用户在画面上点一个点（如"导出"按钮），返回屏幕坐标 (x, y)。"""
        self._click_evt.clear()
        self._click_value = None
        self._awaiting_click = True
        self.set_banner(prompt, show_confirm=False)
        # 取点期间锁定当前矩形，保证坐标映射稳定
        _, rect = self.capture.latest()
        if rect:
            self.capture.lock_rect(rect)
        ok = self._click_evt.wait(timeout)
        self._awaiting_click = False
        self.set_banner(None)
        self.capture.lock_rect(None)
        return self._click_value if ok else None

    # ---- 标记 ----
    def mark(self, x, y, label="", ttl=1.2):
        self.capture.set_marker(x, y, label, ttl)


if __name__ == "__main__":
    # 独立自测：python live_monitor.py [port]
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7865
    mon = Monitor(port=port)
    print("监控地址:", mon.start())
    print("按 Ctrl+C 结束")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mon.stop()
