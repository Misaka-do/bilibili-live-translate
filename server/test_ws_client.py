#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟浏览器插件：把音频文件按 128ms 切成块，通过 WebSocket 发给服务器，打印识别结果。"""
import argparse
import asyncio
import json
import os
import re

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))


def load_audio(path):
    import soundfile as sf
    from scipy.signal import resample_poly

    data, sr = sf.read(path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 16000:
        data = resample_poly(data, 16000, sr)
    return data.astype(np.float32)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--url", default="ws://127.0.0.1:8765")
    args = ap.parse_args()

    import websockets

    audio = load_audio(args.audio)
    chunk = 2048  # 128ms
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)

    async with websockets.connect(args.url, max_size=2 ** 24) as ws:
        async def reader():
            async for msg in ws:
                if isinstance(msg, str):
                    d = json.loads(msg)
                    if d.get("type") == "asr":
                        print("[ASR]", d.get("text"))
                    elif d.get("type") == "ready":
                        print("[ready]")
                    elif d.get("type") == "error":
                        print("[error]", d.get("message"))

        rt = asyncio.create_task(reader())
        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i : i + chunk].tobytes())
            await asyncio.sleep(0.12)  # 模拟实时速率
        # 尾部补静音，让 VAD 把最后一句切出来
        for _ in range(10):
            await ws.send(np.zeros(chunk, dtype=np.int16).tobytes())
            await asyncio.sleep(0.12)
        await asyncio.sleep(3)
        rt.cancel()


if __name__ == "__main__":
    asyncio.run(main())
