"""剪映自动化编排器：把 草稿引擎 + 实时监控 + UI 驱动 串成完整剪辑流程。

每一步操作都会：
  1. 在监控页面上开启一个步骤（begin_step）
  2. 执行（草稿引擎 / UI 模拟）
  3. 截图存证 + 记录状态（end_step）

用法：
  # 端到端演示（默认仅草稿级操作 + 监控；--live-ui 才会驱动剪映界面）
  python jy_live_ops.py demo --video a.mp4 [--audio b.m4a] [--live-ui]
  # 只开监控（看剪映窗口或全屏）
  python jy_live_ops.py monitor [--port 7865]
  # 从已有草稿导出 MP4（ffmpeg 兜底渲染）
  python jy_live_ops.py export <草稿名> <out.mp4>
"""

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from draft_engine import Draft, US, default_drafts_root, probe_media, tim  # noqa: E402
from live_monitor import Monitor                                          # noqa: E402
from ui_driver import JyApp, find_jianying_exe, is_running                # noqa: E402


# ---------------------------------------------------------------- ffmpeg 兜底渲染

def render_draft_ffmpeg(draft_dir, output, fontfile=None, crf=18):
    """把草稿渲染成 MP4（兜底导出）。

    支持：视频段（含裁剪/分割）、字幕段（样式近似）、音频段（音量/淡入淡出/混音）。
    不支持：转场/特效/滤镜（剪映私有渲染，需剪映导出）。调用方需自行提示。
    """
    with open(os.path.join(draft_dir, "draft_info.json"), encoding="utf-8") as f:
        info = json.load(f)
    W, H = info["canvas_config"]["width"], info["canvas_config"]["height"]
    fontfile = fontfile or r"C:\Windows\Fonts\msyhbd.ttc"
    work = os.path.abspath(os.path.dirname(output) or ".")
    font_rel = os.path.join(work, "__jy_font.ttc")
    import shutil
    shutil.copy(fontfile, font_rel)  # 复制到工作目录，避免盘符冒号转义

    mats = info["materials"]
    mat_by_id = {}
    for group in ("videos", "audios", "texts"):
        for m in mats.get(group, []):
            mat_by_id[m["id"]] = (group, m)

    inputs, vlabels, alabels, drawtexts = [], [], [], []
    text_idx = 0
    n_input = 0
    for tr in info["tracks"]:
        for seg in tr["segments"]:
            group, mat = mat_by_id.get(seg["material_id"], (None, None))
            tgt, src = seg["target_timerange"], seg.get("source_timerange")
            start_s = tgt["start"] / US
            dur_s = tgt["duration"] / US
            if group == "videos":
                idx = n_input
                n_input += 1
                inputs += ["-ss", str((src or {"start": 0})["start"] / US),
                           "-t", str((src or tgt)["duration"] / US), "-i", mat["path"]]
                lbl = f"v{idx}"
                vol = seg.get("volume", 1.0)
                vlabels.append((f"[{idx}:v]", lbl))
                if vol and vol > 0 and probe_media(mat["path"]).get("has_audio"):
                    # 视频段音量>0 且素材带音轨 → 原声混入
                    alabels.append((f"[{idx}:a]", f"a{idx}", start_s, vol, None,
                                    dur_s))
            elif group == "audios":
                idx = n_input
                n_input += 1
                inputs += ["-ss", str((src or {"start": 0})["start"] / US),
                           "-t", str((src or tgt)["duration"] / US), "-i", mat["path"]]
                fade = None
                for ref in seg.get("extra_material_refs", []):
                    for fdm in mats.get("audio_fades", []):
                        if fdm["id"] == ref:
                            fade = fdm
                alabels.append((f"[{idx}:a]", f"a{idx}", start_s,
                                seg.get("volume", 1.0), fade, dur_s))
            elif group == "texts":
                try:
                    content = json.loads(mat["content"])
                    text = content.get("text", "")
                    st = (content.get("styles") or [{}])[0]
                    size = float(st.get("size", 5.0))
                    col = st.get("fill", {}).get("content", {}).get(
                        "solid", {}).get("color", [1, 1, 1])
                    strokes = st.get("strokes") or []
                    scol = strokes[0]["content"]["solid"]["color"] if strokes else [0, 0, 0]
                    sw = strokes[0].get("width", 0.08) if strokes else 0
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                clip = seg.get("clip") or {}
                ty = clip.get("transform", {}).get("y", -0.8)
                # 写入字幕文本文件（UTF-8），避免命令行中文编码问题
                tpath = os.path.join(work, f"__jy_text_{text_idx}.txt")
                with open(tpath, "w", encoding="utf-8", newline="") as tf:
                    tf.write(text)
                fontsize = max(12, round(size * H / 88))
                borderw = max(0, round(sw * fontsize))
                hexcol = "".join(f"{int(c * 255):02x}" for c in col)
                hexscol = "".join(f"{int(c * 255):02x}" for c in scol)
                yexpr = f"h*{round((1 - ty) / 2, 4)}-text_h/2"
                drawtexts.append(
                    f"drawtext=fontfile=__jy_font.ttc:textfile=__jy_text_{text_idx}.txt:"
                    f"fontcolor=0x{hexcol}:fontsize={fontsize}:borderw={borderw}:"
                    f"bordercolor=0x{hexscol}:x=(w-text_w)/2:y={yexpr}:"
                    f"enable='between(t,{start_s},{start_s + dur_s})'")
                text_idx += 1

    if not vlabels:
        raise RuntimeError("草稿中没有视频段，无法渲染")

    fc = []
    vchain = "".join(f"[{lbl}]" for _, lbl in vlabels) + \
        f"concat=n={len(vlabels)}:v=1:a=0[vcat]"
    for src_lbl, lbl in vlabels:
        fc.append(f"{src_lbl}scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1[{lbl}]")
    fc.append(vchain)
    last = "[vcat]"
    if drawtexts:
        for i, dt in enumerate(drawtexts):
            nxt = f"[vdt{i}]"
            fc.append(f"{last}{dt}{nxt}")
            last = nxt
    fc.append(f"{last}format=yuv420p[vout]")

    total_dur = info["duration"] / US
    if alabels:
        for src_lbl, lbl, start_s, vol, fade, dur_s in alabels:
            chain = f"{src_lbl}volume={vol}"
            if fade:
                fin = fade.get("fade_in_duration", 0) / US
                fout = fade.get("fade_out_duration", 0) / US
                if fin > 0:
                    chain += f",afade=t=in:st=0:d={fin}"
                if fout > 0 and dur_s:
                    chain += f",afade=t=out:st={max(dur_s - fout, 0)}:d={fout}"
            if start_s > 0:
                chain += f",adelay={int(start_s * 1000)}|{int(start_s * 1000)}"
            fc.append(chain + f"[{lbl}]")
        fc.append("".join(f"[{lbl}]" for _, lbl, *_ in alabels) +
                  f"amix=inputs={len(alabels)}:normalize=0[aout]")
        maps = ["-map", "[vout]", "-map", "[aout]"]
    else:
        maps = ["-map", "[vout]"]

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"] + inputs + [
        "-filter_complex", ";".join(fc)] + maps + [
        "-t", str(total_dur), "-c:v", "libx264", "-preset", "medium",
        "-crf", str(crf), "-r", str(info.get("fps", 30))]
    if alabels:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", os.path.abspath(output)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=work, timeout=1800)
    # 清理临时字幕/字体
    for i in range(text_idx):
        try:
            os.remove(os.path.join(work, f"__jy_text_{i}.txt"))
        except OSError:
            pass
    try:
        os.remove(font_rel)
    except OSError:
        pass
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 渲染失败: " + (proc.stderr or "")[-800:])
    return output


