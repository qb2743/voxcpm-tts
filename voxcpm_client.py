# VoxCPM2 Online Client
# 通过 HTTP 直接调用 VoxCPM2 Gradio API 进行语音克隆/合成
import json
import uuid
import requests
import mimetypes
from pathlib import Path
from typing import Optional

BASE_URL = "https://voxcpm.modelbest.cn"
API_PREFIX = "/gradio_api"

class VoxCPM2Client:
    """VoxCPM2 语音合成客户端，通过 Gradio HTTP API 交互"""

    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}{API_PREFIX}"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })

    def _upload_file(self, file_path: str) -> dict:
        """上传文件到 Gradio 服务器，返回 FileData 对象"""
        abs_path = Path(file_path).resolve()
        if not abs_path.exists():
            raise FileNotFoundError(f"参考音频文件不存在: {abs_path}")

        file_size = abs_path.stat().st_size
        mime_type = mimetypes.guess_type(str(abs_path))[0] or "audio/wav"

        upload_url = f"{self.api_url}/upload"
        with open(abs_path, "rb") as f:
            files = {"files": (abs_path.name, f, mime_type)}
            resp = self.session.post(upload_url, files=files, timeout=60)
            resp.raise_for_status()

        # Gradio 返回上传后的文件路径列表
        result = resp.json()
        server_path = result[0] if isinstance(result, list) else result

        return {
            "path": server_path,
            "orig_name": abs_path.name,
            "mime_type": mime_type,
            "size": file_size,
            "meta": {"_type": "gradio.FileData"},
        }

    def _wait_for_result(self, event_id: str, timeout: float = 120.0) -> list:
        """通过 SSE 监听获取生成结果"""
        sse_url = f"{self.api_url}/call/generate/{event_id}"
        resp = self.session.get(sse_url, stream=True, timeout=timeout)
        resp.raise_for_status()

        last_data = None
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            # SSE 格式: "event: ..." 或 "data: ..."
            if line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                    last_data = data
                except json.JSONDecodeError:
                    continue
            elif line.startswith("event: "):
                event_type = line[7:]
                if event_type == "error":
                    raise RuntimeError(f"服务器返回错误: {line}")

        if last_data is None:
            raise RuntimeError("未收到生成结果")

        return last_data

    def generate(
        self,
        text: str,
        ref_audio_path: Optional[str] = None,
        *,
        control_instruction: str = "",
        use_prompt_text: bool = False,
        prompt_text: str = "",
        cfg_value: float = 2.5,
        do_normalize: bool = True,
        denoise: bool = True,
        dit_steps: int = 50,
        user_id: Optional[str] = None,
        timeout: float = 120.0,
    ) -> bytes:
        """
        调用 VoxCPM2 生成语音。

        参数:
            text: 要合成的目标文本
            ref_audio_path: 参考音频文件路径（用于声音克隆）；声音设计可为空
            control_instruction: 控制指令（情感/风格描述）
            use_prompt_text: 是否启用提示文本模式
            prompt_text: 参考音频对应的文本（用于更高精度克隆）
            cfg_value: CFG引导强度 (1.0-3.0)，越高越忠于参考音色 → 极致克隆
            do_normalize: 是否对文本进行规范化
            denoise: 是否对参考音频降噪
            dit_steps: DiT采样步数 (1-50)，越高音质越好但越慢 → 极致克隆
            user_id: 用户标识（可选，随机生成）
            timeout: 超时秒数

        返回:
            生成的 WAV 音频字节数据
        """
        if user_id is None:
            user_id = f"api_{uuid.uuid4().hex[:12]}"

        # 声音设计模式不需要参考音频；同时关闭参考音频相关处理，贴近官网纯设计调用。
        ref_file_data = self._upload_file(ref_audio_path) if ref_audio_path else None
        if ref_file_data is None:
            use_prompt_text = False
            prompt_text = ""
            denoise = False

        # 构造请求参数（与 Gradio 前端 event 绑定顺序一致）
        # event id=2: inputs=[12(text),11(control),8(ref_wav),9(use_prompt),10(prompt_text),16(cfg),15(normalize),14(denoise),17(dit_steps),3(user_id)]
        payload = {
            "data": [
                text,
                control_instruction,
                ref_file_data,
                use_prompt_text,
                prompt_text,
                cfg_value,
                do_normalize,
                denoise,
                dit_steps,
                user_id,
            ]
        }

        # 发送生成请求
        call_url = f"{self.api_url}/call/generate"
        resp = self.session.post(call_url, json=payload, timeout=30)
        resp.raise_for_status()

        result = resp.json()
        event_id = result.get("event_id")
        if not event_id:
            raise RuntimeError(f"未能获取 event_id: {result}")

        # 等待生成完成
        generation_result = self._wait_for_result(event_id, timeout=timeout)

        # 解析结果：返回的是 [audio_file_data, state_data]
        if isinstance(generation_result, list) and len(generation_result) > 0:
            audio_data = generation_result[0]
        elif isinstance(generation_result, dict):
            audio_data = generation_result
        else:
            raise RuntimeError(f"未知的结果格式: {type(generation_result)}")

        # 从 FileData 中获取音频文件 URL
        audio_url = None
        if isinstance(audio_data, dict):
            audio_url = audio_data.get("url") or audio_data.get("path")
            # 如果只有 path 没有完整 url，补全
            if audio_url and not audio_url.startswith("http"):
                audio_url = f"{self.base_url}{audio_url}"

        if not audio_url:
            # 如果结果直接是文件路径字符串
            if isinstance(audio_data, str) and audio_data:
                audio_url = f"{self.base_url}/gradio_api/file={audio_data}" if not audio_data.startswith("http") else audio_data
            else:
                raise RuntimeError(f"无法解析音频 URL: {audio_data}")

        return self._download_audio(audio_url)

    def _download_audio(self, audio_url: str) -> bytes:
        """下载生成的音频文件"""
        resp = self.session.get(audio_url, timeout=60)
        resp.raise_for_status()
        return resp.content

    def _extract_asr_text(self, data) -> str:
        """Extract transcript text from Gradio ASR return variants."""
        if data is None:
            return ""
        if isinstance(data, str):
            return data.strip()
        if isinstance(data, dict):
            for key in ("text", "value", "transcript", "result"):
                text = self._extract_asr_text(data.get(key))
                if text:
                    return text
            return ""
        if isinstance(data, list):
            for item in data:
                text = self._extract_asr_text(item)
                if text:
                    return text
        return ""

    def transcribe(self, audio_path: str, timeout: float = 60.0) -> str:
        """调用 VoxCPM2 ASR（自动语音识别）功能，返回音频对应的文本。"""
        abs_path = Path(audio_path).resolve()
        if not abs_path.exists():
            raise FileNotFoundError(f"音频文件不存在: {abs_path}")

        # 上传音频文件，使用完整 Gradio FileData，和生成接口保持一致。
        file_data = self._upload_file(str(abs_path))

        # 调用官网极致克隆模式使用的公开 ASR 端点。
        # inputs=[checked, audio_path]，checked=True 表示需要自动识别参考音频文本。
        call_url = f"{self.api_url}/call/_run_asr_if_needed"
        payload = {"data": [True, file_data]}
        resp = self.session.post(call_url, json=payload, timeout=30)
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = resp.text[:300].replace("\n", " ")
            raise RuntimeError(f"ASR 请求失败: HTTP {resp.status_code}, {body}") from exc
        result = resp.json()
        event_id = result.get("event_id")
        if not event_id:
            raise RuntimeError(f"ASR 未能获取 event_id: {result}")

        # 等待识别结果
        sse_url = f"{self.api_url}/call/_run_asr_if_needed/{event_id}"
        sse_resp = self.session.get(sse_url, stream=True, timeout=timeout)
        sse_resp.raise_for_status()

        last_data = None
        for line in sse_resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data: "):
                try:
                    last_data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
            elif line.startswith("event: error"):
                raise RuntimeError(f"ASR 返回错误")

        if last_data is None:
            raise RuntimeError("ASR 未返回结果")

        text = self._extract_asr_text(last_data)
        if not text:
            preview = json.dumps(last_data, ensure_ascii=False)[:500]
            raise RuntimeError(f"ASR 返回空文本: {preview}")
        return text


if __name__ == "__main__":
    import sys

    client = VoxCPM2Client()

    if len(sys.argv) < 3:
        print("用法: python voxcpm_client.py <参考音频.wav> <合成文本>")
        print("示例: python voxcpm_client.py voices/my_voice.wav \"你好，这是测试语音\"")
        sys.exit(1)

    ref_audio = sys.argv[1]
    text = sys.argv[2]

    print(f"参考音频: {ref_audio}")
    print(f"合成文本: {text}")
    print("正在生成...")

    audio = client.generate(
        text=text,
        ref_audio_path=ref_audio,
        cfg_value=2.5,
        do_normalize=True,
        denoise=True,
        dit_steps=50,
    )

    output_path = Path("output.wav")
    output_path.write_bytes(audio)
    print(f"已保存到: {output_path.resolve()}")
    print(f"文件大小: {len(audio) / 1024:.1f} KB")
