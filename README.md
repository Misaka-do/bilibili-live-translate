# B站实时翻译（直播 + 视频）

一个「浏览器插件 + 本地语音识别服务」的实时翻译工具，用来实时翻译 B 站日本 VTuber 直播和视频。

- **本地语音识别**（faster-whisper / kotoba-whisper / SenseVoice）：把直播里的日语语音转成文字
- **DeepSeek 翻译**：把日语文字翻译成简体中文（由本地服务直连调用，避开浏览器代理问题）
- **浏览器插件**：采集标签页声音 → 送到本地识别服务 → 接收识别+翻译结果 → 叠加字幕
- 首次使用时在插件弹窗里输入你的 **DeepSeek API Key**

> 附：**手机端 Web 服务**（`start_live_server.bat`）——不用浏览器插件，电脑自己抓取指定直播间的音频，
> 识别 + 翻译后把字幕推到你手机上打开的网页。详见下文「八、手机端 Web 服务」。

```
┌──────────────┐   WebSocket(16kHz PCM + API Key)   ┌─────────────────────┐
│ 浏览器插件    │ ─────────────────────────────────▶ │ 本地识别服务          │
│ (采集标签页声) │ ◀───────────────────────────────── │ asr_server.py       │
│ (显示字幕)    │   JSON: {"original","translated"}  │ (whisper/kotoba 识别)│
└──────────────┘                                    │ (调用 DeepSeek 翻译)  │
                                                    └──────────┬──────────┘
                                                               │ HTTPS（直连）
                                                               ▼
                                                      DeepSeek API（翻译）
```

---

## 目录

