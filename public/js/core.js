/* ==========================================================================
   CCTipi System — Core State, API Client, RBAC, Prefs & Modal Utilities
   ========================================================================== */
(() => {
  'use strict';

  const App = (window.CCTipi = window.CCTipi || {});

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const ic = (name, cls = '') => `<svg class="ic ${cls}"><use href="#i-${name}"/></svg>`;
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pad = (n) => String(n).padStart(2, '0');
  const fmtTime = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  const fmtDateTime = (d) => d.toLocaleString('id-ID', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  const fmtDur = (sec) => { const s = Math.max(0, Math.floor(sec)); return `${pad(Math.floor(s / 60))}:${pad(s % 60)}`; };
  const fmtBytes = (b) => {
    if (!b && b !== 0) return '-';
    if (b < 1048576) return `${(b / 1024).toFixed(1)} KB`;
    if (b < 1073741824) return `${(b / 1048576).toFixed(1)} MB`;
    return `${(b / 1073741824).toFixed(2)} GB`;
  };
  const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const isCompact = () => matchMedia('(max-width: 1080px)').matches;
  const reduceMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

  const USER_PREF_KEYS = [
    'cctipi_layout_v1', 'cctipi_layout_presets_v1',
    'cctipi_custom_folders_v2', 'cctipi_folder_order_v2', 'cctipi_hide_offline_folders_v2',
  ];
  let prefsReady = false, prefsTimer = null;

  const LS = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) {
      try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* penuh / diblokir */ }
      if (USER_PREF_KEYS.includes(k)) queuePrefsSync();
    },
  };

  function queuePrefsSync() {
    if (!prefsReady || state.mustChange) return;
    clearTimeout(prefsTimer);
    prefsTimer = setTimeout(pushPrefs, 1200);
  }
  async function pushPrefs() {
    if (state.mustChange) return;
    const prefs = Object.fromEntries(USER_PREF_KEYS.map((k) => [k, LS.get(k, null)]));
    try {
      await fetch('/api/prefs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prefs }) });
    } catch { /* coba lagi berikutnya */ }
  }

  function toast(msg, kind = '') {
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.innerHTML = `${ic(kind === 'err' ? 'alert' : 'check')}<span>${esc(msg)}</span>`;
    $('#toasts').appendChild(el);
    setTimeout(() => {
      el.classList.add('out');
      el.addEventListener('animationend', () => el.remove(), { once: true });
      setTimeout(() => el.remove(), 600);
    }, kind === 'err' ? 5200 : 3600);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, opts);
    if (res.status === 401) { window.location.href = '/login'; throw new Error('Sesi berakhir'); }
    return res;
  }
  async function apiJson(path, opts = {}) {
    const res = await api(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (data && data.mustChange && !state.mustChange) {
        state.mustChange = true;
        App.go?.('pengaturan', { tab: 'akun' });
      }
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }
  const postJson = (path, body) => apiJson(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });

  const VOLATILE_PARAMS = new Set([
    'token', 'access_token', 'auth', 'auth_token', 'authtoken', 'jwt',
    'signature', 'sig', 'expires', 'expiry', 'hdnts', 'wmsauthsign',
  ]);
  function normalizeStreamUrl(url) {
    url = url || '';
    const q = url.indexOf('?');
    if (q < 0) return url;
    const base = url.slice(0, q);
    let rest = url.slice(q + 1), frag = '';
    const h = rest.indexOf('#');
    if (h >= 0) { frag = rest.slice(h); rest = rest.slice(0, h); }
    const kept = rest.split('&').filter((part) => {
      if (!part) return false;
      let name = part.split('=')[0];
      try { name = decodeURIComponent(name); } catch { /* biarkan */ }
      return !VOLATILE_PARAMS.has(name.toLowerCase());
    });
    return base + (kept.length ? `?${kept.join('&')}` : '') + frag;
  }
  const camKey = (cam) => (cam.manualId ? `m:${cam.manualId}` : `u:${normalizeStreamUrl(cam.url)}`);
  function camType(cam) {
    if (cam.type) return cam.type;
    if (cam.provider === 'youtube') return 'youtube';
    if (cam.provider === 'jombangkab') return 'mjpeg';
    return 'hls';
  }
  const isManual = (cam) => cam.provider === 'manual' || !!cam.manualId;
  const streamLabel = (c) => ({ hls: 'HLS', mjpeg: 'MJPEG', flv: 'FLV', youtube: 'YouTube', rtsp: 'RTSP', http: 'Snapshot' }[camType(c)] || 'HLS');

  const DIRECT_CORS_HOSTS = new Set([
    'api-cctv.margaharjaya.co.id', 'cctvjss.jogjakota.go.id',
    'livepantau.semarangkota.go.id', 'cctv.rembangkab.go.id', 'surakarta.atcsindonesia.info',
  ]);
  function syncDirectCorsHosts(hosts) {
    if (!Array.isArray(hosts)) return;
    hosts.forEach((h) => { if (typeof h === 'string' && h.trim()) DIRECT_CORS_HOSTS.add(h.trim().toLowerCase()); });
  }
  function resolveStreamUrl(streamUrl, cam = null) {
    if (cam && camType(cam) === 'rtsp' && cam.manualId) return `/api/rtsp/${encodeURIComponent(cam.manualId)}/index.m3u8`;
    if (cam && cam.tuyaDeviceId && camType(cam) === 'hls') return `/api/tuya/hls/${encodeURIComponent(cam.tuyaDeviceId)}.m3u8`;
    try { if (DIRECT_CORS_HOSTS.has(new URL(streamUrl).hostname.toLowerCase())) return streamUrl; } catch { /* abaikan */ }
    return `/api/proxy?url=${encodeURIComponent(streamUrl)}`;
  }

  // Interaksi gerakan ringan (ripple + spotlight kartu daerah)
  const RIPPLE_SEL = '.action-btn, .hero-btn, .nav-item, .region-card, .layout-btn, .mini-accent-btn, .controls-toggle, .pill, .tile-go, .secondary-btn, .cluster-btn, .page-tab, .stat-card';
  document.addEventListener('pointerdown', (e) => {
    if (reduceMotion() || e.button > 0) return;
    const host = e.target.closest(RIPPLE_SEL);
    if (!host || host.disabled) return;
    const rect = host.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height) * 2;
    const dot = document.createElement('span');
    dot.className = 'ripple';
    dot.style.width = dot.style.height = `${size}px`;
    dot.style.left = `${e.clientX - rect.left - size / 2}px`;
    dot.style.top = `${e.clientY - rect.top - size / 2}px`;
    host.classList.add('has-ripple');
    host.appendChild(dot);
    dot.addEventListener('animationend', () => dot.remove(), { once: true });
    setTimeout(() => dot.remove(), 900);
  }, { passive: true });

  let spotFrame = 0, spotEvt = null;
  document.addEventListener('pointermove', (e) => {
    if (e.pointerType !== 'mouse') return;
    spotEvt = e;
    if (spotFrame) return;
    spotFrame = requestAnimationFrame(() => {
      spotFrame = 0;
      const card = spotEvt.target.closest && spotEvt.target.closest('.region-card');
      if (!card) return;
      const r = card.getBoundingClientRect();
      card.style.setProperty('--mx', `${spotEvt.clientX - r.left}px`);
      card.style.setProperty('--my', `${spotEvt.clientY - r.top}px`);
    });
  }, { passive: true });

  const markLoaded = (e) => { const t = e.target; if (t && t.tagName === 'IMG' && t.hasAttribute('data-fade')) t.classList.add('is-loaded'); };
  document.addEventListener('load', markLoaded, true);
  document.addEventListener('error', markLoaded, true);
  function bump(el) {
    if (!el || reduceMotion()) return;
    el.classList.remove('bump'); void el.offsetWidth; el.classList.add('bump');
  }

  const K = {
    folders: 'cctipi_custom_folders_v2', order: 'cctipi_folder_order_v2',
    hideF: 'cctipi_hide_offline_folders_v2', hideGrid: 'cctipi_hide_offline_grid',
    view: 'cctipi_view', quality: 'cctipi_quality', layout: 'cctipi_layout_v1',
    presets: 'cctipi_layout_presets_v1', controls: 'cctipi_controls_open', homePreview: 'cctipi_home_preview',
  };

  const state = {
    user: null, minPass: 8, mustChange: false,
    cameras: [], byKey: new Map(), byId: new Map(), apiStatus: {}, source: '', loaded: false,
    status: new Map(),
    view: 'home',
    gridCols: 2, gridRows: 2, page: 0, pages: 1,
    preset: 'all', provider: 'all', search: '',
    slots: [], editMode: false, selectedSlot: null, focusKey: null,
    customFolders: [], folderOrder: LS.get(K.order, []), expanded: new Set(),
    hideOfflineFolders: new Set(LS.get(K.hideF, [])), hideOffline: LS.get(K.hideGrid, false),
    autoCycle: null, viewStyle: LS.get(K.view, 'card'), quality: LS.get(K.quality, 'auto'),
    layoutPresets: LS.get(K.presets, null) || {},
    liveAi: null,
    aiStatus: null, aiSettings: null, recStatus: null, recSettings: null, liveStatus: null, monStatus: null, healthInfo: null,
    jepretan: [], jpFilter: { date: '', cam: '', cat: '' }, srFilter: { date: '', cam: '', mode: '' },
    pageTab: { rekaman: 'server', pengaturan: 'ai' }, sources: [], perms: null, role: '', roleName: '', regions: [],
    allPerms: [
      'jepretan.view', 'jepretan.delete', 'liveai.use', 'ptz.control',
      'rekaman.view', 'rekaman.delete', 'cameras.manage', 'settings.ai',
      'settings.system', 'users.manage', 'audit.view',
    ],
    mapFolder: 'all', map: null, mapLayer: null,
    lastTodayCount: null, unseenJp: 0, browserRecCount: 0,
    homePreview: LS.get(K.homePreview, false) === true,
  };
  const tiles = new Set();

  {
    const saved = LS.get(K.layout, null);
    if (saved && Array.isArray(saved.slots) && saved.slots.some(Boolean)) {
      state.slots = saved.slots; state.gridRows = saved.rows || 2; state.gridCols = saved.cols || 2;
      state.preset = saved.preset || 'custom';
    }
  }
  const rawFolders = LS.get(K.folders, null) ?? LS.get('cctipi_custom_folders', []);
  const saveLayout = () => LS.set(K.layout, { rows: state.gridRows, cols: state.gridCols, slots: state.slots, preset: state.preset });
  const saveFolders = () => LS.set(K.folders, state.customFolders);
  const savePresets = () => LS.set(K.presets, state.layoutPresets);

  function normalizeFolders(list) {
    return (Array.isArray(list) ? list : []).map((f) => {
      if (Array.isArray(f.keys)) return { id: String(f.id), name: f.name || 'Folder', keys: f.keys };
      const keys = (f.cameraIds || []).map((id) => state.byId.get(Number(id))).filter(Boolean).map(camKey);
      return { id: String(f.id || Date.now()), name: f.name || 'Folder', keys };
    });
  }

  const els = {
    grid: $('#cctv-grid'), page: $('#page-view'), map: $('#map-view'), list: $('#camera-list'),
    title: $('#page-title'), sub: $('#page-sub'), pills: $('#filter-pills'), presetSel: $('#preset-select'),
    pageInd: $('#page-indicator'), ctlSummary: $('#controls-summary'), ctlToggle: $('#controls-toggle'), ctlWrap: $('#control-wrap'),
    toolbar: $('#toolbar-actions'), modal: $('#modal'), dialog: $('#modal-dialog'), quality: $('#quality-select'),
  };

  const folderNames = () => [...new Set(state.cameras.map((c) => c.folder || 'Lainnya'))];
  const camsIn = (f) => state.cameras.filter((c) => (c.folder || 'Lainnya') === f);
  function filtered() {
    const q = state.search.trim().toLowerCase();
    return state.cameras.filter((c) => (
      (state.provider === 'all' || (c.provider || 'manual') === state.provider)
      && (!q || (c.name || '').toLowerCase().includes(q) || (c.folder || '').toLowerCase().includes(q))
    ));
  }
  const isOffline = (c) => state.status.get(camKey(c)) === 'offline';

  // RBAC
  const can = (perm) => !state.perms || state.perms.has(perm);
  const VIEW_PERM = { jepretan: 'jepretan.view', kamera: 'cameras.manage' };
  const viewAllowed = (v) => !VIEW_PERM[v] || can(VIEW_PERM[v]);
  const SET_TAB_PERM = {
    ai: 'settings.ai', rekaman: 'settings.ai', alarm: 'settings.ai',
    sumber: 'settings.system', backup: 'settings.system',
    pengguna: 'users.manage', log: 'audit.view',
  };
  function applyPermStyle() {
    let el = document.getElementById('perm-style');
    if (!el) { el = document.createElement('style'); el.id = 'perm-style'; document.head.appendChild(el); }
    if (!state.perms) { el.textContent = ''; return; }
    const rules = [];
    for (const p of state.allPerms) if (!state.perms.has(p)) rules.push(`[data-perm~="${p}"]`);
    for (const [v, p] of Object.entries(VIEW_PERM)) {
      if (!state.perms.has(p)) rules.push(`[data-goto="${v}"]`, `[data-page-btn="${v}"]`, `[data-nav="${v}"]`, `[data-proxy="open-${v}-btn"]`);
    }
    el.textContent = rules.length ? `${rules.join(',')}{display:none!important}` : '';
  }

  function applyPrefsFromLS() {
    state.layoutPresets = LS.get(K.presets, {}) || {};
    const saved = LS.get(K.layout, null);
    if (saved && Array.isArray(saved.slots)) {
      state.slots = saved.slots; state.gridRows = saved.rows || 2; state.gridCols = saved.cols || 2; state.preset = saved.preset || 'custom';
    } else {
      state.slots = []; state.gridRows = 2; state.gridCols = 2; state.preset = 'all';
    }
    if (state.loaded) state.customFolders = normalizeFolders(LS.get(K.folders, []));
    state.folderOrder = LS.get(K.order, []) || [];
    state.hideOfflineFolders = new Set(LS.get(K.hideF, []) || []);
  }

  async function syncUserPrefs() {
    if (state.mustChange) return;
    let srv;
    try { srv = (await apiJson('/api/prefs')).prefs; } catch { return; }
    const last = LS.get('cctipi_last_user', null);
    if (srv && typeof srv === 'object') {
      for (const k of USER_PREF_KEYS) {
        try { if (srv[k] == null) localStorage.removeItem(k); else localStorage.setItem(k, JSON.stringify(srv[k])); } catch { /* abaikan */ }
      }
      applyPrefsFromLS();
    } else if (last && last !== state.user) {
      for (const k of USER_PREF_KEYS) { try { localStorage.removeItem(k); } catch { /* abaikan */ } }
      applyPrefsFromLS();
    }
    try { localStorage.setItem('cctipi_last_user', JSON.stringify(state.user)); } catch { /* abaikan */ }
    prefsReady = true;
    if (!srv) pushPrefs();
    App.renderLpSelect?.(); App.syncLayoutButtons?.();
  }

  async function loadMe() {
    try {
      const me = await apiJson('/api/auth/me');
      if (!me.authenticated) { window.location.href = '/login'; return; }
      state.user = me.username; state.minPass = me.minPasswordLength || 8;
      state.mustChange = !!(me.mustChange ?? me.defaultPassword);
      state.role = me.role || 'admin'; state.roleName = me.roleName || 'Admin';
      state.regions = Array.isArray(me.regions) ? me.regions : [];
      state.perms = Array.isArray(me.permissions) ? new Set(me.permissions) : null;
      applyPermStyle();
      $('#user-name').textContent = me.username || '-';
      $('#user-initial').textContent = (me.username || '?').charAt(0).toUpperCase();
      $('#user-role').textContent = state.roleName;
      const banner = $('#default-pass-banner');
      banner.classList.toggle('hidden', !state.mustChange);
      const dismissBtn = $('[data-dismiss="default-pass-banner"]');
      if (dismissBtn) dismissBtn.classList.toggle('hidden', state.mustChange);
      await syncUserPrefs();
    } catch { /* api() mengarahkan ke /login */ }
  }

  async function loadCameras(force = false) {
    if (state.mustChange) return;
    $('#api-source-badge').textContent = 'Menyinkronkan…';
    try {
      const data = await apiJson(`/api/cameras${force ? '?refresh=1' : ''}`);
      syncDirectCorsHosts(data.directCorsHosts);
      state.cameras = data.cameras || [];
      state.apiStatus = data.apiStatus || {};
      state.source = data.source || '';
      state.byKey = new Map(state.cameras.map((c) => [camKey(c), c]));
      state.byId = new Map(state.cameras.map((c) => [Number(c.id), c]));
      if (!state.loaded) { state.customFolders = normalizeFolders(LS.get(K.folders, null) ?? rawFolders); saveFolders(); }
      state.loaded = true;
      const t = data.updated_at ? new Date(data.updated_at * 1000) : new Date();
      $('#api-source-badge').textContent = `Sumber: ${state.source || 'live'} (${fmtTime(t)})`;
      App.renderPills?.(); App.renderPresetOptions?.(); App.renderFillSelect?.(); App.renderApiBanner?.();
      if (force) toast(`Daftar kamera tersinkron · ${state.cameras.length} kamera`);
    } catch (err) {
      $('#api-source-badge').textContent = 'Gagal memuat';
      toast(`Gagal memuat kamera: ${err.message}`, 'err');
    }
  }

  async function refreshAiStatus() {
    if (state.mustChange) return;
    try {
      const st = await apiJson('/api/ai/status');
      if (state.lastTodayCount != null && st.today_count > state.lastTodayCount) {
        const n = st.today_count - state.lastTodayCount;
        if (state.view === 'jepretan') App.loadJepretan?.();
        else { state.unseenJp += n; toast(`${n} jepretan AI baru`); }
      }
      state.lastTodayCount = st.today_count;
      state.aiStatus = st;
    } catch { state.aiStatus = { available: false, unavailable_reason: 'Status AI tidak dapat dimuat.' }; }
    App.renderStatusBits?.();
  }

  async function refreshRecStatus() {
    if (state.mustChange) return;
    try { state.recStatus = await apiJson('/api/rekaman/status'); } catch { state.recStatus = null; }
  }

  async function refreshMonStatus() {
    if (state.mustChange) return;
    try { state.monStatus = await apiJson('/api/monitor/status'); } catch { state.monStatus = null; }
  }

  async function refreshHealthInfo() {
    try { state.healthInfo = await apiJson('/api/health'); } catch { state.healthInfo = null; }
  }

  async function checkStatus(body) {
    try {
      const data = await postJson('/api/check-status', body);
      let on = 0, off = 0;
      for (const [id, st] of Object.entries(data.statuses || {})) {
        const cam = state.byId.get(Number(id)); if (!cam) continue;
        state.status.set(camKey(cam), st); st === 'online' ? on++ : off++;
      }
      return { on, off };
    } catch (err) { toast(`Cek status gagal: ${err.message}`, 'err'); return { on: 0, off: 0 }; }
  }

  // Modal generik
  function openModal(html, cls = '') {
    if (els.modal._onClose) { els.modal._onClose(); els.modal._onClose = null; }
    els.dialog.style.width = '';
    els.dialog.className = `modal-dialog ${cls}`;
    els.dialog.innerHTML = html;
    els.modal.classList.remove('hidden');
  }
  function closeModal() {
    els.modal.classList.add('hidden');
    if (els.modal._onClose) { els.modal._onClose(); els.modal._onClose = null; }
    els.dialog.innerHTML = '';
  }
  function confirmBox(title, msg, ok) {
    openModal(
      `<div class="modal-header"><div class="modal-heading"><h2 id="modal-title">${esc(title)}</h2><p class="modal-subtitle">${esc(msg)}</p></div><button class="icon-btn close-btn" data-close-modal aria-label="Tutup">${ic('x')}</button></div>
       <div class="modal-footer"><button class="action-btn" data-close-modal>Batal</button><button class="action-btn primary-btn" id="cf-ok">Ya, lanjut</button></div>`,
      'confirm-dialog'
    );
    els.dialog.style.width = 'min(460px,100%)';
    $('#cf-ok').onclick = () => { closeModal(); ok(); };
  }
  function promptBox(title, label, val, ok) {
    openModal(
      `<div class="modal-header"><div class="modal-heading"><h2 id="modal-title">${esc(title)}</h2></div><button class="icon-btn close-btn" data-close-modal aria-label="Tutup">${ic('x')}</button></div>
       <div style="padding:0 26px 18px"><label class="source-field"><span>${esc(label)}</span><input type="text" id="pr-in" value="${esc(val)}" maxlength="60"></label></div>
       <div class="modal-footer"><button class="action-btn" data-close-modal>Batal</button><button class="action-btn primary-btn" id="pr-ok">Simpan</button></div>`,
      'confirm-dialog'
    );
    els.dialog.style.width = 'min(460px,100%)';
    const inp = $('#pr-in'); inp.focus(); inp.select();
    const done = () => { const v = inp.value.trim(); if (!v) return; closeModal(); ok(v); };
    $('#pr-ok').onclick = done;
    inp.onkeydown = (e) => { if (e.key === 'Enter') done(); };
  }

  Object.assign(App, {
    $, $$, ic, esc, pad, fmtTime, fmtDateTime, fmtDur, fmtBytes, isoDay,
    isCompact, reduceMotion, bump, LS, K, state, tiles, els,
    saveLayout, saveFolders, savePresets, normalizeFolders, syncUserPrefs,
    toast, api, apiJson, postJson,
    normalizeStreamUrl, camKey, camType, isManual, streamLabel, resolveStreamUrl,
    folderNames, camsIn, filtered, isOffline,
    can, VIEW_PERM, viewAllowed, SET_TAB_PERM, applyPermStyle,
    loadMe, loadCameras, refreshAiStatus, refreshRecStatus, refreshMonStatus, refreshHealthInfo, checkStatus,
    openModal, closeModal, confirmBox, promptBox,
  });
})();
