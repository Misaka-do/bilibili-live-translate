# B站实时翻译（直播 / 视频 / 番剧）

一个把 B 站日语直播、视频、番剧**实时翻译成中文字幕**的本地工具。

- 🎙️ **本地语音识别**：faster-whisper（GPU 加速），日语专用模型可选
- 🧠 **Silero VAD 切句**：神经网络判定人声，过滤背景音乐
- 🌐 **DeepSeek 翻译**：口语化中文，带 B 站直播语境（舰长 / 小号 / SC）
- 🪟 **双端可用**：浏览器插件（字幕叠在视频上）或手机网页（电脑抓流推字幕）

> **作者**：Misaka-do ｜ **AI 协作**：DeepSeek V4 Pro ｜ **开发工具**：DeepSeek Harness

---

## 架构

一个本地服务 + 两个入口，模型只加载一次：

```
                  ┌──────────────────────────────────────┐
                  │        本地服务  main.py             │
┌──────────────┐  │  ┌────────────────────────────────┐  │
│  浏览器插件   │──┼─▶│ 插件端 :8765                    │  │
│  (采集标签页) │◀─┼──│ 收 16kHz PCM → VAD → 识别 → 翻译 │  │
└──────────────┘  │  └────────────────────────────────┘  │
                  │  ┌────────────────────────────────┐  │
┌──────────────┐  │  │ Web 端 :8000                    │  │
│   手机网页    │◀─┼──│ 抓直播流 → VAD → 识别 → 翻译     │  │
└──────────────┘  │  └────────────────────────────────┘  │
                  │            共享：模型 / VAD / 提示词   │
                  └───────────────────┬──────────────────┘
                                      │ 翻译
                                      ▼
                              DeepSeek API
```

---

## 快速开始

### 1. 准备 Python

下载并安装 **Python 3.12**（3.10+ 均可）：<https://www.python.org/downloads/>

> ⚠️ 安装时**务必勾选「Add Python to PATH」**
> 国内下载慢可用镜像：<https://mirrors.huaweicloud.com/python/>

### 2. 安装依赖

```bash
# 在项目目录下创建虚拟环境
python -m venv venv
venv\Scripts\activate

# 安装依赖（国内建议加清华镜像）
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

需要抓 B 站直播流（手机端）的话，额外装：

```bash
pip install av -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3. 准备模型

**最省事**：什么都不做，首次启动会自动从国内源（ModelScope）下载通用 Whisper（约 3GB）。

