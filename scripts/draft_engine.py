"""剪映草稿引擎（自包含）：导入素材 / 裁剪 / 分割 / 转场 / 特效 / 滤镜 / 字幕 / 音频 / 保存。

直接生成剪映 v5.9+ 的草稿目录（draft_info.json + draft_meta_info.json + draft_settings + key_value.json），
不依赖剪映界面，可靠性最高。字段结构以剪映专业版 11.5.x 真实草稿为模板。

资源（转场/特效/滤镜）需要剪映官方 resource_id/effect_id，内置 data/resources.json
（源自 Apache-2.0 授权的 pyJianYingDraft metadata，仅免费资源）；也允许调用方直接传入 id。

单位约定：时间一律微秒(int)。辅助函数 tim() 支持 int=微秒 / float=秒 / str("9.3s"|"500ms")。
"""

import copy
import json
import os
import shutil
import subprocess
import time
import uuid

US = 1_000_000  # 1 秒 = 1e6 微秒

# ---------------------------------------------------------------- 工具

def tim(v):
    """int→微秒, float→秒, str→带单位字符串。"""
    if v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v * US)
    s = str(v).strip().lower()
    if s.endswith("ms"):
        return int(float(s[:-2]) * 1000)
    if s.endswith("s"):
        return int(float(s[:-1]) * US)
    if s.endswith("us"):
        return int(s[:-2])
    return int(float(s) * US)


def _uid():
    return uuid.uuid4().hex


def default_drafts_root():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""),
                        "JianyingPro", "User Data", "Projects", "com.lveditor.draft")


