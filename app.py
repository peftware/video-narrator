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

groq_api_key = st.secrets.get("GROQ_API_KEY") if hasattr(st, "secrets") else None
groq_api_key = groq_api_key or os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=groq_api_key)

st.set_page_config(page_title="動画ナレーター自動生成", layout="wide")
st.title("動画ナレーター自動生成アプリ")

# --- サイドバー ---
with st.sidebar:
    st.header("設定")
    st.write("APIキー状態:")
    st.write("Groq:", "✅" if groq_api_key else "❌")
    st.write("Edge TTS:", "✅（キー不要）")

    st.divider()
    st.subheader("解析設定")
    frame_count = st.slider("抽出フレーム数", min_value=3, max_value=5, value=5,
                            help="Groq Vision APIの仕様上、最大5枚")

    st.divider()
    st.subheader("音声設定")
    voice_name = st.selectbox("ナレーター音声", list(VOICE_OPTIONS.keys()))
    voice = VOICE_OPTIONS[voice_name]


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


def merge_audio_video(video_path: str, audio_path: str, output_path: str):
    result = subprocess.run([
        FFMPEG,
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        output_path, "-y"
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpegエラー:\n{result.stderr}")


# --- メインUI ---

st.header("Step 1: 動画をアップロード")

uploaded_file = st.file_uploader(
    "MP4ファイルを選択してください",
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
            selected_style = st.radio("スタイルを選択", list(narrations.keys()), horizontal=True)
            edited_text = st.text_area(
                "ナレーション（自由に編集できます）",
                narrations.get(selected_style, ""),
                height=200,
                key=f"edit_{selected_style}"
            )

            st.divider()
            st.header("Step 3: 音声合成＆動画書き出し")
            st.write(f"音声：{voice_name}")

            if st.button("この内容で動画を生成する", type="primary"):
                with tempfile.TemporaryDirectory() as out_dir:
                    orig_video = os.path.join(out_dir, "input.mp4")
                    with open(orig_video, "wb") as f:
                        f.write(st.session_state["video_bytes"])

                    audio_path = os.path.join(out_dir, "narration.mp3")
                    with st.spinner("Edge TTSで音声を合成中..."):
                        text_to_speech(edited_text, voice, audio_path)

                    output_path = os.path.join(out_dir, "output.mp4")
                    with st.spinner("FFmpegで動画を合成中..."):
                        try:
                            merge_audio_video(orig_video, audio_path, output_path)
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
