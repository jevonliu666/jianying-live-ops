---
name: jianying-live-ops
description: 自动化驱动剪映专业版（CapCut）完成完整视频剪辑流程（导入素材、时间轴裁剪与分割、转场/特效/滤镜、字幕、音频、导出成片），并通过内置 MJPEG 实时监控页面同步直播每一步操作画面、分步快照与执行日志，供用户实时监控与逐步确认。This skill should be used when the user wants to automate JianYing/CapCut video editing with real-time visual monitoring of each operation.
agent_created: true
---

# 剪映自动化（实时监控版）

## 用途

在剪映专业版上自动完成完整剪辑流程：**导入素材 → 时间轴裁剪/分割 → 转场/特效/滤镜 → 字幕 → 音频 → 导出成片**。每执行一步，实时监控页面同步显示剪映画面、当前步骤状态与操作后快照，用户可在页面上「确认」或「取点」回传，实现人在环路的可视化自动化。

## 架构（三模块）

| 模块 | 文件 | 职责 |
|---|---|---|
| 草稿引擎 | `scripts/draft_engine.py` | 直接生成剪映 v5.9+ 草稿文件（最可靠，不碰界面） |
| 实时监控 | `scripts/live_monitor.py` | mss 抓帧 + MJPEG 流 + 分步快照 + 用户回传（确认/取点） |
| UI 驱动 | `scripts/ui_driver.py` | 启动剪映、窗口定位、pynput 键鼠模拟、交互式取点 |
| 编排器 | `scripts/jy_live_ops.py` | 把三者串成完整流程；每步自动截图存证；含 ffmpeg 兜底导出 |
| 监控页面 | `viewer/index.html` | 实时画面 + 步骤时间线 + 快照 + 确认/取点交互 |
| 资源库 | `data/resources.json` | 精选免费转场/特效/滤镜的剪映资源 id |

**关键设计决策**：剪辑操作走「草稿引擎」（直接写 draft_info.json），而不是模拟界面点击。原因：剪映 11.x 界面为 QML 渲染，UIA 控件树为空，无法按控件定位按钮；界面操作只用于「启动剪映 / 引导打开草稿 / 引导导出」这类必须人工或坐标的环节，且全程可被监控页面直播。

## 运行环境与依赖

- Windows + 剪映专业版（已实测 11.5.3）；`JianyingPro.exe` 自动探测（常见路径 + 注册表），探测不到时设环境变量 `JY_EXE`
- Python ≥ 3.10；依赖（`requirements.txt`）：`mss`、`Pillow`、`pynput`、`uiautomation`、`psutil`、`pymediainfo`
- `ffmpeg` / `ffprobe` 在 PATH 中（素材探测 + ffmpeg 兜底导出需要；没有 ffprobe 时自动回退 pymediainfo）
- 安装：`pip install -r requirements.txt`（本机必须用国内镜像：`-i https://mirrors.cloud.tencent.com/pypi/simple/ --trusted-host mirrors.cloud.tencent.com`）

## 使用方式

### 快速端到端演示（先跑通这条验证环境）

```bash
python scripts/jy_live_ops.py demo --video "C:/path/to/video.mp4" \
  --audio "C:/path/to/bgm.m4a" --subtitle "国庆节快乐" \
  --transition 叠化 --filter 港风 --export ffmpeg --output "D:/out/成片.mp4"
```

启动后立即把打印出的监控地址（默认 `http://127.0.0.1:7865/`）用 `present_files` 打开给用户——**这一步必须最先做**，用户全程在页面上看直播。

### 编程用法（在用户的剪辑脚本中）

业务脚本一律放在**用户项目根目录**（不要写进技能目录）：

```python
import sys, os
SKILL = r"<技能目录>"  # 本技能的绝对路径
sys.path.insert(0, os.path.join(SKILL, "scripts"))
from jy_live_ops import LiveEditingSession

sess = LiveEditingSession("我的成片", width=1080, height=1920, port=7865, overwrite=True)
url = sess.start_monitor()          # → 先把 url 展示给用户
v = sess.step_import("a.mp4", volume=0.0)          # 导入并静音原声
a, b = sess.step_split(v, "3s")                    # 3 秒处分割
sess.step_transition(a, "叠化")                     # 转场加在前段
sess.step_filter(b, "港风", intensity=0.8)          # 滤镜
sess.step_effect(a, "星火炸开")                     # 画面特效
sess.step_subtitle("国庆节快乐", "0s", "6s", size=5.0, y=-0.8)
sess.step_audio("bgm.m4a", "0s", duration="6s", volume=0.8, fade_out="0.5s")
sess.step_save()                                    # 写入剪映草稿库
sess.step_export("成片.mp4", mode="ffmpeg")          # 或 mode="guided" 引导剪映导出
sess.stop_monitor()
```

### 导出模式的选择（重要）

- `mode="guided"`（推荐出正式片）：`step_open_in_jianying()` 启动剪映 → 监控页面弹横幅引导用户双击打开草稿 → 用户点「导出」→ 用户在页面点「确认」→ 技能校验输出文件。**转场/特效/滤镜只有剪映能渲染，正式成片必须走这条**。
- `mode="ffmpeg"`（快速预览/兜底）：自动渲染 视频+字幕+音频 为 MP4；**转场/特效/滤镜被跳过**（剪映私有渲染，无法复刻），页面会明确标注。

### 界面取点（需要操作剪映界面时）

```python
pt = sess.app.calibrate_point("请在画面上点击剪映右上角的「导出」按钮")
sess.app.click(*pt, label="导出")
```

用户在监控画面上点一下，坐标即回传；`click()` 会在点击前于画面上画红色标记圈，用户能清楚看到"下一步要点哪"。

## 已知边界（本机实测，勿踩）

1. **不要尝试 UIA 控件自动化**（如 `HomePageDraftTitle` 之类的控件名）：剪映 11.x 是 QML 界面，控件树为空，永远找不到。
2. 草稿级编辑时剪映若开着，需重启剪映或新建草稿页刷新才能看到变更；正常流程是「先编辑草稿 → 后启动剪映」。
3. 转场/特效/滤镜依赖资源 id：优先用 `data/resources.json` 里的名称（全部为免费资源）；用户给定剪映资源 id 时走 `effect_id=`/`resource_id=` 参数。
4. 字幕定位 `y` 为归一化坐标：0=画面中心，-0.8=底部（剪映默认字幕位），0.8=顶部。
5. 时间参数：`int`=微秒，`float`=秒，字符串支持 `"3s"`/`"500ms"`。
6. 同名草稿已存在时 `Draft` 会抛错，传 `overwrite=True` 覆盖。

## 参考

- `references/architecture.md`：草稿 JSON 字段结构、监控协议、排障手册。
- 资源库扩充：从 `GuanYixuan/pyJianYingDraft`（Apache-2.0）的 `metadata/` 提取更多条目到 `data/resources.json`。
