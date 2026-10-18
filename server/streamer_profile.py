#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主播画像 + 日志落盘（共享模块）。

每个主播一份「画像」profile（术语 / 梗 / 高频听错 / 说话风格），翻译时把画像注入提示词，
让不同主播用各自专属的术语和梗；同时把识别到的原文 + 译文 + 截取音频按主播分别落盘，
形成可长期积累、可喂 AI 做主播分析的数据。

目录结构：
    profiles/<room_id>.json                 # 主播画像
    logs/room_<room_id>/transcript.jsonl    # 原文 + 译文 + 音频引用
    logs/room_<room_id>/audio/<file>.wav    # 每段截取的音频（16kHz 单声道）
"""
import json
import os
import sys
import threading
import time
import wave

import numpy as np
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _is_frozen():
    """是否运行在 PyInstaller 打包后的环境。"""
    return getattr(sys, "frozen", False)


# 运行时数据根目录（可写的 profiles/logs/config 放这里）：
#   源码运行 = 项目根目录；打包运行 = exe 所在目录（用户解压后的文件夹）。
APP_ROOT = os.path.dirname(sys.executable) if _is_frozen() else ROOT

# 只读资源目录（web/index.html 等随包分发的数据）：
#   源码运行 = server 目录；打包运行 = PyInstaller 解压的临时目录 _MEIPASS。
BUNDLE_DIR = getattr(sys, "_MEIPASS", HERE) if _is_frozen() else HERE

PROFILES_DIR = os.path.join(APP_ROOT, "profiles")
LOGS_DIR = os.path.join(APP_ROOT, "logs")

# 本程序的共享 HTTP 会话：显式关闭代理（trust_env=False）。
# DeepSeek（api.deepseek.com）与 B站（api.live.bilibili.com）都是国内直连接口，
# 不应受 Windows「系统代理」或 HTTP_PROXY 环境变量影响（否则代理软件一关就 ProxyError）。
# 注意：这只是本程序自己的会话，不改动 requests 库本身。
HTTP_SESSION = requests.Session()
HTTP_SESSION.trust_env = False

# 基础提示词：所有主播通用（规则 + B 站通用术语），不含任何主播专属词/梗。
BASE_PROMPT = (
    "你是资深日语同传口译员，负责把日本虚拟主播（VTuber）在 B 站直播时的日语实时口译成简体中文。\n"
    "\n"
    "【B 站通用术语 · 所有 B 站直播间都适用，近音变体一并识别】\n"
    "- スパチャ／スパチ／スバチャ／すばちゃ／スパちゃん／スバちゃん → SC／打赏（SuperChat 投喂，勿翻成「舰长」或「礼物」）\n"
    "- 艦長／館長／監聴／関長 → 舰长（B 站付费粉丝头衔）\n"
    "- 総督 → 总督、提督 → 提督（比舰长更高档的粉丝头衔）\n"
    "- アカウント／子豚／小豚 → 小号（一人多账号，不是「账户金额」）\n"
    "- 隠れてこっそり乗る → 匿名／隐藏上舰（观众隐藏身份打赏，主播看不到名字）\n"
    "\n"
    "【要求】\n"
    "1. 翻成自然、口语化的中文，像真人直播口译一样，不要翻译腔、不要书面语。\n"
    "2. 保留语气和情绪；语气词和网络用语要贴切（「まじで」→「真的/讲真」，「やばい」→「绝了/太强了」，「うん」→「嗯」）。\n"
    "3. 识别常有近音听错，结合语境纠正成合理意思，不要直译奇怪字面（如「持ってる」听成「乗ってる」）。\n"
    "4. 同一专有名词整场翻译要统一；数字与数量（6000、677、1隻、1席）保留原数字。\n"
    "5. 只输出译文本身，不要解释、拼音、原文或引号。\n"
    "6. 输入只是语气词、噪声、单个字母或乱码时，输出「…」。"
)


def load_profile(room_id):
    """读取某主播画像，不存在或读取失败返回 None。"""
    if room_id is None:
        return None
    path = os.path.join(PROFILES_DIR, f"{room_id}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def list_profiles():
    """扫描 profiles/ 目录，返回所有主播画像的 [{room_id, name, face}, ...]。

    供 Web 端「预设主播」下拉列表使用：往 profiles/ 丢一个画像文件，重启后列表里就多一个主播，
    无需改代码、无需手动输房间号。按 room_id 升序排列。
    """
    result = []
    if not os.path.isdir(PROFILES_DIR):
        return result
    try:
        names = sorted(os.listdir(PROFILES_DIR))
    except OSError:
        return result
    for fn in names:
        if not fn.endswith(".json"):
            continue
        path = os.path.join(PROFILES_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        rid = data.get("room_id")
        name = (data.get("name") or "").strip()
        if rid is None and fn[:-5].isdigit():
            rid = int(fn[:-5])  # 兜底：文件名就是房间号
        if rid is None:
            continue
        result.append(
            {
                "room_id": rid,
                "name": name or str(rid),
                "face": (data.get("face") or "").strip(),
            }
        )
    # 按 room_id 排序
    try:
        result.sort(key=lambda x: (str(x["room_id"]), x["room_id"]))
    except TypeError:
        pass
    return result


def render_profile(profile):
    """把画像里的术语/梗/听错词/风格渲染成提示词片段。"""
    lines = []

    glossary = profile.get("glossary") or []
    if glossary:
        lines.append("【本主播术语】")
        for g in glossary:
            heard = (g.get("heard") or g.get("jp") or "").strip()
            say = (g.get("say") or g.get("zh") or "").strip()
            note = (g.get("note") or "").strip()
            if heard and say:
                line = f"- {heard} → {say}"
                if note:
                    line += f"（{note}）"
                lines.append(line)

    memes = profile.get("memes") or []
    if memes:
        lines.append("【本主播梗/黑话】")
        for m in memes:
            if str(m).strip():
                lines.append(f"- {str(m).strip()}")

    fixes = profile.get("asr_fixes") or []
    if fixes:
        lines.append("【本主播高频听错词 · 结合语境纠正】")
        for f in fixes:
            heard = (f.get("heard") or "").strip()
            correct = (f.get("correct") or "").strip()
            if heard and correct:
                lines.append(f"- {heard} → 常是「{correct}」听错")

    rules = profile.get("rules") or []
    if rules:
        lines.append("【本主播附加规则】")
        for r in rules:
            if str(r).strip():
                lines.append(f"- {str(r).strip()}")

    style = (profile.get("style") or "").strip()
    if style:
        lines.append(f"本主播特点：{style}")

    return "\n".join(lines)


_prompt_cache = {}


def build_prompt(room_id):
    """基础提示词 + 当前主播画像，组成最终翻译提示词（带缓存，画像变更需重启生效）。"""
    key = str(room_id) if room_id is not None else "__base__"
    if key in _prompt_cache:
        return _prompt_cache[key]
    profile = load_profile(room_id) if room_id is not None else None
    extra = render_profile(profile) if profile else ""
    result = (BASE_PROMPT + "\n\n" + extra) if extra else BASE_PROMPT
    _prompt_cache[key] = result
    return result


def _room_dir(room_id):
    return os.path.join(LOGS_DIR, f"room_{_norm_room_id(room_id)}")


def _norm_room_id(room_id):
    """把 room_id 归一化成 int（web 服务传 int、插件传 str，统一后便于后续 AI 按房间聚合）。"""
    try:
        return int(room_id)
    except (TypeError, ValueError):
        return room_id


_session_counter = 0
_session_lock = threading.Lock()


def new_session_id():
    """生成进程内唯一会话 id（时间戳 + 递增计数器，彻底避免重连/多会话撞 id）。"""
    global _session_counter
    with _session_lock:
        _session_counter += 1
        n = _session_counter
    return time.strftime("%Y%m%d_%H%M%S") + f"_{n:04d}"


def save_audio(room_id, session_id, seq, seg):
    """把一段 16kHz 单声道 float32 音频存成 WAV，返回相对文件名（失败返回 None）。"""
    try:
        audio_dir = os.path.join(_room_dir(room_id), "audio")
        os.makedirs(audio_dir, exist_ok=True)
        fname = f"{session_id}_{seq:05d}.wav"
        path = os.path.join(audio_dir, fname)
        pcm = (np.clip(seg, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm.tobytes())
        return f"audio/{fname}"
    except Exception as e:  # noqa: BLE001
        print(f"[log] 保存音频失败：{e}", flush=True)
        return None


def save_transcript(room_id, session_id, uname, original, translated, audio_file=None):
    """追加一条字幕日志到 logs/room_<id>/transcript.jsonl。"""
    try:
        d = _room_dir(room_id)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "transcript.jsonl")
        line = json.dumps(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "session": session_id,
                "room_id": _norm_room_id(room_id),
                "uname": uname or "",
                "original": original,
                "translated": translated,
                "audio": audio_file or "",
            },
            ensure_ascii=False,
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except Exception as e:  # noqa: BLE001
        print(f"[log] 保存字幕失败：{e}", flush=True)