def probe_media(path):
    """返回 {duration_us, width, height, has_audio}；优先 ffprobe，回退 pymediainfo。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30)
        d = json.loads(out.stdout or "{}")
        info = {"duration_us": int(float(d.get("format", {}).get("duration", 0)) * US),
                "width": 0, "height": 0, "has_audio": False}
        for st in d.get("streams", []):
            if st.get("codec_type") == "video":
                info["width"] = int(st.get("width") or 0)
                info["height"] = int(st.get("height") or 0)
            elif st.get("codec_type") == "audio":
                info["has_audio"] = True
        if info["duration_us"] > 0:
            return info
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        pass
    from pymediainfo import MediaInfo
    mi = MediaInfo.parse(path)
    info = {"duration_us": 0, "width": 0, "height": 0, "has_audio": False}
    for t in mi.tracks:
        if t.track_type == "General" and t.duration:
            info["duration_us"] = int(float(t.duration) * 1000)
        elif t.track_type == "Video":
            info["width"] = int(t.width or 0)
            info["height"] = int(t.height or 0)
            if t.duration:
                info["duration_us"] = max(info["duration_us"], int(float(t.duration) * 1000))
        elif t.track_type == "Audio":
            info["has_audio"] = True
            if t.duration:
                info["duration_us"] = max(info["duration_us"], int(float(t.duration) * 1000))
    return info


def load_resources(path=None):
    p = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "data", "resources.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {"transitions": {}, "effects": {}, "filters": {}}


# ---------------------------------------------------------------- 片段

class Segment:
    """轨道片段的轻量包装。"""

    def __init__(self, track_type, material_id, target_start, target_duration,
                 source_start=None, source_duration=None, volume=1.0,
                 clip=None, speed_material_id=None):
        self.id = _uid()
        self.track_type = track_type          # video / audio / text
        self.material_id = material_id
        self.target = {"start": target_start, "duration": target_duration}
        self.source = None
        if source_start is not None:
            self.source = {"start": source_start,
                           "duration": source_duration if source_duration is not None else target_duration}
        self.volume = volume
        self.clip = clip                      # 视觉片段的图像调节（音频为 None）
        self.speed_material_id = speed_material_id
        self.extra_material_refs = []
        if speed_material_id:
            self.extra_material_refs.append(speed_material_id)

    @property
    def end(self):
        return self.target["start"] + self.target["duration"]

    def export_json(self):
        return {
            "enable_adjust": True, "enable_color_correct_adjust": False,
            "enable_color_curves": True, "enable_color_match_adjust": False,
            "enable_color_wheels": True, "enable_lut": True,
            "enable_smart_color_adjust": False, "last_nonzero_volume": 1.0,
            "reverse": False, "track_attribute": 0, "track_render_index": 0,
            "visible": True,
            "id": self.id, "material_id": self.material_id,
            "target_timerange": dict(self.target),
            "common_keyframes": [], "keyframe_refs": [],
            "source_timerange": dict(self.source) if self.source else None,
            "speed": 1.0, "volume": self.volume,
            "extra_material_refs": list(self.extra_material_refs),
            "is_tone_modify": False,
            "clip": copy.deepcopy(self.clip) if self.clip else None,
            "uniform_scale": {"on": True, "value": 1.0} if self.clip else None,
            "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000} if self.track_type == "video" else None,
            "render_index": 0,
        }


def _default_clip(x=0.0, y=0.0, scale=1.0, alpha=1.0, rotation=0.0):
    return {"alpha": alpha,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": rotation,
            "scale": {"x": scale, "y": scale},
            "transform": {"x": x, "y": y}}


# ---------------------------------------------------------------- 草稿

class Draft:
    """一个剪映草稿工程（v5.9+ 架构）。"""

    def __init__(self, name, width=1920, height=1080, fps=30,
                 drafts_root=None, overwrite=False):
        self.name = name
        self.width, self.height, self.fps = width, height, fps
        self.drafts_root = drafts_root or default_drafts_root()
        self.draft_dir = os.path.join(self.drafts_root, name)
        if overwrite and os.path.isdir(self.draft_dir):
            shutil.rmtree(self.draft_dir, ignore_errors=True)
        if os.path.isdir(self.draft_dir):
            raise FileExistsError(f"草稿已存在: {self.draft_dir}（overwrite=True 可覆盖）")
        self.materials = {k: [] for k in (
            "videos", "audios", "texts", "speeds", "transitions",
            "video_effects", "effects", "audio_fades", "canvases")}
        self.tracks = []            # [{id,name,type,segments:[Segment]}]
        self.resources = load_resources()
        self._material_index = {}   # path -> material_id（同文件复用）

    # ---------------- 轨道 ----------------
    def _track(self, track_type, name=None):
        for t in self.tracks:
            if t["type"] == track_type and (name is None or t["name"] == name):
                return t
        t = {"attribute": 0, "flag": 0, "id": _uid(), "is_default_name": False,
             "name": name or track_type.capitalize(), "segments": [], "type": track_type}
        self.tracks.append(t)
        return t

    # ---------------- 素材 ----------------
    def _add_video_material(self, path):
        path = os.path.abspath(path)
        if path in self._material_index:
            return self._material_index[path]
        info = probe_media(path)
        mid = _uid()
        self.materials["videos"].append({
            "audio_fade": None, "category_id": "", "category_name": "local",
            "check_flag": 63487,
            "crop": {"upper_left_x": 0.0, "upper_left_y": 0.0,
                     "upper_right_x": 1.0, "upper_right_y": 0.0,
                     "lower_left_x": 0.0, "lower_left_y": 1.0,
                     "lower_right_x": 1.0, "lower_right_y": 1.0},
            "crop_ratio": "free", "crop_scale": 1.0,
            "duration": info["duration_us"],
            "height": info["height"] or self.height,
            "id": mid, "local_material_id": "", "material_id": mid,
            "material_name": os.path.basename(path), "media_path": "", "path": path,
            "type": "video",
            "video_algorithm": {"algorithms": [], "complement_frame_config": None,
                                "deflicker": None, "gameplay_configs": [],
                                "motion_blur_config": None, "noise_reduction": None,
                                "path": "", "quality_enhance": None, "time_range": None},
            "source_platform": 0, "team_id": "",
            "width": info["width"] or self.width,
        })
        self._material_index[path] = (mid, info)
        return mid, info

    def _add_audio_material(self, path):
        path = os.path.abspath(path)
        if path in self._material_index:
            return self._material_index[path]
        info = probe_media(path)
        mid = _uid()
        self.materials["audios"].append({
            "app_id": 0, "category_id": "", "category_name": "local", "check_flag": 3,
            "copyright_limit_type": "none", "duration": info["duration_us"],
            "effect_id": "", "formula_id": "", "id": mid, "local_material_id": mid,
            "music_id": mid, "name": os.path.basename(path), "path": path,
            "source_platform": 0, "type": "extract_music", "wave_points": [],
        })
        self._material_index[path] = (mid, info)
        return mid, info

    def _add_speed_material(self):
        mid = _uid()
        self.materials["speeds"].append(
            {"curve_speed": None, "id": mid, "mode": 0, "speed": 1.0, "type": "speed"})
        return mid

    # ---------------- 操作：导入 ----------------
    def add_video(self, path, start=0, source_start=0, duration=None,
                  volume=1.0, track_name="VideoTrack"):
        """导入视频到视频轨。duration=None 表示用到素材末尾。"""
        mid, info = self._add_video_material(path)
        src_start = tim(source_start)
        dur = tim(duration) if duration is not None else info["duration_us"] - src_start
        seg = Segment("video", mid, tim(start), dur, src_start, dur,
                      volume=volume, clip=_default_clip(),
                      speed_material_id=self._add_speed_material())
        self._track("video", track_name)["segments"].append(seg)
        return seg

    def add_audio(self, path, start=0, source_start=0, duration=None,
                  volume=1.0, track_name="BGM", fade_in=None, fade_out=None):
        """导入音频到音频轨，可选淡入淡出。"""
        mid, info = self._add_audio_material(path)
        src_start = tim(source_start)
        dur = tim(duration) if duration is not None else info["duration_us"] - src_start
        seg = Segment("audio", mid, tim(start), dur, src_start, dur,
                      volume=volume, speed_material_id=self._add_speed_material())
        if fade_in or fade_out:
            fid = _uid()
            self.materials["audio_fades"].append({
                "id": fid, "fade_in_duration": tim(fade_in or 0),
                "fade_out_duration": tim(fade_out or 0), "fade_type": 0,
                "type": "audio_fade"})
            seg.extra_material_refs.append(fid)
        self._track("audio", track_name)["segments"].append(seg)
        return seg

    def add_subtitle(self, text, start=0, duration=None, *, size=5.0,
                     color=(1.0, 1.0, 1.0), stroke_color=(0.0, 0.0, 0.0),
                     stroke_width=0.08, bold=False, y=-0.8,
                     track_name="Subtitles"):
        """添加字幕。y=-0.8 为底部居中（归一化坐标，0=画面中心）。"""
        content = {"styles": [{
            "fill": {"alpha": 1.0, "content": {"render_type": "solid", "solid": {
                "alpha": 1.0, "color": list(color)}}},
            "range": [0, len(text)], "size": float(size), "bold": bold,
            "italic": False, "underline": False,
            "strokes": [{"content": {"solid": {"alpha": 1.0, "color": list(stroke_color)}},
                         "width": stroke_width}] if stroke_width else []}],
            "text": text}
        mid = _uid()
        self.materials["texts"].append({
            "id": mid, "content": json.dumps(content, ensure_ascii=False),
            "typesetting": 0, "alignment": 0, "letter_spacing": 0.0,
            "line_spacing": 0.02, "line_feed": 1, "line_max_width": 0.82,
            "force_apply_line_max_width": False, "check_flag": 15,
            "type": "text", "global_alpha": 1.0})
        dur = tim(duration) if duration is not None else US * 3
        seg = Segment("text", mid, tim(start), dur, clip=_default_clip(y=y))
        self._track("text", track_name)["segments"].append(seg)
        return seg

    # ---------------- 操作：裁剪 / 分割 ----------------
    def trim(self, seg, source_start=None, source_end=None):
        """调整片段的素材入/出点（target 时长同步变化，时间轴位置不变）。"""
        if not seg.source:
            raise ValueError("该片段没有 source_timerange（字幕不可裁剪）")
        s0 = seg.source["start"]
        s1 = seg.source["start"] + seg.source["duration"]
        ns = tim(source_start) if source_start is not None else s0
        ne = tim(source_end) if source_end is not None else s1
        if ne <= ns:
            raise ValueError("出点必须大于入点")
        seg.source = {"start": ns, "duration": ne - ns}
        seg.target["duration"] = ne - ns
        return seg

    def split(self, seg, at):
        """在时间轴位置 at 处把片段切成两段，返回 (前段, 后段)。"""
        at = tim(at)
        if not (seg.target["start"] < at < seg.end):
            raise ValueError("分割点必须位于片段内部")
        offset = at - seg.target["start"]
        left_dur, right_dur = offset, seg.end - at
        right = Segment(seg.track_type, seg.material_id, at, right_dur,
                        volume=seg.volume, clip=copy.deepcopy(seg.clip))
        right.extra_material_refs = list(seg.extra_material_refs)
        if seg.source:
            right.source = {"start": seg.source["start"] + offset,
                            "duration": right_dur}
            seg.source["duration"] = left_dur
        seg.target["duration"] = left_dur
        for t in self.tracks:
            if seg in t["segments"]:
                t["segments"].insert(t["segments"].index(seg) + 1, right)
                break
        return seg, right

    # ---------------- 操作：转场 / 特效 / 滤镜 ----------------
    def _resource(self, kind, name=None, effect_id=None, resource_id=None):
        pool = self.resources.get(kind, {})
        if name:
            if name not in pool:
                raise KeyError(f"{kind} 中没有资源「{name}」，可选: {list(pool)[:20]}...")
            return name, pool[name]
        if effect_id and resource_id:
            return name or effect_id, {"resource_id": resource_id, "effect_id": effect_id}
        raise ValueError("需要 name（内置资源名）或 effect_id+resource_id")

    def add_transition(self, seg, name=None, *, effect_id=None, resource_id=None,
                       duration=None):
        """给片段末尾添加转场（转场加在**前面的**片段上）。"""
        rname, meta = self._resource("transitions", name, effect_id, resource_id)
        dur = tim(duration) if duration is not None else meta.get("duration_us", 500_000)
        tid = _uid()
        self.materials["transitions"].append({
            "category_id": "", "category_name": "", "duration": dur,
            "effect_id": meta["effect_id"], "id": tid,
            "is_overlap": meta.get("is_overlap", True), "name": rname,
            "platform": "all", "resource_id": meta["resource_id"], "type": "transition"})
        seg.extra_material_refs.append(tid)
        return tid

    def add_effect(self, seg, name=None, *, effect_id=None, resource_id=None):
        """给视频片段添加画面特效（作用于整个片段）。"""
        rname, meta = self._resource("effects", name, effect_id, resource_id)
        eid = _uid()
        self.materials["video_effects"].append({
            "adjust_params": [], "apply_target_type": 0, "apply_time_range": None,
            "category_id": "", "category_name": "", "common_keyframes": [],
            "disable_effect_faces": [], "effect_id": meta["effect_id"],
            "formula_id": "", "id": eid, "name": rname, "platform": "all",
            "render_index": 11000, "resource_id": meta["resource_id"],
            "source_platform": 0, "time_range": None, "track_render_index": 0,
            "type": "video_effect", "value": 1.0, "version": ""})
        seg.extra_material_refs.append(eid)
        return eid

    def add_filter(self, seg, name=None, *, effect_id=None, resource_id=None,
                   intensity=1.0):
        """给视频片段添加滤镜（intensity 0~1）。"""
        rname, meta = self._resource("filters", name, effect_id, resource_id)
        fid = _uid()
        self.materials["effects"].append({
            "adjust_params": [], "algorithm_artifact_path": "", "apply_target_type": 0,
            "bloom_params": None, "category_id": "", "category_name": "",
            "color_match_info": {"source_feature_path": "", "target_feature_path": "",
                                 "target_image_path": ""},
            "effect_id": meta["effect_id"], "enable_skin_tone_correction": False,
            "exclusion_group": [], "face_adjust_params": [], "formula_id": "",
            "id": fid, "intensity_key": "", "multi_language_current": "",
            "name": rname, "panel_id": "", "platform": "all",
            "resource_id": meta["resource_id"], "source_platform": 1,
            "sub_type": "none", "time_range": None, "type": "filter",
            "value": float(intensity), "version": ""})
        seg.extra_material_refs.append(fid)
        return fid

    def set_volume(self, seg, volume):
        seg.volume = float(volume)
        return seg

    # ---------------- 导出 JSON ----------------
    @property
    def duration(self):
        end = 0
        for t in self.tracks:
            for s in t["segments"]:
                end = max(end, s.end)
        return end

    def export_draft_info(self):
        track_type_order = {"video": 0, "audio": 1, "text": 2, "effect": 3}
        tracks = sorted(self.tracks, key=lambda t: track_type_order.get(t["type"], 9))
        return {
            "business_info": None,
            "canvas_config": {"width": self.width, "height": self.height, "ratio": "original"},
            "color_space": 0,
            "complement_frame_config": {"open_close_key_complement_frame_flag": False,
                                        "quality_enhance": False},
            "config": {"adjust_max_index": 1, "attachment_info": [],
                       "combination_max_index": 1, "export_range": None,
                       "extract_audio_last_index": 1, "lyrics_recognition_id": "",
                       "lyrics_sync": True, "lyrics_taskinfo": [],
                       "maintrack_adsorb": True, "material_save_mode": 0,
                       "multi_language_current": "none", "multi_language_list": [],
                       "multi_language_main": "none", "multi_language_mode": "none",
                       "original_sound_last_index": 1, "record_audio_last_index": 1,
                       "sticker_max_index": 1, "subtitle_keywords_config": None,
                       "subtitle_recognition_id": "", "subtitle_sync": True,
                       "subtitle_taskinfo": [], "system_font_list": [],
                       "video_mute": False, "zoom_info_params": None},
            "cover": None, "create_time": int(time.time() * 1000),
            "duration": self.duration, "extra_info": None, "fps": self.fps,
            "free_render_index_mode_on": False,
            "group_container": None, "id": str(uuid.uuid4()).upper(),
            "keyframe_graph_list": [],
            "keyframes": {"adjusts": [], "audios": [], "effects": [], "filters": [],
                          "handwrites": [], "stickers": [], "texts": [], "videos": []},
            "last_modified_platform": {"app_id": 3704, "app_source": "lv",
                                       "app_version": "5.9.0", "os": "windows"},
            "materials": {
                "ai_translates": [], "audio_balances": [], "audio_effects": [],
                "audio_fades": self.materials["audio_fades"], "audio_track_indexes": [],
                "audios": self.materials["audios"], "beats": [],
                "canvases": self.materials["canvases"], "chromas": [], "color_curves": [],
                "digital_humans": [], "drafts": [], "effects": self.materials["effects"],
                "flowers": [], "green_screens": [], "handwrites": [], "hsl": [],
                "images": [], "log_color_wheels": [], "loudnesses": [],
                "manual_deformations": [], "masks": [], "material_animations": [],
                "material_colors": [], "multi_language_refs": [], "placeholders": [],
                "plugin_effects": [], "primary_color_wheels": [], "realtime_denoises": [],
                "shapes": [], "smart_crops": [], "smart_relights": [],
                "sound_channel_mappings": [], "speeds": self.materials["speeds"],
                "stickers": [], "tail_leaders": [], "text_templates": [],
                "texts": self.materials["texts"], "time_marks": [],
                "transitions": self.materials["transitions"],
                "video_effects": self.materials["video_effects"], "video_trackings": [],
                "videos": self.materials["videos"], "vocal_beautifys": [],
                "vocal_separations": []},
            "mutable_config": None, "name": "", "new_version": "110.0.0",
            "platform": {"app_id": 3704, "app_source": "lv",
                         "app_version": "5.9.0", "os": "windows"},
            "relationships": [], "render_index_track_mode_on": True,
            "retouch_cover": None, "source": "default", "static_cover_image_path": "",
            "time_marks": None,
            "tracks": [{"attribute": t["attribute"], "flag": t["flag"], "id": t["id"],
                        "is_default_name": t["is_default_name"], "name": t["name"],
                        "segments": [s.export_json() for s in t["segments"]],
                        "type": t["type"]} for t in tracks],
        }

    # ---------------- 保存 ----------------
    def save(self):
        os.makedirs(self.draft_dir, exist_ok=True)
        os.makedirs(os.path.join(self.draft_dir, "materials"), exist_ok=True)
        info = self.export_draft_info()
        with open(os.path.join(self.draft_dir, "draft_info.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        now_ms = int(time.time() * 1000)
        meta = {
            "draft_cloud_last_action_download": False, "draft_cover": "",
            "draft_fold_path": self.draft_dir,
            "draft_id": info["id"], "draft_is_ai_shorts": False,
            "draft_is_invisible": False,
            "draft_json_file": os.path.join(self.draft_dir, "draft_info.json"),
            "draft_materials": [], "draft_name": self.name, "draft_new_version": "",
            "draft_removable_storage_device": "", "draft_root_path": self.drafts_root,
            "draft_timeline_materials_size_": 0, "draft_type": "",
            "tm_draft_cloud_completed": "", "tm_draft_cloud_modified": 0,
            "tm_draft_cloud_remove_entry": 0, "tm_draft_cloud_user_id": "",
            "tm_draft_create": now_ms, "tm_draft_modified": now_ms,
            "tm_duration": self.duration}
        with open(os.path.join(self.draft_dir, "draft_meta_info.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        with open(os.path.join(self.draft_dir, "draft_settings"), "w", encoding="utf-8") as f:
            f.write("[General]\ndraft_create_time=0\ndraft_last_edit_time=0\n"
                    "real_edit_keys=1\nreal_edit_seconds=0\n")
        with open(os.path.join(self.draft_dir, "key_value.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        return {"status": "SUCCESS", "draft_path": self.draft_dir,
                "duration_us": self.duration,
                "tracks": len(self.tracks),
                "segments": sum(len(t["segments"]) for t in self.tracks)}


if __name__ == "__main__":
    # 简单自测：python draft_engine.py <video> [audio]
    import sys
    v = sys.argv[1] if len(sys.argv) > 1 else None
    if not v or not os.path.exists(v):
        print("用法: python draft_engine.py <video.mp4> [audio.m4a]")
        raise SystemExit(1)
    d = Draft("draft_engine_selftest", width=540, height=960, overwrite=True)
    seg = d.add_video(v, volume=1.0)
    print("导入:", os.path.basename(v), "时长:", seg.target["duration"] / US, "s")
    a, b = d.split(seg, "2s")
    d.add_transition(a, "叠化")
    d.add_filter(b, "港风", intensity=0.8)
    d.add_subtitle("草稿引擎自测", "0s", "3s")
    if len(sys.argv) > 2 and os.path.exists(sys.argv[2]):
        d.add_audio(sys.argv[2], "0s", duration="5s", volume=0.6, fade_out="0.5s")
    print(d.save())