# ---------------------------------------------------------------- 编排会话

class LiveEditingSession:
    """一个可实时监控的剪映剪辑会话。"""

    def __init__(self, draft_name, width=1920, height=1080, fps=30,
                 port=7865, run_dir=None, overwrite=True):
        self.mon = Monitor(port=port, run_dir=run_dir)
        self.app = JyApp(monitor=self.mon)
        self.draft = Draft(draft_name, width=width, height=height, fps=fps,
                           overwrite=overwrite)
        self.draft_name = draft_name

    # ---------------- 监控生命周期 ----------------
    def start_monitor(self):
        url = self.mon.start()
        self.mon.note(f"监控已启动: {url}")
        return url

    def stop_monitor(self):
        self.mon.stop()

    # ---------------- 剪辑步骤 ----------------
    def step_import(self, path, **kw):
        self.mon.begin_step("导入素材", os.path.basename(path))
        try:
            seg = self.draft.add_video(path, **kw)
            self.mon.end_step("ok", f"时长 {seg.target['duration'] / US:.2f}s "
                                    f"音量 {seg.volume}")
            return seg
        except Exception as e:
            self.mon.end_step("fail", str(e))
            raise

    def step_trim(self, seg, source_start=None, source_end=None):
        self.mon.begin_step("时间轴裁剪", f"入点={source_start} 出点={source_end}")
        try:
            self.draft.trim(seg, source_start, source_end)
            self.mon.end_step("ok", f"现时长 {seg.target['duration'] / US:.2f}s")
            return seg
        except Exception as e:
            self.mon.end_step("fail", str(e))
            raise

    def step_split(self, seg, at):
        self.mon.begin_step("片段分割", f"在 {at} 处切开")
        try:
            a, b = self.draft.split(seg, at)
            self.mon.end_step("ok", f"前 {a.target['duration'] / US:.2f}s / "
                                    f"后 {b.target['duration'] / US:.2f}s")
            return a, b
        except Exception as e:
            self.mon.end_step("fail", str(e))
            raise

    def step_transition(self, seg, name=None, **kw):
        self.mon.begin_step("添加转场", name or str(kw))
        try:
            self.draft.add_transition(seg, name, **kw)
            self.mon.end_step("ok", "已挂载到前段末尾")
        except Exception as e:
            self.mon.end_step("fail", str(e))
            raise

    def step_effect(self, seg, name=None, **kw):
        self.mon.begin_step("添加特效", name or str(kw))
        try:
            self.draft.add_effect(seg, name, **kw)
            self.mon.end_step("ok")
        except Exception as e:
            self.mon.end_step("fail", str(e))
            raise

    def step_filter(self, seg, name=None, **kw):
        self.mon.begin_step("添加滤镜", name or str(kw))
        try:
            self.draft.add_filter(seg, name, **kw)
            self.mon.end_step("ok")
        except Exception as e:
            self.mon.end_step("fail", str(e))
            raise

    def step_subtitle(self, text, start=0, duration=None, **kw):
        self.mon.begin_step("插入字幕", text)
        try:
            seg = self.draft.add_subtitle(text, start, duration, **kw)
            self.mon.end_step("ok", f"{tim(start) / US:.1f}s 起 "
                                    f"{seg.target['duration'] / US:.1f}s")
            return seg
        except Exception as e:
            self.mon.end_step("fail", str(e))
            raise

    def step_audio(self, path, **kw):
        self.mon.begin_step("插入音频", os.path.basename(path))
        try:
            seg = self.draft.add_audio(path, **kw)
            self.mon.end_step("ok", f"音量 {seg.volume}")
            return seg
        except Exception as e:
            self.mon.end_step("fail", str(e))
            raise

    def step_save(self):
        self.mon.begin_step("保存草稿", self.draft_name)
        try:
            r = self.draft.save()
            self.mon.end_step("ok", f"{r['tracks']} 轨道 / {r['segments']} 片段")
            return r
        except Exception as e:
            self.mon.end_step("fail", str(e))
            raise

    # ---------------- 剪映界面联动 ----------------
    def step_open_in_jianying(self, timeout=300, auto_click=False):
        """启动剪映并引导打开草稿（UI 自动化在 QML 界面上不可靠，默认引导式）。"""
        self.mon.begin_step("打开剪映", self.draft_name)
        try:
            if not is_running():
                self.mon.note("正在启动剪映专业版…")
            ok = self.app.launch(timeout=90)
            if not ok:
                self.mon.end_step("fail", "剪映窗口未出现")
                return False
            self.app.focus()
            time.sleep(1)
            self.mon.end_step("ok", "剪映已就绪")
            # 引导用户打开草稿并确认
            self.mon.begin_step("确认草稿画面", "人工核对时间轴")
            confirmed = self.mon.wait_confirm(
                f"请在剪映首页双击打开草稿「{self.draft_name}」，"
                f"确认时间轴内容正确后，点此页面的「确认」按钮", timeout=timeout)
            self.mon.end_step("ok" if confirmed else "warn",
                              "用户已确认" if confirmed else "等待超时，未确认")
            return confirmed
        except Exception as e:
            self.mon.end_step("fail", str(e))
            raise

    def step_export(self, output, mode="guided", timeout=600,
                    res_note="1080P / 30fps / 码率推荐更高"):
        """导出成片。mode: guided=引导用户在剪映导出; ffmpeg=自动兜底渲染。"""
        self.mon.begin_step("导出成片", f"mode={mode} → {output}")
        try:
            if mode == "ffmpeg":
                self.mon.note("ffmpeg 兜底渲染：转场/特效/滤镜为剪映私有渲染，"
                              "本模式将跳过（仅视频+字幕+音频）")
                render_draft_ffmpeg(self.draft.draft_dir, output)
                if os.path.exists(output):
                    self.mon.end_step("ok", f"{os.path.getsize(output) / 1048576:.1f}MB")
                    return output
                self.mon.end_step("fail", "未生成输出文件")
                return None
            # guided：引导用户在剪映里导出
            confirmed = self.mon.wait_confirm(
                f"请在剪映中点击右上角「导出」，参数：{res_note}，"
                f"导出到 {output}，完成后点「确认」", timeout=timeout)
            if confirmed and os.path.exists(output):
                self.mon.end_step("ok", "用户确认 + 文件已生成")
                return output
            if confirmed:
                self.mon.end_step("warn", "用户已确认但未检测到文件，请检查导出路径")
                return None
            self.mon.end_step("warn", "等待超时")
            return None
        except Exception as e:
            self.mon.end_step("fail", str(e))
            raise


