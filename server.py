# ============================================================
# VoxCPM2 OpenAI-Compatible TTS API Server
# ============================================================
# 将 VoxCPM2 声音克隆能力封装为 OpenAI /v1/audio/speech 接口
# 启动: python server.py
# 调用: curl http://localhost:7900/v1/audio/speech -H "Content-Type: application/json" -d '{"model":"tts-1","input":"你好世界","voice":"dalao"}'
# ============================================================

import os
import asyncio
import logging
import shutil
import subprocess
import tempfile
import threading
import time
import sys
from pathlib import Path
from typing import Optional

import yaml
import uvicorn
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel, Field

from voxcpm_client import VoxCPM2Client

# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.resolve()


CONFIG_PATH = _app_dir() / "config.yaml"

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

config = load_config()
_config_mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0
_config_lock = threading.Lock()
BASE_DIR = CONFIG_PATH.parent
MAX_CONCURRENT_GENERATIONS = int(
    os.environ.get(
        "MAX_CONCURRENT_GENERATIONS",
        config.get("server", {}).get("max_concurrent_generations", 10),
    )
)
GENERATION_RETRY_ATTEMPTS = int(
    os.environ.get(
        "GENERATION_RETRY_ATTEMPTS",
        config.get("server", {}).get("generation_retry_attempts", 3),
    )
)
GENERATION_RETRY_INITIAL_DELAY = float(
    os.environ.get(
        "GENERATION_RETRY_INITIAL_DELAY",
        config.get("server", {}).get("generation_retry_initial_delay", 2.0),
    )
)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
OPENAI_PLACEHOLDER_VOICES = {
    "alloy", "ash", "ballad", "coral", "echo", "fable",
    "nova", "onyx", "sage", "shimmer", "verse",
}
VOICE_AUDIO_EXTS = (".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg")


def _reload_config_if_changed():
    """Reload config.yaml when WebUI adds or removes voices."""
    global config, _config_mtime
    try:
        current_mtime = CONFIG_PATH.stat().st_mtime
    except FileNotFoundError:
        return config

    if current_mtime == _config_mtime:
        return config

    with _config_lock:
        current_mtime = CONFIG_PATH.stat().st_mtime
        if current_mtime == _config_mtime:
            return config
        config = load_config()
        _config_mtime = current_mtime
        logger.info("配置已重新加载: voices=%s", list(config.get("voices", {}).keys()))
        return config

def _case_insensitive_existing_path(path: Path) -> Path:
    """Return an existing path, allowing case-insensitive filename matches."""
    if path.exists():
        return path
    parent = path.parent
    if not parent.exists():
        return path
    target = path.name.lower()
    for child in parent.iterdir():
        if child.name.lower() == target:
            return child
    return path


def _resolve_ref(ref_audio: str) -> Path:
    """Resolve ref_audio: relative paths, Windows abs paths, and direct filenames."""
    ref_audio = str(ref_audio or "").strip().strip('"').strip("'")
    p = Path(ref_audio)
    # Detect Windows abs path on Linux via drive letter pattern
    if os.name != 'nt' and len(ref_audio) > 2 and ref_audio[1] == ':' and ref_audio[0].isalpha():
        norm = ref_audio.replace(chr(92), chr(47))
        parts = norm.split(chr(47))
        for i, part in enumerate(parts):
            if part == 'voices':
                rel = Path(*parts[i:])
                return _case_insensitive_existing_path(BASE_DIR / rel)
        p = Path(parts[-1])
    if not p.is_absolute():
        p = BASE_DIR / p
    return _case_insensitive_existing_path(p)


def _voice_key_from_path(value: str) -> str:
    """Extract a voice name from values like dalao, dalao.MP3, or C:\\...\\voices\\dalao.MP3."""
    raw = str(value or "").strip().strip('"').strip("'")
    raw = raw.replace(chr(92), chr(47)).rstrip(chr(47))
    name = raw.rsplit(chr(47), 1)[-1]
    return Path(name).stem if Path(name).suffix else name


