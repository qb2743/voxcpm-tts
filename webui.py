"""VoxCPM2 Web UI - Voice Studio v1.4 (Chinese, Gradio-style layout)"""
import logging, re, sys, threading, webbrowser, tempfile, uuid, time, io as _io
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.resolve()

def _ensure_deps():
    for pkg_name, mod_name in [("flask", "flask"), ("pyyaml", "yaml")]:
        try: __import__(mod_name)
        except ImportError:
            import subprocess
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", pkg_name])
            print(f"[setup] Installed {pkg_name}. Restarting...")
            sys.exit(0)
_ensure_deps()

import os
from flask import Flask, request, jsonify, render_template, send_from_directory, send_file
import yaml
import socket

# ---- paths ---------------------------------------------------------------
D = _app_dir()
CC = D / "config.yaml"
OD = D / "output"
OD.mkdir(parents=True, exist_ok=True)

def _resolve_ref(ref_audio: str) -> Path:
    """Resolve ref_audio: relative paths, Windows abs paths, and direct filenames."""
    p = Path(ref_audio)
    # Detect Windows abs path on Linux via drive letter pattern
    if os.name != 'nt' and len(ref_audio) > 2 and ref_audio[1] == ':' and ref_audio[0].isalpha():
        norm = ref_audio.replace(chr(92), chr(47))
        parts = norm.split(chr(47))
        for i, part in enumerate(parts):
            if part == 'voices':
                rel = Path(*parts[i:])
                return D / rel
        p = Path(parts[-1])
    if not p.is_absolute():
        p = D / p
    return p


def _save_upload_to_temp(uploaded, suffix: str | None = None) -> str:
    if suffix is None:
        suffix = Path(uploaded.filename or "").suffix or ".wav"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp_path = tmp.name
    tmp.close()
    uploaded.save(tmp_path)
    return tmp_path


def _safe_voice_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def _save_voice_file(safe_name: str, audio_bytes: bytes, ext: str = ".wav") -> Path:
    VF = D / "voices"
    VF.mkdir(parents=True, exist_ok=True)
    audio_path = VF / f"{safe_name}{ext}"
    audio_path.write_bytes(audio_bytes)
    return audio_path


def _add_voice_to_config(safe_name: str, ref_audio: str, prompt_text: str = "", voice_type: str = "clone"):
    global voices_cfg, cfg
    new_voice = {
        "ref_audio": ref_audio,
        "cfg_value": clone_d.get("cfg_value", 2.5),
        "dit_steps": clone_d.get("dit_steps", 50),
        "do_normalize": clone_d.get("do_normalize", True),
        "denoise": clone_d.get("denoise", False),
        "prompt_text": prompt_text.strip(),
        "type": voice_type,
    }
    voices_cfg[safe_name] = new_voice
    cfg["voices"] = voices_cfg
    CC.write_text(yaml.dump(cfg, allow_unicode=True, default_flow_style=False), encoding="utf-8")


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("voxcpm-webui")

cfg = yaml.safe_load(CC.read_text(encoding="utf-8")) if CC.exists() else {}
voices_cfg = cfg.get("voices", {})
clone_d = cfg.get("clone_defaults", cfg.get("defaults", {}))
design_d = cfg.get("design_defaults", {})
voxcpm_url = cfg.get("voxcpm", {}).get("base_url", "https://voxcpm.modelbest.cn")

VERSION = "1.4"

# ---- In-memory audio cache (no disk saving) ----
_audio_cache: dict[str, bytes] = {}  # cache_key -> raw audio bytes

def _cache_audio(audio_bytes: bytes) -> str:
    """Store audio in memory cache and return a URL-safe key."""
    key = uuid.uuid4().hex[:16]
    _audio_cache[key] = audio_bytes
    # Limit cache to latest 10 entries
    if len(_audio_cache) > 10:
        oldest = next(iter(_audio_cache))
        del _audio_cache[oldest]
    return key

def _voice_list():
    r = []
    for n, v in voices_cfg.items():
        r.append({"name": n, "type": v.get("type", "clone"), "ref_audio": v.get("ref_audio", ""),
            "cfg_value": v.get("cfg_value", clone_d.get("cfg_value", 2.5)),
            "dit_steps": v.get("dit_steps", clone_d.get("dit_steps", 50)),
            "denoise": v.get("denoise", clone_d.get("denoise", False)),
            "do_normalize": v.get("do_normalize", clone_d.get("do_normalize", True)),
            "control_instruction": "", "prompt_text": v.get("prompt_text", "")})
    return r

# ---- Flask app ----------------------------------------------------------
app = Flask(__name__, template_folder=str(D / "templates"))

