#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线测试：加载 SenseVoice 并识别一个音频文件。"""
import argparse
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
ASCII_BASE = os.path.join(_LOCALAPPDATA, "LiveTranslate")
os.environ.setdefault("MODELSCOPE_CACHE", os.path.join(ASCII_BASE, "modelscope"))
os.environ.setdefault("MODELSCOPE_HOME", os.path.join(ASCII_BASE, "modelscope_home"))
os.environ.setdefault("HF_HOME", os.path.join(ASCII_BASE, "huggingface"))
os.environ.setdefault("HF_HUB_CACHE", os.path.join(ASCII_BASE, "huggingface", "hub"))
os.environ.setdefault("TORCH_HOME", os.path.join(ASCII_BASE, "torch"))

_TAG_RE = re.compile(r"<\|[^|]*\|>")


def clean_text(text):
    text = _TAG_RE.sub("", text or "")
    return text.replace("[EMO]", "").replace("</s>", "").replace("<s>", "").strip()


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
    ap.add_argument("audio", help="音频文件路径（wav/mp3/flac）")
    ap.add_argument("--device", default="auto")
    args = ap.parse_args()

    device = args.device
    if device == "auto":
        import torch

        device = "cuda:0" if torch.cuda.is_available() else "cpu"

    from funasr import AutoModel

    print(f"loading SenseVoiceSmall on {device} ...", flush=True)
    model = AutoModel(
        model="iic/SenseVoiceSmall",
        device=device,
        disable_update=True,
    )
    print("model ready", flush=True)

    audio = load_audio(args.audio)
    print(f"audio {len(audio)/16000:.2f}s", flush=True)
    res = model.generate(input=audio, language="auto", use_itn=True)
    print("raw:", res, flush=True)
    text = clean_text((res[0] if res else {}).get("text", ""))
    print("TEXT:", text, flush=True)


if __name__ == "__main__":
    main()
