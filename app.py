import streamlit as st
from dotenv import load_dotenv
import os
import tempfile
import subprocess
import glob
import base64
import asyncio
import threading
from groq import Groq
import edge_tts

load_dotenv()

import shutil
import platform

if platform.system() == "Windows":
    FFMPEG  = r"C:\ffmpeg\bin\ffmpeg.exe"
    FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe"
else:
    FFMPEG  = shutil.which("ffmpeg")  or "/usr/bin/ffmpeg"
    FFPROBE = shutil.which("ffprobe") or "/usr/bin/ffprobe"

VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
TEXT_MODEL   = "llama-3.3-70b-versatile"

NARRATION_STYLES = [
    "感動ドキュメンタリー風",
    "バラエティ・お笑い風",
    "ニュース・報道風",
    "映画予告編風",
    "Vlog・日常動画風",
]

VOICE_OPTIONS = {
    "Nanami（女性・落ち着いた）": "ja-JP-NanamiNeural",
    "Keita（男性・標準）":        "ja-JP-KeitaNeural",
    "Aoi（女性・明るい）":        "ja-JP-AoiNeural",
    "Daichi（男性・若い）":       "ja-JP-DaichiNeural",
}

try:
    groq_api_key = st.secrets.get("GROQ_API_KEY")
except Exception:
    groq_api_key = None
groq_api_key = groq_api_key or os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=groq_api_key)

st.set_page_config(page_title="動画ナレーター自動生成", layout="centered")
st.title("動画ナレーター自動生成")

# 音声選択をセッション状態で管理
if "selected_voice_name" not in st.session_state:
    st.session_state["selected_voice_name"] = list(VOICE_OPTIONS.keys())[0]

# --- 設定（折りたたみ） ---
with st.expander("設定"):
    st.write("Groq API:", "✅" if groq_api_key else "❌")
    st.write("Edge TTS:", "✅（キー不要）")
    st.divider()
    frame_count = st.slider("抽出フレーム数", min_value=3, max_value=5, value=5,
                            help="Groq Vision APIの仕様上、最大5枚")
    voice_keys = list(VOICE_OPTIONS.keys())
    setting_voice = st.selectbox(
        "ナレーター音声",
        voice_keys,
        index=voice_keys.index(st.session_state["selected_voice_name"]),
        key="voice_setting"
    )
    st.session_state["selected_voice_name"] = setting_voice


# --- ユーティリティ関数 ---

def extract_frames(video_path: str, num_frames: int, output_dir: str) -> list[str]:
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True
    )
    duration = float(result.stdout.strip())
    interval = duration / (num_frames + 1)
    output_pattern = os.path.join(output_dir, "frame_%03d.jpg")

    subprocess.run([
        FFMPEG, "-i", video_path,
        "-vf", f"fps=1/{interval:.2f},scale=1280:-1",
        "-vframes", str(num_frames),
        "-q:v", "2",
        output_pattern, "-y"
    ], capture_output=True)

    return sorted(glob.glob(os.path.join(output_dir, "frame_*.jpg")))


def analyze_with_groq(frame_paths: list[str]) -> str:
    content = []
    for frame_path in frame_paths:
        with open(frame_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
        })
    content.append({
        "type": "text",
        "text": f"""これらは動画から均等間隔で抽出した{len(frame_paths)}枚のフレーム画像です（時系列順）。
動画全体の内容を分析し、以下を日本語で説明してください：
1. 動画のテーマや内容
2. 主要なシーンや登場するもの
3. 動画の雰囲気やトーン
4. 動画の流れ（始まり〜中間〜終わり）
ナレーション作成に役立つ具体的な情報を提供してください。"""
    })

    response = groq_client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{"role": "user", "content": content}],
        max_tokens=1000
    )
    return response.choices[0].message.content


