// 后台 Service Worker：协调 popup / offscreen / 内容脚本
let capturingTabId = null;
let restartTimer = null;
let lastCapturedUrl = null;

function isBilibili(url) {
  return (
    url.startsWith('https://live.bilibili.com/') ||
    url.startsWith('https://www.bilibili.com/video/') ||
    url.startsWith('https://www.bilibili.com/bangumi/')
  );
}

// 从 URL 提取「来源标识」：直播→房间号，视频→BV号，番剧→ep/ss号。识别不出返回 null。
function extractSourceId(url) {
  if (!url) return null;
  // 直播：live.bilibili.com/22748536
  let m = /live\.bilibili\.com\/(?:blanc\/)?(\d+)/.exec(url);
  if (m) return { kind: 'live', id: m[1] };
  // 视频：www.bilibili.com/video/BV1xxxxx
  m = /www\.bilibili\.com\/video\/(BV[0-9A-Za-z]+)/.exec(url);
  if (m) return { kind: 'video', id: m[1] };
  // 番剧：www.bilibili.com/bangumi/play/ep123 或 ss123
  m = /www\.bilibili\.com\/bangumi\/play\/((?:ep|ss)\d+)/.exec(url);
  if (m) return { kind: 'bangumi', id: m[1] };
  return null;
}

// 采集中的标签页发生「真实」导航（URL 变了）时自动重启采集。
// 注意：B 站页面会频繁 pushState/replaceState（弹幕、进度等），URL 没变时绝不能重启，
// 否则采集会被反复打断、音频永远流不进来。
function scheduleRestart(tabId, url) {
  if (tabId !== capturingTabId) return;
  if (!isBilibili(url)) {
    lastCapturedUrl = null;
    stopCapture();
    return;
  }
  if (url === lastCapturedUrl) return; // URL 没变（pushState/replaceState）→ 忽略
  lastCapturedUrl = url;
  if (restartTimer) clearTimeout(restartTimer);
  restartTimer = setTimeout(async () => {
    restartTimer = null;
    if (capturingTabId !== tabId) return;
    // 新页面可能还没加载完，失败就重试几次
    for (let i = 0; i < 4; i++) {
      try {
        await startCapture(tabId);
        console.log('[auto-restart] 已自动重启采集:', url);
        return;
      } catch (e) {
        console.error('[auto-restart] 第 ' + (i + 1) + ' 次重启失败:', e);
        await new Promise((r) => setTimeout(r, 1000));
      }
    }
  }, 1200);
}

if (chrome.webNavigation) {
  chrome.webNavigation.onCommitted.addListener((d) => {
    if (d.frameId === 0) scheduleRestart(d.tabId, d.url);
  });
  chrome.webNavigation.onHistoryStateUpdated.addListener((d) => {
    if (d.frameId === 0) scheduleRestart(d.tabId, d.url);
  });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // offscreen 文档没有 chrome.tabs，字幕由它发到这里，再从这里转发给页面
  if (msg.type === 'relayToTab') {
    const tabId = msg.targetTabId != null ? msg.targetTabId : capturingTabId;
    if (tabId != null) {
      chrome.tabs.sendMessage(tabId, msg.payload).catch(() => {});
    }
    sendResponse({ ok: true });
    return true;
  }
  handleMessage(msg, sender).then(sendResponse).catch((e) => sendResponse({ ok: false, error: String(e) }));
  return true; // 异步响应
});

async function handleMessage(msg, sender) {
  switch (msg.type) {
    case 'startCapture':
      return startCapture(msg.targetTabId);
    case 'stopCapture':
      return stopCapture();
    case 'getStatus':
      return { ok: true, capturing: capturingTabId != null };
    default:
      return { ok: false, error: 'unknown message type: ' + msg.type };
  }
}

