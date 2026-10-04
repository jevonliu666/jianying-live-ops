# jianying-live-ops

**剪映专业版（CapCut）自动化剪辑 Skill —— 带实时画面回传与人在环路确认**

一个面向 AI Agent（WorkBuddy / Claude Code / Codex 等支持 Skill 的运行环境）的技能包：自动完成剪映完整剪辑流程，同时把每一步操作画面直播到浏览器，你可以在页面上实时监控、逐步确认。

---

## 它解决什么问题

剪映没有公开的渲染与自动化 API。常见做法是用 UI 自动化点按钮，但**剪映 11.x 的界面是 QML 渲染，UI 自动化控件树为空**（`WindowControl.GetChildren()` 返回 0），按控件名驱动按钮的路子在较新版本上直接失效。

本项目采用**双通道设计**：

| 通道 | 做什么 | 为什么 |
|---|---|---|
| **草稿引擎**（主） | 直接读写剪映工程文件 `draft_info.json`，构建视频/音频/字幕/特效轨 | 不依赖界面，剪映打开即见结果，稳定 |
| **实时监控**（辅） | 5fps 抓取剪映窗口 → MJPEG 直播到浏览器 + 每步快照 | 让"AI 在剪你的视频"变成可见、可确认的过程 |

界面操作只保留三件事：启动剪映、引导打开草稿、引导导出——且全程在监控页面上直播。

---

## 特性

- **剪辑操作**：导入素材、时间轴裁剪、按时间点分割、转场、画面特效、滤镜、字幕（颜色/字号/描边/位置）、音频（音量/淡入淡出）
- **实时回传**：MJPEG 画面流（默认 5fps）、分步快照存证、执行日志时间线
- **人在环路**：关键步骤可在页面上点「确认」才继续；支持在画面上十字取点，回传屏幕坐标驱动点击
- **自动兜底导出**：剪映界面自动化不可用时，用 ffmpeg 渲染成片（视频 + 字幕 + 音频）
- **自带资源库**：24 个转场 / 16 个特效 / 16 个滤镜的剪映资源 ID（全部为免费素材）

---

## 快速开始

### 1. 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows（依赖剪映桌面版 + `uiautomation`） |
| 剪映 | 专业版桌面端（草稿格式基于 v5.9+） |
| Python | 3.10+ |
| 外部程序 | `ffmpeg` / `ffprobe` 需在 PATH 中 |

```bash
pip install -r requirements.txt
```

依赖清单一览：`mss`（屏幕捕获）、`Pillow`、`pywin32`（窗口定位）、`pynput`（键鼠模拟）、`uiautomation`、`opencv-python`、`numpy`、`psutil`、`requests`。

> 无 `ffmpeg` 时的取巧方案：`pip install imageio-ffmpeg ffmpeg-binaries`，从包内提取二进制放入 PATH。

### 2. 一条命令跑通全流程

```bash
python scripts/jy_live_ops.py demo \
  --video "C:/path/to/video.mp4" \
  --audio "C:/path/to/bgm.m4a" \
  --subtitle "你的字幕" \
  --transition 叠化 --filter 港风 \
  --export ffmpeg --output "D:/out/成片.mp4"
```

启动后打开打印出的监控地址（默认 `http://127.0.0.1:7865/`），整个剪辑过程就在页面上直播。

### 3. 编程调用

```python
import sys, os
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
from jy_live_ops import LiveEditingSession

sess = LiveEditingSession("我的成片", width=1080, height=1920, port=7865, overwrite=True)
url = sess.start_monitor()                      # 先把 url 展示给用户

v = sess.step_import("a.mp4", volume=0.0)       # 导入并静音原声
a, b = sess.step_split(v, "3s")                 # 3 秒处分割
sess.step_transition(a, "叠化")                  # 转场挂在前段
sess.step_filter(b, "港风", intensity=0.8)
sess.step_effect(a, "星火炸开")
sess.step_subtitle("faker六冠王", 0, "10s", size=6.0, color=(1.0, 0.4, 0.65))
sess.step_audio("bgm.m4a", start="0s", duration="10s", volume=0.85, fade_out="1.5s")
sess.step_save()
sess.step_export("D:/out/成片.mp4", mode="ffmpeg")   # 或 mode="guided" 在剪映里手动导出
```

> ⚠️ `step_audio(path, **kw)` 只接受 1 个位置参数，时间与音量必须用关键字传递。

---

## 目录结构

```
jianying-live-ops/
├── SKILL.md                      # 技能说明（Agent 加载入口）
├── scripts/
│   ├── draft_engine.py           # 自包含草稿引擎（读写 draft_info.json）
│   ├── live_monitor.py           # MJPEG 直播 + 快照 + 确认/取点回传
│   ├── ui_driver.py              # 剪映启动、窗口定位、键鼠模拟
│   └── jy_live_ops.py            # 编排器 LiveEditingSession + ffmpeg 兜底渲染 + CLI
├── viewer/index.html             # 监控台页面（实时画面 / 步骤时间线 / 快照）
├── data/resources.json           # 精选免费转场/特效/滤镜资源 ID
├── references/architecture.md    # 草稿字段结构、渲染映射、排障手册
└── requirements.txt
```

---

## 监控页面能力

| 端点 | 说明 |
|---|---|
| `GET /` | 监控台页面 |
| `GET /stream` | MJPEG 实时画面（5fps） |
| `GET /api/frame` | 当前帧 JPEG |
| `GET /api/state` | 步骤列表、状态、快照索引（JSON） |
| `POST /api/confirm` | 前端点「确认」→ 解除自动化阻塞 |
| `POST /api/click` | 页面取点回传 `{x, y}` 屏幕坐标 |

服务只绑定 `127.0.0.1`，不对外暴露。剪映窗口未找到时自动回退为全屏捕获。

---

## 已知限制

- **转场 / 特效 / 滤镜是剪映私有渲染**，ffmpeg 兜底导出会跳过它们（视频、字幕、音频不受影响）。需要完整效果请在剪映中手动导出（`mode="guided"`）。
- 剪映界面自动化导出仅在旧版本（Qt Widgets 时代，控件树可枚举）可用；11.x QML 版本需手动导出。
- 仅在 Windows 上验证。草稿格式随剪映版本演进，升级剪映后建议先用 `draft_engine.py` 生成测试草稿验证。

---

## 安全说明

- 服务仅监听本地回环地址，无任何外发数据的行为。
- 使用 `pynput` 模拟键鼠 —— 仅在界面操作步骤触发，不做任何按键记录。
- 所有文件写入限定在剪映草稿目录与用户指定的输出路径。
- 无网络请求代码（除可选地解析剪映官方素材 CDN）。

---

## 致谢

`data/resources.json` 中的资源 ID 提取自 [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)（Apache-2.0）的 metadata，仅包含免费素材。

## License

MIT
