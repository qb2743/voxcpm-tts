# ============================================================
# VoxCPM2 客户端工具函数
# ============================================================
# split_long_text  — 长文本切分（中英文标点、换行、破折号）
# concat_wav_files — WAV 音频拼接（纯 Python，无需 ffmpeg）
# ============================================================

import io
import re
import wave


# ---------------------------------------------------------------------------
# 长文本切分
# ---------------------------------------------------------------------------

def split_long_text(text: str, config: dict) -> list[str]:
    """
    将长文本按标点符号切分为句子列表。

    切分规则：
    1. 中英文标点：。！？；. ! ? ;
    2. 破折号 —、省略号 …、波浪号 ~
    3. 换行（无标点的独立行）视为句子边界
    4. 过短的片段合并到上一句（< min_chars）
    5. 过长的片段按逗号进一步切分（> max_chars）
    """
    split_cfg = config.get("text_split", {})
    delimiters = split_cfg.get("delimiters", [
        "。", "！", "？", "；",          # 中文标点
        ". ", "! ", "? ", "; ",          # 英文标点（带空格）
        "—", "…", "~",                   # 破折号/省略号/波浪号
    ])
    min_chars = split_cfg.get("min_segment_chars", 2)
    max_chars = split_cfg.get("max_segment_chars", 500)

    # ── 步骤 1: 处理换行 ──
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 将前面无标点的独立换行替换为特殊标记
    text = re.sub(r"([^。！？；.!?;—…~\n])\n", r"\1__NL__", text)
    # 压缩多个换行为空
    text = text.replace("\n", "")

    # ── 步骤 2: 按所有分隔符切分 ──
    punct_delimiters = [d for d in delimiters]
    escaped = [re.escape(d) for d in punct_delimiters]
    escaped.append("__NL__")  # 换行标记
    pattern = "(" + "|".join(escaped) + ")"
    parts = re.split(pattern, text)

    # ── 步骤 3: 重组片段（分隔符跟在前一段后面）──
    segments = []
    buffer = ""
    for part in parts:
        if not part:
            continue
        if re.fullmatch(pattern, part):
            if part == "__NL__":
                buffer = buffer.rstrip()
                if buffer:
                    segments.append(buffer.strip())
                    buffer = ""
            else:
                buffer += part
            continue
        if buffer:
            segments.append(buffer.strip())
            buffer = ""
        if part.strip():
            segments.append(part.strip())
    if buffer.strip():
        segments.append(buffer.strip())

    # ── 步骤 4: 合并过短的片段 ──
    merged = []
    for seg in segments:
        if merged and len(seg.strip()) <= min_chars:
            merged[-1] = merged[-1] + seg
        else:
            merged.append(seg)

    # ── 步骤 5: 对过长片段按逗号再切分 ──
    final = []
    for seg in merged:
        if len(seg) > max_chars:
            sub_parts = re.split(r"([，,])", seg)
            sub_buffer = ""
            for sub in sub_parts:
                if sub in (None, ""):
                    continue
                if sub in ("，", ","):
                    sub_buffer += sub
                    if len(sub_buffer) >= max_chars:
                        final.append(sub_buffer.strip())
                        sub_buffer = ""
                    continue
                sub_buffer += sub
            if sub_buffer.strip():
                final.append(sub_buffer.strip())
        else:
            final.append(seg)

    # ── 步骤 6: 最终合并过短片段 ──
    merged2 = []
    for seg in final:
        if merged2 and len(seg.strip()) <= min_chars:
            merged2[-1] = merged2[-1] + seg
        else:
            merged2.append(seg)

    return [s for s in merged2 if s.strip()]


# ---------------------------------------------------------------------------
# 音频拼接（纯 Python，零外部依赖）
# ---------------------------------------------------------------------------

def generate_silence(duration_sec: float, sample_rate: int = 24000,
                     channels: int = 1, sample_width: int = 2) -> bytes:
    """生成指定时长的静音 PCM 数据"""
    n_samples = int(duration_sec * sample_rate)
    return b"\x00" * (n_samples * channels * sample_width)


def concat_wav_files(wav_data_list: list[bytes], pause_sec: float = 0.2) -> bytes:
    """
    将多个 WAV 音频拼接为一个，片段间插入停顿。

    假设所有 WAV 文件有相同的采样率、声道数和位深。
    """
    if not wav_data_list:
        return b""
    if len(wav_data_list) == 1:
        return wav_data_list[0]

    # 读取第一个 WAV 获取参数
    with wave.open(io.BytesIO(wav_data_list[0]), "rb") as wf:
        params = wf.getparams()
        sample_rate = params.framerate
        channels = params.nchannels
        sample_width = params.sampwidth
        audio_frames = [wf.readframes(wf.getnframes())]

    # 读取其余 WAV
    for data in wav_data_list[1:]:
        with wave.open(io.BytesIO(data), "rb") as wf:
            audio_frames.append(wf.readframes(wf.getnframes()))

    # 生成静音
    silence = generate_silence(pause_sec, sample_rate, channels, sample_width)

    # 拼接：在每个片段间插入静音
    combined = audio_frames[0]
    for frame in audio_frames[1:]:
        combined += silence + frame

    # 写入输出
    out_buf = io.BytesIO()
    with wave.open(out_buf, "wb") as wf:
        wf.setparams(params)
        wf.writeframes(combined)

    return out_buf.getvalue()