def _voice_config_from_audio_path(ref_audio_path: Path, current_config: dict):
    defaults = current_config.get("clone_defaults", current_config.get("defaults", {}))
    try:
        ref_audio = str(ref_audio_path.relative_to(BASE_DIR))
    except ValueError:
        ref_audio = str(ref_audio_path)
    return {
        "ref_audio": ref_audio,
        "cfg_value": defaults.get("cfg_value", 2.5),
        "dit_steps": defaults.get("dit_steps", 50),
        "do_normalize": defaults.get("do_normalize", True),
        "denoise": defaults.get("denoise", True),
        "control_instruction": defaults.get("control_instruction", ""),
        "use_prompt_text": False,
        "prompt_text": "",
        "type": "clone",
    }


def _find_voice_audio_by_name(voice_name: str) -> Path | None:
    """Find a saved voice in voices/ by stem, so WebUI-created files work without API config sync."""
    stem = _voice_key_from_path(voice_name)
    if not stem:
        return None

    voices_dir = BASE_DIR / "voices"
    if not voices_dir.exists():
        return None

    lowered = stem.lower()
    for ext in VOICE_AUDIO_EXTS:
        candidate = _case_insensitive_existing_path(voices_dir / f"{stem}{ext}")
        if candidate.exists() and candidate.is_file():
            return candidate

    for child in voices_dir.iterdir():
        if child.is_file() and child.suffix.lower() in VOICE_AUDIO_EXTS and child.stem.lower() == lowered:
            return child
    return None


def _available_voice_names(current_config: dict | None = None) -> list[str]:
    current_config = current_config or _reload_config_if_changed()
    names = list((current_config.get("voices") or {}).keys())
    seen = {name.lower() for name in names}

    voices_dir = BASE_DIR / "voices"
    if voices_dir.exists():
        for child in sorted(voices_dir.iterdir(), key=lambda p: p.name.lower()):
            if child.is_file() and child.suffix.lower() in VOICE_AUDIO_EXTS:
                lowered = child.stem.lower()
                if lowered not in seen:
                    names.append(child.stem)
                    seen.add(lowered)
    return names


def _get_voice_config(voice: str):
    """Accept configured voice names and path-like voice values from OpenAI-compatible clients."""
    current_config = _reload_config_if_changed()
    voices = current_config.get("voices", {})
    if voice in voices:
        return voice, voices[voice]

    voice_key = _voice_key_from_path(voice)
    if voice_key in voices:
        return voice_key, voices[voice_key]

    lowered = voice_key.lower()
    for name, voice_config in voices.items():
        if name.lower() == lowered:
            return name, voice_config

    voice_audio_path = _find_voice_audio_by_name(voice_key)
    if voice_audio_path is not None:
        return voice_audio_path.stem, _voice_config_from_audio_path(voice_audio_path, current_config)

    ref_audio_path = _resolve_ref(voice)
    if ref_audio_path.exists() and ref_audio_path.is_file():
        return ref_audio_path.stem, _voice_config_from_audio_path(ref_audio_path, current_config)

    if lowered in OPENAI_PLACEHOLDER_VOICES and voices:
        name = next(iter(voices))
        return name, voices[name]

    return None, None
# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("voxcpm-tts")


def _is_retryable_generation_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in RETRYABLE_STATUS_CODES:
        return True

    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True

    message = str(exc)
    return any(f"{code} " in message or f"{code} Server Error" in message for code in RETRYABLE_STATUS_CODES)