def generate_narrations(analysis: str) -> dict[str, str]:
    styles_text = "\n".join([f"{i+1}. {s}" for i, s in enumerate(NARRATION_STYLES)])
    prompt = f"""以下は動画の解析内容です：

{analysis}

この動画に対して、以下の5つのスタイルでナレーションを生成してください。
各ナレーションは読み上げ時間が30〜60秒程度（200〜400文字）にしてください。

{styles_text}

以下のフォーマットで出力してください（スタイル名を【】で囲む）：
【感動ドキュメンタリー風】
（ナレーション本文）

【バラエティ・お笑い風】
（ナレーション本文）

※以降同様に5スタイル分出力"""

    response = groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000
    )

    text = response.choices[0].message.content
    narrations = {}
    current_style = None
    current_lines = []

    for line in text.split("\n"):
        stripped = line.strip()
        matched = False
        for style in NARRATION_STYLES:
            if f"【{style}】" in stripped:
                if current_style and current_lines:
                    narrations[current_style] = "\n".join(current_lines).strip()
                current_style = style
                current_lines = []
                matched = True
                break
        if not matched and current_style and stripped:
            current_lines.append(stripped)

    if current_style and current_lines:
        narrations[current_style] = "\n".join(current_lines).strip()

    return narrations


def text_to_speech(text: str, voice: str, output_path: str):
    async def _run():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(output_path)

    def run_in_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()

    thread = threading.Thread(target=run_in_thread)
    thread.start()
    thread.join()


WINDOWS_FONTS = {
    "メイリオ（丸ゴシック）":  "Meiryo",
    "游ゴシック（細め）":      "Yu Gothic",
    "游明朝（明朝体）":        "Yu Mincho",
    "ＭＳ ゴシック（等幅）":   "MS Gothic",
}
LINUX_FONTS = {
    "Noto Sans CJK（標準）":   "Noto Sans CJK JP",
    "Noto Serif CJK（明朝）":  "Noto Serif CJK JP",
}

def get_font_options() -> dict:
    return WINDOWS_FONTS if platform.system() == "Windows" else LINUX_FONTS


def get_audio_duration(audio_path: str) -> float:
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


def split_into_segments(text: str) -> list[str]:
    """句読点でテキストをカラオケ字幕用セグメントに分割"""
    import re
    parts = re.split(r'(?<=[。！？\n])', text)
    segments = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 長いセグメントはさらに20文字で分割
        while len(part) > 22:
            segments.append(part[:22])
            part = part[22:]
        if part:
            segments.append(part)
    return segments


def hex_to_ass(hex_color: str, opacity: int) -> str:
    """#RRGGBBとopacity(0=透明〜100=不透明)をASS色に変換"""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    alpha = int((100 - opacity) / 100 * 255)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def generate_ass(segments: list[str], total_duration: float,
                 font: str = "", bold: bool = False,
                 text_color: str = "#FFFFFF", text_opacity: int = 100,
                 bg_color: str = "#000000", bg_opacity: int = 20) -> str:
    if not font:
        font = list(get_font_options().values())[0]
    bold_flag = -1 if bold else 0
    seg_dur = total_duration / max(len(segments), 1)

    def to_bgr(hex_color: str) -> str:
        h = hex_color.lstrip("#")
        return f"{h[4:6]}{h[2:4]}{h[0:2]}"

    def to_alpha(opacity: int) -> str:
        return f"{int((100 - opacity) / 100 * 255):02X}"

    t_bgr   = to_bgr(text_color)
    t_alpha = to_alpha(text_opacity)
    b_bgr   = to_bgr(bg_color)
    b_alpha = to_alpha(bg_opacity)

    # inline override tags applied per-line so alpha actually takes effect in libass
    override = (
        f"{{\\1c&H{t_bgr}&\\1a&H{t_alpha}&"
        f"\\4c&H{b_bgr}&\\4a&H{b_alpha}&}}"
    )

    def fmt(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},28,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,{bold_flag},0,0,0,100,100,0,0,3,10,0,2,20,20,30,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for i, seg in enumerate(segments):
        start = fmt(i * seg_dur)
        end   = fmt((i + 1) * seg_dur)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{override}{seg}")

    return "\n".join(lines)