async function startCapture(tabId) {
  // 注意：这里不显式 stopCapture()。切换视频时 offscreen 文档保持不变，
  // 由 offscreen 的 start() 内部先停旧音频/旧 WebSocket 再采新流，
  // 避免「关闭重建 offscreen」带来的竞态（否则会表现为切换视频后一直连不上）。

  // 先确认内容脚本是否存活；不存活才注入，避免与 manifest 的 content_scripts
  // 重复注入（重复注入会让「字幕历史逐条追加」被重复执行两次）。
  let alive = false;
  try {
    const pong = await chrome.tabs.sendMessage(tabId, { type: 'pingContent' });
    alive = !!(pong && pong.alive);
  } catch (e) {
    alive = false;
  }
  if (!alive) {
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
    } catch (e) {
      console.error('[startCapture] 注入内容脚本失败:', e);
    }
    try {
      await chrome.scripting.insertCSS({ target: { tabId }, files: ['content.css'] });
    } catch (e) {
      console.error('[startCapture] 注入样式失败:', e);
    }
  } else {
    console.log('[startCapture] 内容脚本已存活，跳过重复注入');
  }

  // 先建好 offscreen 文档，再取 streamId（streamId 有时效，要尽快用掉）
  await ensureOffscreen();

  const { apiKey, serverUrl } = await chrome.storage.local.get(['apiKey', 'serverUrl']);
  if (!apiKey) {
    throw new Error('未配置 DeepSeek API Key，请先点击插件图标填写');
  }

  const streamId = await getStreamId(tabId);

  // 提取直播间号（视频页/番剧页没有 room_id，传空走基础提示词）
  let tabUrl = '';
  try {
    const t = await chrome.tabs.get(tabId);
    tabUrl = (t && t.url) || '';
    lastCapturedUrl = tabUrl;
  } catch (e) {
    lastCapturedUrl = null;
  }
  const source = extractSourceId(tabUrl);
  const sourceId = source ? source.id : null;

  await sendToOffscreen({
    type: 'start',
    streamId,
    serverUrl: serverUrl || 'ws://127.0.0.1:8765',
    apiKey,
    targetTabId: tabId,
    roomId: sourceId
  });

  capturingTabId = tabId;
  return { ok: true };
}

function getStreamId(tabId) {
  return new Promise((resolve, reject) => {
    chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (streamId) => {
      if (chrome.runtime.lastError) {
        reject(new Error('获取音频流失败: ' + chrome.runtime.lastError.message));
      } else if (!streamId) {
        reject(new Error('获取音频流失败：返回空 streamId'));
      } else {
        resolve(streamId);
      }
    });
  });
}

async function ensureOffscreen() {
  // 复用已有 offscreen 文档：切换视频时反复「关闭再重建」容易撞上竞态，
  // 导致新文档还没就绪、采集/连接迟迟起不来。扩展重载时 Chrome 会自动销毁旧文档，
  // 这里只需在不存在时创建一次。
  try {
    if (chrome.offscreen.hasDocument && (await chrome.offscreen.hasDocument())) {
      return;
    }
  } catch (e) {
    // 忽略，继续尝试创建
  }
  await chrome.offscreen.createDocument({
    url: 'offscreen.html',
    reasons: ['USER_MEDIA'],
    justification: '采集当前标签页音频用于实时语音识别与翻译'
  });
}

function sendToOffscreen(msg) {
  return new Promise((resolve, reject) => {
    const attempt = (n) => {
      chrome.runtime.sendMessage(msg, (resp) => {
        if (chrome.runtime.lastError) {
          // offscreen 文档可能还没注册监听，稍后重试
          if (n > 0) {
            setTimeout(() => attempt(n - 1), 200);
          } else {
            reject(new Error(chrome.runtime.lastError.message));
          }
        } else {
          resolve(resp);
        }
      });
    };
    attempt(10);
  });
}

async function stopCapture() {
  try {
    await sendToOffscreen({ type: 'stop' });
  } catch (e) {
    // offscreen 可能已关闭
  }
  capturingTabId = null;
  try {
    if (chrome.offscreen.hasDocument && (await chrome.offscreen.hasDocument())) {
      await chrome.offscreen.closeDocument();
    }
  } catch (e) {
    // 忽略
  }
  return { ok: true };
}
