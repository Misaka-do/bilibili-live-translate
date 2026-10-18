// 内容脚本：把字幕叠加到页面上（磨砂玻璃框）。
// 功能：
//   - 固定尺寸的磨砂玻璃框，右下角把手可鼠标拖拽调整宽高，头部把手可拖动位置
//   - 字幕按历史逐条记录、框内可滚轮回看；只在刷新页面/重载插件时清空
// 注意：这里故意不做「只注入一次」的拦截——插件重新加载后，页面里旧脚本的
// chrome.runtime 监听会失效，若拦截掉重新注入就永远收不到字幕。允许重复注入，
// overlay 节点从 DOM 里复用即可。
(() => {
  let overlay = null;
  let listEl = null;
  let statusEl = null;
  let statusTimer = null;
  let userAdjusted = false;   // 用户拖拽过位置或大小后，停止自动定位
  let moveDrag = null;
  let resizeDrag = null;

  const MAX_ITEMS = 500;      // 历史条数上限，防止超长直播内存无限增长

  function setStatus(text) {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.toggle('show', !!text);
    if (statusTimer) clearTimeout(statusTimer);
    if (text) {
      // 状态信息 4 秒后自动淡出，不占画面
      statusTimer = setTimeout(() => {
        if (statusEl) statusEl.classList.remove('show');
        statusTimer = null;
      }, 4000);
    }
  }

  function bindRefs() {
    if (!overlay) return;
    listEl = overlay.querySelector('#lt-list');
    statusEl = overlay.querySelector('#lt-status');
  }

  function ensureOverlay() {
    if (!overlay || !overlay.isConnected) {
      const existing = document.getElementById('live-translate-overlay');
      if (existing) {
        overlay = existing;
      } else {
        overlay = document.createElement('div');
        overlay.id = 'live-translate-overlay';
        overlay.innerHTML =
          '<div class="lt-box">' +
          '<div class="lt-header" id="lt-header"><span>直播翻译 · 拖动移动</span></div>' +
          '<div class="lt-list" id="lt-list"></div>' +
          '<div class="lt-status" id="lt-status"></div>' +
          '<div class="lt-resizer" id="lt-resizer" title="拖动调整大小"></div>' +
          '</div>';
        document.body.appendChild(overlay);
      }
      bindRefs();
      setupMoveDrag();
      setupResize();
      positionOverlay();
    }
    if (!window.__ltPositionBound) {
      window.__ltPositionBound = true;
      window.addEventListener('resize', positionOverlay);
      setInterval(positionOverlay, 2000);
    }
  }

  // —— 拖动移动（头部把手）——
  function setupMoveDrag() {
    const hdr = overlay.querySelector('#lt-header');
    if (!hdr) return;
    hdr.addEventListener('pointerdown', (e) => {
      const el = document.getElementById('live-translate-overlay');
      if (!el) return;
      const rect = el.getBoundingClientRect();
      moveDrag = {
        pointerId: e.pointerId,
        dx: e.clientX - rect.left,
        dy: e.clientY - rect.top
      };
      try { hdr.setPointerCapture(e.pointerId); } catch (err) {}
      e.preventDefault();
    });
    hdr.addEventListener('pointermove', (e) => {
      if (!moveDrag || e.pointerId !== moveDrag.pointerId) return;
      const el = document.getElementById('live-translate-overlay');
      if (!el) return;
      el.style.position = 'fixed';
      el.style.left = e.clientX - moveDrag.dx + 'px';
      el.style.top = e.clientY - moveDrag.dy + 'px';
      el.style.bottom = 'auto';
      el.style.transform = 'none';
    });
    const endMove = (e) => {
      if (moveDrag && e.pointerId === moveDrag.pointerId) {
        userAdjusted = true;
        moveDrag = null;
      }
    };
    hdr.addEventListener('pointerup', endMove);
    hdr.addEventListener('pointercancel', endMove);
  }

  // —— 拖动调整大小（右下角把手）——
  function setupResize() {
    const rz = overlay.querySelector('#lt-resizer');
    if (!rz) return;
    rz.addEventListener('pointerdown', (e) => {
      const el = document.getElementById('live-translate-overlay');
      if (!el) return;
      const rect = el.getBoundingClientRect();
      resizeDrag = {
        pointerId: e.pointerId,
        startX: e.clientX,
        startY: e.clientY,
        startW: rect.width,
        startH: rect.height
      };
      try { rz.setPointerCapture(e.pointerId); } catch (err) {}
      e.preventDefault();
    });
    rz.addEventListener('pointermove', (e) => {
      if (!resizeDrag || e.pointerId !== resizeDrag.pointerId) return;
      const el = document.getElementById('live-translate-overlay');
      if (!el) return;
      const w = Math.max(240, resizeDrag.startW + (e.clientX - resizeDrag.startX));
      const h = Math.max(120, resizeDrag.startH + (e.clientY - resizeDrag.startY));
      el.style.width = w + 'px';
      el.style.height = h + 'px';
    });
    const endResize = (e) => {
      if (resizeDrag && e.pointerId === resizeDrag.pointerId) {
        userAdjusted = true;
        resizeDrag = null;
      }
    };
    rz.addEventListener('pointerup', endResize);
    rz.addEventListener('pointercancel', endResize);
  }

  // —— 追加一条字幕（原文 + 译文），滚轮可回看历史 ——
  function appendSubtitle(original, translated) {
    ensureOverlay();
    if (!listEl) return;
    // 关键：在「追加之前」判断是否在底部附近。追加后 scrollHeight 变大，
    // 若追加后再判断，新条目一高就会被误判成「用户往上翻了」而不滚到底。
    const nearBottom = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight < 60;

    const item = document.createElement('div');
    item.className = 'lt-item';
    const o = document.createElement('div');
    o.className = 'lt-item-orig';
    o.textContent = original || '';
    const t = document.createElement('div');
    t.className = 'lt-item-trans';
    t.textContent = translated || '翻译中…';
    t.classList.toggle('placeholder', !translated);
    item.appendChild(o);
    item.appendChild(t);
    listEl.appendChild(item);

    // 历史条数上限
    while (listEl.children.length > MAX_ITEMS) {
      listEl.removeChild(listEl.firstChild);
    }
    // 之前在底部附近 → 追加后滚到底；用户往上翻看历史时则不打扰
    if (nearBottom) {
      listEl.scrollTop = listEl.scrollHeight;
    }
  }

  function positionOverlay() {
    if (userAdjusted) return; // 用户拖拽过位置/大小，保持其状态
    const el = document.getElementById('live-translate-overlay');
    if (!el) return;
    const video =
      document.querySelector('#live-player video') ||            // 直播
      document.querySelector('.live-player-mounter video') ||    // 直播（旧版）
      document.querySelector('.bpx-player-video-wrap video') ||  // 视频（新版播放器）
      document.querySelector('.bpx-player-video video') ||       // 视频
      document.querySelector('#bilibili-player video') ||        // 视频（旧版播放器）
      document.querySelector('video');                           // 兜底
    if (video) {
      const r = video.getBoundingClientRect();
      if (r.width > 100 && r.height > 100 && r.bottom > 0) {
        // 固定在视频左上角内侧，尺寸由 CSS 固定，不再随视频宽度拉伸
        el.style.position = 'fixed';
        el.style.left = (r.left + 20) + 'px';
        el.style.top = (r.top + 20) + 'px';
        el.style.bottom = 'auto';
        el.style.transform = 'none';
        return;
      }
    }
    el.style.position = 'fixed';
    el.style.left = '24px';
    el.style.top = 'auto';
    el.style.bottom = '24px';
    el.style.transform = 'none';
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === 'subtitle') {
      appendSubtitle(msg.original, msg.translated);
      sendResponse({ ok: true });
    } else if (msg.type === 'status') {
      ensureOverlay();
      setStatus(msg.text);
      sendResponse({ ok: true });
    } else if (msg.type === 'clear') {
      if (listEl) listEl.innerHTML = '';
      sendResponse({ ok: true });
    } else if (msg.type === 'pingContent') {
      // 让「开始翻译」时可以主动确认内容脚本活着
      sendResponse({ ok: true, alive: true, hasOverlay: !!document.getElementById('live-translate-overlay') });
    }
    return false;
  });
})();
