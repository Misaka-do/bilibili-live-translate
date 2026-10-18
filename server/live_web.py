#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B 站直播间监测 + 实时翻译 Web 服务（手机端用）。

与浏览器插件的区别：插件靠「采集浏览器标签页声音」拿到音频，手机上没有插件；
这个服务由电脑自己抓取 B 站直播流的音频，识别 + 翻译后，把字幕通过 WebSocket
推给手机 / 浏览器打开的网页。

流程：
    监测直播间（live_status）→ 开播后取播放地址(playUrl) → PyAV 解码直播流音频
    → 重采样 16kHz 单声道 → Silero VAD 切句 → whisper/SenseVoice 识别
    → DeepSeek 翻译 → WebSocket 广播字幕给所有网页客户端。

复用 asr_server.py 里的 VAD / 识别 / 翻译逻辑，不重复造轮子。

用法：
    python live_web.py [--room 12345678] [--host 0.0.0.0] [--port 8000]
                       [--engine whisper|sensevoice] [--model kotoba]
                       [--device auto|cpu|cuda]
"""
import argparse
import asyncio
import collections
import json
import os
import socket
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)  # 让 import asr_server 生效（不管从哪个目录启动）

import websockets  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402
from websockets.http11 import Response  # noqa: E402
from websockets.datastructures import Headers  # noqa: E402

import asr_server  # noqa: E402
import streamer_profile  # noqa: E402

# av（PyAV）只在「抓直播流」时才需要，属于可选依赖。
# 这里不顶层导入，避免只用插件端的用户因没装 av 而无法启动统一入口。
# 真正用到的地方（capture_thread / 开播抓流）会按需 import。

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
SAMPLE_RATE = 16000
REFERER = "https://live.bilibili.com/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
HTTP_HEADERS = {"User-Agent": UA, "Referer": REFERER}

CONFIG_PATH = os.path.join(ROOT, "config.json")
WEB_INDEX = os.path.join(HERE, "web", "index.html")

POLL_INTERVAL = 10          # 秒：轮询直播间状态
MAX_SEG_QUEUE = 3           # 待识别队列上限，超出丢弃最旧（保证低延迟、不堆积）
vad_threshold = 0.5         # Silero VAD 置信度阈值（越小越灵敏，越容易把音乐当人声）

# --------------------------------------------------------------------------- #
# 全局状态
# --------------------------------------------------------------------------- #
config = {"room_id": 0, "api_key": ""}
live_info = {"live": False, "title": "", "uname": "", "face": ""}

# 预设主播（下拉选择用）。默认留空，用户可在网页设置里自行添加常看的主播。
PRESET_ROOMS = []

# 头像/昵称缓存：card 接口不随轮询反复请求，命中缓存直接复用（避免 403）
anchor_cache = {}            # room_id -> {"uname": str, "face": str, "ts": float}
AVATAR_TTL = 30 * 60         # 头像缓存 30 分钟（头像/昵称几乎不变，无需频繁刷新）

# 字幕历史：断线重连后回放，移动端切后台再回来不丢这段时间的字幕
subtitle_history = collections.deque(maxlen=80)

model_ready = False
engine_name = "whisper"
model_name = "kotoba"
device_name = "cpu"

clients = set()              # 已连接的网页 WebSocket
seg_queue = asyncio.Queue()  # 待识别音频片段（浮点 16k 单声道）
seq = 0                      # 字幕消息自增 id
seq_lock = threading.Lock()

capture_active = False
current_session_id = ""      # 当前抓流会话 id（音频文件名 + 日志 session 字段）
_capture_stop = None         # 当前抓流线程的停止事件（每线程独立，避免误杀新线程）
_capture_thread = None
_current_container = None
_container_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 配置持久化
# --------------------------------------------------------------------------- #
def load_config(args):
    global config
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                config.update(data)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 读取 config.json 失败：{e}", flush=True)
    # 命令行参数优先级最高
    if args.room is not None:
        config["room_id"] = args.room
    if isinstance(config.get("room_id"), str):
        config["room_id"] = int(config["room_id"])


def save_config():
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 保存 config.json 失败：{e}", flush=True)


# --------------------------------------------------------------------------- #
# B 站接口
# --------------------------------------------------------------------------- #
def _fetch_card(uid):
    """通过 uid 拿主播 {name, face}（card 接口）。失败返回空。"""
    try:
        r = streamer_profile.HTTP_SESSION.get(
            "https://api.bilibili.com/x/web-interface/card",
            params={"mid": uid},
            headers=HTTP_HEADERS,
            timeout=10,
        )
        card = (r.json().get("data") or {}).get("card") or {}
        return {"name": card.get("name") or "", "face": card.get("face") or ""}
    except Exception:
        return {"name": "", "face": ""}


def _cached_anchor(room_id, uid):
    """带缓存的头像/昵称获取：命中且未过期直接复用，否则调 card 接口并缓存。"""
    now = time.time()
    c = anchor_cache.get(room_id)
    if c and c.get("face") and (now - c.get("ts", 0)) < AVATAR_TTL:
        return c
    card = _fetch_card(uid)
    if card.get("face"):
        anchor_cache[room_id] = {"uname": card["name"], "face": card["face"], "ts": now}
        return anchor_cache[room_id]
    # 接口失败（face 空）：有旧缓存就用旧的，否则返回空（不缓存失败结果，下次再试）
    return c or {"uname": "", "face": "", "ts": now}


def fetch_room_info(room_id):
    """返回 {'live': bool, 'title': str, 'uname': str, 'face': str}，失败返回 None。

    注意：这里每次都会请求 get_info（轮询开播状态，必要）；头像/昵称走 _cached_anchor
    缓存，不会每次轮询都请求 card 接口。
    """
    try:
        r = streamer_profile.HTTP_SESSION.get(
            "https://api.live.bilibili.com/room/v1/Room/get_info",
            params={"room_id": str(room_id)},
            headers=HTTP_HEADERS,
            timeout=15,
        )
        d = r.json()
        if d.get("code") != 0:
            return None
        data = d.get("data") or {}
        info = {
            "live": int(data.get("live_status", 0)) == 1,
            "title": data.get("title", "") or "",
            "uname": "",
            "face": "",
        }
        uid = data.get("uid")
        if uid:
            anchor = _cached_anchor(room_id, uid)
            info["uname"] = anchor.get("uname", "")
            info["face"] = anchor.get("face", "")
        return info
    except Exception as e:  # noqa: BLE001
        print(f"[room] 获取房间信息失败：{e}", flush=True)
        return None


def fetch_preset_faces():
    """预热预设主播的头像缓存（best-effort，失败不影响主流程）。"""
    got = 0
    for p in PRESET_ROOMS:
        try:
            if fetch_room_info(p["room_id"]):
                got += 1
        except Exception:
            pass
    print(f"[preset] 头像缓存预热 {got}/{len(PRESET_ROOMS)}", flush=True)


def fetch_play_url(room_id):
    """返回 FLV 直播流直链；失败抛异常。"""
    r = streamer_profile.HTTP_SESSION.get(
        "https://api.live.bilibili.com/room/v1/Room/playUrl",
        params={
            "cid": str(room_id),
            "platform": "web",
            # 音频各档位恒定 128kbps AAC，qn 只影响视频码率；取 150 高清即可，
            # 既保证音频质量，又不至于把用不上的原画视频白拉下来浪费带宽。
            "qn": "150",
            "https_url_req": "1",
            "ptype": "16",
        },
        headers=HTTP_HEADERS,
        timeout=15,
    )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"playUrl code={d.get('code')}")
    durl = (d.get("data") or {}).get("durl") or []
    if not durl:
        raise RuntimeError("playUrl 返回空 durl")
    return durl[0]["url"]


# --------------------------------------------------------------------------- #
# 字幕广播
# --------------------------------------------------------------------------- #
def status_message():
    presets = [
        {
            "room_id": p["room_id"],
            "name": p["name"],
            "face": (anchor_cache.get(p["room_id"]) or {}).get("face", ""),
        }
        for p in PRESET_ROOMS
    ]
    return {
        "type": "status",
        "live": live_info.get("live", False),
        "room_id": config.get("room_id"),
        "title": live_info.get("title", ""),
        "uname": live_info.get("uname", ""),
        "face": live_info.get("face", ""),
        "presets": presets,
        "has_key": bool(config.get("api_key")),
        "model_ready": model_ready,
        "engine": engine_name,
        "model": model_name,
        "device": device_name,
        "capturing": capture_active,
        "lan_url": lan_url,
    }


async def broadcast(msg):
    if not clients:
        return
    data = json.dumps(msg, ensure_ascii=False)
    dead = []
    for ws in list(clients):
        try:
            await ws.send(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


# --------------------------------------------------------------------------- #
# 识别 + 翻译（复用 asr_server）
# --------------------------------------------------------------------------- #
async def handle_segment(seg):
    global seq
    if not model_ready:
        return
    try:
        text = await asr_server.recognize(seg)
    except Exception as e:  # noqa: BLE001
        print(f"[asr error] {e}", flush=True)
        return
    if not text:
        return

    with seq_lock:
        seq += 1
        sid = seq
    print(f"[ASR] {text}", flush=True)

    # 先推原文（原文秒出），译文稍后补上
    await broadcast({"type": "asr", "id": sid, "original": text, "translated": ""})

    room_id = config.get("room_id")
    session_id = current_session_id
    uname = live_info.get("uname", "")

    # 截取音频落盘
    audio_file = streamer_profile.save_audio(room_id, session_id, sid, seg)

    translated = ""
    key = config.get("api_key", "")
    if key:
        async with asr_server.TRANSLATE_LOCK:
            try:
                translated = await asyncio.to_thread(
                    asr_server.translate_deepseek, text, key, room_id
                )
                print(f"[TR] {translated}", flush=True)
            except Exception as e:  # noqa: BLE001
                translated = f"[翻译失败: {e}]"
                print(f"[tr error] {e}", flush=True)

    # 落盘：原文 + 译文 + 音频引用，供后续喂 AI 做主播分析
    streamer_profile.save_transcript(
        room_id, session_id, uname, text, translated, audio_file
    )

    final = {
        "type": "asr",
        "id": sid,
        "original": text,
        "translated": translated,
        "done": True,
    }
    # 记录到历史缓冲，供断线重连的客户端回放
    subtitle_history.append(
        {"id": sid, "original": text, "translated": translated, "done": True}
    )
    await broadcast(final)


async def segment_worker():
    """串行消费音频片段：保证字幕顺序、避免 GPU 并发争抢。"""
    while True:
        seg = await seg_queue.get()
        try:
            await handle_segment(seg)
        except Exception as e:  # noqa: BLE001
            print(f"[worker error] {e}", flush=True)


def enqueue_segment(loop, seg):
    """从抓流线程把片段交给事件循环的消费队列（线程安全）。"""

    def _put():
        if seg_queue.qsize() >= MAX_SEG_QUEUE:
            try:
                seg_queue.get_nowait()  # 丢弃最旧，防止延迟越拉越大
            except Exception:
                pass
        seg_queue.put_nowait(seg)

    loop.call_soon_threadsafe(_put)


# --------------------------------------------------------------------------- #
# 抓流线程
# --------------------------------------------------------------------------- #
def capture_thread(room_id, loop, stop_event):
    """阻塞式线程：取播放地址 → 解码音频 → 重采样 → VAD 切句 → 交给识别队列。

    stop_event 是本次抓流的专属停止信号，主线程只 set 事件、绝不跨线程 close 容器，
    容器始终由本线程在 finally 里关闭，避免 ffmpeg 被别的线程 close 导致进程崩溃。
    """
    global _current_container
    try:
        import av  # noqa: F401  惰性导入：抓流时才需要 PyAV
    except ImportError as e:
        print("[capture] 缺少 PyAV（av），无法抓流。请先安装：pip install av", flush=True)
        raise e
    vad = asr_server.VadState()
    vad.threshold = vad_threshold
    print("[capture] 开始抓取直播音频", flush=True)

    while not stop_event.is_set():
        container = None
        try:
            url = fetch_play_url(room_id)
            container = av.open(
                url,
                options={
                    "user_agent": UA,
                    "referer": REFERER,
                    "rw_timeout": "10000000",      # 10 秒读超时，流断了能感知
                    "reconnect": "1",
                    "reconnect_streamed": "1",
                    "reconnect_delay_max": "5",
                },
            )
            with _container_lock:
                _current_container = container
            astream = next(s for s in container.streams if s.type == "audio")
            resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
            print("[capture] 直播流已打开", flush=True)

            for frame in container.decode(astream):
                if stop_event.is_set():
                    break
                for out in resampler.resample(frame):
                    arr = out.to_ndarray().reshape(-1)
                    seg = vad.process(arr.astype(np.float32, copy=False))
                    if seg is not None:
                        enqueue_segment(loop, seg)
        except Exception as e:  # noqa: BLE001
            if not stop_event.is_set():
                print(f"[capture] 中断，3 秒后重连：{e}", flush=True)
                time.sleep(3)
        finally:
            if container is not None:
                try:
                    container.close()  # 本线程内关闭，安全
                except Exception:
                    pass
            with _container_lock:
                if _current_container is container:
                    _current_container = None

    print("[capture] 已停止", flush=True)


def start_capture(room_id, loop):
    global capture_active, _capture_stop, _capture_thread, current_session_id
    if capture_active:
        return
    capture_active = True
    current_session_id = streamer_profile.new_session_id()
    _capture_stop = threading.Event()
    t = threading.Thread(
        target=capture_thread,
        args=(room_id, loop, _capture_stop),
        daemon=True,
        name="bili-capture",
    )
    _capture_thread = t
    t.start()


def stop_capture():
    """请求停止抓流：只 set 当前线程的停止事件，不做任何跨线程 close。"""
    global capture_active
    if not capture_active:
        return
    capture_active = False
    ev = _capture_stop
    if ev is not None:
        ev.set()


# --------------------------------------------------------------------------- #
# 监测循环
# --------------------------------------------------------------------------- #
async def check_room(loop):
    """拉取当前房间状态并据此启停抓流（监测循环和切换房间后都调用它）。"""
    global live_info
    room_id = config.get("room_id")
    info = await asyncio.to_thread(fetch_room_info, room_id)

    if info is None:
        await broadcast(status_message())
        return

    live_info.update(info)
    if info["live"] and not capture_active:
        print(f"[monitor] 检测到开播：{info['title']}", flush=True)
        start_capture(room_id, loop)
    elif not info["live"] and capture_active:
        print("[monitor] 检测到下播，停止抓流", flush=True)
        stop_capture()
    await broadcast(status_message())


async def monitor_loop(loop):
    while True:
        await check_room(loop)
        await asyncio.sleep(POLL_INTERVAL)


# --------------------------------------------------------------------------- #
# HTTP / WebSocket 服务
# --------------------------------------------------------------------------- #
_index_html = b""
lan_url = "http://127.0.0.1:8000"


def load_index_html():
    global _index_html
    try:
        with open(WEB_INDEX, "r", encoding="utf-8") as f:
            _index_html = f.read().encode("utf-8")
    except FileNotFoundError:
        _index_html = (
            "<html><body><h1>字幕页面缺失</h1>"
            "<p>请确认 server/web/index.html 存在。</p></body></html>"
        ).encode("utf-8")


def process_request(connection, request):
    # WebSocket 升级请求：放行交给 websockets 握手
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    path = request.path
    if path in ("/", "/index.html"):
        return Response(
            200,
            "OK",
            Headers({"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"}),
            _index_html,
        )
    if path == "/health":
        body = json.dumps(
            {
                "ok": True,
                "live": live_info.get("live", False),
                "room_id": config.get("room_id"),
                "model_ready": model_ready,
                "capturing": capture_active,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return Response(
            200,
            "OK",
            Headers({"Content-Type": "application/json; charset=utf-8"}),
            body,
        )
    return Response(
        404,
        "Not Found",
        Headers({"Content-Type": "text/plain; charset=utf-8"}),
        b"not found",
    )


async def ws_handler(ws):
    clients.add(ws)
    peer = getattr(ws, "remote_address", "?")
    print(f"[web] 客户端接入：{peer}（共 {len(clients)} 个）", flush=True)
    try:
        await ws.send(json.dumps(status_message(), ensure_ascii=False))
        # 回放断线期间错过的字幕历史（新进/重连的客户端都能补上）
        await ws.send(
            json.dumps(
                {"type": "history", "items": list(subtitle_history)},
                ensure_ascii=False,
            )
        )
        async for message in ws:
            try:
                data = json.loads(message)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            t = data.get("type")
            if t == "setKey":
                config["api_key"] = (data.get("apiKey") or "").strip()
                save_config()
                print("[setKey]", "ok" if config["api_key"] else "(清空)", flush=True)
                await broadcast(status_message())
            elif t == "setRoom":
                rid = str(data.get("roomId") or "").strip()
                if rid.isdigit():
                    config["room_id"] = int(rid)
                    save_config()
                    # 换房间：先停旧抓流，再立即按新房间重新判断（不等下轮监测）
                    stop_capture()
                    live_info.update({"live": False, "title": "", "uname": "", "face": ""})
                    print(f"[setRoom] 切换到房间 {rid}", flush=True)
                    await check_room(asyncio.get_running_loop())
            elif t == "getStatus":
                await ws.send(json.dumps(status_message(), ensure_ascii=False))
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        clients.discard(ws)
        print(f"[web] 客户端断开：{peer}", flush=True)


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def set_model_ready(engine, model, device):
    """由统一入口（main.py）在模型加载完成后调用，标记模型可用并广播状态。"""
    global model_ready, engine_name, model_name, device_name
    engine_name = engine
    model_name = model
    device_name = device
    model_ready = True
    print(f"[model] ready: {engine} {model} on {device}", flush=True)


async def _load_presets_bg():
    """后台拉取预设主播头像，完成后广播最新状态。"""
    await asyncio.to_thread(fetch_preset_faces)
    await broadcast(status_message())


def parse_args():
    p = argparse.ArgumentParser(description="B 站直播间监测 + 实时翻译 Web 服务")
    p.add_argument("--room", type=int, default=None, help="直播间号（默认读 config.json）")
    p.add_argument("--host", default="0.0.0.0", help="监听地址（手机访问需 0.0.0.0）")
    p.add_argument("--port", type=int, default=8000, help="服务端口")
    p.add_argument("--engine", default="whisper", choices=["whisper", "sensevoice"])
    p.add_argument("--model", default="kotoba", help="whisper 模型：kotoba | Systran/faster-whisper-large-v3 | 本地路径")
    p.add_argument("--device", default="auto", help="auto | cpu | cuda")
    p.add_argument("--vad-threshold", type=float, default=0.5, help="VAD 灵敏度（0~1，越小越灵敏；字幕不出现可降到 0.3）")
    p.add_argument("--poll-interval", type=int, default=10, help="轮询直播间开播状态的间隔秒数（默认 10，怕 403 可调到 15~30）")
    return p.parse_args()


async def run_web(args):
    """启动 Web 端（手机网页字幕）。模型需由调用方先加载，再调 set_model_ready() 标记就绪。

    args 需含：host / port / room / vad_threshold / poll_interval / engine / model。
    """
    global vad_threshold, lan_url, POLL_INTERVAL
    load_config(args)
    save_config()  # 启动即生成 config.json，方便用户直接编辑文件填 API Key

    vad_threshold = args.vad_threshold
    POLL_INTERVAL = max(3, args.poll_interval)

    load_index_html()
    loop = asyncio.get_running_loop()

    async with serve(
        ws_handler,
        args.host,
        args.port,
        process_request=process_request,
        max_size=2 ** 24,
        ping_interval=20,
    ):
        lan = _lan_ip()
        lan_url = f"http://{lan}:{args.port}/"
        print("=" * 60, flush=True)
        print(f"[web] listening http://{args.host}:{args.port}", flush=True)
        print(f"  电脑本机访问：http://127.0.0.1:{args.port}", flush=True)
        print(f"  手机访问：    http://{lan}:{args.port}", flush=True)
        print(f"  监测直播间：  {config['room_id']}", flush=True)
        print("  手机浏览器打开上面地址即可看实时字幕；在页面设置里填 DeepSeek API Key。", flush=True)
        print("  提示：若手机打不开，请在 Windows 防火墙放行本端口（或临时关防火墙）。", flush=True)
        print(f"  字幕日志：    {streamer_profile.LOGS_DIR}", flush=True)
        print("=" * 60, flush=True)

        asyncio.create_task(monitor_loop(loop))
        asyncio.create_task(segment_worker())
        asyncio.create_task(_load_presets_bg())

        await asyncio.Future()


async def main():
    """独立运行入口（向后兼容）：只跑 Web 端。统一入口请用 main.py。"""
    args = parse_args()
    device = args.device
    if device == "auto":
        device = asr_server._detect_device(args.engine)

    asr_server.load_model(device, args.engine, args.model)
    set_model_ready(args.engine, args.model, device)
    await run_web(args)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        stop_capture()
        sys.exit(0)