def generate_speech_audio(
    *,
    text: str,
    ref_audio_path: Path,
    control_instruction: str,
    use_prompt_text: bool,
    prompt_text: str,
    cfg_value: float,
    do_normalize: bool,
    denoise: bool,
    dit_steps: int,
    timeout: float,
    response_format: str,
) -> bytes:
    last_error = None
    for attempt in range(1, GENERATION_RETRY_ATTEMPTS + 1):
        try:
            client = get_client()
            audio_wav = client.generate(
                text=text,
                ref_audio_path=str(ref_audio_path),
                control_instruction=control_instruction,
                use_prompt_text=use_prompt_text,
                prompt_text=prompt_text,
                cfg_value=cfg_value,
                do_normalize=do_normalize,
                denoise=denoise,
                dit_steps=dit_steps,
                timeout=timeout,
            )
            break
        except Exception as exc:
            last_error = exc
            if attempt >= GENERATION_RETRY_ATTEMPTS or not _is_retryable_generation_error(exc):
                raise

            delay = GENERATION_RETRY_INITIAL_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "语音生成失败，将重试: attempt=%d/%d delay=%.1fs error=%s",
                attempt,
                GENERATION_RETRY_ATTEMPTS,
                delay,
                exc,
            )
            time.sleep(delay)
    else:
        raise last_error

    output_format = response_format if response_format in ("mp3", "opus", "aac", "flac", "wav", "pcm") else "wav"
    return convert_audio(audio_wav, output_format)

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
app = FastAPI(
    title="VoxCPM2 TTS API",
    description="OpenAI-compatible TTS API powered by VoxCPM2 voice cloning",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 客户端实例（线程内懒加载）
# ---------------------------------------------------------------------------
_client_local = threading.local()
_generation_semaphore = asyncio.Semaphore(MAX_CONCURRENT_GENERATIONS)

def get_client() -> VoxCPM2Client:
    client = getattr(_client_local, "client", None)
    if client is None:
        client = VoxCPM2Client(base_url=config["voxcpm"]["base_url"])
        _client_local.client = client
    return client

# ---------------------------------------------------------------------------
# 音频格式转换 WAV -> MP3（可选，需要 ffmpeg）
# ---------------------------------------------------------------------------
_ffmpeg_path = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")

def convert_audio(audio_bytes: bytes, output_format: str) -> bytes:
    """将 WAV 音频转换为目标格式（需要 ffmpeg）"""
    if output_format in ("wav", "pcm"):
        return audio_bytes

    if not _ffmpeg_path:
        logger.warning(f"ffmpeg 未安装，无法转换为 {output_format}，返回 WAV 格式")
        return audio_bytes

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as infile:
        infile.write(audio_bytes)
        in_path = infile.name

    out_path = in_path + f".{output_format}"
    try:
        codecs = {
            "mp3": ["-codec:a", "libmp3lame", "-b:a", "192k"],
            "opus": ["-codec:a", "libopus", "-b:a", "128k"],
            "aac": ["-codec:a", "aac", "-b:a", "192k"],
            "flac": ["-codec:a", "flac"],
        }
        codec_args = codecs.get(output_format, ["-codec:a", "libmp3lame", "-b:a", "192k"])

        subprocess.run(
            [_ffmpeg_path, "-y", "-i", in_path, *codec_args, out_path],
            capture_output=True,
            timeout=60,
            check=True,
        )
        return Path(out_path).read_bytes()
    except subprocess.CalledProcessError as e:
        logger.error(f"音频转换失败: {e.stderr.decode() if e.stderr else e}")
        return audio_bytes
    finally:
        Path(in_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# 模型定义
# ---------------------------------------------------------------------------

class SpeechRequest(BaseModel):
    """OpenAI TTS 请求格式"""
    model: str = Field(default="tts-1", description="模型名称（保留兼容性，实际忽略）")
    input: str = Field(..., description="要合成的文本", min_length=1, max_length=4096)
    voice: str = Field(default="alloy", description="声音名称，对应 config.yaml 中配置的 voice")
    response_format: str = Field(
        default="wav",
        description="音频格式: mp3, opus, aac, flac, wav, pcm",
    )
    speed: float = Field(default=1.0, ge=0.25, le=4.0, description="语速（VoxCPM2 不直接支持，通过 control_instruction 可调节）")

    # 扩展参数（非 OpenAI 标准，但可用于精细控制）
    cfg_value: Optional[float] = Field(default=None, ge=1.0, le=3.0)
    dit_steps: Optional[int] = Field(default=None, ge=1, le=50)
    control_instruction: Optional[str] = Field(default=None)
    use_prompt_text: Optional[bool] = Field(default=None)
    prompt_text: Optional[str] = Field(default=None)


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str


class ModelsResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo]


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    current_config = _reload_config_if_changed()
    return {
        "service": "VoxCPM2 OpenAI-compatible TTS API",
        "endpoints": ["/v1/audio/speech", "/v1/models"],
        "voices": _available_voice_names(current_config),
    }

@app.get("/v1/models")
async def list_models():
    """列出可用模型"""
    return ModelsResponse(
        data=[
            ModelInfo(
                id="tts-1",
                created=1700000000,
                owned_by="voxcpm2",
            )
        ]
    )

@app.post("/v1/audio/speech")
async def create_speech(req: SpeechRequest, request: Request):
    """
    OpenAI 兼容的 TTS 端点。
    使用 VoxCPM2 在线服务进行声音克隆和语音合成。
    """
    voice_name, voice_config = _get_voice_config(req.voice)
    if voice_config is None:
        available = _available_voice_names()
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"未知的声音: '{req.voice}'。可用声音: {available}",
                    "type": "invalid_request_error",
                    "param": "voice",
                }
            },
        )

    ref_audio_path = _resolve_ref(voice_config["ref_audio"])
    if not ref_audio_path.exists():
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "message": (
                        f"参考音频文件不存在: {ref_audio_path}。"
                        f"请求 voice={req.voice!r} 匹配到配置 voice={voice_name!r}，"
                        "请确认 Docker 容器内 /app/voices 已挂载该文件。"
                    ),
                    "type": "server_error",
                }
            },
        )

    # 合并参数：request 参数 > voice 配置 > 全局默认
    defaults = config.get("clone_defaults", config.get("defaults", {}))
    cfg = req.cfg_value if req.cfg_value is not None else voice_config.get("cfg_value", defaults["cfg_value"])
    steps = req.dit_steps if req.dit_steps is not None else voice_config.get("dit_steps", defaults["dit_steps"])
    normalize = voice_config.get("do_normalize", defaults["do_normalize"])
    denoise_flag = voice_config.get("denoise", defaults["denoise"])
    control = req.control_instruction if req.control_instruction is not None else voice_config.get("control_instruction", defaults["control_instruction"])
    timeout = defaults.get("timeout", 300)
    configured_prompt_text = voice_config.get("prompt_text", "")
    use_prompt_text = (
        req.use_prompt_text
        if req.use_prompt_text is not None
        else bool(voice_config.get("use_prompt_text", False))
    )
    prompt_text = req.prompt_text if req.prompt_text is not None else configured_prompt_text
    if not use_prompt_text:
        prompt_text = ""

    # 如果用户设置了 speed 参数，通过 control_instruction 传递给模型
    if req.speed != 1.0 and control == "":
        if req.speed > 1.0:
            control = f"speak faster, speed {req.speed}x"
        else:
            control = f"speak slower, speed {req.speed}x"

    logger.info(
        f"TTS 请求: voice={req.voice} resolved_voice={voice_name} input_len={len(req.input)} "
        f"cfg={cfg} steps={steps} format={req.response_format}"
    )

    output_format = req.response_format if req.response_format in ("mp3", "opus", "aac", "flac", "wav", "pcm") else "wav"
    start_time = time.time()
    try:
        async with _generation_semaphore:
            audio_bytes = await asyncio.to_thread(
                generate_speech_audio,
                text=req.input,
                ref_audio_path=ref_audio_path,
                control_instruction=control,
                use_prompt_text=use_prompt_text,
                prompt_text=prompt_text,
                cfg_value=cfg,
                do_normalize=normalize,
                denoise=denoise_flag,
                dit_steps=steps,
                timeout=timeout,
                response_format=output_format,
            )
    except Exception as e:
        logger.error(f"语音生成失败: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "message": f"语音生成失败: {str(e)}",
                    "type": "server_error",
                }
            },
        )

    elapsed = time.time() - start_time
    logger.info(f"生成完成: {len(audio_bytes) / 1024:.1f} KB, 耗时 {elapsed:.1f}s")

    # 设置合适的 Content-Type
    content_types = {
        "mp3": "audio/mpeg",
        "opus": "audio/opus",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "pcm": "audio/l16",
    }

    return Response(
        content=audio_bytes,
        media_type=content_types.get(output_format, "audio/wav"),
        headers={
            "Content-Disposition": f"inline; filename=speech.{output_format}",
        },
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """统一返回 OpenAI 风格的错误格式"""
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.detail if isinstance(exc.detail, dict) else {"error": {"message": str(exc.detail), "type": "server_error"}},
    )

# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    server_cfg = config["server"]
    logger.info(f"启动 VoxCPM2 TTS API 服务器: http://{server_cfg['host']}:{server_cfg['port']}")
    logger.info(f"配置文件: {CONFIG_PATH}")
    logger.info(f"可用声音: {list(config.get('voices', {}).keys())}")
    logger.info(f"VoxCPM2 服务: {config['voxcpm']['base_url']}")

    uvicorn.run(
        app,
        host=os.environ.get("API_HOST", server_cfg["host"]),
        port=int(os.environ.get("API_PORT", server_cfg["port"])),
        log_level="info",
    )
