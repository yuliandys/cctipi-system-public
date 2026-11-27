/* ==========================================================================
   CCTipi System — Fitur Beranda, Pantau Multiview, Player, Peta & Health
   ========================================================================== */
(() => {
  'use strict';

  const App = window.CCTipi;
  const {
    $, $$, ic, esc, fmtTime, fmtBytes, reduceMotion, LS, K, state, tiles, els,
    saveLayout, saveFolders, savePresets, toast, api, apiJson, postJson,
    camKey, camType, isManual, streamLabel, resolveStreamUrl,
    folderNames, camsIn, filtered, isOffline, can, SET_TAB_PERM,
    loadCameras, refreshHealthInfo, checkStatus, openModal, closeModal, confirmBox, promptBox,
  } = App;

  // ------------------------------------------------------------------ player
  function mountPlayer(host, cam, opts = {}) {
    const type = camType(cam);
    const report = opts.onState || (() => {});
    const onState = cam.tokenExpired
      ? (st, msg) => report(st, st === 'error' ? `${msg || 'Kamera offline'} · token sumber mungkin kedaluwarsa` : msg)
      : report;
    const out = { cleanup: () => {}, mediaEl: null, hls: null };
    if (type === 'youtube') {
      const ifr = document.createElement('iframe');
      ifr.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');
      ifr.setAttribute('allow', 'autoplay; encrypted-media; picture-in-picture; fullscreen');
      ifr.setAttribute('allowfullscreen', '');
      ifr.onload = () => onState('live');
      host.appendChild(ifr);
      onState('loading', 'Memuat YouTube Live…');
      ifr.src = cam.url;
      out.cleanup = () => { ifr.src = 'about:blank'; ifr.remove(); };
      return out;
    }
    if (type === 'http' || type === 'mjpeg') {
      const img = document.createElement('img');
      img.className = 'mjpeg-view'; img.alt = '';
      img.crossOrigin = 'anonymous';
      host.appendChild(img);
      out.mediaEl = img;
      onState('loading', type === 'http' ? 'Memuat snapshot…' : 'Memuat MJPEG…');
      img.onload = () => onState('live');
      img.onerror = () => onState('error', 'Kamera offline / timeout');
      if (type === 'http') {
        const refresh = () => {
          const sep = cam.url.includes('?') ? '&' : '?';
          img.src = resolveStreamUrl(`${cam.url}${sep}_t=${Date.now()}`, cam);
        };
        refresh();
        const timer = setInterval(refresh, 1000);
        out.cleanup = () => { clearInterval(timer); img.removeAttribute('src'); img.remove(); };
      } else {
        let seq = 0, fails = 0, stopped = false, timer = null, ctrl = null, shown = null, pendingRevoke = null;
        const MIN_GAP_MS = 200;
        img.onload = () => { onState('live'); if (pendingRevoke) { URL.revokeObjectURL(pendingRevoke); pendingRevoke = null; } };
        img.onerror = null;
        const next = async () => {
          if (stopped) return;
          const t0 = performance.now();
          ctrl = new AbortController();
          const abortTimer = setTimeout(() => ctrl.abort(), 20000);
          try {
            const res = await fetch(`/api/mjpeg-frame?url=${encodeURIComponent(cam.url)}&seq=${seq}`, { signal: ctrl.signal, cache: 'no-store' });
            if (res.status === 401) { window.location.href = '/login'; return; }
            if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.error || `HTTP ${res.status}`); }
            const blob = await res.blob();
            if (stopped) return;
            seq = Number(res.headers.get('X-Frame-Seq')) || seq;
            if (pendingRevoke) URL.revokeObjectURL(pendingRevoke);
            pendingRevoke = shown;
            shown = URL.createObjectURL(blob);
            img.src = shown;
            fails = 0;
          } catch (err) {
            if (stopped) return;
            fails += 1;
            if (fails >= 2) onState('error', `Kamera offline — ${String(err.message || err).slice(0, 80)}`);
          } finally { clearTimeout(abortTimer); }
          if (stopped) return;
          const gap = fails ? Math.min(10000, 2000 * fails) : Math.max(0, MIN_GAP_MS - (performance.now() - t0));
          timer = setTimeout(next, gap);
        };
        next();
        out.cleanup = () => {
          stopped = true; clearTimeout(timer);
          try { if (ctrl) ctrl.abort(); } catch { /* abaikan */ }
          img.removeAttribute('src'); img.remove();
          for (const u of [shown, pendingRevoke]) if (u) URL.revokeObjectURL(u);
        };
      }
      return out;
    }
    if (type === 'flv') {
      const video = document.createElement('video');
      video.muted = true; video.playsInline = true; video.autoplay = true;
      host.appendChild(video);
      out.mediaEl = video;
      const src = resolveStreamUrl(cam.url, cam);
      let destroyed = false, retryTimer = null, flvPlayer = null;
      const markLive = () => { if (!destroyed) onState('live'); };
      video.addEventListener('loadeddata', markLive, { once: true });
      video.addEventListener('canplay', markLive, { once: true });
      video.addEventListener('playing', markLive, { once: true });
      const startFlv = (retriesLeft) => {
        if (destroyed) return;
        if (window.flvjs && flvjs.isSupported()) {
          flvPlayer = flvjs.createPlayer({ type: 'flv', isLive: true, url: src }, { enableWorker: true, enableStashBuffer: false, lazyLoad: false });
          flvPlayer.attachMediaElement(video);
          flvPlayer.load();
          flvPlayer.play().catch(() => {});
          flvPlayer.on(flvjs.Events.ERROR, () => {
            if (destroyed) return;
            try { flvPlayer.destroy(); } catch { /* abaikan */ }
            flvPlayer = null;
            if (retriesLeft > 0) { onState('loading', `Mencoba ulang (${4 - retriesLeft}/3)…`); retryTimer = setTimeout(() => startFlv(retriesLeft - 1), 3000); }
            else onState('error', 'Kamera offline / timeout');
          });
        } else onState('error', 'Browser tidak mendukung FLV');
      };
      onState('loading', 'Memuat FLV…');
      startFlv(3);
      out.cleanup = () => {
        destroyed = true;
        if (retryTimer) clearTimeout(retryTimer);
        if (flvPlayer) { try { flvPlayer.destroy(); } catch { /* abaikan */ } flvPlayer = null; }
        video.pause(); video.removeAttribute('src'); video.load(); video.remove();
      };
      return out;
    }
    // HLS (termasuk jembatan RTSP)
    const video = document.createElement('video');
    video.autoplay = true; video.muted = true; video.playsInline = true; video.preload = 'auto';
    video.crossOrigin = 'anonymous';
    host.appendChild(video);
    out.mediaEl = video;
    const src = resolveStreamUrl(cam.url, cam);
    onState('loading', 'Memuat stream…');
    let destroyed = false, retryTimer = null;
    const startHls = (retriesLeft) => {
      if (destroyed) return;
      if (window.Hls && Hls.isSupported()) {
        const hls = new Hls({
          manifestLoadingTimeOut: 20000, manifestLoadingMaxRetry: 4, manifestLoadingRetryDelay: 1500,
          levelLoadingTimeOut: 20000, levelLoadingMaxRetry: 4, fragLoadingTimeOut: 25000, fragLoadingMaxRetry: 4,
          liveSyncDurationCount: 1, liveMaxLatencyDurationCount: 4, maxBufferLength: 4, maxMaxBufferLength: 8,
          backBufferLength: 0, lowLatencyMode: true, startFragPrefetch: true, enableWorker: true,
        });
        let mediaRecoveries = 0;
        out.hls = hls;
        hls.loadSource(src);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => { if (opts.onLevels) opts.onLevels(hls); video.play().catch(() => {}); });
        const markLive = () => { if (!destroyed) onState('live'); };
        video.addEventListener('loadeddata', markLive, { once: true });
        video.addEventListener('canplay', markLive, { once: true });
        video.addEventListener('playing', markLive, { once: true });
        hls.on(Hls.Events.ERROR, (_e, data) => {
          if (!data || !data.fatal || destroyed) return;
          if (data.type === Hls.ErrorTypes.MEDIA_ERROR && mediaRecoveries < 2) {
            mediaRecoveries += 1; hls.recoverMediaError(); return;
          }
          hls.destroy(); out.hls = null;
          if (retriesLeft > 0) { onState('loading', `Mencoba ulang (${5 - retriesLeft}/4)…`); retryTimer = setTimeout(() => startHls(retriesLeft - 1), 3000); }
          else onState('error', 'Kamera offline / timeout');
        });
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = src;
        video.addEventListener('playing', () => onState('live'), { once: true });
        video.addEventListener('error', () => onState('error', 'Stream error'), { once: true });
      } else onState('error', 'Browser tidak mendukung HLS');
    };
    startHls(4);
    out.cleanup = () => {
      destroyed = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (out.hls) { try { out.hls.destroy(); } catch { /* abaikan */ } out.hls = null; }
      video.pause(); video.removeAttribute('src'); video.load(); video.remove();
    };
    return out;
  }

  function qualityTier(level) {
    const h = level.height || 0, w = level.width || 0;
    if (h >= 2160 || w >= 3840) return { key: '2160', label: `4K (${h || 2160}p)`, short: '4K', order: 4 };
    if (h >= 1080 || w >= 1920) return { key: '1080', label: `Full HD (${h || 1080}p)`, short: `${h || 1080}p`, order: 3 };
    if (h >= 720 || w >= 1280) return { key: '720', label: `HD (${h || 720}p)`, short: `${h || 720}p`, order: 2 };
    if (h > 0) return { key: '480', label: `SD (${h}p)`, short: `${h}p`, order: 1 };
    const kbps = level.bitrate ? Math.round(level.bitrate / 1000) : 0;
    return { key: String(kbps || '0'), label: kbps ? `${kbps} kbps` : 'Stream', short: kbps ? `${kbps}k` : 'Live', order: 0 };
  }
  function pickLevel(levels, preferred) {
    if (!levels || !levels.length || preferred === 'auto') return -1;
    const target = Number(preferred);
    let best = 0, diff = Infinity;
    levels.forEach((l, i) => { const h = l.height || 0; const d = Math.abs(h - target) + (h > target ? 500 : 0); if (d < diff) { diff = d; best = i; } });
    return best;
  }
  function applyQuality(hls, sel, preferred, initial = false) {
    if (!hls || !hls.levels || !hls.levels.length) return;
    if (hls.levels.length === 1) { if (sel) { sel.innerHTML = `<option value="0">${esc(qualityTier(hls.levels[0]).short)}</option>`; sel.disabled = true; } return; }
    const idx = pickLevel(hls.levels, preferred);
    if (initial) hls.startLevel = idx; else hls.nextLevel = idx;
    if (sel) {
      sel.disabled = false;
      sel.innerHTML = '<option value="-1">Auto</option>' + hls.levels.map((l, i) => `<option value="${i}">${esc(qualityTier(l).short)}</option>`).join('');
      sel.value = String(idx);
    }
  }
  function syncGlobalQuality() {
    const tiers = new Map();
    for (const t of tiles) { const lv = t._player?.hls?.levels; if (lv) for (const l of lv) { const q = qualityTier(l); if (!tiers.has(q.key)) tiers.set(q.key, q); } }
    const sorted = [...tiers.values()].sort((a, b) => a.order - b.order);
    els.quality.innerHTML = '<option value="auto">Auto</option>' + sorted.map((t) => `<option value="${esc(t.key)}">${esc(t.label)}</option>`).join('');
    els.quality.value = tiers.has(state.quality) ? state.quality : 'auto';
  }

  // ------------------------------------------------------------- sidebar
  const PROVIDER_LABEL = { lifemedia: 'Lifemedia', diskominfo: 'Jepara', kuduskab: 'Kudus', youtube: 'Demak', jombangkab: 'Jombang', tol: 'Tol', jogja: 'Jogja', semarang: 'Semarang', pati: 'Pati', rembang: 'Rembang', manual: 'Kamera saya' };
  function renderPills() {
    const provs = [...new Set(state.cameras.map((c) => c.provider || 'manual'))];
    els.pills.innerHTML = [['all', 'Semua'], ...provs.map((p) => [p, PROVIDER_LABEL[p] || p])].map(([k, l]) => `<button class="pill${state.provider === k ? ' active' : ''}" data-provider="${esc(k)}">${esc(l)}</button>`).join('');
  }
  function renderPresetOptions() {
    const opts = [['all', 'Semua Kamera (Paginated)'], ...folderNames().map((f) => ['folder:' + f, 'Area ' + f]), ['pusat', 'Pusat Kota & Alun-Alun'], ['rsud', 'RSUD Kartini (1–4)'], ['rbra', 'RBRA (1–5)'], ['jalan', 'Jalan Utama'], ...state.customFolders.map((f) => ['cf:' + f.id, '★ ' + f.name]), ['custom', 'Kustom (Pilihan Sendiri)']];
    els.presetSel.innerHTML = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
    els.presetSel.value = opts.some(([v]) => v === state.preset) ? state.preset : 'all';
  }

  function renderSidebar() {
    const q = state.search.trim();
    const inView = new Set(state.view === 'pantau' ? currentItems().filter(Boolean).map(camKey) : []);
    const fset = new Set(filtered());
    const entries = [];
    for (const f of state.customFolders) entries.push({ kind: 'custom', key: 'custom:' + f.id, f, cams: f.keys.map((k) => state.byKey.get(k)).filter((c) => c && fset.has(c)) });
    const groups = new Map();
    for (const c of fset) { const f = c.folder || 'Lainnya'; if (!groups.has(f)) groups.set(f, []); groups.get(f).push(c); }
    for (const [name, list] of groups) entries.push({ kind: 'region', key: 'region:' + name, name, cams: list });
    const order = new Map(state.folderOrder.map((k, i) => [k, i]));
    entries.sort((a, b) => (order.get(a.key) ?? 1e9) - (order.get(b.key) ?? 1e9));
    els.list._entries = entries;
    if (entries.length && !els.list.dataset.intro) {
      els.list.dataset.intro = '1';
      els.list.classList.add('intro');
      setTimeout(() => els.list.classList.remove('intro'), 900);
    }
    if (!entries.length) { els.list.innerHTML = `<div class="empty-state">${state.loaded ? 'Tidak ada kamera yang cocok.' : 'Memuat daftar kamera…'}</div>`; return; }
    const aiSet = new Set(state.aiSettings?.cameras || []);
    els.list.innerHTML = entries.map((e) => {
      const hide = state.hideOffline || state.hideOfflineFolders.has(e.key);
      const vis = hide ? e.cams.filter((c) => !isOffline(c)) : e.cams;
      const open = state.expanded.has(e.key) || (q && e.cams.length);
      const custom = e.kind === 'custom';
      const label = custom ? e.f.name : e.name;
      const off = !custom && state.apiStatus[e.name] && state.apiStatus[e.name].online === false ? '<span class="api-off-badge" title="API sumber sedang tidak merespons">Off</span>' : '';
      const LIMIT = 150;
      return `<div class="folder-group${open ? '' : ' collapsed'}${state.justOpened === e.key ? ' opening' : ''}" data-key="${esc(e.key)}">
        <div class="folder-header">
          <div class="folder-title"><span class="folder-chevron">${ic('chev-down')}</span>${custom ? ic('bookmark', 'is-custom') : ic('folder')}
            <span class="folder-name">${esc(label)}</span><span class="folder-count">${vis.length !== e.cams.length ? vis.length + '/' : ''}${e.cams.length}</span>${off}</div>
          <div class="folder-actions">
            <button class="folder-act-btn folder-move-btn" data-act="up" title="Naikkan urutan" aria-label="Naikkan urutan">${ic('chev-up')}</button>
            <button class="folder-act-btn folder-move-btn" data-act="down" title="Turunkan urutan" aria-label="Turunkan urutan">${ic('chev-down')}</button>
            <button class="folder-act-btn${hide ? ' active' : ''}" data-act="hide" title="${hide ? 'Tampilkan CCTV offline' : 'Cek status & sembunyikan CCTV offline'}">${ic(hide ? 'eye-off' : 'eye')}</button>
            <button class="folder-act-btn" data-act="play" title="Putar folder ini di multiview">${ic('play')}</button>
            ${custom ? `<button class="folder-act-btn" data-act="edit" title="Edit nama &amp; daftar CCTV">${ic('pencil')}</button><button class="folder-act-btn del" data-act="del" title="Hapus folder">${ic('trash')}</button>` : ''}
          </div>
        </div>
        <div class="folder-items">${open ? vis.slice(0, LIMIT).map((c) => {
          const k = camKey(c); const st = state.status.get(k) || '';
          return `<div class="camera-item${inView.has(k) ? ' in-view' : ''}" data-cam="${esc(k)}" title="Tampilkan ${esc(c.name)} di layar besar">
            <div class="camera-item-info"><span class="cam-status-dot ${st}"></span><span class="cam-num">${esc(c.id)}</span><span class="cam-name">${esc(c.name)}</span></div>
            <div class="cam-row-actions">${aiSet.has(k) ? '<span class="tile-ai-tag" title="Dipantau AI">AI</span>' : ''}
              ${!custom && isManual(c) ? '<button class="cam-add-btn" data-act="del-manual" title="Hapus kamera manual">×</button>' : ''}
              <button class="cam-add-btn" data-act="${custom ? 'rm' : 'add'}" title="${custom ? 'Hapus dari folder ini' : 'Tambahkan ke folder custom'}">${custom ? '×' : '+'}</button>
              <span class="provider-tag ${esc(c.provider || 'manual')}">${esc(c.provider || 'manual')}</span></div>
          </div>`; }).join('') + (vis.length > LIMIT ? `<div class="empty-state" style="padding:8px;text-align:left">+${vis.length - LIMIT} kamera lagi · persempit dengan pencarian</div>` : '') + (!vis.length ? `<div class="empty-state" style="padding:8px;text-align:left">${e.cams.length ? 'Semua CCTV di folder ini offline.' : 'Folder kosong. Klik ikon pensil untuk menambah CCTV.'}</div>` : '') : ''}</div>
      </div>`;
    }).join('');
  }

  els.list.addEventListener('click', async (e) => {
    const grp = e.target.closest('.folder-group'); if (!grp) return;
    const key = grp.dataset.key;
    const entry = (els.list._entries || []).find((x) => x.key === key); if (!entry) return;
    const act = e.target.closest('[data-act]')?.dataset.act;
    const item = e.target.closest('.camera-item');
    if (item) {
      const cam = state.byKey.get(item.dataset.cam); if (!cam) return;
      if (act === 'add') { quickAdd(cam); return; }
      if (act === 'rm') { entry.f.keys = entry.f.keys.filter((k) => k !== item.dataset.cam); saveFolders(); renderSidebar(); return; }
      if (act === 'del-manual') { App.deleteManual?.(cam); return; }
      App.closeDrawer?.(); playKeys([camKey(cam)], 1, 1); return;
    }
    if (act === 'up' || act === 'down') {
      const keys = els.list._entries.map((x) => x.key), i = keys.indexOf(key), j = i + (act === 'up' ? -1 : 1);
      if (j < 0 || j >= keys.length) return;
      [keys[i], keys[j]] = [keys[j], keys[i]]; state.folderOrder = keys; LS.set(K.order, keys); renderSidebar(); return;
    }
    if (act === 'hide') {
      if (state.hideOfflineFolders.has(key)) state.hideOfflineFolders.delete(key);
      else {
        state.hideOfflineFolders.add(key);
        if (entry.kind === 'region') await checkStatus({ folder: entry.name });
        else await checkStatus({ ids: entry.cams.map((c) => c.id) });
      }
      LS.set(K.hideF, [...state.hideOfflineFolders]); renderSidebar(); if (state.view === 'pantau') renderGrid(); return;
    }
    if (act === 'play') { App.closeDrawer?.(); if (entry.kind === 'custom') playPreset('cf:' + entry.f.id); else playPreset('folder:' + entry.name); return; }
    if (act === 'edit') { openFolderModal(entry.f.id); return; }
    if (act === 'del') { confirmBox('Hapus folder?', `Folder “${entry.f.name}” akan dihapus. Kamera di dalamnya tidak ikut terhapus.`, () => { state.customFolders = state.customFolders.filter((f) => f !== entry.f); saveFolders(); renderPresetOptions(); renderFillSelect(); renderSidebar(); }); return; }
    if (state.expanded.has(key)) state.expanded.delete(key); else { state.expanded.add(key); state.justOpened = key; }
    renderSidebar();
    state.justOpened = null;
  });
  els.pills.addEventListener('click', (e) => {
    const b = e.target.closest('.pill'); if (!b) return;
    state.provider = b.dataset.provider; state.page = 0; renderPills(); renderSidebar();
    if (state.view === 'pantau' && state.preset !== 'custom') renderGrid();
  });
  $('#search-input').addEventListener('input', (e) => { state.search = e.target.value; state.page = 0; renderSidebar(); if (state.view === 'pantau' && state.preset !== 'custom') renderGrid(); });
  els.presetSel.addEventListener('change', () => playPreset(els.presetSel.value));
  $('#refresh-api-btn').addEventListener('click', async (e) => {
    const b = e.currentTarget; b.disabled = true;
    await loadCameras(true); b.disabled = false; App.renderView?.();
  });
  $('#new-folder-btn').addEventListener('click', () => openFolderModal(null));

  function quickAdd(cam) {
    if (!state.customFolders.length) { openFolderModal(null, [camKey(cam)]); return; }
    const choose = (f) => { if (!f.keys.includes(camKey(cam))) f.keys.push(camKey(cam)); saveFolders(); renderSidebar(); toast(`${cam.name} → folder “${f.name}”`); };
    if (state.customFolders.length === 1) { choose(state.customFolders[0]); return; }
    openModal(`<div class="modal-header"><div class="modal-heading"><h2 id="modal-title">Tambahkan ke folder</h2><p class="modal-subtitle">${esc(cam.name)}</p></div><button class="icon-btn close-btn" data-close-modal aria-label="Tutup">${ic('x')}</button></div>
      <div class="folder-picker-list">${state.customFolders.map((f) => `<div class="picker-item" data-fid="${esc(f.id)}"><span class="picker-item-left">${ic('bookmark')}<span>${esc(f.name)}</span></span><span class="picker-folder-tag">${f.keys.length} kamera</span></div>`).join('')}</div>`, 'confirm-dialog');
    els.dialog.style.width = 'min(460px,100%)';
    $$('[data-fid]', els.dialog).forEach((it) => it.onclick = () => { closeModal(); choose(state.customFolders.find((f) => f.id === it.dataset.fid)); });
  }

  function openFolderModal(fid, preset = []) {
    const f = fid ? state.customFolders.find((x) => x.id === fid) : { id: `f${Date.now()}`, name: '', keys: [...preset] };
    const sel = new Set(f.keys);
    openModal(`<span class="sheet-handle" aria-hidden="true"></span>
      <div class="modal-header"><div class="modal-heading"><h2 id="modal-title">${fid ? 'Kelola Folder' : 'Folder Baru'}</h2><p class="modal-subtitle">Centang CCTV yang ingin dimasukkan ke folder ini</p></div><button class="icon-btn close-btn" data-close-modal aria-label="Tutup">${ic('x')}</button></div>
      <div class="folder-modal-toolbar"><input type="text" id="fm-name" class="input" placeholder="Nama folder" value="${esc(f.name)}" maxlength="60"><div class="search-box"><svg class="ic"><use href="#i-search"/></svg><input type="search" id="fm-q" placeholder="Cari nama CCTV atau daerah…"></div><span class="folder-count" id="fm-cnt">${sel.size} dipilih</span></div>
      <div class="folder-picker-list" id="fm-list"></div>
      <div class="modal-footer">${fid ? `<button class="action-btn danger-btn" id="fm-del">${ic('trash')}<span>Hapus folder</span></button>` : ''}<button class="action-btn primary-btn" id="fm-save">Simpan &amp; tutup</button></div>`, 'folder-modal-dialog');
    const draw = () => {
      const q = $('#fm-q').value.toLowerCase();
      const rows = state.cameras.filter((c) => !q || `${c.name} ${c.folder}`.toLowerCase().includes(q));
      rows.sort((a, b) => sel.has(camKey(b)) - sel.has(camKey(a)));
      $('#fm-list').innerHTML = rows.slice(0, 200).map((c) => { const k = camKey(c); return `<label class="picker-item"><span class="picker-item-left"><input type="checkbox" value="${esc(k)}"${sel.has(k) ? ' checked' : ''} class="pick-check"><span>${esc(c.name)}</span></span><span class="picker-folder-tag">${esc(c.folder || '')}</span></label>`; }).join('');
    };
    draw(); $('#fm-q').oninput = draw;
    $('#fm-list').onchange = (e) => { const k = e.target.value; if (e.target.checked) sel.add(k); else sel.delete(k); $('#fm-cnt').textContent = `${sel.size} dipilih`; };
    $('#fm-save').onclick = () => {
      f.name = $('#fm-name').value.trim() || 'Folder baru'; f.keys = [...sel];
      if (!fid) state.customFolders.push(f);
      saveFolders(); closeModal(); renderPresetOptions(); renderFillSelect(); renderSidebar();
      if (state.view === 'home') renderHome();
      toast(`Folder “${f.name}” disimpan`);
    };
    const d = $('#fm-del'); if (d) d.onclick = () => { state.customFolders = state.customFolders.filter((x) => x !== f); saveFolders(); closeModal(); renderPresetOptions(); renderFillSelect(); renderSidebar(); };
  }

  function openPicker(slot) {
    openModal(`<span class="sheet-handle" aria-hidden="true"></span>
      <div class="modal-header"><div class="modal-heading"><h2 id="modal-title">Pilih kamera · slot ${slot + 1}</h2><p class="modal-subtitle">Daftar nyata dari server (CCTV publik + kamera saya)</p></div><button class="icon-btn close-btn" data-close-modal aria-label="Tutup">${ic('x')}</button></div>
      <div class="folder-modal-toolbar"><div class="search-box" style="flex:1"><svg class="ic"><use href="#i-search"/></svg><input type="search" id="pk-q" placeholder="Cari nama / wilayah kamera…"></div><button class="action-btn danger-btn" id="pk-empty">Kosongkan slot</button></div>
      <div class="folder-picker-list" id="pk-list"></div>`, 'folder-modal-dialog');
    const draw = () => {
      const q = $('#pk-q').value.toLowerCase();
      $('#pk-list').innerHTML = state.cameras.filter((c) => !q || `${c.name} ${c.folder}`.toLowerCase().includes(q)).slice(0, 200).map((c) => `<div class="picker-item" data-pk="${esc(camKey(c))}"><span class="picker-item-left"><span class="cam-status-dot ${state.status.get(camKey(c)) || ''}"></span><span>${esc(c.name)}</span></span><span class="picker-folder-tag">${esc(c.folder || '')}</span></div>`).join('');
    };
    draw(); $('#pk-q').oninput = draw; $('#pk-q').focus();
    $('#pk-list').onclick = (e) => { const it = e.target.closest('[data-pk]'); if (!it) return; ensureCustom(); while (state.slots.length < perPage()) state.slots.push(null); state.slots[slot] = it.dataset.pk; saveLayout(); closeModal(); renderGrid(); };
    $('#pk-empty').onclick = () => { ensureCustom(); state.slots[slot] = null; saveLayout(); closeModal(); renderGrid(); };
  }

  // ------------------------------------------------------------ multiview
  const perPage = () => state.gridCols * state.gridRows;
  function presetList(p = state.preset) {
    const base = filtered();
    if (p.startsWith('folder:')) return base.filter((c) => (c.folder || 'Lainnya') === p.slice(7));
    if (p.startsWith('cf:')) { const f = state.customFolders.find((x) => x.id === p.slice(3)); return f ? f.keys.map((k) => state.byKey.get(k)).filter(Boolean) : []; }
    if (p === 'pusat') return base.filter((c) => /alun|simpang|tugu|pusat|kartini square/i.test(c.name));
    if (p === 'rsud') return base.filter((c) => /rsud|kartini/i.test(c.name));
    if (p === 'rbra') return base.filter((c) => /rbra/i.test(c.name));
    if (p === 'jalan') return base.filter((c) => /\bjl\b|jalan|raya|pertigaan|perempatan/i.test(c.name));
    return base;
  }
  function currentItems() {
    if (state.focusKey) return [state.byKey.get(state.focusKey)].filter(Boolean);
    if (state.preset === 'custom') {
      const n = perPage();
      const s = state.slots.slice(0, n); while (s.length < n) s.push(null);
      state.slots = s;
      return s.map((k) => (k ? state.byKey.get(k) || null : null));
    }
    let list = presetList();
    if (state.hideOffline) list = list.filter((c) => !isOffline(c));
    state.pages = Math.max(1, Math.ceil(list.length / perPage()));
    if (state.page >= state.pages) state.page = 0;
    return list.slice(state.page * perPage(), (state.page + 1) * perPage());
  }
  function playPreset(p) { state.preset = p; state.page = 0; state.focusKey = null; saveLayout(); App.go('pantau'); }
  function playKeys(keys, cols, rows) {
    state.preset = 'custom'; state.focusKey = null;
    if (cols) { state.gridCols = cols; state.gridRows = rows; }
    else { const n = Math.max(1, keys.length); const c = n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4; state.gridCols = c; state.gridRows = Math.min(8, Math.ceil(n / c)); }
    state.slots = keys.slice(0, perPage());
    saveLayout(); App.go('pantau');
  }

  function destroyTiles() {
    for (const t of tiles) destroyTile(t);
    tiles.clear();
  }
  function destroyTile(t) {
    App.stopRecording?.(t, true);
    if (t._livePoll) clearInterval(t._livePoll);
    if (t._player) { try { t._player.cleanup(); } catch { /* abaikan */ } t._player = null; }
  }

  function renderGrid() {
    destroyTiles();
    const items = currentItems();
    const custom = state.preset === 'custom' && !state.focusKey;
    const cols = state.focusKey ? 1 : state.gridCols, rows = state.focusKey ? 1 : state.gridRows;
    els.grid.className = 'cctv-grid';
    els.grid.dataset.cols = String(cols);
    els.grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
    els.grid.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
    els.pageInd.textContent = custom ? 'Kustom' : `Hal ${state.page + 1}/${state.pages}`;
    els.grid.innerHTML = '';
    if (!state.loaded) { els.grid.innerHTML = '<div class="empty-state">Memuat daftar kamera…</div>'; return; }
    items.forEach((cam, i) => els.grid.appendChild(cam ? createTile(cam, i) : emptyTile(i)));
    if (!items.length) els.grid.innerHTML = '<div class="empty-state">Tidak ada kamera untuk filter ini.</div>';
    if (state.liveAi && !items.some((c) => c && camKey(c) === state.liveAi.key)) stopLiveAi(true);
    renderEditHelp();
    syncGlobalQuality(); renderSidebar(); updateSummary(); syncLayoutButtons();
  }
  function renderEditHelp() {
    const h = $('#edit-help');
    if (state.view !== 'pantau') { h.classList.add('hidden'); return; }
    if (state.focusKey) {
      const c = state.byKey.get(state.focusKey);
      h.classList.remove('hidden');
      h.innerHTML = `${ic('maximize')}<span><b>Fokus:</b> ${esc(c ? c.name : '')} — tampilan satu kamera.</span><button class="mini-accent-btn" data-help="back">← Kembali ke grid</button>`;
    } else if (state.editMode) {
      h.classList.remove('hidden');
      h.innerHTML = `${ic('layout')}<span><b>Mode Atur aktif.</b> Klik satu tile lalu tile lain untuk menukar, atau seret &amp; letakkan. <b>Ganti</b> memilih kamera, <b>Kosongkan</b> mengosongkan slot.</span><button class="mini-accent-btn" data-help="done">Selesai</button>`;
    } else h.classList.add('hidden');
  }
  $('#edit-help').addEventListener('click', (e) => {
    const a = e.target.closest('[data-help]')?.dataset.help;
    if (a === 'back') { state.focusKey = null; renderGrid(); }
    if (a === 'done') setEditMode(false);
  });

  function createTile(cam, i) {
    const key = camKey(cam), type = camType(cam);
    const yt = type === 'youtube', snap = type === 'http', mj = type === 'mjpeg';
    const aiOn = new Set(state.aiSettings?.cameras || []).has(key);
    const recOn = state.recSettings?.record_enabled && (state.recSettings?.continuous_cameras || []).includes(key);
    const t = document.createElement('div');
    t.className = 'cam-tile' + (state.selectedSlot === i ? ' selected' : '');
    t.dataset.status = 'loading'; t.dataset.slot = i; t.dataset.key = key;
    t.style.setProperty('--i', i);
    t.draggable = state.editMode;
    t.innerHTML = `
      <div class="tile-media">
        <div class="media-host"></div>
        <div class="tile-badges"><span class="live-pill">LIVE</span><span class="rec-badge" data-role="rec-badge">00:00</span></div>
        <button class="tile-reload" data-action="reload" title="Muat ulang" aria-label="Muat ulang">${ic('rotate')}</button>
        <div class="tile-actions">
          ${cam.tuyaDeviceId ? `<a class="tile-btn" href="https://protect-us.ismartlife.me/" target="_blank" rel="noopener noreferrer" style="text-decoration:none" title="Buka Tuya Smart Life Web (HD)">${ic('arrow-ur')}<span>HD Web</span></a>` : ''}
          ${yt || mj || snap ? '' : '<select class="tile-quality-select" data-role="quality" title="Kualitas stream kamera ini" aria-label="Kualitas stream kamera ini"><option value="-1">Auto</option></select>'}
          ${yt || snap || !can('liveai.use') ? '' : `<button class="tile-btn ai-btn" data-action="liveai" title="Live AI — deteksi realtime (pilot 1 kamera)">${ic('scan')}<span>Live AI</span></button>`}
          ${yt ? '' : `<button class="tile-btn" data-action="record" title="Rekam ke browser ini" aria-label="Rekam">${ic('rec')}<span>Rec</span></button>`}
          ${yt ? '' : `<button class="tile-btn" data-action="snapshot" title="Ambil gambar (PNG)" aria-label="Ambil gambar">${ic('camera')}</button>`}
        </div>
        ${/jepara\.go\.id/.test(cam.url || '') ? '<img class="tile-watermark" src="https://samudra.jepara.go.id/assets/public/media/logos/jepara.webp" alt="Pemkab Jepara">' : ''}
        ${(cam.onvif || cam.tuyaDeviceId) ? `<div class="ptz-pad">${[['zoom_in', '＋', 'Zoom in'], ['up', '▲', 'Atas'], ['zoom_out', '－', 'Zoom out'], ['left', '◀', 'Kiri'], ['stop', '■', 'Stop'], ['right', '▶', 'Kanan'], null, ['down', '▼', 'Bawah'], null].map((p) => (p ? `<button class="ptz-btn" data-ptz="${p[0]}" title="${p[2]}">${p[1]}</button>` : '<span></span>')).join('')}</div>` : ''}
        <span class="ai-badge" data-role="ai-badge">LIVE AI</span>
        <div class="slot-tools"><button data-action="pick">${ic('swap', 'ic-sm')}Ganti</button><button class="danger" data-action="clear">${ic('x', 'ic-sm')}Kosongkan</button></div>
        <div class="tile-state" data-role="state"><div class="spinner"></div><span data-role="state-text">Menghubungkan...</span>
          <button class="action-btn tile-hide-btn hidden" data-action="hide-tile">${ic('eye-off')}<span>Sembunyikan</span></button></div>
      </div>
      <div class="tile-footer">
        <div class="tile-meta">
          <span class="tile-name" title="${esc(cam.name)}">${esc(cam.name)}</span>
          <span class="tile-sub"><span class="prov-dot ${esc(cam.provider || 'manual')}"></span><span class="tile-folder">${esc(cam.folder || '')}</span><span class="sep"></span>
            <span class="status-dot loading" data-role="dot"></span><span class="status-text" data-role="status-text">Menghubungkan</span>
            <span class="tile-type">${streamLabel(cam)}</span>
            ${aiOn ? '<span class="tile-ai-tag" title="Dipantau AI Jepretan">AI</span>' : ''}${recOn ? '<span class="tile-ai-tag rec" title="Direkam server berkelanjutan">REC</span>' : ''}</span>
        </div>
        <button class="tile-go" data-action="fullscreen" title="Fokus satu kamera (klik dua kali video: layar penuh)" aria-label="Fokus">${ic('maximize')}</button>
      </div>`;
    tiles.add(t);
    if (state.liveAi && state.liveAi.key === key) attachLiveAi(t, cam);
    else mountTilePlayer(t, cam);
    return t;
  }
  function mountTilePlayer(t, cam) {
    const host = $('.media-host', t);
    const qSel = $('[data-role="quality"]', t);
    t._player = mountPlayer(host, cam, {
      onState: (st, msg) => setTileStatus(t, cam, st, msg),
      onLevels: (hls) => { applyQuality(hls, qSel, state.quality, true); syncGlobalQuality(); },
    });
    const m = t._player.mediaEl;
    if (m) m.addEventListener('dblclick', () => { if (!state.editMode) toggleFullscreen(t); });
  }
  function setTileStatus(t, cam, st, msg = '') {
    if (!t.isConnected && !tiles.has(t)) return;
    if (st === 'live' && t.dataset.status !== 'live' && !reduceMotion()) {
      t.classList.add('just-live');
      setTimeout(() => t.classList.remove('just-live'), 1100);
    }
    t.dataset.status = st;
    const dot = $('[data-role="dot"]', t); if (dot) dot.className = `status-dot ${st}`;
    const stTxt = $('[data-role="status-text"]', t); if (stTxt) stTxt.textContent = st === 'live' ? 'Live' : st === 'error' ? 'Offline' : 'Menghubungkan';
    const box = $('[data-role="state"]', t);
    box.style.display = st === 'live' ? 'none' : '';
    $('.spinner', t).style.display = st === 'loading' ? '' : 'none';
    $('[data-role="state-text"]', t).textContent = st === 'error' ? (msg || 'Stream Error') : (msg || 'Menghubungkan...');
    $('[data-action="hide-tile"]', t).classList.toggle('hidden', st !== 'error');
    if (st === 'live') state.status.set(camKey(cam), 'online');
    if (st === 'error') state.status.set(camKey(cam), 'offline');
  }
  function emptyTile(i) {
    const t = document.createElement('div');
    t.className = 'cam-tile empty-slot' + (state.selectedSlot === i ? ' selected' : '');
    t.dataset.slot = i; t.dataset.status = 'empty'; t.style.setProperty('--i', i); t.draggable = state.editMode;
    t.innerHTML = `<div class="tile-media"><button class="empty-slot-inner" data-action="pick"><span class="ic-wrap">${ic('plus')}</span>Slot ${i + 1} kosong · pilih kamera</button></div><div class="tile-footer"><div class="tile-meta"><span class="tile-name">Slot kosong</span><span class="tile-sub">Tata letak kustom</span></div></div>`;
    return t;
  }
  function toggleFullscreen(t) {
    try { if (document.fullscreenElement) document.exitFullscreen(); else t.requestFullscreen?.().catch(() => {}); } catch { /* abaikan */ }
  }

  els.grid.addEventListener('click', (e) => {
    const tile = e.target.closest('.cam-tile'); if (!tile || tile.closest('.home')) return;
    const slot = Number(tile.dataset.slot);
    const cam = tile.dataset.key ? state.byKey.get(tile.dataset.key) : null;
    const act = e.target.closest('[data-action]')?.dataset.action;
    if (e.target.closest('[data-ptz]') || e.target.closest('select')) return;
    if (act === 'pick') { openPicker(slot); return; }
    if (act === 'clear') { ensureCustom(); state.slots[slot] = null; saveLayout(); renderGrid(); return; }
    if (state.editMode && !act) {
      ensureCustom();
      if (state.selectedSlot == null) { state.selectedSlot = slot; tile.classList.add('selected'); }
      else { swap(state.selectedSlot, slot); state.selectedSlot = null; saveLayout(); renderGrid(); }
      return;
    }
    if (!cam) return;
    if (act === 'reload') { App.stopRecording?.(tile, false); if (tile._player) tile._player.cleanup(); mountTilePlayer(tile, cam); return; }
    if (act === 'hide-tile') { state.hideOffline = true; LS.set(K.hideGrid, true); syncHideBtn(); toast('CCTV offline disembunyikan dari multiview'); renderGrid(); return; }
    if (act === 'snapshot') { App.takeSnapshot?.(tile, cam); return; }
    if (act === 'record') { App.toggleRecording?.(tile, cam); return; }
    if (act === 'liveai') { toggleLiveAi(tile, cam); return; }
    if (act === 'fullscreen') { state.focusKey = state.focusKey ? null : camKey(cam); renderGrid(); }
  });
  els.grid.addEventListener('change', (e) => {
    const sel = e.target.closest('[data-role="quality"]'); if (!sel) return;
    const tile = sel.closest('.cam-tile'); const hls = tile?._player?.hls; if (!hls) return;
    hls.nextLevel = Number(sel.value);
  });
  const sendPtz = (cam, action) => {
    if (cam.tuyaDeviceId) return api('/api/tuya/ptz', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ deviceId: cam.tuyaDeviceId, action }) }).catch(() => {});
    return api('/api/onvif/ptz', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ manualId: cam.manualId || '', action }) }).catch(() => {});
  };
  els.grid.addEventListener('pointerdown', (e) => {
    const b = e.target.closest('[data-ptz]'); if (!b) return;
    e.stopPropagation();
    const cam = state.byKey.get(b.closest('.cam-tile').dataset.key); if (!cam || (!cam.onvif && !cam.tuyaDeviceId)) return;
    const act = b.dataset.ptz;
    sendPtz(cam, act);
    if (act !== 'stop') {
      const stop = () => { sendPtz(cam, 'stop'); b.removeEventListener('pointerup', stop); b.removeEventListener('pointerleave', stop); };
      b.addEventListener('pointerup', stop); b.addEventListener('pointerleave', stop);
    }
  });
  let dragFrom = null;
  els.grid.addEventListener('dragstart', (e) => { const t = e.target.closest('.cam-tile'); if (!t || !state.editMode) return; dragFrom = Number(t.dataset.slot); e.dataTransfer.effectAllowed = 'move'; });
  els.grid.addEventListener('dragover', (e) => { if (dragFrom == null) return; e.preventDefault(); $$('.drop-over', els.grid).forEach((x) => x.classList.remove('drop-over')); e.target.closest('.cam-tile')?.classList.add('drop-over'); });
  els.grid.addEventListener('drop', (e) => { const t = e.target.closest('.cam-tile'); if (dragFrom == null || !t) return; e.preventDefault(); ensureCustom(); swap(dragFrom, Number(t.dataset.slot)); dragFrom = null; saveLayout(); renderGrid(); });
  els.grid.addEventListener('dragend', () => { dragFrom = null; $$('.drop-over', els.grid).forEach((x) => x.classList.remove('drop-over')); });

  function ensureCustom() {
    if (state.preset === 'custom') return;
    state.slots = currentItems().map((c) => (c ? camKey(c) : null));
    state.preset = 'custom'; els.presetSel.value = 'custom'; saveLayout();
  }
  function swap(a, b) { while (state.slots.length < perPage()) state.slots.push(null); [state.slots[a], state.slots[b]] = [state.slots[b], state.slots[a]]; }
  function setEditMode(on) {
    state.editMode = on; state.selectedSlot = null; state.focusKey = null;
    if (on) ensureCustom();
    document.body.classList.toggle('edit-mode', on);
    $('#edit-layout-btn').classList.toggle('active', on);
    if (state.view !== 'pantau') App.go('pantau'); else renderGrid();
  }
  $('#edit-layout-btn').addEventListener('click', () => setEditMode(!state.editMode));

  // ------------------------------------------------------- Live AI (server)
  async function toggleLiveAi(tile, cam) {
    const key = camKey(cam);
    if (state.liveAi && state.liveAi.key === key) { stopLiveAi(true); toast('Live AI dihentikan'); return; }
    if (state.liveAi) { toast(`Live AI sedang aktif di ${state.byKey.get(state.liveAi.key)?.name || 'kamera lain'}. Matikan dulu (pilot 1 kamera).`, 'err'); return; }
    const btn = $('[data-action="liveai"]', tile);
    if (btn) { btn.disabled = true; btn.innerHTML = `${ic('loader', 'spin')}<span>Menyiapkan…</span>`; }
    try { await postJson(`/api/live-ai/${encodeURIComponent(key)}/start`); }
    catch (err) {
      toast(`Live AI gagal: ${err.message}`, 'err');
      if (btn) { btn.disabled = false; btn.innerHTML = `${ic('scan')}<span>Live AI</span>`; }
      return;
    }
    state.liveAi = { key };
    App.stopRecording?.(tile, false);
    if (tile._player) { tile._player.cleanup(); tile._player = null; }
    attachLiveAi(tile, cam);
    toast(`Live AI aktif di ${cam.name} — berhenti otomatis setelah 10 menit`);
    App.renderStatusBits?.();
  }
  function attachLiveAi(tile, cam) {
    const key = camKey(cam);
    const host = $('.media-host', tile);
    const img = document.createElement('img');
    img.className = 'mjpeg-view'; img.alt = '';
    img.src = `/api/live-ai/${encodeURIComponent(key)}.mjpg`;
    host.appendChild(img);
    tile._player = { mediaEl: img, hls: null, cleanup: () => { img.removeAttribute('src'); img.remove(); } };
    tile.classList.add('live-ai');
    setTileStatus(tile, cam, 'live');
    const btn = $('[data-action="liveai"]', tile);
    if (btn) { btn.disabled = false; btn.classList.add('on'); btn.innerHTML = `${ic('stop')}<span>Stop AI</span>`; }
    const badge = $('[data-role="ai-badge"]', tile);
    badge.textContent = 'LIVE AI · menyiapkan…';
    const poll = async () => {
      try {
        const st = await apiJson(`/api/live-ai/status?camera=${encodeURIComponent(key)}`);
        if (!st.active && !st.starting) {
          if (state.liveAi && state.liveAi.key === key) {
            state.liveAi = null;
            toast(`Live AI berhenti — ${st.ended_reason || 'auto-stop / dimatikan.'}`, /terputus|tidak mengirim|error|gagal/i.test(st.ended_reason || '') ? 'err' : '');
            App.renderStatusBits?.();
            if (tile.isConnected) { clearInterval(tile._livePoll); resetLiveAiTile(tile); mountTilePlayer(tile, cam); }
          }
          return;
        }
        const menit = Math.ceil((st.remaining_seconds || 0) / 60);
        badge.textContent = `LIVE AI · tampil ${Math.round(st.display_fps || 0)} fps · deteksi ${Number(st.detect_fps ?? st.fps ?? 0).toFixed(1).replace('.', ',')} fps · ${st.detection_count || 0} deteksi · sisa ${menit} mnt`;
      } catch { /* gangguan sesaat */ }
    };
    poll();
    tile._livePoll = setInterval(poll, 2000);
  }
  function resetLiveAiTile(tile) {
    tile.classList.remove('live-ai');
    if (tile._player) { tile._player.cleanup(); tile._player = null; }
    const btn = $('[data-action="liveai"]', tile);
    if (btn) { btn.classList.remove('on'); btn.innerHTML = `${ic('scan')}<span>Live AI</span>`; }
  }
  function stopLiveAi(notifyServer = true) {
    const cur = state.liveAi; if (!cur) return;
    state.liveAi = null;
    if (notifyServer) api(`/api/live-ai/${encodeURIComponent(cur.key)}/stop`, { method: 'POST' }).catch(() => {});
    const tile = [...tiles].find((t) => t.dataset.key === cur.key);
    if (tile) { clearInterval(tile._livePoll); resetLiveAiTile(tile); const cam = state.byKey.get(cur.key); if (cam) mountTilePlayer(tile, cam); }
    App.renderStatusBits?.();
  }
  window.addEventListener('pagehide', () => { if (state.liveAi) fetch(`/api/live-ai/${encodeURIComponent(state.liveAi.key)}/stop`, { method: 'POST', keepalive: true }).catch(() => {}); });

  // ------------------------------------------------------------ kontrol
  function setGrid(c, r) { state.gridCols = c; state.gridRows = r; state.page = 0; $('#custom-cols').value = c; $('#custom-rows').value = r; saveLayout(); if (state.view !== 'pantau') App.go('pantau'); else renderGrid(); }
  function syncLayoutButtons() { $$('#layout-presets .layout-btn').forEach((b) => b.classList.toggle('active', Number(b.dataset.cols) === state.gridCols && Number(b.dataset.rows) === state.gridRows)); $('#custom-cols').value = state.gridCols; $('#custom-rows').value = state.gridRows; }
  $('#layout-presets').addEventListener('click', (e) => { const b = e.target.closest('.layout-btn'); if (b) setGrid(Number(b.dataset.cols), Number(b.dataset.rows)); });
  $('#apply-custom-grid-btn').addEventListener('click', () => setGrid(Math.min(8, Math.max(1, Number($('#custom-cols').value) || 1)), Math.min(8, Math.max(1, Number($('#custom-rows').value) || 1))));
  function setViewStyle(v) { state.viewStyle = v; document.body.dataset.view = v; $$('.view-btn').forEach((b) => b.classList.toggle('active', b.dataset.view === v)); LS.set(K.view, v); }
  $$('.view-btn').forEach((b) => b.addEventListener('click', () => setViewStyle(b.dataset.view)));
  setViewStyle(state.viewStyle);
  els.quality.addEventListener('change', () => {
    state.quality = els.quality.value; LS.set(K.quality, state.quality);
    for (const t of tiles) { const hls = t._player?.hls; if (hls) applyQuality(hls, $('[data-role="quality"]', t), state.quality, false); }
    updateSummary();
  });
  $('#prev-page-btn').addEventListener('click', () => { if (state.preset === 'custom') return; state.page = (state.page - 1 + state.pages) % state.pages; renderGrid(); });
  $('#next-page-btn').addEventListener('click', () => { if (state.preset === 'custom') return; state.page = (state.page + 1) % state.pages; renderGrid(); });
  function setControlsOpen(open) { els.ctlWrap.classList.toggle('open', open); els.ctlToggle.setAttribute('aria-expanded', String(open)); LS.set(K.controls, open); }
  els.ctlToggle.addEventListener('click', () => setControlsOpen(!els.ctlWrap.classList.contains('open')));
  setControlsOpen(LS.get(K.controls, false));
  function updateSummary() {
    const q = state.quality === 'auto' ? 'Auto' : `${state.quality}p`;
    els.ctlSummary.textContent = state.view === 'pantau' ? `${state.gridCols}×${state.gridRows} · ${q} · ${state.preset === 'custom' ? 'Kustom' : `Hal ${state.page + 1}/${state.pages}`}` : 'Atur tampilan';
  }
  function renderFillSelect() {
    $('#fill-select').innerHTML = [...folderNames().map((f) => `<option value="r:${esc(f)}">${esc(f)} (${camsIn(f).length})</option>`), ...state.customFolders.map((f) => `<option value="c:${esc(f.id)}">★ ${esc(f.name)}</option>`)].join('');
  }
  $('#fill-btn').addEventListener('click', () => {
    const v = $('#fill-select').value; if (!v) return;
    const keys = v.startsWith('r:') ? camsIn(v.slice(2)).map(camKey) : (state.customFolders.find((f) => f.id === v.slice(2))?.keys || []);
    state.preset = 'custom'; state.slots = keys.slice(0, perPage()); state.focusKey = null; saveLayout();
    App.go('pantau'); toast(`Grid diisi ${Math.min(keys.length, perPage())} kamera`);
  });
  function renderLpSelect() {
    const names = Object.keys(state.layoutPresets).sort();
    $('#lp-select').innerHTML = '<option value="">— pilih preset —</option>' + names.map((n) => { const p = state.layoutPresets[n]; return `<option value="${esc(n)}">${esc(n)} · ${p.cols}×${p.rows}</option>`; }).join('');
  }
  const lpName = () => $('#lp-select').value;
  $('#lp-apply').addEventListener('click', () => {
    const p = state.layoutPresets[lpName()]; if (!p) { toast('Pilih preset dulu', 'err'); return; }
    state.gridCols = Math.min(8, Math.max(1, p.cols || 2)); state.gridRows = Math.min(8, Math.max(1, p.rows || 2));
    state.slots = [...(p.slots || [])]; state.preset = 'custom'; state.focusKey = null; saveLayout();
    App.go('pantau'); toast(`Preset “${lpName()}” diterapkan`);
  });
  $('#lp-save').addEventListener('click', () => promptBox('Simpan preset tata letak', 'Nama preset', `Tata letak ${Object.keys(state.layoutPresets).length + 1}`, (name) => {
    const doSave = () => { ensureCustom(); state.layoutPresets[name] = { rows: state.gridRows, cols: state.gridCols, slots: state.slots.slice(0, perPage()) }; savePresets(); renderLpSelect(); $('#lp-select').value = name; toast(`Preset “${name}” disimpan`); };
    if (state.layoutPresets[name]) confirmBox('Timpa preset?', `Preset “${name}” sudah ada. Timpa dengan susunan sekarang?`, doSave); else doSave();
  }));
  $('#lp-overwrite').addEventListener('click', () => { const n = lpName(); if (!state.layoutPresets[n]) { toast('Pilih preset yang mau ditimpa', 'err'); return; } ensureCustom(); state.layoutPresets[n] = { rows: state.gridRows, cols: state.gridCols, slots: state.slots.slice(0, perPage()) }; savePresets(); renderLpSelect(); $('#lp-select').value = n; toast(`Preset “${n}” ditimpa`); });
  $('#lp-delete').addEventListener('click', () => { const n = lpName(); if (!state.layoutPresets[n]) { toast('Pilih preset yang mau dihapus', 'err'); return; } confirmBox('Hapus preset?', `Preset “${n}” akan dihapus.`, () => { delete state.layoutPresets[n]; savePresets(); renderLpSelect(); }); });
  $('#lp-reset').addEventListener('click', () => { state.slots = []; state.preset = 'all'; state.gridCols = 2; state.gridRows = 2; state.page = 0; state.focusKey = null; saveLayout(); App.go('pantau'); toast('Tata letak diatur ulang'); });

  function syncHideBtn() { $('#toggle-hide-offline-btn').classList.toggle('active', state.hideOffline); }
  syncHideBtn();
  $('#toggle-hide-offline-btn').addEventListener('click', async () => {
    state.hideOffline = !state.hideOffline; LS.set(K.hideGrid, state.hideOffline); syncHideBtn();
    if (state.hideOffline && state.view === 'pantau' && state.preset !== 'custom') {
      const ids = presetList().slice(0, 120).filter((c) => !state.status.has(camKey(c))).map((c) => c.id);
      if (ids.length) { toast('Mengecek status kamera…'); await checkStatus({ ids }); }
    }
    toast(state.hideOffline ? 'CCTV offline disembunyikan' : 'CCTV offline ditampilkan');
    renderSidebar(); if (state.view === 'pantau') renderGrid();
  });
  $('#reload-all-btn').addEventListener('click', () => { if (state.view === 'pantau') renderGrid(); toast('Semua stream dimuat ulang'); App.closeMenus?.(); });
  $('#auto-cycle-btn').addEventListener('click', (e) => {
    const b = e.currentTarget;
    if (state.autoCycle) { clearInterval(state.autoCycle); state.autoCycle = null; b.classList.remove('active'); toast('Auto-Cycle mati'); return; }
    state.autoCycle = setInterval(() => { if (state.view === 'pantau' && state.preset !== 'custom' && !state.editMode && !state.focusKey) { state.page = (state.page + 1) % state.pages; renderGrid(); } }, 20000);
    b.classList.add('active'); toast('Auto-Cycle: ganti halaman tiap 20 detik');
  });
  let tvClock = null;
  function setTvWall(on) {
    document.body.classList.toggle('tv-wall-mode', on);
    $('#tv-wall-hud').classList.toggle('hidden', !on);
    clearInterval(tvClock);
    if (on) {
      if (state.view !== 'pantau') App.go('pantau');
      const tick = () => { $('#tv-wall-clock').textContent = new Date().toLocaleString('id-ID', { weekday: 'short', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' }); };
      tick(); tvClock = setInterval(tick, 1000);
      try { document.documentElement.requestFullscreen?.().catch(() => {}); } catch { /* abaikan */ }
    } else { try { if (document.fullscreenElement) document.exitFullscreen(); } catch { /* abaikan */ } }
  }
  $('#tv-wall-btn').addEventListener('click', () => { App.closeMenus?.(); setTvWall(true); });
  $('#exit-tv-wall-btn').addEventListener('click', () => setTvWall(false));
  document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement && document.body.classList.contains('tv-wall-mode')) setTvWall(false); });

  // ------------------------------------------------------------- Beranda
  const avatarText = (n) => { const w = String(n || '?').trim().split(/\s+/); return w.length > 1 ? (w[0][0] + w[1][0]).toUpperCase() : w[0].charAt(0).toUpperCase() + w[0].charAt(1).toLowerCase(); };
  function countUp(el, to) {
    const target = Number(to) || 0;
    if (reduceMotion() || target <= 0) { el.textContent = target.toLocaleString('id-ID'); return; }
    const s = performance.now();
    const step = (n) => { const p = Math.min(1, (n - s) / 900); el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3))).toLocaleString('id-ID'); if (p < 1) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  }
  function previewKeys() {
    const out = [];
    const push = (k) => { if (k && state.byKey.has(k) && !out.includes(k) && camType(state.byKey.get(k)) !== 'youtube') out.push(k); };
    state.slots.forEach(push);
    (state.aiSettings?.cameras || []).forEach(push);
    for (const c of state.cameras) { if (out.length >= 8) break; if (!isOffline(c)) push(camKey(c)); }
    return out.slice(0, 8);
  }
  function statusRow(icon, title, sub, on, goto, tab) {
    return `<div class="status-row"><span class="stat-ico${on ? '' : ' navy'}">${ic(icon)}</span><div class="grow"><strong>${esc(title)}</strong><span class="sub">${esc(sub)}</span></div><span class="state-chip${on ? ' on' : ''}"><span class="ai-dot"></span>${on ? 'Aktif' : 'Mati'}</span><button class="icon-btn" data-goto="${goto}"${tab ? ` data-tab="${tab}"` : ''}${goto === 'pengaturan' && SET_TAB_PERM[tab] ? ` data-perm="${SET_TAB_PERM[tab]}"` : ''} title="Buka" aria-label="Buka ${esc(title)}">${ic('chev-right')}</button></div>`;
  }
  async function renderHome() {
    destroyTiles();
    els.grid.className = 'cctv-grid is-home';
    els.grid.style.gridTemplateColumns = ''; els.grid.style.gridTemplateRows = '';
    const groups = folderNames();
    const down = new Set(Object.entries(state.apiStatus || {}).filter(([, s]) => s && s.online === false).map(([f]) => f));
    const publicFolders = groups.filter((f) => camsIn(f).some((c) => !isManual(c)));
    const active = groups.filter((f) => !down.has(f)).length;
    const online = [...state.status.values()].filter((v) => v === 'online').length;
    const manualN = state.cameras.filter(isManual).length;
    const ai = state.aiStatus || {}, rs = state.recStatus || {};
    const live = state.liveStatus;
    const mon = state.monStatus || {};
    const prev = state.homePreview ? previewKeys() : [];
    els.grid.innerHTML = `
      <div class="home">
        <section class="hero-card">
          <span class="hero-stat">${online ? `${online} kamera online` : `${state.cameras.length} kamera`}</span>
          <div class="hero-main">
            <span class="hero-pill"><span class="live-dot"></span>LIVE MONITOR</span>
            <div><p class="hero-title"><b data-count="${active}">0</b> dari ${groups.length} sumber daerah aktif</p>
            <p class="hero-sub"><span data-count="${state.cameras.length}">0</span> titik pantau${ai.running ? ` · AI memantau ${ai.monitored_cameras} kamera` : ''}</p></div>
            <button type="button" class="hero-btn" data-goto="pantau">Mulai pantau ${ic('arrow-ur', 'ic-sm')}</button>
          </div>
          <div class="hero-art" aria-hidden="true"><span class="sweep"></span><span class="ring"></span><span class="ring"></span><span class="ring"></span><span class="ring wave"></span><span class="ring wave delay"></span>
            <span class="blip" style="--a:38deg;--d:62px"></span><span class="blip" style="--a:150deg;--d:48px"></span><span class="blip" style="--a:255deg;--d:70px"></span><span class="hero-cam">${ic('video')}</span></div>
        </section>

        <div class="stat-strip${can('cameras.manage') ? '' : ' n4'}">
          <button class="stat-card" style="--i:0" data-goto="pantau"><span class="stat-ico">${ic('video')}</span><span class="stat-txt"><strong data-count="${state.cameras.length}">0</strong><span>Total kamera</span></span></button>
          <button class="stat-card" style="--i:1" data-goto="health"><span class="stat-ico navy">${ic('map')}</span><span class="stat-txt"><strong>${publicFolders.length}</strong><span>Wilayah publik</span></span></button>
          ${can('cameras.manage') ? `<button class="stat-card" style="--i:2" data-goto="kamera"><span class="stat-ico warn">${ic('home')}</span><span class="stat-txt"><strong>${manualN}</strong><span>Kamera saya</span></span></button>` : ''}
          <button class="stat-card" style="--i:3" data-goto="jepretan"><span class="stat-ico danger">${ic('scan')}</span><span class="stat-txt"><strong>${ai.today_count || 0}</strong><span>Jepretan AI hari ini</span></span></button>
          <button class="stat-card" style="--i:4" data-goto="rekaman"><span class="stat-ico navy">${ic('hdd')}</span><span class="stat-txt"><strong>${(rs.recordings_count || 0) + (state.browserRecCount || 0)}</strong><span>Rekaman · ${fmtBytes(rs.total_size_bytes || 0)}</span></span></button>
        </div>

        <section class="card">
          <div class="card-row" style="justify-content:space-between;flex-wrap:wrap;gap:8px">
            <h3>${ic('grid')}Pratinjau langsung</h3>
            <div class="card-row" style="gap:8px">
              <button type="button" class="mini-accent-btn" id="toggle-home-preview">${ic(state.homePreview ? 'eye-off' : 'eye', 'ic-sm')} ${state.homePreview ? 'Sembunyikan' : 'Tampilkan'}</button>
              <button type="button" class="mini-accent-btn" data-goto="pantau">Buka Pantau ${ic('chev-right', 'ic-sm')}</button>
            </div>
          </div>
          ${state.homePreview
            ? `<div class="preview-grid preview-8">${prev.map((k) => { const c = state.byKey.get(k); return `<button class="mini-tile" data-preview="${esc(k)}"><span class="media-host"></span><span class="live-pill">LIVE</span><span class="mini-label">${esc(c.name)} · ${esc(c.folder || '')}</span></button>`; }).join('') || '<div class="empty-state">Belum ada kamera.</div>'}</div>`
            : '<p class="card-sub" style="margin-top:8px">Pratinjau langsung disembunyikan agar hemat bandwidth. Klik <b>Tampilkan</b> untuk memutar 8 kamera pratinjau.</p>'}
        </section>

        <div class="home-split${can('jepretan.view') ? '' : ' single'}">
          <section class="card">
            <div class="card-head"><h3>${ic('activity')}Status sistem</h3>${(() => { const n = [!!ai.running, !!live?.active, !!(rs.available && rs.enabled), !!(ai.telegram_configured && ai.telegram_enabled), !!mon.running].filter(Boolean).length; return `<span class="head-chip${n ? ' on' : ''}"><b>${n}</b> dari 5 aktif</span>`; })()}</div>
            <div class="status-list">
              ${statusRow('ai', 'AI Jepretan', !ai.available ? `Tidak tersedia · ${ai.unavailable_reason || 'dependensi AI belum terpasang'}` : ai.running ? `Berjalan · ${ai.monitored_cameras} kamera opt-in · ${ai.today_count} jepretan hari ini` : 'Berhenti', !!ai.running, 'pengaturan', 'ai')}
              ${statusRow('scan', 'Live AI realtime', live?.active ? `Aktif di ${live.camera_name}` : live && live.available === false ? 'Tidak tersedia' : 'Mati · nyalakan dari tile kamera', !!live?.active, 'pantau')}
              ${statusRow('hdd', 'Rekaman server', !rs.available ? 'Tidak tersedia (butuh ffmpeg)' : rs.enabled ? `${rs.continuous_cameras} kamera berkelanjutan · ${fmtBytes(rs.total_size_bytes)} / ${fmtBytes(rs.quota_bytes)}` : 'Nonaktif', !!(rs.available && rs.enabled), 'pengaturan', 'rekaman')}
              ${statusRow('bell', 'Alarm kamera & disk', !mon.enabled ? 'Mati · atur kamera yang dipantau' : mon.offline ? `${mon.offline} kamera offline dari ${mon.monitored} dipantau` : (mon.cameras || []).some((c) => c.status === 'gangguan') ? `${mon.cameras.filter((c) => c.status === 'gangguan').length} kamera tidak merespons · dicek ulang` : mon.last_run ? `${mon.monitored} kamera dipantau · semua merespons` : `${mon.monitored} kamera dipantau · menunggu cek pertama`, !!mon.running && !mon.offline, 'pengaturan', 'alarm')}
              ${statusRow('send', 'Notifikasi Telegram', ai.telegram_configured ? (ai.telegram_enabled ? 'Aktif' : 'Terkonfigurasi · notifikasi mati') : 'Belum dikonfigurasi', !!(ai.telegram_configured && ai.telegram_enabled), 'pengaturan', 'ai')}
            </div>
          </section>
          ${can('jepretan.view') ? `<section class="card">
            <div class="card-head"><h3>${ic('scan')}Jepretan terbaru</h3><button class="mini-accent-btn" data-goto="jepretan">Lihat semua ${ic('chev-right', 'ic-sm')}</button></div>
            <div class="jp-strip" id="home-jp"><div class="empty-state">Memuat…</div></div>
          </section>` : ''}
        </div>

        <div class="section-head"><h3>Daerah</h3><span>${groups.length} sumber</span></div>
        <div class="region-grid">${groups.map((f, i) => { const cs = camsIn(f); const off = down.has(f); return `<button type="button" class="region-card tone-${esc(cs[0]?.provider || 'manual')}" data-region="${esc(f)}" style="--i:${i}"><span class="region-avatar">${esc(avatarText(f))}</span><span class="region-info"><span class="region-name">${esc(f)}</span><span class="region-status${off ? ' is-off' : ''}">${off ? 'Sumber off' : 'Terhubung'} <small>· ${cs.length} kamera</small></span></span><span class="region-go">${ic('chev-right')}</span></button>`; }).join('') || '<div class="empty-state">Belum ada data kamera.</div>'}</div>

        ${state.customFolders.length ? `<div class="section-head"><h3>Folder saya</h3><span>${state.customFolders.length} folder</span></div><div class="region-grid">${state.customFolders.map((f, i) => `<button type="button" class="region-card tone-custom" data-custom="${esc(f.id)}" style="--i:${i}"><span class="region-avatar">${ic('bookmark')}</span><span class="region-info"><span class="region-name">${esc(f.name)}</span><span class="region-status">${f.keys.length ? 'Siap diputar' : 'Masih kosong'} <small>· ${f.keys.length} kamera</small></span></span><span class="region-go">${ic('chev-right')}</span></button>`).join('')}</div>` : ''}

        <div class="privacy-card"><span class="privacy-icon">${ic('shield')}</span><div><strong>Privasi opt-in &amp; penyimpanan lokal</strong>
          <p>AI dan rekaman server hanya berjalan untuk kamera yang dicentang. Jepretan &amp; rekaman server tersimpan di server ini; klip tombol Rec tersimpan di browser ini.</p></div></div>
      </div>`;
    $('#toggle-home-preview')?.addEventListener('click', () => {
      state.homePreview = !state.homePreview;
      LS.set(K.homePreview, state.homePreview);
      destroyTiles();
      renderHome();
    });
    $$('[data-count]', els.grid).forEach((el) => countUp(el, el.dataset.count));
    $$('[data-region]', els.grid).forEach((b) => b.addEventListener('click', () => playPreset('folder:' + b.dataset.region)));
    $$('[data-custom]', els.grid).forEach((b) => b.addEventListener('click', () => playPreset('cf:' + b.dataset.custom)));
    $$('[data-preview]', els.grid).forEach((b) => {
      const cam = state.byKey.get(b.dataset.preview);
      const fake = b; fake.dataset.status = 'loading'; tiles.add(fake);
      fake._player = mountPlayer($('.media-host', b), cam, { onState: (st) => { b.dataset.status = st; if (st === 'live') state.status.set(camKey(cam), 'online'); } });
      b.addEventListener('click', () => playKeys([b.dataset.preview], 1, 1));
    });
    if (can('jepretan.view')) try {
      const data = await apiJson('/api/jepretan?limit=4');
      const box = $('#home-jp'); if (!box) return;
      const items = (data.jepretan || []).slice(0, 4);
      box.innerHTML = items.length ? items.map((j, i) => App.jpTile(j, i)).join('') : `<div class="empty-state">${ai.available ? 'Belum ada jepretan. Aktifkan AI dan pilih kamera opt-in.' : 'AI belum aktif di server ini.'}</div>`;
      state.jepretanById = new Map(items.map((j) => [j.id, j]));
      App.bindJpCards(box, items);
    } catch { const box = $('#home-jp'); if (box) box.innerHTML = '<div class="empty-state">Jepretan tidak tersedia.</div>'; }
  }

  // ------------------------------------------------------------- Health
  async function renderHealth() {
    await refreshHealthInfo();
    if (state.view !== 'health') return;
    let on = 0, off = 0;
    for (const c of state.cameras) { const s = state.status.get(camKey(c)); if (s === 'online') on++; else if (s === 'offline') off++; }
    const unk = state.cameras.length - on - off;
    const h = state.healthInfo || {};
    const ff = h.ffmpeg || {}, vw = h.viewers || {}, mem = h.memory || {}, dsk = h.disk || {};
    els.page.innerHTML = `<div class="page">
      <div class="card-row" style="justify-content:space-between"><p class="page-intro">Cek ketersediaan stream lewat server, serta pantau kapasitas proses <code>ffmpeg</code>, memori, dan ruang disk.</p>
        <div class="card-row"><button class="action-btn" id="h-refresh">${ic('rotate')}<span>Segarkan beban</span></button><button class="action-btn primary-btn" id="h-all">${ic('activity')}<span>Cek semua daerah</span></button></div></div>
      ${h.status ? `<section class="card"><div class="card-head"><h3>${ic('gauge')}Beban &amp; kesehatan server</h3><span class="state-chip${h.status === 'ok' ? ' on' : ' warn'}"><span class="ai-dot"></span>${h.status === 'ok' ? 'Sehat' : 'Degradasi'}</span></div>
        <div class="health-summary" style="margin-top:12px">
          <div class="health-stat"><strong>${ff.active_processes ?? 0} / ${ff.max_processes ?? '-'}</strong><span>Proses ffmpeg aktif${ff.available === false ? ' (tidak terpasang)' : ''}</span></div>
          <div class="health-stat"><strong>${ff.rtsp_bridges ?? 0} / ${ff.rtsp_bridges_max ?? '-'}</strong><span>Jembatan RTSP → HLS</span></div>
          <div class="health-stat"><strong>${vw.active ?? 0} / ${vw.max ?? '-'}</strong><span>Penonton stream aktif</span></div>
          <div class="health-stat"><strong>${mem.rss_mb != null ? mem.rss_mb + ' MB' : '–'}</strong><span>RAM server · Disk ${dsk.percent != null ? dsk.percent + '%' : '–'}</span></div>
        </div></section>` : ''}
      <div class="health-summary"><div class="health-stat"><strong>${state.cameras.length}</strong><span>Total kamera</span></div><div class="health-stat is-online"><strong>${on}</strong><span>Online</span></div><div class="health-stat is-offline"><strong>${off}</strong><span>Offline</span></div><div class="health-stat"><strong>${unk}</strong><span>Belum dicek</span></div></div>
      <div class="health-list">${folderNames().map((f) => {
        const cs = camsIn(f); const o = cs.filter((c) => state.status.get(camKey(c)) === 'online').length, x = cs.filter((c) => state.status.get(camKey(c)) === 'offline').length;
        const pct = o + x ? Math.round((o / (o + x)) * 100) : 0;
        const apiOff = state.apiStatus[f] && state.apiStatus[f].online === false;
        return `<div class="source-card"><div class="source-card-top"><span class="source-card-title">${ic('folder')}${esc(f)} <span class="folder-count">${cs.length}</span>${apiOff ? `<span class="api-off-badge" title="${esc(state.apiStatus[f].error || '')}">API off</span>` : ''}</span>
          <div class="health-row-actions"><span class="folder-count">${o} online · ${x} offline${o + x ? ` · ${pct}%` : ''}</span><button class="rec-mini-btn" data-check="${esc(f)}">${ic('rotate')}Cek</button><button class="rec-mini-btn" data-playf="${esc(f)}">${ic('play')}Putar</button></div></div>
          <div class="health-bar-track"><div class="health-bar-fill" style="width:${pct}%"></div></div></div>`;
      }).join('')}</div></div>`;
    $('#h-refresh').onclick = () => renderHealth();
    $$('[data-check]', els.page).forEach((b) => b.onclick = async () => {
      b.disabled = true; b.innerHTML = `${ic('loader', 'spin')}Cek…`;
      const r = await checkStatus({ folder: b.dataset.check });
      toast(`${b.dataset.check}: ${r.on} online, ${r.off} offline`);
      if (state.view === 'health') renderHealth();
    });
    $$('[data-playf]', els.page).forEach((b) => b.onclick = () => playPreset('folder:' + b.dataset.playf));
    $('#h-all').onclick = async (e) => {
      const b = e.currentTarget; b.disabled = true;
      const fs = folderNames();
      for (let i = 0; i < fs.length; i++) {
        b.innerHTML = `${ic('loader', 'spin')}<span>Mengecek ${esc(fs[i])} (${i + 1}/${fs.length})…</span>`;
        await checkStatus({ folder: fs[i] });
      }
      toast('Semua daerah selesai dicek');
      if (state.view === 'health') renderHealth();
      renderSidebar();
    };
  }

  // ---------------------------------------------------------------- Peta
  const TONE = { lifemedia: '#0f8a6b', diskominfo: '#0f8a6b', kuduskab: '#d99a22', youtube: '#d6463e', jombangkab: '#7a5cc7', tol: '#e07b39', jogja: '#1f8fb0', semarang: '#5a9a3a', pati: '#c2477a', rembang: '#4a62c7', manual: '#0b2a45' };
  function renderMap() {
    const withCoords = state.cameras.filter((c) => typeof c.lat === 'number' && typeof c.lng === 'number');
    const fl = [...new Set(withCoords.map((c) => c.folder || 'Lainnya'))];
    $('#map-legend').innerHTML = `<button class="pill${state.mapFolder === 'all' ? ' active' : ''}" data-mf="all">Semua (${withCoords.length})</button>` + fl.map((f) => `<button class="pill${state.mapFolder === f ? ' active' : ''}" data-mf="${esc(f)}">${esc(f)}</button>`).join('');
    $$('[data-mf]', els.map).forEach((b) => b.onclick = () => { state.mapFolder = b.dataset.mf; renderMap(); });
    if (!window.L) { $('#leaflet-map').innerHTML = '<div class="empty-state">Pustaka peta (Leaflet) gagal dimuat.</div>'; return; }
    if (!state.map) {
      state.map = L.map('leaflet-map', { zoomControl: false }).setView([-6.9, 110.8], 8);
      L.control.zoom({ position: 'bottomright' }).addTo(state.map);
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '&copy; OpenStreetMap contributors', maxZoom: 19 }).addTo(state.map);
    }
    if (state.mapLayer) state.mapLayer.remove();
    state.mapLayer = L.layerGroup().addTo(state.map);
    const pts = withCoords.filter((c) => state.mapFolder === 'all' || c.folder === state.mapFolder);
    const bounds = [];
    for (const c of pts) {
      const st = state.status.get(camKey(c));
      const m = L.circleMarker([c.lat, c.lng], { radius: 7, color: '#ffffff', weight: 2, fillColor: st === 'offline' ? '#d6463e' : TONE[c.provider] || '#0b2a45', fillOpacity: 0.95 });
      m.bindPopup(`<div class="map-popup"><b>${esc(c.name)}</b><span>${esc(c.folder || '')} · ${esc(streamLabel(c))} · ${st === 'online' ? 'Online' : st === 'offline' ? 'Offline' : 'Belum dicek'}</span><button class="mini-accent-btn" data-play-key="${esc(camKey(c))}">${ic('play', 'ic-sm')}Putar di Pantau</button></div>`);
      m.addTo(state.mapLayer); bounds.push([c.lat, c.lng]);
    }
    setTimeout(() => { state.map.invalidateSize(); if (bounds.length) state.map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 }); }, 60);
  }

  Object.assign(App, {
    mountPlayer, applyQuality, syncGlobalQuality,
    renderPills, renderPresetOptions, renderSidebar, openFolderModal, openPicker,
    perPage, presetList, currentItems, playPreset, playKeys,
    destroyTiles, renderGrid, renderEditHelp, setEditMode, stopLiveAi,
    setGrid, syncLayoutButtons, updateSummary, renderFillSelect, renderLpSelect, syncHideBtn, setTvWall,
    renderHome, statusRow, renderHealth, renderMap,
  });
})();
