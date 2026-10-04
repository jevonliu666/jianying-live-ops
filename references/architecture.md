# 架构与排障参考

## 1. 为什么剪辑走草稿引擎而不是界面模拟

剪映专业版 11.x 的主界面是 **QML 渲染**（顶层窗口类形如 `HomePage_QMLTYPE_214`），
Windows UI Automation 拿到的控件树为空（`children: 0`）。因此：

- 任何「按控件名找按钮」的自动化（如 jianying-editor 的 `auto_exporter.py` 依赖的
  `HomePageDraftTitle:xxx`）在 11.x 上**必然失败**；
- 可靠的 UI 自动化只剩两条路：
  1. **屏幕坐标 + 输入模拟**（pynput）——坐标来自用户在监控页面「取点」校准，
     或相对窗口比例坐标；
  2. **绕开界面，直接写草稿文件**（draft_info.json）——剪映下次打开时读取，
     这是本技能所有剪辑操作的执行方式。

## 2. 草稿目录结构（v5.9+，剪映 11.x 沿用）

草稿根目录：`%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\<草稿名>\`

| 文件 | 说明 |
|---|---|
| `draft_info.json` | 全部时间轴数据（画布、素材、轨道、片段） |
| `draft_meta_info.json` | 元信息（路径、名称、创建/修改时间、总时长） |
| `draft_settings` | INI，4 个字段，剪映启动时校验用 |
| `key_value.json` | `{}` 即可 |

核心字段（值以真实草稿验证）：

- `canvas_config`: `{width, height, ratio:"original"}`；`duration`: 微秒；`fps`
- `materials.videos[]`: `crop`(8 点 0~1)、`duration`、`width/height`、`id`、`material_id`、
  `material_name`、`path`（绝对路径）、`check_flag:63487`
- `materials.audios[]`: `id=local_material_id=music_id`、`path`、`duration`、`type:"extract_music"`、`check_flag:3`
- `materials.texts[]`: `content` 是**字符串化的 JSON**：
  `{"styles":[{"fill":..., "range":[0,len], "size":5.0, "strokes":[{"width":0.08, ...}]}], "text":"..."}`
- `materials.speeds[]`: 每个媒体片段一个，`{curve_speed:null, id, mode:0, speed:1.0, type:"speed"}`，
  片段的 `extra_material_refs` 必须引用它
- 片段公共字段：`id`、`material_id`、`target_timerange{start,duration}`、
  `source_timerange`（文本段为 null）、`volume`、`extra_material_refs`、`clip`
- `clip.transform.y`：归一化坐标，0=画面中心，-0.8=底部（默认字幕位）
- 转场：`materials.transitions[]` + 挂载在**前一段**的 `extra_material_refs`
- 特效 → `materials.video_effects[]`；滤镜 → `materials.effects[]`；音频淡入淡出 → `materials.audio_fades[]`
- `tracks[]` 顺序：video → audio → text；每轨 `{attribute:0, flag:0, id, is_default_name:false, name, segments[], type}`

## 3. 监控协议

- 抓帧：`mss.grab(rect)`，默认 5fps；窗口丢失时自动回退全屏
- 传输：`GET /stream` 返回 `multipart/x-mixed-replace`（MJPEG），浏览器 `<img>` 直接渲染
- 状态：`GET /api/state` → `{fps, frame_seq, window, banner, awaiting_click, steps[], run_dir}`
- 快照：每个 `end_step` 自动存 `run_dir/step_NN_<标题>.jpg`，页面按 `GET /api/steps/<idx>` 取
- 回传：`POST /api/confirm`（解除 `wait_confirm`）；`POST /api/click {x,y}`（解除 `wait_point`，屏幕绝对坐标）
- 取点坐标映射：页面按 `naturalWidth/显示宽度` 比例换算 + `window.left/top` 偏移；
  取点期间抓帧矩形被锁定，保证映射稳定
- 端口默认 7865，`127.0.0.1` 绑定（本机访问，不暴露局域网）

## 4. ffmpeg 兜底渲染的映射规则

- 视频段：按 `source_timerange` 做 `-ss/-t` 输入截取 → `scale+pad` 统一画布 → `concat`
- 字幕段：`drawtext`，`fontsize ≈ size × 高 / 88`（近似），
  `y = h×(1-transform.y)/2 - text_h/2`；中文经 `textfile=`（UTF-8 临时文件）传入；
  字体复制到工作目录再引用（绕开盘符冒号转义）
- 音频段：`volume` + `afade`（淡入淡出）+ `adelay`（偏移）→ `amix normalize=0`
- 视频段 `volume>0` 且素材带音轨 → 原声参与混音；`volume=0` → 丢弃
- **不渲染**：转场/特效/滤镜（剪映私有渲染器，无公开实现）

## 5. 排障

| 症状 | 原因与处理 |
|---|---|
| 监控页黑屏/停滞 | 目标窗口最小化会暂停渲染——恢复窗口；或窗口已关（回退全屏） |
| `add_video` 报 FileNotFoundError | ffprobe 不在 PATH 且 pymediainfo 未装；装其一即可 |
| 剪映里看不到新草稿 | 剪映已打开时不会热加载草稿库——重启剪映 |
| `Draft` 报「草稿已存在」 | 传 `overwrite=True`，或换草稿名 |
| 取点坐标偏移 | 取点时窗口被移动过——重新取点（取点期间矩形已锁定，移动窗口才需重取） |
| ffmpeg 导出缺转场效果 | 预期行为：正式成片改走 `guided` 模式由剪映导出 |
