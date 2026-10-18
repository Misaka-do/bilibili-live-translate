// Offscreen 文档：采集标签页音频 -> 降采样 16kHz -> WebSocket 发给本地识别服务
// 本地服务负责识别 + 调用 DeepSeek 翻译，这里只负责把字幕发给内容脚本
let audioContext = null;
let processor = null;
let mediaStream = null;
let ws = null;
let wsUrl = '';
let apiKey = '';
let roomId = null;
let targetTabId = null;
let running = false;
let reconnectTimer = null;

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'start') {
    start(msg)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  if (msg.type === 'stop') {
    stop();
    sendResponse({ ok: true });
    return false;
  }
  return false;
});

async function start(msg) {
  stop();
  targetTabId = msg.targetTabId;
  wsUrl = msg.serverUrl || 'ws://127.0.0.1:8765';
  roomId = msg.roomId || null;

  // 双保险：优先用消息里的 key，缺了就再从 chrome.storage 读一次
  let key = msg.apiKey || '';
  if (!key) {
    try {
      const stored = await chrome.storage.local.get('apiKey');
      key = (stored && stored.apiKey) || '';
    } catch (e) {}
  }
  apiKey = key;

  // tab 采集的标准写法：chromeMediaSource/chromeMediaSourceId 必须放 mandatory 里
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: 'tab',
          chromeMediaSourceId: msg.streamId
        }
      },
      video: false
    });
  } catch (e) {
    console.error('[offscreen] getUserMedia 失败:', e && e.message, e && e.name);
    sendStatus('音频采集失败: ' + ((e && e.message) || e));
    throw e;
  }

  running = true;
  connectWS(wsUrl);
  setupAudio();
  sendStatus('正在识别…');
}

function stop() {
  running = false;
  if (processor) {
    try { processor.disconnect(); } catch (e) {}
    processor = null;
  }
  if (audioContext) {
    try { audioContext.close(); } catch (e) {}
    audioContext = null;
  }
  if (mediaStream) {
    mediaStream.getTracks().forEach((t) => t.stop());
    mediaStream = null;
  }
  if (ws) {
    try { ws.close(); } catch (e) {}
    ws = null;
  }
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

function connectWS(url) {
  try {
    ws = new WebSocket(url);
  } catch (e) {
    sendStatus('无法连接识别服务，请先启动本地服务');
    return;
  }
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    // 告诉本地服务当前来源（直播房间号 / 视频 BV 号 / 番剧号），用于落盘 + 加载主播画像
    if (roomId) {
      ws.send(JSON.stringify({ type: 'setSource', source: roomId }));
    }
    // 把 DeepSeek API Key 交给本地服务，由服务端直接调用 DeepSeek 翻译
    if (apiKey) {
      ws.send(JSON.stringify({ type: 'setKey', apiKey }));
      sendStatus('识别服务已连接，已发送 API Key');
    } else {
      sendStatus('识别服务已连接，但未找到 API Key（请在插件弹窗里填写并保存）');
    }
  };
  ws.onerror = () => {};
  ws.onclose = () => {
    if (!running) return;
    sendStatus('识别服务断开，重连中…');
    if (!reconnectTimer) {
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        if (running) connectWS(url);
      }, 2000);
    }
  };
  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    if (data.type === 'asr' && data.original) {
      sendSubtitle(data.original, data.translated || '');
    }
  };
}

function setupAudio() {
  // 用原生采样率创建 AudioContext：音频会「原样透传」回扬声器，保证直播声音不消失
  audioContext = new AudioContext();
  const source = audioContext.createMediaStreamSource(mediaStream);
  processor = audioContext.createScriptProcessor(2048, 1, 1);
  source.connect(processor);
  processor.connect(audioContext.destination);
  let sentChunks = 0;
  let lastPeak = 0;
  processor.onaudioprocess = (e) => {
    const input = e.inputBuffer.getChannelData(0);
    const output = e.outputBuffer.getChannelData(0);
    // 关键修复：把输入原样写到输出，直播声音才能继续播放
    output.set(input);
    // 统计本块峰值（判断采集到的是不是静音）
    let peak = 0;
    for (let i = 0; i < input.length; i++) {
      const a = Math.abs(input[i]);
      if (a > peak) peak = a;
    }
    lastPeak = Math.max(lastPeak, peak);
    if (!running || !ws || ws.readyState !== WebSocket.OPEN) return;
    // 单独降采样到 16kHz 送去识别（不影响播放音质）
    const down = downsampleTo16k(input, audioContext.sampleRate);
    ws.send(floatTo16BitPCM(down).buffer);
    sentChunks++;
    if (sentChunks === 1 || sentChunks % 50 === 0) {
      console.log('[offscreen] 已发送', sentChunks, '块 峰值=', peak.toFixed(4), '累计峰值=', lastPeak.toFixed(4));
    }
  };
  console.log('[offscreen] setupAudio 完成，采样率', audioContext.sampleRate);
}

// 线性插值降采样到 16kHz（SenseVoice 需要 16kHz 单声道）
function downsampleTo16k(input, fromRate) {
  const toRate = 16000;
  if (fromRate <= toRate) return input;
  const ratio = fromRate / toRate;
  const outLen = Math.floor(input.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio;
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const frac = pos - i0;
    out[i] = input[i0] * (1 - frac) + input[i1] * frac;
  }
  return out;
}

function floatTo16BitPCM(input) {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    let s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

// offscreen 文档里没有 chrome.tabs（拿不到），所以字幕必须经后台 Service Worker 转发给页面
function relayToTab(payload) {
  if (targetTabId == null) return;
  try {
    const p = chrome.runtime.sendMessage({ type: 'relayToTab', targetTabId, payload });
    if (p && typeof p.catch === 'function') p.catch(() => {});
  } catch (e) {
    // 忽略：后台可能暂时不可用
  }
}

function sendSubtitle(original, translated) {
  relayToTab({ type: 'subtitle', original, translated });
}

function sendStatus(text) {
  relayToTab({ type: 'status', text });
}
