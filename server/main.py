#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一服务入口：一个进程同时服务「浏览器插件」和「手机 Web」两端。

底层（模型 / VAD / 识别 / 翻译 / 主播画像 / 日志）只加载一次，两端共享：
  - 插件端：ws://<plugin-host>:<plugin-port>（8765）——浏览器插件采集标签页音频连这里
  - Web 端 ：http://<web-host>:<web-port>（8000）——手机/浏览器网页看字幕，电脑抓流

用法：
    python main.py                     # 默认两端都开
    python main.py --no-web            # 只开插件端
    python main.py --no-plugin         # 只开 Web 端
    python main.py [--engine whisper] [--model kotoba] [--device auto]
                   [--plugin-host 127.0.0.1] [--plugin-port 8765]
                   [--web-host 0.0.0.0] [--web-port 8000]
                   [--room 12345678] [--vad-threshold 0.5] [--poll-interval 10]
                   [--debug]
"""
import argparse
import asyncio
import sys
import types

HERE = sys.path[0]
import os  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

import asr_server  # noqa: E402
import live_web  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(
        description="B站直播实时翻译统一服务（插件端 + Web 端）"
    )
    # 识别引擎（两端共享）
    p.add_argument("--engine", default="whisper", choices=["whisper", "sensevoice"],
                   help="识别引擎：whisper（默认，日语更准）| sensevoice")
    p.add_argument("--model", default="kotoba",
                   help="whisper 模型：kotoba（日语专用，需手动下载）| Systran/faster-whisper-large-v3 | 本地路径")
    p.add_argument("--device", default="auto", help="auto | cpu | cuda")

    # 插件端
    p.add_argument("--plugin-host", default="127.0.0.1", help="插件端监听地址")
    p.add_argument("--plugin-port", type=int, default=8765, help="插件端端口（浏览器插件连这里）")

    # Web 端
    p.add_argument("--web-host", default="0.0.0.0", help="Web 端监听地址（手机访问需 0.0.0.0）")
    p.add_argument("--web-port", type=int, default=8000, help="Web 端端口（手机浏览器开这里）")
    p.add_argument("--room", type=int, default=None, help="直播间号（Web 端默认读 config.json）")
    p.add_argument("--vad-threshold", type=float, default=0.5,
                   help="VAD 灵敏度（0~1，越小越灵敏；字幕不出现可降到 0.3）")
    p.add_argument("--poll-interval", type=int, default=10,
                   help="Web 端轮询直播间开播状态的间隔秒数（默认 10）")

    # 开关
    p.add_argument("--no-web", action="store_true", help="关闭 Web 端，只开插件端")
    p.add_argument("--no-plugin", action="store_true", help="关闭插件端，只开 Web 端")
    p.add_argument("--debug", action="store_true", help="打开诊断日志")
    return p.parse_args()


async def main():
    args = parse_args()
    if args.debug:
        asr_server.DEBUG_AUDIO_LOG = True

    # 设备自动检测
    device = args.device
    if device == "auto":
        device = asr_server._detect_device(args.engine)

    if args.no_web and args.no_plugin:
        print("[error] --no-web 与 --no-plugin 不能同时开启，至少开一个端口。", flush=True)
        sys.exit(1)

    # 加载模型一次（两端共享）
    print("=" * 60, flush=True)
    print("[loading] 加载识别模型（两端共享）...", flush=True)
    asr_server.load_model(device, args.engine, args.model)
    live_web.set_model_ready(args.engine, args.model, device)
    print("[loading] done", flush=True)
    print("=" * 60, flush=True)

    tasks = []

    # 插件端
    if not args.no_plugin:
        tasks.append(asr_server.run_plugin(args.plugin_host, args.plugin_port))

    # Web 端
    if not args.no_web:
        # 构造 live_web.run_web 需要的 args（复用它的配置读取逻辑）
        web_args = types.SimpleNamespace(
            room=args.room,
            host=args.web_host,
            port=args.web_port,
            engine=args.engine,
            model=args.model,
            device=args.device,
            vad_threshold=args.vad_threshold,
            poll_interval=args.poll_interval,
        )
        tasks.append(live_web.run_web(web_args))

    # 两个端口同时运行，任一被取消则整体退出
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        live_web.stop_capture()
        sys.exit(0)
