#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线测试 faster-whisper：加载模型并识别一个音频文件。"""
import argparse
import os
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
ASCII_BASE = os.path.join(_LOCALAPPDATA, "LiveTranslate")
os.environ.setdefault("HF_HOME", os.path.join(ASCII_BASE, "huggingface"))
os.environ.setdefault("HF_HUB_CACHE", os.path.join(ASCII_BASE, "huggingface", "hub"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def load_audio(path):
    import soundfile as sf
    from scipy.signal import resample_poly

    data, sr = sf.read(path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 16000:
        data = resample_poly(data, 16000, sr)
    return data.astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    from faster_whisper import WhisperModel

    compute = "float16" if args.device.startswith("cuda") else "int8"
    print(f"loading {args.model} on {args.device} ({compute}) ...", flush=True)
    t = time.time()
    model = WhisperModel(args.model, device=args.device, compute_type=compute)
    print(f"model ready in {time.time()-t:.1f}s", flush=True)

    audio = load_audio(args.audio)
    print(f"audio {len(audio)/16000:.2f}s", flush=True)
    t = time.time()
    segments, info = model.transcribe(
        audio,
        language="ja",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        temperature=0.0,
    )
    text = "".join(s.text for s in segments).strip()
    dt = time.time() - t
    print(f"transcribe in {dt:.2f}s (rtf={dt/(len(audio)/16000):.3f})", flush=True)
    print("TEXT:", text, flush=True)
    out = os.path.join(ROOT, "whisper_result.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print("saved:", out, flush=True)


if __name__ == "__main__":
    main()
