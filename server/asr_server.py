#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地实时语音识别 + 翻译服务器（WebSocket）。

默认使用 faster-whisper（large-v3，日语识别更准），也可用 --engine sensevoice 切回 SenseVoice。

协议：
  客户端 -> 服务器：二进制帧 = int16 单声道 16kHz PCM 音频
                   文本帧 = {"type": "reset"}  清空当前 VAD 状态
                   文本帧 = {"type": "setKey", "apiKey": "sk-..."}  设置 DeepSeek API Key（用于翻译）
  服务器 -> 客户端：{"type": "ready"}
                    {"type": "asr", "original": "...", "translated": "..."}  识别+翻译结果
                    {"type": "error", "message": "..."}

用法：
  python asr_server.py [--host 127.0.0.1] [--port 8765]
                       [--engine whisper|sensevoice] [--model large-v3]
                       [--device auto|cpu|cuda]
"""
import argparse
import asyncio
import json
import os
import re
import sys
import threading

import numpy as np

import streamer_profile  # noqa: E402  本地共享模块：主播画像 + 日志落盘

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 模型必须放在纯 ASCII 路径：sentencepiece 等 C++ 库在 Windows 上无法打开含中文的路径。
# 默认使用 %LOCALAPPDATA%\LiveTranslate（用户环境可写且持久），可用环境变量覆盖。
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
ASCII_BASE = os.path.join(_LOCALAPPDATA, "LiveTranslate")
# 用户手动下载的 kotoba-whisper（日语专用，精度更高）目录
KOTOBA_MODEL_DIR = os.path.join(ASCII_BASE, "models", "kotoba-whisper-v2.0-faster")
os.environ.setdefault("MODELSCOPE_CACHE", os.path.join(ASCII_BASE, "modelscope"))
os.environ.setdefault("MODELSCOPE_HOME", os.path.join(ASCII_BASE, "modelscope_home"))
os.environ.setdefault("HF_HOME", os.path.join(ASCII_BASE, "huggingface"))
os.environ.setdefault("HF_HUB_CACHE", os.path.join(ASCII_BASE, "huggingface", "hub"))
os.environ.setdefault("TORCH_HOME", os.path.join(ASCII_BASE, "torch"))
# HuggingFace 模型用国内镜像下载（faster-whisper 模型）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

SAMPLE_RATE = 16000
_TAG_RE = re.compile(r"<\|[^|]*\|>")

# 诊断日志开关：默认关。需要排查时把这里改成 True，或加启动参数 --debug
DEBUG_AUDIO_LOG = os.environ.get("LT_DEBUG_AUDIO") == "1"


def clean_text(text):
    """去掉 SenseVoice 输出的 <|ja|> 之类特殊标记。"""
    text = _TAG_RE.sub("", text or "")
    text = text.replace("[EMO]", "").replace("</s>", "").replace("<s>", "")
    return text.strip()


_SILERO_MODEL = None
_SILERO_LOCK = threading.Lock()


def _get_silero_model():
    """加载 silero VAD 模型（全局单例）。模型文件必须放 ASCII 路径，torch.jit.load 在中文路径下会失败。"""
    global _SILERO_MODEL
    if _SILERO_MODEL is not None:
        return _SILERO_MODEL
    with _SILERO_LOCK:
        if _SILERO_MODEL is not None:
            return _SILERO_MODEL
        import torch

        torch.set_num_threads(1)
        target = os.path.join(ASCII_BASE, "models", "silero_vad.jit")
        if not os.path.isfile(target):
            import importlib.resources as _res
            import shutil

            src = str(_res.files("silero_vad.data").joinpath("silero_vad.jit"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(src, target)
        model = torch.jit.load(target, map_location="cpu")
        model.eval()
        _SILERO_MODEL = model
        return _SILERO_MODEL


def _silero_confidence(model, audio_chunk):
    import torch

    window = 512
    chunk = audio_chunk[:window]
    if len(chunk) < window:
        chunk = np.pad(chunk, (0, window - len(chunk)))
    with torch.inference_mode():
        tensor = torch.from_numpy(np.ascontiguousarray(chunk)).float()
        return float(model(tensor, SAMPLE_RATE).item())


class VadState:
    """Silero 神经网络 VAD：从连续音频流里切分出「一句话」。

    相比能量阈值，Silero 能更好地区分「人声」和「背景音乐/噪声」。
    内部按 32ms 分块逐块打分，配合 pre-buffer（补前导音）、渐进静音阈值、智能切分、语音密度过滤。
    """

    def __init__(self, sample_rate=SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.model = _get_silero_model()
        self.threshold = 0.5

        self.chunk_samples = int(0.032 * sample_rate)  # 512 @ 16k
        self.min_speech_samples = int(0.2 * sample_rate)
        self.max_speech_samples = int(15.0 * sample_rate)

        self._speech_buf = []      # list of float32 arrays
        self._conf_history = []    # 每块置信度（与 _speech_buf 同步）
        self._speech_samples = 0
        self._is_speaking = False
        self._silence_chunks = 0

        self._pre_buf = []         # 语音开始前的预缓冲（补前导音）
        self._pre_max = 3          # 3 块 ≈ 96ms
        self._pending = np.zeros(0, dtype=np.float32)  # 累积缓冲，凑满 512 样本

        self._silence_limit = max(1, round(0.8 / 0.032))  # 0.8s 静音 → 25 块
        # 渐进：缓冲越长，静音阈值越短（更快切分）
        self._progressive = [(3.0, 1.0), (6.0, 0.5), (10.0, 0.25)]

    def _eff_silence_limit(self):
        sec = self._speech_samples / self.sample_rate
        mult = 1.0
        for tier_sec, tier_mult in self._progressive:
            if sec < tier_sec:
                break
            mult = tier_mult
        return max(1, round(self._silence_limit * mult))

    def _reset(self):
        self._speech_buf = []
        self._conf_history = []
        self._speech_samples = 0
        self._is_speaking = False
        self._silence_chunks = 0
        self._pre_buf = []
        self._pending = np.zeros(0, dtype=np.float32)

    def process(self, audio):
        """audio: float32 单声道。返回完成的语音片段或 None。

        客户端每帧大小不定（可能不是 512 的整数倍），这里先累积到 _pending，
        凑满 512 样本再喂 Silero——否则碎块被补零会严重拉低置信度。
        """
        if audio.size == 0:
            return None
        self._pending = np.concatenate([self._pending, audio])
        result = None
        while self._pending.size >= self.chunk_samples:
            chunk = self._pending[: self.chunk_samples]
            self._pending = self._pending[self.chunk_samples :]
            conf = _silero_confidence(self.model, chunk)
            eff_limit = self._eff_silence_limit()

            if conf >= self.threshold:
                if not self._is_speaking:
                    for pc in self._pre_buf:
                        self._speech_buf.append(pc)
                        self._conf_history.append(self.threshold)
                        self._speech_samples += len(pc)
                    self._pre_buf = []
                self._is_speaking = True
                self._silence_chunks = 0
                self._speech_buf.append(chunk)
                self._conf_history.append(conf)
                self._speech_samples += len(chunk)
            elif self._is_speaking:
                self._silence_chunks += 1
                self._speech_buf.append(chunk)
                self._conf_history.append(conf)
                self._speech_samples += len(chunk)
            else:
                self._pre_buf.append(chunk)
                if len(self._pre_buf) > self._pre_max:
                    self._pre_buf.pop(0)

            if self._speech_samples >= self.max_speech_samples:
                return self._split_at_best_pause()

            if self._is_speaking and self._silence_chunks >= eff_limit:
                if self._speech_samples >= self.min_speech_samples:
                    seg = self._flush_segment()
                    if seg is not None:
                        return seg
                # 太短：保留，等下一段合并
                self._is_speaking = False
                self._silence_chunks = 0
        return None

    def _flush_segment(self):
        if not self._speech_buf:
            return None
        # 语音密度过滤：大部分块低于阈值 → 当噪声/音乐丢弃
        if len(self._conf_history) >= 4:
            voiced = sum(1 for c in self._conf_history if c >= self.threshold)
            if voiced / len(self._conf_history) < 0.25:
                self._reset()
                return None
        seg = np.concatenate(self._speech_buf)
        self._reset()
        return seg

    def _split_at_best_pause(self):
        n = len(self._conf_history)
        if n < 4:
            seg = np.concatenate(self._speech_buf) if self._speech_buf else None
            self._reset()
            return seg
        # 平滑（~5 块窗口）后在后 70% 里找置信度最低谷作为切点
        smooth_win = min(5, n // 2)
        smoothed = []
        for i in range(n):
            lo = max(0, i - smooth_win // 2)
            hi = min(n, i + smooth_win // 2 + 1)
            smoothed.append(sum(self._conf_history[lo:hi]) / (hi - lo))
        search_start = max(1, n * 3 // 10)
        split_idx = min(range(search_start, n), key=lambda i: smoothed[i])
        first_bufs = self._speech_buf[:split_idx]
        remain_bufs = self._speech_buf[split_idx:]
        seg = np.concatenate(first_bufs) if first_bufs else np.concatenate(self._speech_buf)
        self._speech_buf = remain_bufs
        self._conf_history = self._conf_history[split_idx:]
        self._speech_samples = sum(len(b) for b in remain_bufs)
        self._is_speaking = True
        self._silence_chunks = 0
        return seg

    def reset(self):
        self._reset()


MODEL = None
ENGINE = "whisper"
MODEL_LOCK = asyncio.Lock()
TRANSLATE_LOCK = asyncio.Lock()


def _resolve_whisper_model(name):
    """把模型名解析为本地路径：优先用本地/ModelScope 缓存，其次从 ModelScope 下载（国内快）。"""
    if os.path.isdir(name) and os.path.isfile(os.path.join(name, "model.bin")):
        return name
    # kotoba 别名：优先用用户手动下载的日语专用模型
    if name == "kotoba":
        if os.path.isfile(os.path.join(KOTOBA_MODEL_DIR, "model.bin")):
            return KOTOBA_MODEL_DIR
        print("[warn] 未找到 kotoba-whisper，回退到 large-v3", flush=True)
        name = "Systran/faster-whisper-large-v3"
    mc = os.environ.get("MODELSCOPE_CACHE") or os.path.join(ASCII_BASE, "modelscope")
    local = os.path.join(mc, "models", name.replace("/", "--"), "snapshots", "master")
    if os.path.isfile(os.path.join(local, "model.bin")):
        return local
    try:
        from modelscope import snapshot_download

        print(f"[download] 从 ModelScope 下载 {name} ...", flush=True)
        p = snapshot_download(name)
        if p and os.path.isfile(os.path.join(p, "model.bin")):
            return p
    except Exception as e:  # noqa: BLE001
        print(f"[warn] ModelScope 下载失败，回退到 HuggingFace: {e}", flush=True)
    return name


def load_model(device, engine, model_name):
    global MODEL, ENGINE
    ENGINE = engine
    if engine == "whisper":
        from faster_whisper import WhisperModel

        model_path = _resolve_whisper_model(model_name)
        compute = "float16" if device.startswith("cuda") else "int8"
        print(
            f"[loading] faster-whisper {model_name} on {device} ({compute}) ...",
            flush=True,
        )
        MODEL = WhisperModel(model_path, device=device, compute_type=compute)
    else:
        from funasr import AutoModel

        print(f"[loading] SenseVoiceSmall on {device} ...", flush=True)
        MODEL = AutoModel(
            model="iic/SenseVoiceSmall",
            device=device,
            disable_update=True,
        )
    print("[loading] done", flush=True)


# 用于引导 whisper 偏向口语化日语（VTuber 直播常见语气词）
_JA_INITIAL_PROMPT = (
    "はい、えっと、あの、まあ、そうですね、みなさん、こんばんは、"
    "ありがとうございます、配信、ゲーム、チャット、コメント、"
)


def _transcribe(audio):
    """同步转录（在锁内/线程里调用）。audio: float32 单声道 16kHz。"""
    if ENGINE == "whisper":
        segments, info = MODEL.transcribe(
            audio,
            language="ja",
            beam_size=8,
            vad_filter=True,  # 用神经网络 VAD 过滤背景音乐
            condition_on_previous_text=False,
            initial_prompt=_JA_INITIAL_PROMPT,
            temperature=0.0,
        )
        text = "".join(s.text for s in segments)
        return text.strip()
    # sensevoice
    res = MODEL.generate(input=audio, language="auto", use_itn=True)
    if not res:
        return ""
    item = res[0] if isinstance(res, list) else res
    return clean_text(item.get("text", ""))


async def recognize(audio):
    """识别一段完整语音，返回清理后的文本。"""
    global MODEL
    async with MODEL_LOCK:
        return await asyncio.to_thread(_transcribe, audio)


def translate_deepseek(text, api_key, room_id=None):
    """调用 DeepSeek 把日语翻译成简体中文（非流式，一次性返回整句译文）。

    room_id：直播间号，用于加载对应主播画像、注入专属术语/梗；为空则用基础提示词。
    """
    system = streamer_profile.build_prompt(room_id)
    resp = streamer_profile.HTTP_SESSION.post(
        "https://api.deepseek.com/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        json={
            "model": "deepseek-flash",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "temperature": 0.2,
            "max_tokens": 200,
            "stream": False,
            # 关闭思考模式：实时翻译不需要 CoT 推理，开着会先"思考"再输出、拖慢延迟
            "thinking": {"type": "disabled"},
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"DeepSeek HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message", {}) or {}).get("content", "").strip()


async def handle_segment(ws, seg, api_key, room_id, session_id, uname, seg_seq):
    try:
        text = await recognize(seg)
    except Exception as e:  # noqa: BLE001
        print(f"[asr error] {e}", flush=True)
        try:
            await ws.send(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass
        return
    if not text:
        return

    print(f"[ASR] {text}", flush=True)

    # 截取音频落盘（只存有实际识别结果的段；直播/视频/番剧都记录，供第三方 AI 分析）
    audio_file = None
    if room_id:
        audio_file = streamer_profile.save_audio(room_id, session_id, seg_seq, seg)

    translated = ""
    if api_key:
        # 串行翻译，避免多句译文交错
        async with TRANSLATE_LOCK:
            try:
                translated = await asyncio.to_thread(
                    translate_deepseek, text, api_key, room_id
                )
                print(f"[TR] {translated}", flush=True)
            except Exception as e:  # noqa: BLE001
                translated = f"[翻译失败: {e}]"
                print(f"[tr error] {e}", flush=True)
    else:
        translated = "[未配置翻译]"

    # 落盘：原文 + 译文 + 音频引用（直播房间号 / 视频 BV 号 / 番剧号，供后续喂 AI 做主播分析）
    if room_id:
        streamer_profile.save_transcript(
            room_id, session_id, uname, text, translated, audio_file
        )

    # 一次性把原文+译文发给前端
    try:
        await ws.send(
            json.dumps(
                {"type": "asr", "original": text, "translated": translated},
                ensure_ascii=False,
            )
        )
    except Exception:
        pass


async def handler(ws):
    vad = VadState()
    api_key = ""
    room_id = None              # 当前直播间号（用于画像 + 落盘）
    uname = ""
    session_id = streamer_profile.new_session_id()  # 本次采集会话（毫秒级唯一）
    seg_seq = 0                 # 本会话音频片段序号
    _rms_sum = 0.0
    _rms_n = 0
    _bytes_n = 0
    try:
        await ws.send(json.dumps({"type": "ready", "message": "ASR server ready"}))
    except Exception:
        return
    peer = getattr(ws, "remote_address", "?")
    print(f"[connect] {peer}", flush=True)

    try:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                if len(message) % 2 != 0 or len(message) == 0:
                    continue
                audio = (
                    np.frombuffer(message, dtype=np.int16).astype(np.float32) / 32768.0
                )
                if DEBUG_AUDIO_LOG:
                    _bytes_n += len(message)
                    rms = float(np.sqrt(np.mean(audio ** 2) + 1e-12))
                    _rms_sum += rms
                    _rms_n += 1
                    if _rms_n % 50 == 0:
                        print(
                            f"[audio] 收到 {_rms_n} 帧, 平均RMS={_rms_sum/_rms_n:.4f}, "
                            f"累计字节={_bytes_n}",
                            flush=True,
                        )
                seg = vad.process(audio)
                if seg is not None:
                    seg_seq += 1
                    asyncio.create_task(
                        handle_segment(ws, seg, api_key, room_id, session_id, uname, seg_seq)
                    )
            else:
                try:
                    data = json.loads(message)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "reset":
                    vad.reset()
                    print("[reset]", flush=True)
                elif data.get("type") == "setKey":
                    api_key = (data.get("apiKey") or "").strip()
                    print("[setKey]", "ok" if api_key else "(empty)", flush=True)
                elif data.get("type") == "setRoom":
                    # 兼容旧协议：只传直播间号
                    room_id = str(data.get("roomId") or "").strip() or None
                    profile = streamer_profile.load_profile(room_id) if room_id else None
                    uname = (profile or {}).get("name", "")
                    print(f"[setRoom] {room_id} ({uname})", flush=True)
                elif data.get("type") == "setSource":
                    # 通用来源：直播房间号 / 视频 BV 号 / 番剧号，用于落盘 + 画像
                    room_id = str(data.get("source") or "").strip() or None
                    # 只有直播房间号才能匹配到主播画像；视频/番剧走基础提示词
                    profile = streamer_profile.load_profile(room_id) if room_id else None
                    uname = (profile or {}).get("name", "")
                    print(f"[setSource] {room_id} ({uname})", flush=True)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if DEBUG_AUDIO_LOG:
            if _rms_n:
                print(
                    f"[disconnect] {peer} 共收 {_rms_n} 帧音频, 平均RMS={_rms_sum/_rms_n:.4f}",
                    flush=True,
                )
            else:
                print(f"[disconnect] {peer} 未收到任何音频帧", flush=True)
        else:
            print(f"[disconnect] {peer}", flush=True)


def _detect_device(engine):
    if engine == "whisper":
        try:
            import ctranslate2

            return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            return "cpu"
    import torch

    return "cuda:0" if torch.cuda.is_available() else "cpu"


def parse_args():
    p = argparse.ArgumentParser(description="实时语音识别 + 翻译 WebSocket 服务器（插件端，可独立运行）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--engine",
        default="whisper",
        choices=["whisper", "sensevoice"],
        help="识别引擎：whisper（默认，日语更准）| sensevoice",
    )
    p.add_argument(
        "--model",
        default="kotoba",
        help="whisper 模型：kotoba（日语专用，需手动下载）| Systran/faster-whisper-large-v3 | 本地路径",
    )
    p.add_argument(
        "--device",
        default="auto",
        help="auto | cpu | cuda（auto 有显卡就用显卡）",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="打开诊断日志（打印收到的音频 RMS/帧数，用于排查）",
    )
    return p.parse_args()


async def run_plugin(host="127.0.0.1", port=8765):
    """启动插件端 WebSocket 服务（模型需先 load_model）。会一直运行直到被取消。"""
    import websockets

    async with websockets.serve(
        handler, host, port, max_size=2 ** 24, ping_interval=20
    ):
        print(f"[plugin] listening ws://{host}:{port}", flush=True)
        await asyncio.Future()


async def main():
    """独立运行入口（向后兼容）：只跑插件端。统一入口请用 main.py。"""
    args = parse_args()
    if args.debug:
        global DEBUG_AUDIO_LOG
        DEBUG_AUDIO_LOG = True
    device = args.device
    if device == "auto":
        device = _detect_device(args.engine)

    load_model(device, args.engine, args.model)
    print(
        f"[listening] ws://{args.host}:{args.port}  "
        f"(engine={args.engine} model={args.model} device={device})",
        flush=True,
    )
    await run_plugin(args.host, args.port)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