# ---------------------------------------------------------------- CLI

def cmd_monitor(args):
    mon = Monitor(port=args.port, window_regex=args.regex, fps=args.fps)
    print("监控地址:", mon.start())
    print("按 Ctrl+C 结束")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mon.stop()


def cmd_demo(args):
    if not os.path.exists(args.video):
        print("视频不存在:", args.video)
        return 1
    info = probe_media(args.video)
    w, h = info["width"] or 1920, info["height"] or 1080
    sess = LiveEditingSession(args.name, width=w, height=h,
                              port=args.port, overwrite=True)
    url = sess.start_monitor()
    print("=" * 60)
    print("监控页面:", url, "（在浏览器打开即可实时观看）")
    print("=" * 60)
    try:
        vseg = sess.step_import(args.video, volume=1.0 if args.keep_sound else 0.0)
        half = vseg.target["duration"] // 2
        a, b = sess.step_split(vseg, half)
        try:
            sess.step_transition(a, args.transition)
        except KeyError as e:
            sess.mon.note(f"转场跳过: {e}")
        try:
            sess.step_filter(b, args.filter, intensity=0.8)
        except KeyError as e:
            sess.mon.note(f"滤镜跳过: {e}")
        sess.step_subtitle(args.subtitle, "0s", vseg.target["duration"])
        if args.audio and os.path.exists(args.audio):
            sess.step_audio(args.audio, "0s",
                            duration=vseg.target["duration"],
                            volume=0.8, fade_out="0.5s")
        sess.step_save()
        if args.live_ui:
            sess.step_open_in_jianying()
            sess.step_export(args.output, mode="guided")
        elif args.export == "ffmpeg":
            sess.step_export(args.output, mode="ffmpeg")
            print("成片:", args.output)
        print("全部步骤完成。监控页面仍在线，Ctrl+C 结束。")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sess.stop_monitor()
    return 0


