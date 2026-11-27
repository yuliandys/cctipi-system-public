/* ==========================================================================
   CCTipi System — Router, Shell Listeners & Inisialisasi Utama
   Modul fitur dimuat berurutan sebelum berkas ini:
   - /js/core.js       : State, API, RBAC, Prefs, Modal
   - /js/pantau.js     : Beranda, Multiview, Player, Sidebar, Peta, Health
   - /js/rekaman.js    : Rekaman Server & Browser (IndexedDB)
   - /js/jepretan.js   : Galeri Jepretan AI & Perjelas Malam
   - /js/pengaturan.js : Kamera Saya (ONVIF/Tuya) & Pengaturan Sistem
   ========================================================================== */
(() => {
  'use strict';

  const App = window.CCTipi;
  const {
    $, $$, esc, fmtTime, isCompact, reduceMotion, bump, state, els,
    toast, apiJson, postJson, folderNames, viewAllowed,
    loadMe, loadCameras, refreshAiStatus, refreshRecStatus, refreshMonStatus,
    syncUserPrefs, closeModal, confirmBox,
  } = App;

  const TITLES = {
    home: 'Beranda', pantau: 'Pantau', peta: 'Peta', health: 'Health Monitor',
    rekaman: 'Rekaman', jepretan: 'Jepretan AI', kamera: 'Kamera Saya', pengaturan: 'Pengaturan',
  };

  function closeMenus() {
    els.toolbar.classList.remove('open');
    document.body.classList.remove('menu-open');
    $('#more-btn').setAttribute('aria-expanded', 'false');
  }
  function closeDrawer() { document.body.classList.remove('sidebar-open'); }

  function go(view, opt = {}) {
    if (state.mustChange && (view !== 'pengaturan' || (opt.tab && opt.tab !== 'akun'))) {
      toast('Ganti password bawaan/awal terlebih dahulu di Pengaturan → Akun saya.', 'err');
      view = 'pengaturan';
      opt = { tab: 'akun' };
    }
    if (!viewAllowed(view)) { toast('Akun Anda tidak memiliki akses ke halaman ini', 'err'); return; }
    if (state.editMode && view !== 'pantau') {
      state.editMode = false;
      document.body.classList.remove('edit-mode');
      $('#edit-layout-btn').classList.remove('active');
    }
    if (view !== state.view && (state.view === 'pantau' || state.view === 'home')) App.destroyTiles();
    if (view !== 'pantau' && state.liveAi) { App.stopLiveAi(true); toast('Live AI dimatikan karena meninggalkan Pantau'); }
    state.view = view;
    if (opt.tab) state.pageTab[view] = opt.tab;
    if (view === 'jepretan') state.unseenJp = 0;
    closeMenus(); closeDrawer();
    try { if (location.hash.slice(1) !== view) history.replaceState(null, '', view === 'home' ? location.pathname : `#${view}`); } catch { /* abaikan */ }
    renderView();
    els.page.scrollTop = 0;
  }
  window.addEventListener('hashchange', () => {
    const h = location.hash.slice(1) || 'home';
    if (TITLES[h] && h !== state.view && viewAllowed(h)) go(h);
  });

  function renderNav() {
    const v = state.view, b = document.body.classList;
    $('#bottom-nav').dataset.active = b.contains('menu-open') ? 'lainnya'
      : b.contains('sidebar-open') ? 'daftar'
      : v === 'home' ? 'beranda'
      : v === 'pantau' ? 'pantau'
      : v === 'jepretan' ? 'jepretan' : 'lainnya';
  }

  function renderView() {
    const v = state.view;
    if (els.title.textContent !== TITLES[v]) {
      els.title.textContent = TITLES[v];
      if (!reduceMotion()) { els.title.classList.remove('swap'); void els.title.offsetWidth; els.title.classList.add('swap'); }
    }
    document.body.dataset.page = v;
    els.grid.classList.toggle('hidden', !(v === 'home' || v === 'pantau'));
    els.map.classList.toggle('hidden', v !== 'peta');
    els.page.classList.toggle('hidden', ['home', 'pantau', 'peta'].includes(v));
    els.ctlToggle.classList.toggle('hidden', v !== 'pantau' && v !== 'home');
    $$('[data-page-btn]').forEach((b) => b.classList.toggle('active', b.dataset.pageBtn === v));
    const subs = {
      peta: `${state.cameras.filter((c) => typeof c.lat === 'number').length} kamera berkoordinat`,
      health: 'Status online / offline per daerah & beban server',
      rekaman: 'Rekaman server & rekaman browser',
      jepretan: 'Deteksi objek otomatis (YOLO11n)',
      kamera: 'Kamera manual & lokal',
      pengaturan: 'AI, rekaman server, sumber stream, akun, backup',
    };
    els.sub.innerHTML = subs[v] ? esc(subs[v]) : `<span id="camera-count">${state.cameras.length}</span> kamera · <span id="folder-count">${folderNames().length}</span> daerah`;
    renderNav();
    if (v === 'home') App.renderHome();
    else if (v === 'pantau') App.renderGrid();
    else if (v === 'peta') App.renderMap();
    else {
      App.destroyTiles();
      ({
        health: App.renderHealth, rekaman: App.renderRekaman,
        jepretan: App.renderJepretan, kamera: App.renderKamera,
        pengaturan: App.renderPengaturan,
      })[v]();
    }
    if (v !== 'pantau') { App.renderEditHelp(); App.renderSidebar(); }
    App.updateSummary(); renderStatusBits();
  }

  function updateCounts() {
    const server = state.recStatus?.recordings_count || 0;
    const total = server + (state.browserRecCount || 0);
    const rc = $('#recordings-count');
    if (rc.textContent !== String(total || '')) { rc.textContent = total || ''; if (total) bump(rc); }
    $$('[data-mirror="recordings-count"]').forEach((m) => { m.textContent = total || ''; });
    const jc = $('#jepretan-count'), jv = String(state.aiStatus?.today_count || '');
    if (jc.textContent !== jv) { const grew = Number(jv) > Number(jc.textContent || 0); jc.textContent = jv; if (grew) bump(jc); }
    $('#nav-jp-dot').classList.toggle('on', state.unseenJp > 0);
  }

  function renderStatusBits() {
    const st = state.aiStatus;
    const on = !!(st && st.available && st.running);
    $('#ai-quick-btn').classList.toggle('on', on);
    $('#ai-quick-lbl').textContent = !st ? 'AI…' : !st.available ? 'AI tidak tersedia' : on ? `AI aktif · ${st.monitored_cameras} kamera` : 'AI mati';
    updateCounts();
  }

  function renderApiBanner() {
    const down = Object.entries(state.apiStatus || {}).filter(([, st]) => st && st.online === false).map(([f]) => f);
    const b = $('#api-alert-banner');
    b.classList.toggle('hidden', !down.length);
    if (down.length) $('#api-alert-text').textContent = `${down.length} sumber daerah tidak merespons (${down.join(', ')}). Menampilkan data cache terakhir.`;
  }

  async function setAiEnabled(on) {
    try {
      const cur = state.aiSettings || (await apiJson('/api/ai/settings')).settings || {};
      const data = await postJson('/api/ai/settings', { settings: { ...cur, enabled: on } });
      state.aiSettings = data.settings || cur;
      await refreshAiStatus();
      toast(on ? 'Worker AI dimulai (hanya kamera opt-in)' : 'Worker AI dihentikan');
      if (state.view === 'pengaturan' || state.view === 'home') renderView();
    } catch (err) { toast(`Gagal mengubah worker AI: ${err.message}`, 'err'); }
  }

  // --------------------------------------------------- event shell global
  document.addEventListener('click', (e) => {
    const g = e.target.closest('[data-goto]'); if (g) { e.preventDefault(); go(g.dataset.goto, { tab: g.dataset.tab }); return; }
    const p = e.target.closest('[data-page-btn]'); if (p) { go(p.dataset.pageBtn); return; }
    const h = e.target.closest('[data-go="home"]'); if (h) { go('home'); return; }
    const px = e.target.closest('[data-proxy]'); if (px) { document.getElementById(px.dataset.proxy)?.click(); return; }
    const pk = e.target.closest('[data-play-key]'); if (pk) { App.playKeys([pk.dataset.playKey], 1, 1); return; }
    if (e.target.closest('[data-close-modal]')) { closeModal(); return; }
    if (els.toolbar.classList.contains('open') && !e.target.closest('#toolbar-actions') && !e.target.closest('#more-btn') && !e.target.closest('#bottom-nav')) { closeMenus(); renderNav(); }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!els.modal.classList.contains('hidden')) closeModal();
    else if (document.body.classList.contains('tv-wall-mode')) App.setTvWall(false);
    else if (state.editMode) App.setEditMode(false);
    else if (state.focusKey) { state.focusKey = null; App.renderGrid(); }
  });
  $('#more-btn').addEventListener('click', (e) => { e.stopPropagation(); const open = !els.toolbar.classList.contains('open'); els.toolbar.classList.toggle('open', open); $('#more-btn').setAttribute('aria-expanded', String(open)); document.body.classList.toggle('menu-open', open && isCompact()); renderNav(); });
  $('#toggle-sidebar-btn').addEventListener('click', () => { if (isCompact()) document.body.classList.toggle('sidebar-open'); else $('#sidebar').classList.toggle('collapsed'); renderNav(); });
  $('#sidebar-backdrop').addEventListener('click', () => { closeDrawer(); renderNav(); });
  $('#sheet-backdrop').addEventListener('click', () => { closeMenus(); renderNav(); });
  $('[data-close-drawer]').addEventListener('click', () => { closeDrawer(); renderNav(); });
  $('#bottom-nav').addEventListener('click', (e) => {
    const it = e.target.closest('[data-nav]'); if (!it) return;
    e.stopPropagation();
    const n = it.dataset.nav;
    if (n !== 'lainnya') closeMenus();
    if (n === 'beranda') go('home');
    else if (n === 'pantau') go('pantau');
    else if (n === 'jepretan') go('jepretan');
    else if (n === 'daftar') document.body.classList.toggle('sidebar-open');
    else { closeDrawer(); const open = !els.toolbar.classList.contains('open'); els.toolbar.classList.toggle('open', open); document.body.classList.toggle('menu-open', open); }
    renderNav();
  });
  $('#logout-btn').addEventListener('click', () => confirmBox('Keluar?', 'Sesi di perangkat ini akan dicabut.', async () => {
    try { await fetch('/api/logout', { method: 'POST' }); } catch { /* abaikan */ }
    window.location.href = '/login';
  }));
  $('#ai-quick-btn').addEventListener('click', async () => {
    closeMenus();
    if (!state.aiStatus?.available) { toast(`AI tidak tersedia: ${state.aiStatus?.unavailable_reason || 'dependensi AI belum terpasang'}`, 'err'); go('pengaturan', { tab: 'ai' }); return; }
    await setAiEnabled(!state.aiStatus.enabled);
  });
  $('[data-dismiss="default-pass-banner"]').addEventListener('click', () => { if (!state.mustChange) $('#default-pass-banner').classList.add('hidden'); });
  $('[data-dismiss="api-alert-banner"]').addEventListener('click', () => $('#api-alert-banner').classList.add('hidden'));
  $('#api-alert-retry-btn').addEventListener('click', async () => { await loadCameras(true); renderView(); });

  // -------------------------------------------------------------- start
  function tickClock() {
    const d = new Date(), h = d.getHours();
    $('#clock').textContent = fmtTime(d);
    $('#greeting').textContent = `Selamat ${h < 11 ? 'pagi' : h < 15 ? 'siang' : h < 18 ? 'sore' : 'malam'}`;
  }
  tickClock(); setInterval(tickClock, 1000);

  async function loadInitialOperationalData() {
    await syncUserPrefs();
    await loadCameras();
    await Promise.all([
      refreshAiStatus(), refreshRecStatus(), App.countBrowserRecs(),
      apiJson('/api/ai/settings').then((d) => { state.aiSettings = d.settings || {}; }).catch(() => {}),
      apiJson('/api/rekaman/settings').then((d) => { state.recSettings = d.settings || {}; }).catch(() => {}),
      apiJson('/api/live-ai/status').then((d) => { state.liveStatus = d; }).catch(() => {}),
      refreshMonStatus(),
    ]);
  }

  async function init() {
    App.renderLpSelect();
    App.syncLayoutButtons();
    renderView();
    await loadMe();
    if (state.mustChange) {
      state.view = 'pengaturan';
      state.pageTab.pengaturan = 'akun';
      renderView();
    } else {
      await loadInitialOperationalData();
      const h = (location.hash || '').slice(1);
      if (TITLES[h] && viewAllowed(h)) state.view = h;
      renderView();
    }
    setInterval(refreshAiStatus, 20000);
    setInterval(async () => {
      if (state.mustChange) return;
      await refreshRecStatus(); await refreshMonStatus();
      try { state.liveStatus = await apiJson('/api/live-ai/status'); } catch { /* abaikan */ }
      updateCounts();
    }, 30000);
    setInterval(() => {
      if (!state.mustChange) loadCameras().then(() => { if (state.view !== 'pantau' && state.view !== 'home') App.renderSidebar(); });
    }, 5 * 60 * 1000);
  }

  Object.assign(App, {
    TITLES, go, renderView, renderNav, closeMenus, closeDrawer,
    updateCounts, renderStatusBits, renderApiBanner, loadInitialOperationalData,
  });

  init();
})();
