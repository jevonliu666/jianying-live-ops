"""剪映进程与输入驱动模块：启动剪映、定位窗口、键鼠模拟、画面取点校准。

说明（本机实测结论，剪映专业版 11.5.x）：
- 剪映界面为 QML 渲染，UIA 控件树为空，**无法**通过控件名定位按钮；
  因此 UI 操作一律走「屏幕坐标 + 输入模拟」，坐标来源有两种：
    1) 用户在监控页面上「取点」（wait_point），即交互式校准（推荐，稳定）；
    2) 相对窗口比例坐标（相对窗口左上角，分辨率变化时仍大致可用）。
- 顶层窗口本身可以定位（标题/类名），因此抓帧、置前、取窗口矩形都可用。

依赖：pynput（输入模拟）、uiautomation（顶层窗口定位）、psutil（进程检测）。
"""

import os
import subprocess
import time
import winreg

import psutil

# 常见安装位置（本机实测为 D:\jianyin）
KNOWN_EXE_PATHS = [
    r"D:\jianyin\JianyingPro\JianyingPro.exe",
    r"C:\Program Files\JianyingPro\JianyingPro.exe",
    r"C:\Program Files (x86)\JianyingPro\JianyingPro.exe",
    r"D:\Program Files\JianyingPro\JianyingPro.exe",
]

WINDOW_REGEX = r"剪映|Jianying|CapCut"
PROCESS_NAMES = ("jianyingpro.exe", "lveditor.exe", "capcut.exe")


def find_jianying_exe():
    """按 环境变量 -> 常见路径 -> 注册表卸载项 的顺序定位 JianyingPro.exe。"""
    env = os.environ.get("JY_EXE", "").strip()
    if env and os.path.exists(env):
        return env
    for p in KNOWN_EXE_PATHS:
        if os.path.exists(p):
            return p
    # 注册表卸载项兜底
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                with winreg.OpenKey(hive, sub) as root:
                    for i in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            kn = winreg.EnumKey(root, i)
                            with winreg.OpenKey(root, kn) as k:
                                name, _ = winreg.QueryValueEx(k, "DisplayName")
                                if "剪映" in str(name) or "Jianying" in str(name) \
                                   or "CapCut" in str(name):
                                    for val in ("DisplayIcon", "InstallLocation"):
                                        try:
                                            v, _ = winreg.QueryValueEx(k, val)
                                            v = str(v).strip('"')
                                            if val == "InstallLocation":
                                                v = os.path.join(v, "JianyingPro.exe")
                                            if v.lower().endswith(".exe") and os.path.exists(v):
                                                return v
                                        except OSError:
                                            continue
                        except OSError:
                            continue
            except OSError:
                continue
    return None


def is_running():
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() in PROCESS_NAMES:
                return True
        except (psutil.Error, AttributeError):
            continue
    return False


class JyApp:
    """剪映进程 + 窗口 + 输入模拟的封装。"""

    def __init__(self, monitor=None, exe=None, window_regex=WINDOW_REGEX):
        """
        monitor: live_monitor.Monitor 实例（用于点击前标记、置前确认；可空）。
        exe    : JianyingPro.exe 路径（None 时自动探测）。
        """
        self.monitor = monitor
        self.exe = exe or find_jianying_exe()
        self.window_regex = window_regex
        self._win = None  # uiautomation WindowControl（惰性获取）

    # ---------------- 窗口 ----------------
    def _window(self, wait=0.0):
        """获取顶层窗口控件；wait>0 时轮询等待出现。"""
        import uiautomation as uia
        deadline = time.time() + wait
        while True:
            try:
                wt = uia.WindowControl(searchDepth=1, RegexName=self.window_regex)
                if wt.Exists(0.3):
                    self._win = wt
                    return wt
            except Exception:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(0.5)

    def rect(self):
        wt = self._window()
        if not wt:
            return None
        r = wt.BoundingRectangle
        return {"left": r.left, "top": r.top, "width": r.width(), "height": r.height()}

    def focus(self):
        wt = self._window()
        if not wt:
            return False
        try:
            if wt.IsMinimize():
                wt.Restore()
                time.sleep(0.5)
            wt.SetFocus()
            wt.SetActive()
            return True
        except Exception:
            return False

    # ---------------- 进程 ----------------
    def launch(self, timeout=60):
        """启动剪映并等待主窗口出现。返回是否成功。"""
        if is_running() and self._window():
            self.focus()
            return True
        if not self.exe:
            raise FileNotFoundError("未找到 JianyingPro.exe，请设置环境变量 JY_EXE")
        subprocess.Popen([self.exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self._window(wait=timeout) is not None

    # ---------------- 输入模拟 ----------------
    @staticmethod
    def _mouse():
        from pynput.mouse import Controller
        return Controller()

    @staticmethod
    def _keyboard():
        from pynput.keyboard import Controller
        return Controller()

    def click(self, x, y, label=None, pre_delay=0.45, button="left", clicks=1):
        """屏幕绝对坐标点击；点击前在监控画面上画标记。"""
        from pynput.mouse import Button
        if self.monitor and label is not False:
            self.monitor.mark(x, y, label or "", ttl=pre_delay + 0.8)
            time.sleep(pre_delay)
        m = self._mouse()
        m.position = (int(x), int(y))
        time.sleep(0.12)
        btn = Button.left if button == "left" else Button.right
        m.click(btn, clicks)
        return (x, y)

    def double_click(self, x, y, label=None):
        return self.click(x, y, label=label, clicks=2)

    def click_rel(self, fx, fy, label=None):
        """相对窗口比例坐标点击（fx/fy ∈ 0~1）。"""
        r = self.rect()
        if not r:
            raise RuntimeError("找不到剪映窗口，无法换算相对坐标")
        return self.click(r["left"] + r["width"] * fx,
                          r["top"] + r["height"] * fy, label=label)

    def type_text(self, text, interval=0.02):
        kb = self._keyboard()
        for ch in text:
            kb.type(ch)
            time.sleep(interval)

    def hotkey(self, *keys):
        """如 hotkey('ctrl', 's')。"""
        from pynput.keyboard import Key
        kb = self._keyboard()
        mapped = [getattr(Key, k, k) if isinstance(k, str) and len(k) > 1 else k for k in keys]
        for k in mapped:
            kb.press(k)
        time.sleep(0.08)
        for k in reversed(mapped):
            kb.release(k)

    # ---------------- 交互式取点（经监控页面） ----------------
    def calibrate_point(self, prompt, timeout=120):
        """请用户在监控页面上点一个点，返回屏幕坐标 (x, y)。"""
        if not self.monitor:
            raise RuntimeError("calibrate_point 需要 Monitor 实例")
        pt = self.monitor.wait_point(prompt, timeout=timeout)
        return pt


if __name__ == "__main__":
    print("JianyingPro.exe:", find_jianying_exe())
    print("进程在运行:", is_running())