1. [功能特性](#一功能特性)
2. [安装依赖](#二安装依赖)
3. [下载模型](#三下载模型)
4. [启动本地识别服务](#四启动本地识别服务)
5. [安装浏览器插件](#五安装浏览器插件)
6. [使用](#六使用)
7. [常见问题](#七常见问题)
8. [手机端 Web 服务](#八手机端-web-服务)
9. [主播画像](#九主播画像)
10. [目录结构](#十目录结构)
11. [许可证与免责声明](#许可证与免责声明)

---

## 一、功能特性

- 🎙️ **本地语音识别**：默认 faster-whisper，支持 GPU（CUDA）加速
- 🇯🇵 **日语优化模型**：可选 kotoba-whisper（专为动漫/日本口语微调）
- 🧠 **Silero VAD**：神经网络语音活动检测，准确切句、过滤背景音乐
- 🌐 **DeepSeek 翻译**：口语化中文，带 B 站直播语境（舰长/小号等术语）
- 🪟 **毛玻璃字幕**：叠加在视频上，可拖拽定位
- 📱 **手机端**：电脑抓流 → 手机网页看字幕

---

## 二、安装依赖

需要 **Python 3.10+**（推荐 3.12）。

```bash
# 创建虚拟环境
python -m venv venv

# 激活（Windows）
venv\Scripts\activate

# 安装核心依赖
pip install -r requirements.txt
```

> 只做翻译识别（whisper 引擎），核心依赖即可，无需安装 funasr/modelscope（那套是 SenseVoice 引擎才需要，体积大、安装慢）。

> **GPU 加速（可选）**：faster-whisper 走 CTranslate2，CUDA 需要额外补装
> `nvidia-cublas-cu12`，并把 `cublas64_12.dll` / `cublasLt64_12.dll` 复制到
> `venv\Lib\site-packages\ctranslate2\` 目录。没有显卡时自动回退到 CPU（int8）。

---

## 三、下载模型

模型文件**不会随仓库提供**，请自行下载到本地。默认模型缓存目录：

```
%LOCALAPPDATA%\LiveTranslate\
├── models\
│   ├── kotoba-whisper-v2.0-faster\   # 日语专用（约 1.5GB）
│   └── silero_vad.jit                # VAD 模型（silero-vad 包自动带）
└── modelscope\models\
    └── Systran--faster-whisper-large-v3\  # 通用 Whisper（约 3GB）
```

**推荐：日语专用模型 kotoba-whisper**（精度更高，`--model kotoba` 默认）：

1. 创建目录：`%LOCALAPPDATA%\LiveTranslate\models\kotoba-whisper-v2.0-faster\`
2. 从 HuggingFace 下载以下 5 个文件放入该目录：
   - `model.bin`（约 1.5GB）`https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0-faster/resolve/main/model.bin`
   - `config.json`
   - `tokenizer.json`
   - `vocabulary.json`
   - `preprocessor_config.json`

> 没有 kotoba 时，服务会自动回退到 faster-whisper-large-v3（首次运行从 ModelScope 自动下载，约 3GB，需联网）。

> 模型必须放在**纯英文路径**（默认 `%LOCALAPPDATA%\LiveTranslate`），因为 sentencepiece 等 C++ 库
> 在 Windows 上无法打开含中文的路径。

---

## 四、启动本地识别服务

双击运行（Windows）：

```
start_server.bat
```

看到类似输出即成功：

```
[loading] faster-whisper kotoba on cuda (float16) ...
[loading] done
[listening] ws://127.0.0.1:8765  (engine=whisper model=kotoba device=cuda)
```

- 默认地址：`ws://127.0.0.1:8765`
- 关闭窗口即停止服务

常用参数：

| 参数 | 说明 |
|------|------|
| `--model kotoba` | 日语专用模型（默认，需手动下载） |
| `--model Systran/faster-whisper-large-v3` | 通用 Whisper，自动下载 |
| `--engine sensevoice` | 切回 SenseVoice 引擎（需装 funasr） |
| `--device cpu` / `--device cuda` | 手动指定设备 |
| `--debug` | 打开诊断日志（排查音频/VAD 问题） |

---

## 五、安装浏览器插件（Chrome / Edge）

1. 打开 Chrome，地址栏输入 `chrome://extensions`（Edge 是 `edge://extensions`）
2. 打开右上角 **「开发者模式」**
3. 点 **「加载已解压的扩展程序」**
4. 选择本目录下的 **`extension`** 文件夹
5. 加载成功后，工具栏会出现插件图标

---

## 六、使用

1. **先启动本地识别服务**（双击 `start_server.bat`，保持窗口开着）
2. 打开一个 B 站直播页面（`live.bilibili.com/房间号`）或视频页面（`www.bilibili.com/video/BV...`、番剧 `www.bilibili.com/bangumi/play/...`）
3. 点击浏览器工具栏的插件图标，**首次使用先填入 DeepSeek API Key**，点「保存设置」
4. 点 **「开始翻译」**
   - 浏览器会请求采集当前标签页的声音，选择/确认后开始
   - 视频底部会出现字幕条：上面是识别出的日语原文，下面是中文翻译（字幕条可拖拽）
5. 点 **「停止」** 结束采集

> API Key 获取：登录 <https://platform.deepseek.com> → API Keys → 创建，得到 `sk-...`

---

## 七、常见问题

| 现象 | 处理 |
|------|------|
| 插件提示「无法连接识别服务」 | 确认 `start_server.bat` 已启动且显示 `listening` |
| 字幕只有原文没有翻译 | 确认已保存 DeepSeek API Key，且账户有余额 |
| 识别结果不对 / 断句奇怪 | 直播背景音乐较吵时会受影响；可调 `server/asr_server.py` 里的 VAD 参数 |
| 首次启动卡在下载模型 | 保持网络畅通；模型缓存在 `%LOCALAPPDATA%\LiveTranslate` |
| 换直播间后字幕串了 | 重新点一次「停止」再「开始」 |
| 音频正常但无字幕 | 用 `--debug` 启动服务端，看 `[audio]` 帧的 RMS 是否正常流入 |

---

## 八、手机端 Web 服务（监测指定直播间）

浏览器插件在手机上没法用（手机浏览器不能装插件、也采集不到标签页声音）。这个服务让
**电脑自己抓取 B 站直播流的音频**，识别 + 翻译后，把字幕实时推到你手机打开的网页上。
适合「手机看直播、旁边电脑跑翻译」的场景。

```
┌─────────────┐  手机浏览器打开网页（只显示字幕）
│   手机       │  http://<电脑局域网IP>:8000
└──────┬──────┘
       │ WebSocket（字幕推送）
┌──────▼─────────────────────────────────────────────┐
│  live_web.py（电脑）                                │
│  监测房间 → 开播后 playUrl → PyAV 解码直播流音频     │
│  → VAD 切句 → whisper/SenseVoice 识别 → DeepSeek 翻译 │
└─────────────────────────────────────────────────────┘
```

### 启动

1. 双击 `start_live_server.bat`（首次启动会加载识别模型，稍等片刻）
2. 控制台会打印两个地址：
   - 电脑本机：`http://127.0.0.1:8000`
   - 手机访问：`http://<局域网IP>:8000`
3. **手机和电脑连同一个 Wi-Fi**。电脑上打开 `http://127.0.0.1:8000`，点「设置」，用手机**扫页面里的二维码**即可直达
4. 手机点右上角「设置」，填入 **DeepSeek API Key** 和直播间号，保存
5. 等主播开播，页面会自动从「未开播」变成「直播中」，并开始滚动显示字幕

### 说明

- API Key 保存在本目录 `config.json`（本地生成，不入库），只存在电脑上。
- 手机上看直播照常用 B 站 App / 浏览器，字幕页是**单独的页面**（二者会有一点时间差，属正常）。
- 手机打不开页面：多半是 Windows 防火墙拦了 8000 端口，双击 `add_firewall_rule.bat` 一键放行。
- 抓流只取音频（128kbps AAC），视频流会被丢弃，后台带宽占用很小。

---

## 九、主播画像（按主播定制翻译）

不同主播有各自的术语、梗、高频听错词，硬编码在一套提示词里会「好心办坏事」。
所以翻译提示词拆成两层：

- **基础提示词**（`server/streamer_profile.py` 里的 `BASE_PROMPT`）：所有主播通用的规则 + B 站通用术语（SC/舰长/小号等）
- **主播画像**（`profiles/<房间号>.json`）：该主播专属的术语表、梗、高频听错词、说话风格

翻译时自动「基础提示词 + 当前主播画像」拼成最终提示词。**换主播不用改代码**，只需维护对应画像文件。

画像结构示例（`profiles/<房间号>.json`）：

```json
{
  "room_id": 123456,
  "name": "某主播",
  "glossary": [{"heard": "おば／オバ／オパ", "say": "阿姨", "note": "B站官方，对应『叔叔』"}],
  "memes": ["某梗的说明"],
  "asr_fixes": [{"heard": "指針／感情／高い手", "correct": "艦長（舰长）"}],
  "style": "爱聊 FF14 和动漫、语气活泼"
}
```

- 没有画像的主播：自动退化为只用基础提示词。
- 画像改动后需**重启服务**生效。

> **注意**：`profiles/` 目录（含主播画像、用户自定义词表）**不随仓库提供**，属于个人数据。
> 你可以参考上面结构自己创建 `profiles/<房间号>.json`。

---

## 十、目录结构

```
直播翻译/
├── start_server.bat         # 一键启动识别服务
├── start_server.ps1
├── requirements.txt         # Python 依赖清单
├── LICENSE                  # MIT 许可证
├── config.example.json      # 手机端服务配置示例（复制为 config.json 使用）
├── server/
│   ├── asr_server.py        # 识别服务（WebSocket + VAD + whisper + 翻译）
│   ├── streamer_profile.py  # 主播画像 + 提示词拼接 + 日志落盘
│   ├── live_web.py          # 【手机端】监测直播间 + 抓流 + 识别翻译 + Web 服务
│   ├── web/index.html       # 【手机端】字幕页面（手机浏览器打开）
│   ├── test_asr.py          # 离线测试：识别单个音频文件
│   ├── test_whisper.py      # 离线测试 faster-whisper
│   └── test_ws_client.py    # 测试：模拟插件把音频流发给服务
├── extension/               # 浏览器插件（加载这个目录）
│   ├── manifest.json
│   ├── background.js        # 协调 popup / offscreen / 内容脚本
│   ├── offscreen.js         # 采集标签页音频 + 降采样 + 发送
│   ├── content.js           # 字幕叠加
│   ├── content.css          # 毛玻璃字幕样式
│   └── popup.html / popup.js / popup.css
├── start_live_server.bat    # 一键启动手机端 Web 服务
├── add_firewall_rule.bat    # 放行 8000 端口（手机端需要）
└── generate_icons.py        # 生成插件图标（一次性脚本）

模型缓存（首次运行自动生成 / 手动下载）：
  %LOCALAPPDATA%\LiveTranslate\models          # kotoba / VAD
  %LOCALAPPDATA%\LiveTranslate\modelscope      # faster-whisper-large-v3 / SenseVoice
```

---

## 许可证与免责声明

本项目采用 [MIT License](LICENSE) 开源。

- 仅供学习交流使用，请勿用于商业用途。
- 请遵守相关平台（Bilibili、DeepSeek）的服务条款。
- 翻译内容由第三方模型生成，可能存在错误，请自行甄别。
- 涉及视频/音频的采集与使用，请尊重内容版权。
