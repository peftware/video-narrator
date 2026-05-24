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

st.markdown("""
<style>
button[kind="secondary"] {
    background-color: #1976d2 !important;
    color: white !important;
    border: none !important;
}
button[kind="secondary"]:hover {
    background-color: #1565c0 !important;
}
</style>
""", unsafe_allow_html=True)

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


def text_to_speech_bytes(text: str, voice: str, rate: str = "+0%") -> bytes:
    """edge_tts からオーディオバイト列を取得して返す（例外は RuntimeError で再送出）"""
    result: list = [None]
    error:  list = [None]

    async def _collect():
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        chunks = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks += chunk["data"]
        return chunks

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result[0] = loop.run_until_complete(_collect())
        except Exception as e:
            error[0] = e
        finally:
            loop.close()

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()

    if error[0] is not None:
        raise RuntimeError(f"Edge TTS エラー: {error[0]}")
    return result[0] or b""


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
    out = result.stdout.strip()
    if not out:
        raise RuntimeError(f"音声ファイルの長さを取得できません。ffprobeエラー:\n{result.stderr}")
    return float(out)


def get_video_duration(video_path: str) -> float:
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True
    )
    out = result.stdout.strip()
    if not out:
        raise RuntimeError(f"動画ファイルの長さを取得できません。ffprobeエラー:\n{result.stderr}")
    return float(out)


def build_atempo_filter(ratio: float) -> str:
    """atempo フィルターチェーンを生成（0.5-2.0の範囲外はチェーン）"""
    filters = []
    r = ratio
    while r > 2.0:
        filters.append("atempo=2.0")
        r /= 2.0
    while r < 0.5:
        filters.append("atempo=0.5")
        r /= 0.5
    if abs(r - 1.0) > 0.001:
        filters.append(f"atempo={r:.4f}")
    return ",".join(filters) if filters else "anull"


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


WINDOWS_FONT_PATHS = {
    "Meiryo":    ["meiryo.ttc", "meiryob.ttc"],
    "Yu Gothic": ["YuGoth.ttc", "yugothib.ttc"],
    "Yu Mincho": ["yumin.ttf"],
    "MS Gothic": ["msgothic.ttc"],
}
LINUX_FONT_FALLBACKS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
]


def resolve_font_path(font_name: str) -> str:
    if platform.system() == "Windows":
        fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        for fname in WINDOWS_FONT_PATHS.get(font_name, []):
            path = os.path.join(fonts_dir, fname)
            if os.path.exists(path):
                return path
        return ""
    try:
        r = subprocess.run(
            ["fc-match", font_name, "--format=%{file}"],
            capture_output=True, text=True, timeout=5
        )
        path = r.stdout.strip()
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    for candidate in LINUX_FONT_FALLBACKS:
        if os.path.exists(candidate):
            return candidate
    return ""


def build_drawtext_filter(segments: list[str], total_duration: float,
                          font_path: str,
                          text_color: str = "#FFFFFF", text_opacity: int = 100,
                          bg_color: str = "#000000", bg_opacity: int = 20,
                          font_size: int = 28, sub_speed: float = 1.0) -> str:
    seg_dur = (total_duration / max(len(segments), 1)) / sub_speed
    box_pad = 8

    def to_ffcolor(hex_color: str, opacity: int) -> str:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"0x{r:02x}{g:02x}{b:02x}@{opacity/100:.2f}"

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("'", "\\'")

    text_col = to_ffcolor(text_color, text_opacity)
    box_col  = to_ffcolor(bg_color, bg_opacity)
    font_opt = f":fontfile='{font_path}'" if font_path else ""
    y_pos    = f"h-{font_size + box_pad * 2 + 10}"

    parts = []
    for i, seg in enumerate(segments):
        start = i * seg_dur
        end   = (i + 1) * seg_dur
        part = (
            f"drawtext=text='{esc(seg)}'"
            f"{font_opt}"
            f":fontsize={font_size}"
            f":fontcolor={text_col}"
            f":box=1"
            f":boxcolor={box_col}"
            f":boxborderw={box_pad}"
            f":x=(w-text_w)/2"
            f":y={y_pos}"
            f":enable='between(t,{start:.3f},{end:.3f})'"
        )
        parts.append(part)

    return ",".join(parts)