def cmd_export(args):
    draft_dir = os.path.join(default_drafts_root(), args.draft)
    if not os.path.isdir(draft_dir):
        print("草稿不存在:", draft_dir)
        return 1
    render_draft_ffmpeg(draft_dir, args.output)
    print("成片:", args.output)
    return 0


def main():
    ap = argparse.ArgumentParser(description="剪映自动化（实时监控版）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("monitor", help="仅开启画面监控")
    p.add_argument("--port", type=int, default=7865)
    p.add_argument("--regex", default=r"剪映|Jianying|CapCut")
    p.add_argument("--fps", type=float, default=5.0)
    p.set_defaults(fn=cmd_monitor)

    p = sub.add_parser("demo", help="端到端演示流程")
    p.add_argument("--video", required=True)
    p.add_argument("--audio", default=None)
    p.add_argument("--name", default="live_ops_demo")
    p.add_argument("--subtitle", default="自动剪辑演示")
    p.add_argument("--transition", default="叠化")
    p.add_argument("--filter", default="港风")
    p.add_argument("--keep-sound", action="store_true", help="保留视频原声")
    p.add_argument("--export", choices=["none", "ffmpeg"], default="ffmpeg")
    p.add_argument("--output", default="live_ops_demo.mp4")
    p.add_argument("--live-ui", action="store_true",
                   help="启动剪映并引导人工确认/导出")
    p.add_argument("--port", type=int, default=7865)
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("export", help="从已有草稿 ffmpeg 兜底导出")
    p.add_argument("draft")
    p.add_argument("output")
    p.set_defaults(fn=cmd_export)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