def merge_audio_video(video_path: str, audio_path: str, output_path: str,
                      subtitle_text: str = "", subtitle_font: str = "",
                      subtitle_bold: bool = False,
                      text_color: str = "#FFFFFF", text_opacity: int = 100,
                      bg_color: str = "#000000", bg_opacity: int = 20):
    vf_filters = []

    if subtitle_text:
        duration = get_audio_duration(audio_path)
        segments = split_into_segments(subtitle_text)
        ass_content = generate_ass(segments, duration, subtitle_font, subtitle_bold,
                                   text_color, text_opacity, bg_color, bg_opacity)

        ass_file = output_path + ".ass"
        with open(ass_file, "w", encoding="utf-8") as f:
            f.write(ass_content)

        escaped_ass = ass_file.replace("\\", "/").replace(":", "\\:")
        vf_filters.append(f"subtitles='{escaped_ass}'")

    cmd = [FFMPEG, "-i", video_path, "-i", audio_path]
    if vf_filters:
        cmd += ["-vf", ",".join(vf_filters), "-c:a", "aac"]
    else:
        cmd += ["-c:v", "copy", "-c:a", "aac"]
    cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest", output_path, "-y"]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpegエラー:\n{result.stderr}")


# --- メインUI ---

st.header("Step 1: 動画をアップロード")

uploaded_file = st.file_uploader(
    "動画ファイルを選択してください（MP4・MOV・M4V対応）",
    type=["mp4", "mov", "m4v"],
    help="サイズ制限なし（フレーム抽出方式）。iPhone動画（.mov）対応"
)