@app.route("/")
def index():
    return render_template("index.html", voxcpm_url=voxcpm_url, version=VERSION)

@app.route("/api/version")
def api_version():
    return jsonify({"version": VERSION})

@app.route("/api/voices")
def api_voices():
    return jsonify({"voices": _voice_list()})

@app.route("/api/voice-preview/<int:voice_index>")
def api_voice_preview(voice_index):
    """Serve the reference audio file of a voice for preview/audition."""
    voices = _voice_list()
    if voice_index < 0 or voice_index >= len(voices):
        return jsonify({"ok": False, "error": "Invalid voice index"}), 404
    vc = voices[voice_index]
    ref_path_raw = vc.get("ref_audio", "")
    ref_path = _resolve_ref(ref_path_raw) if ref_path_raw else ""
    if not ref_path or not Path(ref_path).exists():
        return jsonify({"ok": False, "error": "No reference audio available for this voice"}), 404

    # Determine MIME type from extension
    ext = Path(ref_path).suffix.lower()
    mime_map = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
                ".flac": "audio/flac", ".m4a": "audio/mp4", ".aac": "audio/aac"}
    mime = mime_map.get(ext, "audio/wav")
    return send_file(ref_path, mimetype=mime)

@app.route("/api/generate", methods=["POST"])
def api_generate():
    from voxcpm_client import VoxCPM2Client

    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = {k: v for k, v in request.form.items()}

    txt = (data.get("text") or "").strip()
    vi_raw = data.get("voice_index")
    vi = int(vi_raw) if isinstance(vi_raw, str) else vi_raw

    if not txt:
        return jsonify({"ok": False, "error": "缺少合成文本"})

    voices = _voice_list()
    if vi is None or vi < 0 or vi >= len(voices):
        return jsonify({"ok": False, "error": "无效的音色选择"})

    vc = voices[vi]
    ref_audio_raw = vc.get("ref_audio", "")
    ref_audio = str(_resolve_ref(ref_audio_raw)) if ref_audio_raw else ""
    uploaded = request.files.get("ref_audio")
    if uploaded and uploaded.filename:
        ref_audio = _save_upload_to_temp(uploaded)

    if not ref_audio or not Path(ref_audio).exists():
        return jsonify({"ok": False, "error": f"参考音频不存在: {ref_audio}"})

    cfg_val = float(data.get("cfg", vc.get("cfg_value", 2.5)))
    steps = int(data.get("steps", vc.get("dit_steps", 50)))
    dn = data.get("denoise", "true") in (True, "true", "1")
    nm = data.get("normalize", "true") in (True, "true", "1")
    ultimate = data.get("ultimate", "false") in (True, "true", "1")
    timeout = clone_d.get("timeout", 300)
    max_segment_workers = int(cfg.get("text_split", {}).get("max_concurrent_segments", 3))

    if vc.get("type") == "design_voice":
        ci, use_pt = "", True
        pt = vc.get("prompt_text", "").strip()
    elif ultimate:
        ci, use_pt = "", True
        pt = (data.get("prompt_text") or vc.get("prompt_text", "")).strip()
    else:
        ci = ""
        use_pt, pt = False, ""

    is_long = data.get("long_text", "false") in (True, "true", "1")
    n_seg = 1
    total_start = time.perf_counter()

    try:
        if is_long and len(txt) > 50:
            from client import split_long_text, concat_wav_files
            segs = split_long_text(txt, cfg)
            worker_count = max(1, min(max_segment_workers, len(segs)))
            seg_lengths = [len(seg) for seg in segs]
            logger.info(
                "Long text started: chars=%d segments=%d workers=%d segment_lengths=%s cfg=%.1f steps=%d denoise=%s normalize=%s",
                len(txt), len(segs), worker_count, seg_lengths, cfg_val, steps, dn, nm,
            )

            def generate_segment(index: int, seg: str) -> tuple[int, bytes, float, int]:
                seg_start = time.perf_counter()
                logger.info("Segment %d/%d started: chars=%d", index + 1, len(segs), len(seg))
                segment_client = VoxCPM2Client(base_url=voxcpm_url)
                audio_bytes = segment_client.generate(
                    text=seg, ref_audio_path=ref_audio,
                    control_instruction=ci, use_prompt_text=use_pt, prompt_text=pt,
                    cfg_value=cfg_val, dit_steps=steps,
                    do_normalize=nm, denoise=dn, timeout=timeout)
                elapsed = time.perf_counter() - seg_start
                return index, audio_bytes, elapsed, len(seg)

            results = [None] * len(segs)
            completed = 0
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(generate_segment, i, seg) for i, seg in enumerate(segs)]
                for future in as_completed(futures):
                    index, audio_bytes, elapsed, chars = future.result()
                    results[index] = audio_bytes
                    completed += 1
                    speed = chars / elapsed if elapsed > 0 else 0
                    logger.info(
                        "Segment %d/%d done: elapsed=%.1fs speed=%.2f chars/s progress=%d/%d",
                        index + 1, len(segs), elapsed, speed, completed, len(segs),
                    )

            audio = concat_wav_files(results,
                cfg.get("text_split", {}).get("pause_between_segments", 0.3))
            n_seg = len(results)
        else:
            logger.info(
                "Single text started: chars=%d cfg=%.1f steps=%d denoise=%s normalize=%s",
                len(txt), cfg_val, steps, dn, nm,
            )
            client = VoxCPM2Client(base_url=voxcpm_url)
            audio = client.generate(
                text=txt, ref_audio_path=ref_audio,
                control_instruction=ci, use_prompt_text=use_pt, prompt_text=pt,
                cfg_value=cfg_val, dit_steps=steps,
                do_normalize=nm, denoise=dn, timeout=timeout)

        # Store in memory cache instead of saving to disk
        total_elapsed = time.perf_counter() - total_start
        cache_key = _cache_audio(audio)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        display_name = f"{vc['name']}_{ts}.wav"

        logger.info(
            "Generated (cached): %s, %.1fKB, segments=%d total_elapsed=%.1fs avg_segment_elapsed=%.1fs",
            display_name,
            len(audio) / 1024,
            n_seg,
            total_elapsed,
            total_elapsed / n_seg if n_seg else total_elapsed,
        )
        return jsonify({"ok": True, "file": display_name,
            "size": f"{len(audio) / 1024:.1f} KB",
            "segments": n_seg,
            "audio_url": f"/output/cache/{cache_key}.wav"})
    except Exception as e:
        logger.exception("Generate failed")
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/design", methods=["POST"])
def api_design():
    from voxcpm_client import VoxCPM2Client

    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = {k: v for k, v in request.form.items()}

    desc = (data.get("desc") or "").strip()
    tt = (data.get("text") or "").strip()

    if not desc:
        return jsonify({"ok": False, "error": "缺少声音描述"})
    if not tt:
        tt = "你好，这是我为你设计的声音，听起来怎么样？"

    try:
        client = VoxCPM2Client(base_url=voxcpm_url)
        audio = client.generate(
            text=tt, ref_audio_path=None, control_instruction=desc,
            cfg_value=design_d.get("cfg_value", 2.0),
            dit_steps=design_d.get("dit_steps", 10),
            do_normalize=design_d.get("do_normalize", True),
            denoise=design_d.get("denoise", False),
            timeout=design_d.get("timeout", 300))

        # Store in memory cache instead of saving to disk
        cache_key = _cache_audio(audio)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        display_name = f"design_{ts}.wav"

        logger.info("Design (cached): %s, %.1fKB", display_name, len(audio) / 1024)
        return jsonify({"ok": True, "file": display_name,
            "size": f"{len(audio) / 1024:.1f} KB",
            "cache_key": cache_key,
            "audio_url": f"/output/cache/{cache_key}.wav"})
    except Exception as e:
        logger.exception("Design failed")
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/save-designed-voice", methods=["POST"])
def api_save_designed_voice():
    """Save the latest designed preview audio as a cloneable voice."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    cache_key = (data.get("cache_key") or "").strip()
    prompt_text = (data.get("prompt_text") or "").strip()

    if not name:
        return jsonify({"ok": False, "error": "缺少音色名称"})
    safe_name = _safe_voice_name(name)
    if not safe_name:
        return jsonify({"ok": False, "error": "无效的音色名称"})
    if safe_name in voices_cfg:
        return jsonify({"ok": False, "error": f"音色 \"{safe_name}\" 已存在，请换一个名称"})
    if not cache_key or cache_key not in _audio_cache:
        return jsonify({"ok": False, "error": "请先试听生成声音，再保存为音色"})

    audio_path = _save_voice_file(safe_name, _audio_cache[cache_key], ".wav")
    _add_voice_to_config(safe_name, f"voices/{safe_name}.wav", prompt_text, voice_type="design_voice")

    logger.info("Designed voice saved: %s -> %s", safe_name, audio_path)
    return jsonify({"ok": True, "name": safe_name, "path": str(audio_path)})

@app.route("/api/asr", methods=["POST"])
def api_asr():
    from voxcpm_client import VoxCPM2Client
    uploaded = request.files.get("audio")
    if not uploaded or not uploaded.filename:
        return jsonify({"ok": False, "error": "没有上传音频文件"})
    tmp_name = _save_upload_to_temp(uploaded)
    asr_start = time.perf_counter()
    logger.info("ASR started: file=%s size=%.1fKB", uploaded.filename, Path(tmp_name).stat().st_size / 1024)
    try:
        client = VoxCPM2Client(base_url=voxcpm_url)
        text = client.transcribe(tmp_name)
        elapsed = time.perf_counter() - asr_start
        logger.info(
            "ASR done: file=%s chars=%d elapsed=%.1fs text_preview=%s",
            uploaded.filename,
            len(text),
            elapsed,
            text[:80],
        )
    except Exception as e:
        logger.exception("ASR failed")
        return jsonify({"ok": False, "error": str(e)})
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    return jsonify({"ok": True, "text": text})

@app.route("/output/<path:filename>")
def serve_output(filename):
    """Serve audio. Check in-memory cache first, then fall back to disk files."""
    # Handle cache-based audio: /output/cache/<key>.wav
    if filename.startswith("cache/") and filename.endswith(".wav"):
        cache_key = filename[len("cache/"):-len(".wav")]
        if cache_key in _audio_cache:
            return send_file(
                _io.BytesIO(_audio_cache[cache_key]),
                mimetype="audio/wav",
                as_attachment=False,
                download_name=f"voxcpm_{cache_key[:8]}.wav"
            )
        else:
            return jsonify({"ok": False, "error": "Audio expired or not found"}), 404

    # Fallback: serve from disk for legacy files
    return send_from_directory(str(OD), filename)

@app.route("/api/delete-voice", methods=["POST"])
def api_delete_voice():
    """Remove a voice from config.yaml"""
    global voices_cfg, cfg
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "缺少音色名称"})
    if name not in voices_cfg:
        return jsonify({"ok": False, "error": f"音色 {name} 不存在"})
    del voices_cfg[name]
    cfg["voices"] = voices_cfg
    CC.write_text(yaml.dump(cfg, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    logger.info("Voice deleted: %s", name)
    return jsonify({"ok": True, "name": name})

@app.route("/api/save-voice", methods=["POST"])
def api_save_voice():
    """Save uploaded reference audio as a new voice in config.yaml"""
    global voices_cfg, cfg
    uploaded = request.files.get("audio")
    if not uploaded or not uploaded.filename:
        return jsonify({"ok": False, "error": "没有上传音频文件"})

    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "缺少音色名称"})

    safe_name = _safe_voice_name(name)
    if not safe_name:
        return jsonify({"ok": False, "error": "无效的音色名称"})

    # Check duplicate
    if safe_name in voices_cfg:
        return jsonify({"ok": False, "error": f"音色 \"{safe_name}\" 已存在，请换一个名称"})

    # Save audio file to voices/
    VF = D / "voices"
    VF.mkdir(parents=True, exist_ok=True)
    ext = (Path(uploaded.filename).suffix or ".wav").lower()
    audio_path = VF / f"{safe_name}{ext}"
    uploaded.save(audio_path)

    _add_voice_to_config(safe_name, f"voices/{safe_name}{ext}",
        request.form.get("prompt_text", "").strip(), voice_type="clone")

    logger.info("Voice saved: %s -> %s", safe_name, audio_path)
    return jsonify({"ok": True, "name": safe_name, "path": str(audio_path)})

# ---- Entry point -------------------------------------------------------- --------------------------------------------------------
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    bind_host = "0.0.0.0"
    local_url = f"http://127.0.0.1:{port}"
    auto_open = os.environ.get("AUTO_OPEN_BROWSER", "1").lower() not in ("0", "false", "no")
    public_url = os.environ.get("PUBLIC_URL", "").strip()

    def _lan_url():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            return f"http://{ip}:{port}"
        except Exception:
            return ""

    lan_url = _lan_url()

    if auto_open:
        def _open():
            import time; time.sleep(0.8); webbrowser.open(local_url)
        threading.Thread(target=_open, daemon=True).start()
    print("=" * 55)
    print(f"  VoxCPM2 Voice Studio v{VERSION}")
    print(f"  Local:    {local_url}")
    if lan_url:
        print(f"  LAN:      {lan_url}")
    if public_url:
        print(f"  Public:   {public_url}")
    print(f"  Bind:     http://{bind_host}:{port}")
    print(f"  API source: {voxcpm_url}")
    print("  Ctrl+C to stop")
    print("=" * 55)
    app.run(host=bind_host, port=port, debug=False)
