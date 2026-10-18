const $ = (id) => document.getElementById(id);

function showVersion() {
  try {
    const v = chrome.runtime.getManifest().version;
    $('version').textContent = 'v' + v;
  } catch (e) {
    $('version').textContent = '';
  }
}

async function loadSettings() {
  const { apiKey, serverUrl } = await chrome.storage.local.get(['apiKey', 'serverUrl']);
  $('apiKey').value = apiKey || '';
  $('serverUrl').value = serverUrl || 'ws://127.0.0.1:8765';
  $('keyHint').textContent = apiKey ? '已保存 API Key ✓' : '首次使用请填写你的 DeepSeek API Key';
}

$('saveBtn').onclick = async () => {
  await chrome.storage.local.set({
    apiKey: $('apiKey').value.trim(),
    serverUrl: $('serverUrl').value.trim() || 'ws://127.0.0.1:8765'
  });
  $('status').textContent = '设置已保存';
  loadSettings();
};

$('startBtn').onclick = async () => {
  const { apiKey } = await chrome.storage.local.get('apiKey');
  if (!apiKey) {
    $('status').textContent = '请先填写并保存 DeepSeek API Key';
    return;
  }
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const u = tab && tab.url ? tab.url : '';
  const isBili =
    u.startsWith('https://live.bilibili.com/') ||
    u.startsWith('https://www.bilibili.com/video/') ||
    u.startsWith('https://www.bilibili.com/bangumi/');
  if (!isBili) {
    $('status').textContent = '请先打开 B 站直播或视频页面';
    return;
  }
  const resp = await chrome.runtime.sendMessage({ type: 'startCapture', targetTabId: tab.id });
  $('status').textContent = resp && resp.ok ? '已开始，正在识别…' : '启动失败：' + ((resp && resp.error) || '未知错误');
};

$('stopBtn').onclick = async () => {
  await chrome.runtime.sendMessage({ type: 'stopCapture' });
  $('status').textContent = '已停止';
};

loadSettings();
showVersion();