**更准（推荐）**：手动下载日语专用模型 kotoba-whisper（约 1.5GB），详见 [模型下载](#模型下载)。

### 4. 启动服务

双击 **`start_server.bat`**，看到下面这行就成功了：

```
[plugin] listening ws://127.0.0.1:8765
[web] listening http://0.0.0.0:8000
```

保持窗口开着（关掉即停止服务）。

### 5. 开始翻译

**在电脑上看** → 装插件（[方式 A](#方式-a浏览器插件)），点「开始翻译」。

**在手机上看** → 手机浏览器打开 `http://电脑IP:8000`（[方式 B](#方式-b手机网页)）。

---

## 使用方式

### 方式 A：浏览器插件

**安装插件**（只需一次）：

1. Chrome 地址栏输入 `chrome://extensions`（Edge 用 `edge://extensions`）
2. 打开右上角「**开发者模式**」
3. 点「**加载已解压的扩展程序**」，选择本项目的 **`extension`** 文件夹

**使用**：

1. 确认本地服务已启动（`start_server.bat` 窗口开着）
2. 打开 B 站直播页 / 视频页 / 番剧页
3. 点插件图标 → 填入 **DeepSeek API Key** → 保存
4. 点「**开始翻译**」→ 视频上出现字幕条（可拖拽移动）
5. 点「**停止**」结束

> API Key 获取：<https://platform.deepseek.com> → API Keys → 创建，得到 `sk-` 开头的字符串。

### 方式 B：手机网页

适合「手机看直播、电脑跑翻译」：电脑自己抓 B 站直播流的音频，把字幕推到手机网页。

1. 双击 `start_server.bat`（只想开 Web 端、不开插件端，可加 `--no-plugin` 参数）
2. 控制台会打印手机访问地址，形如 `http://192.168.x.x:8000`
3. **手机和电脑连同一 Wi-Fi**，手机浏览器打开该地址
4. 点右上角「**设置**」→ 填入 DeepSeek API Key + 直播间号（或从下拉列表选主播）→ 保存
5. 等主播开播，页面自动开始滚动字幕；觉得检测慢可点「**检测开播**」立即检查

> 手机打不开页面？多半是防火墙拦了 8000 端口，双击 **`add_firewall_rule.bat`** 一键放行。

#### 公网访问（Cloudflare Tunnel 等）

想把字幕分享给外网朋友，可以用 Cloudflare Tunnel 把本机 8000 端口映射出去。

**默认公网只能看字幕，不能改设置**（不会暴露 API Key、也不能乱切直播间）。管理权限分两种：

| 来源 | 管理权限 |
|------|---------|
| 内网直连（`10.0.0.0/24`） | ✅ 免令牌，设置全开 |
| 公网 / 外部 | ❌ 只读（默认） |
| 公网 + URL 带令牌 | ✅ 设置全开 |

启用公网管理：在 `config.json` 里加一行 `admin_token`（**不要提交到仓库**，该文件已被 gitignore）：

```json
{ "admin_token": "你自己的一串随机字符" }
```

之后用 `https://你的域名/?admin=你的令牌` 访问即可管理；不带令牌则为只读。

> ⚠️ `10.0.0.0/24` 这个「内网免令牌」网段可在 `server/live_web.py` 的 `LAN_NETWORKS` 里按需修改。
> 注意 Cloudflare Tunnel 跑在本机，公网流量对端 IP 恒为 `127.0.0.1`，所以**不要**把回环地址加进放行列表。

---

## 模型下载

模型不随仓库提供，放在 `%LOCALAPPDATA%\LiveTranslate\`（**纯英文路径**，sentencepiece 等 C++ 库无法处理中文路径）。

### kotoba-whisper（日语专用，推荐）

精度更高，专为日语口语微调。下载 **5 个文件**到
`%LOCALAPPDATA%\LiveTranslate\models\kotoba-whisper-v2.0-faster\`：

| 文件 | 大小 | 下载 |
|------|------|------|
| `model.bin` | ~1.5GB | [HuggingFace](https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0-faster/resolve/main/model.bin) ｜ [国内镜像](https://hf-mirror.com/kotoba-tech/kotoba-whisper-v2.0-faster/resolve/main/model.bin) |
| `config.json` | ~1KB | [HuggingFace](https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0-faster/resolve/main/config.json) ｜ [国内镜像](https://hf-mirror.com/kotoba-tech/kotoba-whisper-v2.0-faster/resolve/main/config.json) |
| `tokenizer.json` | ~2.4MB | [HuggingFace](https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0-faster/resolve/main/tokenizer.json) ｜ [国内镜像](https://hf-mirror.com/kotoba-tech/kotoba-whisper-v2.0-faster/resolve/main/tokenizer.json) |
| `vocabulary.json` | ~1MB | [HuggingFace](https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0-faster/resolve/main/vocabulary.json) ｜ [国内镜像](https://hf-mirror.com/kotoba-tech/kotoba-whisper-v2.0-faster/resolve/main/vocabulary.json) |
| `preprocessor_config.json` | ~1KB | [HuggingFace](https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0-faster/resolve/main/preprocessor_config.json) ｜ [国内镜像](https://hf-mirror.com/kotoba-tech/kotoba-whisper-v2.0-faster/resolve/main/preprocessor_config.json) |

> 也可从 ModelScope 整包下载：<https://www.modelscope.cn/models/kotoba-tech/kotoba-whisper-v2.0-faster>

### faster-whisper-large-v3（通用，自动下载）

**无需手动操作**，没有 kotoba 时首次启动会自动从 ModelScope 下载（约 3GB）。
手动地址：[ModelScope](https://www.modelscope.cn/models/Systran/faster-whisper-large-v3) ｜ [HuggingFace](https://huggingface.co/Systran/faster-whisper-large-v3)

### 其他模型

- **Silero VAD**：`pip install silero-vad` 自带模型，无需手动下载
- **SenseVoice**（可选引擎）：装 `funasr` 后自动从 ModelScope 下载

---

## 主播画像

不同主播有各自的术语、梗、高频听错词。翻译提示词分两层：

- **基础提示词**（`server/streamer_profile.py` 的 `BASE_PROMPT`）：所有主播通用规则 + B 站通用术语（SC / 舰长 / 小号）
- **主播画像**（`profiles/<房间号>.json`）：该主播专属的术语、梗、听错词、说话风格

翻译时自动拼接两层，**换主播不用改代码**。

### 用现成示例

仓库自带一份实战画像 `profiles.example/22748536.json`（B 站主播「火羽ひのめ」，
含 20 条术语 + 3 个梗 + 8 组听错词），直接复制即可用：

```bash
mkdir profiles
copy profiles.example\22748536.json profiles\
```

重启后 Web 端「选择直播间」下拉列表会自动出现该主播。

### 画像格式

```json
{
  "room_id": 123456,
  "name": "某主播",
  "face": "https://...（可选，头像 URL）",
  "style": "爱聊 FF14 和动漫、语气活泼",
  "glossary": [{"heard": "おば／オバ", "say": "阿姨", "note": "B站官方梗"}],
  "memes": ["某梗的说明"],
  "asr_fixes": [{"heard": "指針／感情", "correct": "艦長（舰长）"}],
  "rules": ["附加规则（自由文本）"]
}
```

| 字段 | 说明 |
|------|------|
| `room_id` | 直播间号（必填，决定与哪个房间对应） |
| `name` | 主播名（Web 端下拉列表显示） |
| `glossary` | 术语表：听到的日语 → 应翻译成 |
| `asr_fixes` | 高频听错词纠正：识别错的 → 实际意思 |
| `memes` / `rules` / `style` / `face` | 可选，梗、附加规则、风格描述、头像 |

- 没有画像的主播自动退化为只用基础提示词
- 画像改动后需**重启服务**生效
- `profiles.example/` 随仓库公开；`profiles/` 是你私人的，不进仓库

---

## 启动参数

`start_server.bat` 可加参数（直接编辑 bat 或命令行传入）：

| 参数 | 说明 |
|------|------|
| `--no-web` | 只开插件端（纯浏览器插件用户可省资源） |
| `--no-plugin` | 只开 Web 端，不开插件端 |
| `--model kotoba` | 日语专用模型（默认，需手动下载） |
| `--model Systran/faster-whisper-large-v3` | 通用 Whisper，自动下载 |
| `--engine sensevoice` | 切到 SenseVoice 引擎（需装 funasr） |
| `--device cpu` / `--device cuda` | 手动指定设备（默认自动检测） |
| `--plugin-port` / `--web-port` | 自定义端口（默认 8765 / 8000） |
| `--room 房间号` | Web 端默认监测的直播间 |
| `--vad-threshold 0.5` | VAD 灵敏度（越小越灵敏，字幕不出可降到 0.3） |
| `--debug` | 打印音频诊断日志（排查收不到字幕的问题） |

### GPU 加速（可选）

faster-whisper 走 CTranslate2。要用 CUDA 需要：

```bash
pip install nvidia-cublas-cu12
```

然后把 `cublas64_12.dll` / `cublasLt64_12.dll` 复制到
`venv\Lib\site-packages\ctranslate2\`。没显卡时自动回退 CPU（int8）。

---

## 字幕记录（供 AI 分析）

看直播 / 视频 / 番剧时，服务会自动把「原文 + 译文 + 音频」按来源落盘到 `logs/`：

```
logs/
├── room_<房间号>/       直播
├── video_<BV号>/        视频
└── bangumi_<ep号>/      番剧
    ├── transcript.jsonl     每行一条：时间/session/source/original/translated/audio
    └── audio/*.wav          每句的源音频（16kHz 单声道）
```

可用于第三方 AI 分析主播的术语、口癖，提炼成新的主播画像。

---

## 常见问题

| 现象 | 处理 |
|------|------|
| 插件提示「无法连接识别服务」 | 确认 `start_server.bat` 窗口在跑且显示 `listening` |
| 字幕只有原文，没有翻译 | 检查 API Key 是否填对（`sk-` 开头）；显示「[未配置翻译]」说明没填 |
| 字幕完全不出现 | 加 `--debug` 启动，看是否打印 `[audio]` 帧和 `[ASR]` 结果 |
| 识别不准 / 断句奇怪 | 背景音乐吵时会受影响；可试 `--vad-threshold 0.3` |
| 首次启动卡在下载模型 | 保持联网；模型缓存在 `%LOCALAPPDATA%\LiveTranslate` |
| 换直播间后字幕串了 | 点一次「停止」再「开始」 |
| 手机打不开字幕页 | 防火墙拦了 8000 端口，跑 `add_firewall_rule.bat` |
| 「检测开播」很慢 | 确认服务端能走代理（B 站接口直连可能卡死） |

---

## 目录结构

```
直播翻译/
├── start_server.bat          # 一键启动（插件端 + Web 端，可加 --no-web/--no-plugin）
├── start_server.ps1
├── add_firewall_rule.bat     # 放行 8000 端口（手机端需要）
├── requirements.txt          # Python 依赖
├── LICENSE                   # MIT
├── CHANGELOG.md              # 更新日志
├── config.example.json       # 手机端配置示例（复制为 config.json）
├── profiles.example/         # 主播画像示例（复制到 profiles/）
│   └── 22748536.json
├── server/
│   ├── main.py               # 统一入口：同时开两个端口
│   ├── asr_server.py         # 核心：VAD + 识别 + 翻译 + 插件端协议
│   ├── live_web.py           # Web 端：监测直播间 + 抓流 + 网页字幕
│   ├── streamer_profile.py   # 主播画像 + 提示词拼接 + 落盘
│   └── web/
│       └── index.html        # 手机字幕页
└── extension/                # 浏览器插件（加载这个目录）
    ├── manifest.json
    ├── background.js         # 协调 popup / offscreen / content
    ├── offscreen.js          # 采集标签页音频 + 降采样 + 发送
    ├── content.js            # 字幕叠加
    ├── content.css           # 毛玻璃字幕样式
    └── popup.html / popup.js / popup.css
```

模型缓存（不入库）：`%LOCALAPPDATA%\LiveTranslate\`

---

## 许可证

[MIT License](LICENSE)

- 仅供学习交流，请勿用于商业用途
- 请遵守 Bilibili、DeepSeek 的服务条款
- 翻译由第三方模型生成，可能存在错误，请自行甄别
- 涉及音视频采集，请尊重内容版权