if uploaded_file is not None:
    # アップロードされた動画データをセッションに保持
    if "video_bytes" not in st.session_state or st.session_state.get("video_name") != uploaded_file.name:
        st.session_state["video_bytes"] = uploaded_file.getvalue()
        st.session_state["video_name"] = uploaded_file.name
        st.session_state.pop("video_analysis", None)
        st.session_state.pop("narrations", None)
        st.session_state.pop("output_video", None)

    st.success(f"アップロード完了: {uploaded_file.name}")

    col1, col2 = st.columns([2, 1])
    with col1:
        st.video(uploaded_file)
    with col2:
        st.info(f"""
        **ファイル情報**
        - ファイル名: {uploaded_file.name}
        - サイズ: {uploaded_file.size / 1024 / 1024:.1f} MB
        """)
        analyze_btn = st.button("ナレーションを生成する", type="primary")

    if analyze_btn:
        with tempfile.TemporaryDirectory() as tmp_dir:
            # 動画を一時保存
            video_path = os.path.join(tmp_dir, "input.mp4")
            with open(video_path, "wb") as f:
                f.write(st.session_state["video_bytes"])

            # フレーム抽出
            with st.spinner(f"フレームを{frame_count}枚抽出中..."):
                frames = extract_frames(video_path, frame_count, tmp_dir)

            if not frames:
                st.error("フレームの抽出に失敗しました。FFmpegを確認してください。")
                st.stop()

            with st.expander(f"抽出フレーム（{len(frames)}枚）"):
                cols = st.columns(len(frames))
                for i, fp in enumerate(frames):
                    cols[i].image(fp, use_container_width=True)

            # Groq Vision で解析
            with st.spinner("Groqが映像を解析中..."):
                try:
                    analysis = analyze_with_groq(frames)
                    st.session_state["video_analysis"] = analysis
                except Exception as e:
                    st.error(f"解析エラー: {e}")
                    st.stop()

            # ナレーション生成
            with st.spinner("5パターンのナレーションを生成中..."):
                try:
                    narrations = generate_narrations(analysis)
                    st.session_state["narrations"] = narrations
                    st.success("生成完了！")
                except Exception as e:
                    st.error(f"ナレーション生成エラー: {e}")
                    st.stop()

    # --- Step 2: 解析結果 ---
    if "video_analysis" in st.session_state:
        st.divider()
        with st.expander("Groq 解析結果（クリックで表示）"):
            st.text_area("解析内容", st.session_state["video_analysis"], height=200)

    # --- Step 3: ナレーション選択・編集 ---
    if "narrations" in st.session_state:
        st.divider()
        st.header("Step 2: ナレーションを選択・編集")

        narrations = st.session_state["narrations"]
        if narrations:
            selected_style = st.radio("スタイルを選択", list(narrations.keys()))

            # 編集内容をセッション状態に保持
            edit_key = f"edited_{selected_style}"
            if edit_key not in st.session_state:
                st.session_state[edit_key] = narrations.get(selected_style, "")

            edited_text = st.text_area(
                "ナレーション（自由に編集できます）",
                st.session_state[edit_key],
                height=200,
                key=f"edit_{selected_style}",
                on_change=lambda: st.session_state.update(
                    {edit_key: st.session_state[f"edit_{selected_style}"]}
                )
            )

            st.divider()
            st.header("Step 3: 音声合成＆動画書き出し")
            voice_keys = list(VOICE_OPTIONS.keys())
            step3_voice = st.selectbox(
                "ナレーター音声",
                voice_keys,
                index=voice_keys.index(st.session_state["selected_voice_name"]),
                key="voice_step3"
            )
            st.session_state["selected_voice_name"] = step3_voice
            voice = VOICE_OPTIONS[step3_voice]

            show_subtitle = st.checkbox("字幕を表示する（テキストを動画に焼き込む）")
            if show_subtitle:
                font_options = get_font_options()
                sub_font_name = st.selectbox("字幕フォント", list(font_options.keys()))
                sub_font = font_options[sub_font_name]
                sub_bold = st.checkbox("太字にする")
                st.write("文字")
                c1, c2 = st.columns(2)
                text_color   = c1.color_picker("文字色", "#FFFFFF")
                text_opacity = c2.slider("不透明度", 0, 100, 100, key="text_op")
                st.write("背景")
                c3, c4 = st.columns(2)
                bg_color   = c3.color_picker("背景色", "#000000")
                bg_opacity = c4.slider("不透明度", 0, 100, 20, key="bg_op")
            else:
                sub_font = ""
                sub_bold = False
                text_color, text_opacity = "#FFFFFF", 100
                bg_color, bg_opacity = "#000000", 20

            if st.button("この内容で動画を生成する", type="primary"):
                with tempfile.TemporaryDirectory() as out_dir:
                    orig_video = os.path.join(out_dir, "input.mp4")
                    with open(orig_video, "wb") as f:
                        f.write(st.session_state["video_bytes"])

                    raw_audio = os.path.join(out_dir, "narration_raw.mp3")
                    audio_path = os.path.join(out_dir, "narration.wav")
                    with st.spinner("Edge TTSで音声を合成中..."):
                        text_to_speech(edited_text, voice, raw_audio)
                        # WAVに変換してFFmpegが確実に読めるようにする
                        subprocess.run([FFMPEG, "-y", "-i", raw_audio,
                                        "-ar", "44100", "-ac", "1", audio_path],
                                       capture_output=True)

                    output_path = os.path.join(out_dir, "output.mp4")
                    with st.spinner("FFmpegで動画を合成中..."):
                        try:
                            subtitle = edited_text if show_subtitle else ""
                            merge_audio_video(orig_video, audio_path, output_path,
                                              subtitle, sub_font, sub_bold,
                                              text_color, text_opacity,
                                              bg_color, bg_opacity)
                        except RuntimeError as e:
                            st.error(str(e))
                            st.stop()

                    with open(output_path, "rb") as f:
                        st.session_state["output_video"] = f.read()
                    st.session_state["output_filename"] = f"narrated_{uploaded_file.name}"
                    st.success("動画の生成が完了しました！")

        else:
            st.warning("ナレーションの解析に失敗しました。再度試してください。")

    # --- ダウンロード ---
    if "output_video" in st.session_state:
        st.divider()
        st.download_button(
            label="完成動画をダウンロード",
            data=st.session_state["output_video"],
            file_name=st.session_state.get("output_filename", "output.mp4"),
            mime="video/mp4",
            type="primary"
        )

else:
    st.info("動画ファイルをアップロードすると解析を開始できます。")