def merge_audio_video(video_path: str, audio_path: str, output_path: str,
                      subtitle_text: str = "", subtitle_font: str = "",
                      text_color: str = "#FFFFFF", text_opacity: int = 100,
                      bg_color: str = "#000000", bg_opacity: int = 20,
                      font_size: int = 28, sub_speed: float = 1.0,
                      total_duration: float = 0.0):
    vf_filters = []

    if subtitle_text:
        duration = total_duration if total_duration > 0 else get_audio_duration(audio_path)
        segments = split_into_segments(subtitle_text)
        font_path = resolve_font_path(subtitle_font)
        dt = build_drawtext_filter(
            segments, duration, font_path,
            text_color, text_opacity, bg_color, bg_opacity, font_size, sub_speed
        )
        vf_filters.append(dt)

    cmd = [FFMPEG, "-i", video_path, "-i", audio_path]
    if vf_filters:
        cmd += ["-vf", ",".join(vf_filters), "-c:a", "aac"]
    else:
        cmd += ["-c:v", "copy", "-c:a", "aac"]
    # total_duration > 0 means audio already matches video length — no -shortest needed
    if total_duration > 0:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", output_path, "-y"]
    else:
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
        analyze_btn = st.button("ナレーションを生成する", type="primary", use_container_width=True)
        manual_btn  = st.button("ナレーションを自分で構成する", use_container_width=True)

    if manual_btn:
        st.session_state["narrations"] = {"自由入力": ""}
        st.session_state.pop("video_analysis", None)
        st.rerun()

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
            style_keys = list(narrations.keys())
            if len(style_keys) == 1:
                selected_style = style_keys[0]
            else:
                selected_style = st.radio("スタイルを選択", style_keys)

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

            # 速度オプション: 文字列で定義してfloatに変換（format_funcのfloat比較エラーを回避）
            _speed_strs  = ["0.5x", "0.75x", "1.0x（標準）", "1.25x", "1.5x", "1.75x", "2.0x"]
            _speed_vals  = [0.5,    0.75,     1.0,            1.25,    1.5,    1.75,    2.0   ]
            _speed_map   = dict(zip(_speed_strs, _speed_vals))

            voice_keys = list(VOICE_OPTIONS.keys())
            step3_voice = st.selectbox(
                "ナレーター音声",
                voice_keys,
                index=voice_keys.index(st.session_state["selected_voice_name"]),
                key="voice_step3"
            )
            st.session_state["selected_voice_name"] = step3_voice
            voice = VOICE_OPTIONS[step3_voice]

            narr_speed_str = st.select_slider("読み上げ速度", options=_speed_strs, value="1.0x（標準）")
            narr_speed = _speed_map[narr_speed_str]
            tts_rate = f"{int((narr_speed - 1) * 100):+d}%"

            # --- プレビュー ---
            if st.button("ナレーション音声をプレビュー再生"):
                with st.spinner("音声を生成中..."):
                    try:
                        prev_bytes = text_to_speech_bytes(edited_text, voice, tts_rate)
                        st.session_state["preview_audio"] = prev_bytes
                    except Exception as e:
                        st.error(f"プレビューエラー: {e}")
            if "preview_audio" in st.session_state:
                st.audio(st.session_state["preview_audio"], format="audio/mpeg")

            show_subtitle = st.checkbox("字幕を表示する（テキストを動画に焼き込む）")
            if show_subtitle:
                font_options = get_font_options()
                sub_font_name = st.selectbox("字幕フォント", list(font_options.keys()))
                sub_font = font_options[sub_font_name]
                sub_font_size = st.slider("文字サイズ", 16, 64, 28, key="font_sz")
                sub_speed_str = st.select_slider("字幕流し込み速度", options=_speed_strs, value="1.0x（標準）")
                sub_speed = _speed_map[sub_speed_str]
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
                sub_font_size = 28
                sub_speed = 1.0
                text_color, text_opacity = "#FFFFFF", 100
                bg_color, bg_opacity = "#000000", 20

            if st.button("この内容で動画を生成する", type="primary"):
                with tempfile.TemporaryDirectory() as out_dir:
                    orig_video = os.path.join(out_dir, "input.mp4")
                    with open(orig_video, "wb") as f:
                        f.write(st.session_state["video_bytes"])

                    audio_path = os.path.join(out_dir, "narration.wav")
                    with st.spinner("Edge TTSで音声を合成中..."):
                        try:
                            audio_data = text_to_speech_bytes(edited_text, voice, tts_rate)
                        except Exception as e:
                            st.error(f"Edge TTS エラー: {e}")
                            st.stop()

                        if not audio_data:
                            st.error("Edge TTSが音声データを生成しませんでした")
                            st.stop()

                        # バイト列を直接 FFmpeg の stdin にパイプして WAV に変換
                        conv = subprocess.run(
                            [FFMPEG, "-y", "-f", "mp3", "-i", "pipe:0",
                             "-ar", "44100", "-ac", "1", audio_path],
                            input=audio_data, capture_output=True
                        )
                        if conv.returncode != 0 or not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
                            # mp3 ヒントが失敗した場合はフォーマット自動検出で再試行
                            conv = subprocess.run(
                                [FFMPEG, "-y", "-i", "pipe:0",
                                 "-ar", "44100", "-ac", "1", audio_path],
                                input=audio_data, capture_output=True
                            )
                        if conv.returncode != 0 or not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
                            hdr_hex = audio_data[:16].hex() if audio_data else "empty"
                            st.error(
                                f"音声変換エラー（header: {hdr_hex}）:\n"
                                f"{conv.stderr.decode(errors='replace')[-400:]}"
                            )
                            st.stop()

                    # 動画の長さに合わせて音声を伸縮
                    with st.spinner("動画の長さに合わせてナレーションを調整中..."):
                        try:
                            video_dur = get_video_duration(orig_video)
                            audio_dur = get_audio_duration(audio_path)
                            ratio = video_dur / audio_dur if audio_dur > 0 else 1.0
                            if abs(ratio - 1.0) > 0.01:
                                atempo_f = build_atempo_filter(ratio)
                                stretched_path = os.path.join(out_dir, "narration_stretched.wav")
                                stretch_result = subprocess.run(
                                    [FFMPEG, "-y", "-i", audio_path,
                                     "-af", atempo_f, stretched_path],
                                    capture_output=True, text=True
                                )
                                if stretch_result.returncode == 0 and os.path.getsize(stretched_path) > 0:
                                    audio_path = stretched_path
                                else:
                                    st.warning("音声の長さ調整に失敗しました。元の長さで続行します。")
                                    video_dur = 0.0  # fallback: use -shortest
                        except Exception as e:
                            st.warning(f"長さ取得エラー（元の速度で続行）: {e}")
                            video_dur = 0.0

                    output_path = os.path.join(out_dir, "output.mp4")
                    with st.spinner("FFmpegで動画を合成中..."):
                        try:
                            subtitle = edited_text if show_subtitle else ""
                            merge_audio_video(orig_video, audio_path, output_path,
                                              subtitle, sub_font,
                                              text_color, text_opacity,
                                              bg_color, bg_opacity, sub_font_size, sub_speed,
                                              total_duration=video_dur)
                        except Exception as e:
                            st.error(f"動画生成エラー: {e}")
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
