# VoxCPM2 Voice Studio v1.4

将 [VoxCPM2](https://voxcpm.modelbest.cn/) 在线语音合成服务封装为：
- 兼容 OpenAI 的本地 TTS API（`server.py`）
- Web 图形界面（`webui.py`）：常规配音、声音设计、音色管理、长文本合成

## 功能概览

### WebUI

- **常规配音**：选择已有音色，输入合成文本后生成语音。
- **声音设计**：不需要参考音频，只填写声音描述和试听文本，试听满意后保存为音色。
- **音色管理**：集中查看和删除已保存音色。
- **极致克隆模式**：常规配音中可开启参考音频文本，用于更严格地贴合参考音色。
- **长文本合成**：自动切分文本，多段生成后拼接。

### OpenAI-Compatible API

`server.py` 提供 `/v1/audio/speech`，可被 OpenAI SDK 或兼容客户端调用。

## 一键启动

| 脚本 | 功能 | 说明 |
|------|------|------|
| `start.bat` | 启动 API 服务 | 启动 `http://localhost:7900` |
| `webui.bat` | 启动 Web 界面 | 启动 `http://127.0.0.1:5000`，局域网访问用本机 IP |
| `build_exe.bat` | 打包为 EXE | 使用 PyInstaller 打包 WebUI |

WebUI 默认监听 `0.0.0.0:5000`。在服务器或 Docker 中使用时，请通过主机 IP 或域名访问，例如 `http://192.168.1.32:5000/`。

## Windows 下载 Release 压缩包使用方法

普通 Windows 用户请优先下载 **Windows 免 Python 便携版**：

`voxcpm-tts-v1.4-windows.zip`

1. 打开 [Releases](https://github.com/qb2743/voxcpm-tts/releases/tag/v1.4) 页面，在 **Assets** 中下载 `voxcpm-tts-v1.4-windows.zip`。
2. 解压到英文路径目录，例如 `D:\Apps\voxcpm-tts-v1.4-windows`。
3. 双击 `voxcpm-webui.exe` 启动 WebUI。
4. 程序启动后通常会自动打开浏览器；如果没有自动打开，请手动访问 `http://127.0.0.1:5000`。
5. 如果只想启动 OpenAI 兼容 TTS API，双击 `voxcpm-server.exe`，服务地址为 `http://localhost:7900/v1/audio/speech`。

注意：

- 这个便携版已经内置 Python 运行时和 Python 依赖，电脑无需另外安装 Python。
- 语音生成仍然需要联网访问 VoxCPM2 在线服务。
- 如需 MP3、Opus、AAC、FLAC 等格式转换，请安装 ffmpeg；WAV 输出不需要 ffmpeg。
- 如果程序无法启动，请在解压目录空白处右键打开终端，运行 `voxcpm-webui.exe` 查看错误提示。
- 示例音色文件已包含在 `voices/` 目录中，可按需替换或新增自己的参考音频。

`voxcpm-tts-v1.4.zip` 是源码包，需要电脑已安装 Python 3.10+，首次运行会联网安装 `requirements.txt` 中的依赖。

## Docker Hub 安装

镜像：`qb2743/voxcpm-tts:1.4`

中文说明：这是一个基于 VoxCPM2 在线语音合成服务的 WebUI 和 OpenAI 兼容 TTS API。支持常规配音、声音设计、音色管理、极致克隆、长文本切分合成，并适合 Docker/VPS 部署。

默认端口：
- WebUI: `5000`
- OpenAI-Compatible API: `7900`

### Docker Compose 安装方法

1. 创建项目目录：

```bash
mkdir -p voxcpm-tts/voices voxcpm-tts/output
cd voxcpm-tts
```

2. 创建 `docker-compose.yml`：

```yaml
services:
  api:
    image: qb2743/voxcpm-tts:1.4
    container_name: voxcpm-api
    ports:
      - "7900:7900"
    environment:
      - API_HOST=0.0.0.0
      - API_PORT=7900
      - GENERATION_RETRY_ATTEMPTS=3
      - GENERATION_RETRY_INITIAL_DELAY=2.0
      - MAX_CONCURRENT_GENERATIONS=5
    volumes:
      - ./voices:/app/voices:ro
      - ./output:/app/output
    command: python server.py
    restart: unless-stopped

  webui:
    image: qb2743/voxcpm-tts:1.4
    container_name: voxcpm-webui
    ports:
      - "5000:5000"
    environment:
      - PORT=5000
      - AUTO_OPEN_BROWSER=0
    volumes:
      - ./voices:/app/voices
      - ./output:/app/output
    command: python webui.py
    restart: unless-stopped
```

3. 启动：

```bash
docker compose up -d
```

4. 访问：

- WebUI: `http://服务器IP:5000`
- API: `http://服务器IP:7900/v1/audio/speech`

查看日志：

```bash
docker compose logs -f
```

停止服务：

```bash
docker compose down
```

WebUI 容器已设置 `AUTO_OPEN_BROWSER=0`，适合 Docker 和 VPS 环境，不会尝试在容器内打开浏览器。

## API 示例

### cURL

```bash
curl http://localhost:7900/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1","input":"你好世界","voice":"dalao"}' \
  --output speech.wav
```

### Python

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:7900/v1", api_key="x")
response = client.audio.speech.create(
    model="tts-1",
    input="你好，这是一次语音合成测试。",
    voice="dalao",
)
response.stream_to_file("output.wav")
```

### 请求参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `model` | string | `tts-1`，保留兼容性 |
| `input` | string | 合成文本，最长 4096 字符 |
| `voice` | string | 音色名称，对应 `config.yaml` |
| `response_format` | string | `wav` / `mp3` / `opus` / `aac` / `flac` |
| `speed` | float | 0.25-4.0，通过 API 的 `control_instruction` 辅助表达 |
| `cfg_value` | float | 可选，覆盖音色配置 |
| `dit_steps` | int | 可选，覆盖音色配置 |
| `control_instruction` | string | 可选，API 扩展参数 |

## 极致克隆 vs 声音设计

| 模式 | 是否需要参考音频 | 控制方式 | 适用场景 |
|------|------------------|----------|----------|
| 极致克隆 | 需要 | 参考音频和参考文本主导 | 严格模仿某个已有声音 |
| 声音设计 | 不需要 | 声音描述和试听文本主导 | 创造特定风格音色 |

声音设计生成的试听音频可以保存为音色。保存后它会写入 `voices/` 和 `config.yaml`，后续可在常规配音中作为参考音色使用。

## 项目结构

```text
VoxCPM2-online/
├── server.py           # FastAPI 服务端（OpenAI 兼容 TTS API）
├── webui.py            # Flask Web 图形界面
├── client.py           # 工具函数库（文本切分/WAV 拼接）
├── voxcpm_client.py    # VoxCPM2 Gradio API 底层客户端
├── config.yaml         # 配置文件和音色列表
├── templates/          # WebUI 页面
├── voices/             # 音色参考音频目录
├── output/             # 运行时输出目录
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── start.bat
├── webui.bat
├── build_exe.bat
└── README.md
```

## 注意事项

1. 每次生成都需要访问 VoxCPM2 在线服务。
2. 声音设计不需要参考音频，也不应开启极致克隆模式。
3. 常规配音面板不使用 `Control Instruction`；声音设计面板才负责用描述创建声音。
4. Docker/VPS 部署时请保留 `0.0.0.0` 监听，访问时使用服务器 IP、域名或反向代理地址。
5. MP3、AAC、FLAC 等格式转换依赖 `ffmpeg`，Docker 镜像中已安装。
