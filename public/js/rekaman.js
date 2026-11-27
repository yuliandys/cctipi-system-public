/* ==========================================================================
   CCTipi System — Fitur Rekaman (Server MP4 & Browser IndexedDB) & Snapshot
   ========================================================================== */
(() => {
  'use strict';

  const App = window.CCTipi;
  const { $, $$, ic, esc, fmtDateTime, fmtDur, fmtBytes, state, els, toast, apiJson, camKey, can, refreshRecStatus, openModal, confirmBox } = App;

  function takeSnapshot(tile, cam) {
    const el = tile._player?.mediaEl;
    if (!el) { toast('Snapshot tidak tersedia untuk tipe stream ini.', 'err'); return; }
    const w = el.videoWidth || el.naturalWidth, h = el.videoHeight || el.naturalHeight;
    if (!w || !h) { toast('Frame belum siap, coba lagi sebentar.', 'err'); return; }
    try {
      const c = document.createElement('canvas'); c.width = w; c.height = h;
      c.getContext('2d').drawImage(el, 0, 0);
      const a = document.createElement('a');
      a.download = `cctipi-${(cam.name || 'kamera').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')}-${Date.now()}.png`;
      a.href = c.toDataURL('image/png'); a.click();
      toast(`Snapshot “${cam.name}” diunduh`);
    } catch { toast('Snapshot gagal (stream lintas domain tanpa CORS).', 'err'); }
  }

  function idbOpen() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open('cctipi', 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('recordings')) db.createObjectStore('recordings', { keyPath: 'id', autoIncrement: true });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  async function idbRun(mode, fn) {
    const db = await idbOpen();
    return new Promise((resolve, reject) => {
      const tx = db.transaction('recordings', mode);
      const r = fn(tx.objectStore('recordings'));
      tx.oncomplete = () => { db.close(); resolve(r && r.result); };
      tx.onerror = () => { db.close(); reject(tx.error); };
    });
  }
  const idbAdd = (rec) => idbRun('readwrite', (s) => s.add(rec));
  const idbAll = () => idbRun('readonly', (s) => s.getAll()).then((x) => x || []);
  const idbDel = (id) => idbRun('readwrite', (s) => s.delete(id));
  const idbClear = () => idbRun('readwrite', (s) => s.clear());

  async function countBrowserRecs() {
    try { state.browserRecCount = (await idbAll()).length; } catch { state.browserRecCount = 0; }
    App.updateCounts?.();
  }

  function pickMime() {
    return ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm', 'video/mp4']
      .find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || '';
  }

  function toggleRecording(tile, cam) {
    if (tile._rec) { stopRecording(tile, false); return; }
    const el = tile._player?.mediaEl;
    if (!el) { toast('Rekaman tidak tersedia untuk tipe stream ini.', 'err'); return; }
    let stream = null, canvasTimer = null;
    try {
      if (el.captureStream) stream = el.captureStream();
      else if (el.tagName === 'IMG') {
        const c = document.createElement('canvas'); c.width = el.naturalWidth || 640; c.height = el.naturalHeight || 360;
        const ctx = c.getContext('2d');
        canvasTimer = setInterval(() => { try { ctx.drawImage(el, 0, 0, c.width, c.height); } catch { /* abaikan */ } }, 100);
        stream = c.captureStream(10);
      }
    } catch { /* abaikan */ }
    if (!stream) { toast('Browser tidak dapat menangkap stream ini.', 'err'); return; }
    const mime = pickMime();
    let mr;
    try { mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined); } catch (err) { toast(`Rekaman gagal: ${err.message}`, 'err'); return; }
    const chunks = [];
    const startedAt = Date.now();
    mr.ondataavailable = (ev) => { if (ev.data.size) chunks.push(ev.data); };
    mr.onstop = async () => {
      if (canvasTimer) clearInterval(canvasTimer);
      const blob = new Blob(chunks, { type: mr.mimeType || 'video/webm' });
      try {
        await idbAdd({ camName: cam.name, camKey: camKey(cam), createdAt: startedAt, durationMs: Date.now() - startedAt, size: blob.size, blob });
        toast(`Rekaman “${cam.name}” tersimpan di browser`);
      } catch (err) { toast(`Gagal menyimpan rekaman: ${err.message}`, 'err'); }
      countBrowserRecs();
      if (state.view === 'rekaman') renderRekaman();
    };
    try { mr.start(500); } catch {
      if (canvasTimer) clearInterval(canvasTimer);
      toast('Stream belum mengalir — tunggu video tampil lalu tekan Rec lagi.', 'err');
      return;
    }
    const badge = $('[data-role="rec-badge"]', tile);
    const btn = $('[data-action="record"]', tile);
    badge.classList.add('active'); badge.textContent = '00:00';
    btn.innerHTML = `${ic('stop')}<span>Stop</span>`;
    tile.classList.add('is-recording');
    const tick = setInterval(() => { badge.textContent = fmtDur((Date.now() - startedAt) / 1000); }, 500);
    tile._rec = { mr, tick, canvasTimer };
  }

  function stopRecording(tile, silent) {
    if (!tile || !tile._rec) return;
    const { mr, tick, canvasTimer } = tile._rec;
    tile._rec = null; clearInterval(tick);
    const badge = $('[data-role="rec-badge"]', tile); if (badge) badge.classList.remove('active');
    const btn = $('[data-action="record"]', tile); if (btn) btn.innerHTML = `${ic('rec')}<span>Rec</span>`;
    tile.classList.remove('is-recording');
    if (mr.state !== 'inactive') {
      if (silent) { mr.onstop = null; if (canvasTimer) clearInterval(canvasTimer); }
      mr.stop();
    }
  }

  function downloadBlob(r) {
    if (!r) return;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(r.blob);
    a.download = `cctipi-${(r.camName || 'rekaman').toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${r.createdAt}.${(r.blob.type || '').includes('mp4') ? 'mp4' : 'webm'}`;
    a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }

  async function renderRekaman() {
    if (!can('rekaman.view')) state.pageTab.rekaman = 'browser';
    const tab = state.pageTab.rekaman;
    await refreshRecStatus();
    let browser = [];
    try { browser = (await idbAll()).sort((a, b) => b.createdAt - a.createdAt); } catch { /* IndexedDB absen */ }
    state.browserRecCount = browser.length; App.updateCounts?.();
    if (state.view !== 'rekaman') return;
    const rs = state.recStatus || {};
    const pct = rs.quota_bytes ? Math.min(100, (rs.total_size_bytes / rs.quota_bytes) * 100) : 0;
    const bused = browser.reduce((a, r) => a + (r.size || 0), 0);
    els.page.innerHTML = `<div class="page">
      <div class="page-tabs" role="tablist">
        <button class="page-tab${tab === 'server' ? ' active' : ''}" data-rtab="server" data-perm="rekaman.view">${ic('hdd')}Rekaman server<span class="tab-count">${rs.recordings_count || 0}</span></button>
        <button class="page-tab${tab === 'browser' ? ' active' : ''}" data-rtab="browser">${ic('film')}Rekaman browser<span class="tab-count">${browser.length}</span></button>
      </div>
      ${tab === 'server' ? `
      <div class="card">
        ${!rs.available ? `<div class="empty-card">${ic('alert')}<b>Rekaman Server tidak tersedia</b><span>${esc(rs.unavailable_reason || 'ffmpeg tidak ditemukan di server.')}</span></div>` : `
        <div class="filter-bar">
          <label class="source-field"><span>Tanggal</span><input type="date" id="sr-date" value="${esc(state.srFilter.date)}"></label>
          <label class="source-field"><span>Kamera</span><select id="sr-cam"><option value="">Semua kamera</option></select></label>
          <label class="source-field"><span>Mode</span><select id="sr-mode"><option value="">Semua mode</option><option value="berkelanjutan"${state.srFilter.mode === 'berkelanjutan' ? ' selected' : ''}>Berkelanjutan</option><option value="deteksi"${state.srFilter.mode === 'deteksi' ? ' selected' : ''}>Klip deteksi</option></select></label>
          <span class="spacer"></span>
          <div class="usage-meter">${ic('hdd', 'ic-sm')}<span>${fmtBytes(rs.total_size_bytes || 0)} / ${fmtBytes(rs.quota_bytes || 0)}</span><div class="health-bar-track"><div class="health-bar-fill" style="width:${pct}%"></div></div></div>
          <button class="action-btn" id="sr-reload">${ic('rotate')}<span>Muat ulang</span></button>
          <button class="action-btn" data-goto="pengaturan" data-tab="rekaman" data-perm="settings.ai">${ic('sliders')}<span>Atur</span></button>
        </div>
        <p class="field-note" style="margin-top:10px">${rs.enabled ? `Perekaman aktif · ${(rs.recording || []).length} kamera sedang direkam${(rs.recording || []).length ? ': ' + (rs.recording || []).map((r) => esc(r.camera_name)).join(', ') : ''}` : 'Perekaman server nonaktif — aktifkan di Pengaturan → Rekaman server.'}</p>
        <div class="srec-list" id="sr-list"><div class="empty-state">Memuat…</div></div>`}
      </div>` : `
      <div class="card">
        <div class="card-row" style="justify-content:space-between"><div><h3>${ic('film')}Klip manual dari tombol Rec</h3><p class="card-sub">Tersimpan hanya di browser ini (IndexedDB) · ${fmtBytes(bused)} · hilang bila data browser dibersihkan</p></div>
          ${browser.length ? `<button class="action-btn danger-btn" id="br-clear">${ic('trash')}<span>Hapus semua</span></button>` : ''}</div>
        <div class="srec-list">${browser.map((r, i) => `<div class="srec-row no-thumb" style="--i:${i}"><span class="rec-ico">${ic('film')}</span><div><div class="t">${esc(r.camName)}</div><div class="m">${esc(fmtDateTime(new Date(r.createdAt)))} · ${fmtDur((r.durationMs || 0) / 1000)} · ${fmtBytes(r.size)}</div></div>
          <div class="row-actions"><button class="rec-mini-btn" data-play-br="${r.id}">${ic('play')}Putar</button><button class="rec-mini-btn" data-dl-br="${r.id}">${ic('download')}Unduh</button><button class="rec-mini-btn" data-act="delete" data-del-br="${r.id}">${ic('trash')}Hapus</button></div></div>`).join('') || '<div class="empty-state">Belum ada rekaman.<br>Tekan <b>Rec</b> pada tile kamera untuk mulai merekam.</div>'}</div>
      </div>`}
    </div>`;
    $$('[data-rtab]', els.page).forEach((b) => b.onclick = () => { state.pageTab.rekaman = b.dataset.rtab; renderRekaman(); });
    if (tab === 'server' && rs.available) {
      const f = state.srFilter;
      $('#sr-date').onchange = (e) => { f.date = e.target.value; loadServerRecs(); };
      $('#sr-cam').onchange = (e) => { f.cam = e.target.value; loadServerRecs(); };
      $('#sr-mode').onchange = (e) => { f.mode = e.target.value; loadServerRecs(); };
      $('#sr-reload').onclick = () => renderRekaman();
      loadServerRecs();
    }
    if (tab === 'browser') {
      const byId = new Map(browser.map((r) => [String(r.id), r]));
      $$('[data-play-br]', els.page).forEach((b) => b.onclick = () => openPlayer(byId.get(b.dataset.playBr), 'browser'));
      $$('[data-dl-br]', els.page).forEach((b) => b.onclick = () => downloadBlob(byId.get(b.dataset.dlBr)));
      $$('[data-del-br]', els.page).forEach((b) => b.onclick = async () => { await idbDel(Number(b.dataset.delBr)); toast('Rekaman browser dihapus'); renderRekaman(); });
      const clr = $('#br-clear');
      if (clr) clr.onclick = () => confirmBox('Hapus semua rekaman browser?', 'Semua klip di browser ini akan dihapus.', async () => { await idbClear(); renderRekaman(); });
    }
  }

  async function loadServerRecs() {
    const f = state.srFilter, p = new URLSearchParams();
    if (f.date) p.set('date', f.date); if (f.cam) p.set('camera', f.cam); if (f.mode) p.set('mode', f.mode);
    const list = $('#sr-list'); if (!list) return;
    let items = [];
    try { items = (await apiJson(`/api/rekaman?${p}`)).rekaman || []; }
    catch (err) { list.innerHTML = `<div class="empty-state">Gagal memuat: ${esc(err.message)}</div>`; return; }
    const sel = $('#sr-cam');
    if (sel && sel.options.length <= 1) {
      const names = new Map(); for (const it of items) names.set(it.camera_id, it.camera_name);
      for (const k of state.recSettings?.continuous_cameras || []) if (!names.has(k) && state.byKey.get(k)) names.set(k, state.byKey.get(k).name);
      sel.innerHTML = '<option value="">Semua kamera</option>' + [...names].map(([k, n]) => `<option value="${esc(k)}">${esc(n)}</option>`).join('');
      sel.value = f.cam;
    }
    const byId = new Map(items.map((r) => [String(r.id), r]));
    list.innerHTML = items.map((r, i) => `<div class="srec-row no-thumb" style="--i:${i}"><span class="rec-ico ${r.mode === 'deteksi' ? 'det' : ''}">${ic(r.mode === 'deteksi' ? 'scan' : 'hdd')}</span><div><div class="t">${esc(r.camera_name || 'Kamera')}</div><div class="m"><span class="mode-chip ${esc(r.mode)}">${r.mode === 'deteksi' ? 'Klip deteksi' : 'Berkelanjutan'}</span>${esc(fmtDateTime(new Date((r.started_at || 0) * 1000)))} · ${fmtDur(r.duration_s || 0)} · ${fmtBytes(r.size_bytes)}</div></div>
      <div class="row-actions"><button class="rec-mini-btn" data-play-sr="${esc(r.id)}">${ic('play')}Putar</button><a class="rec-mini-btn" href="${esc(r.file_url)}?download=1" download>${ic('download')}Unduh</a><button data-perm="rekaman.delete" class="rec-mini-btn" data-act="delete" data-del-sr="${esc(r.id)}">${ic('trash')}Hapus</button></div></div>`).join('')
      || `<div class="empty-card">${ic('film')}<b>Belum ada rekaman server pada filter ini</b><span>Aktifkan Rekaman Server dan pilih kamera opt-in, atau tunggu klip deteksi AI.</span></div>`;
    $$('[data-play-sr]', list).forEach((b) => b.onclick = () => openPlayer(byId.get(b.dataset.playSr), 'server'));
    $$('[data-del-sr]', list).forEach((b) => b.onclick = () => confirmBox('Hapus rekaman server?', 'Berkas MP4 akan dihapus dari server.', async () => {
      try { await apiJson(`/api/rekaman/${encodeURIComponent(b.dataset.delSr)}`, { method: 'DELETE' }); toast('Rekaman server dihapus'); renderRekaman(); }
      catch (err) { toast(`Gagal menghapus: ${err.message}`, 'err'); }
    }));
  }

  function openPlayer(r, kind, opt = {}) {
    if (!r) return;
    const server = kind === 'server';
    const title = server ? r.camera_name : r.camName;
    const when = server ? new Date((r.started_at || 0) * 1000) : new Date(r.createdAt);
    const src = server ? r.file_url : URL.createObjectURL(r.blob);
    openModal(
      `<span class="sheet-handle" aria-hidden="true"></span>
       <div class="modal-header"><div class="modal-heading"><h2 id="modal-title">${esc(title || 'Rekaman')}</h2><p class="modal-subtitle">${server ? (r.mode === 'deteksi' ? 'Klip deteksi' : 'Segmen berkelanjutan') + ' · MP4 dari server' : 'Rekaman browser'} · ${esc(fmtDateTime(when))} · ${fmtBytes(server ? r.size_bytes : r.size)}</p></div>
       <button class="icon-btn close-btn" data-close-modal aria-label="Tutup">${ic('x')}</button></div>
       <div class="player"><video id="pl-video" class="player-video" controls playsinline preload="metadata" src="${esc(src)}"></video>
         <div class="player-bar"><label class="segmented quality-group"><span class="quality-label">Kecepatan</span><select id="pl-speed"><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="1.5">1.5×</option><option value="2">2×</option><option value="4">4×</option></select></label></div></div>
       <div class="modal-footer"><button class="action-btn danger-btn" id="pl-del"${server ? ' data-perm="rekaman.delete"' : ''}>${ic('trash')}<span>Hapus</span></button>${server ? `<a class="action-btn primary-btn" href="${esc(r.file_url)}?download=1" download>${ic('download')}<span>Unduh MP4</span></a>` : `<button class="action-btn primary-btn" id="pl-dl">${ic('download')}<span>Unduh</span></button>`}</div>`,
      'playback-dialog'
    );
    const v = $('#pl-video');
    if (opt.startAt) {
      v.addEventListener('loadedmetadata', () => {
        try { v.currentTime = Math.min(opt.startAt, Math.max(0, (v.duration || opt.startAt) - 0.5)); } catch { /* abaikan */ }
      }, { once: true });
    }
    v.play().catch(() => {});
    $('#pl-speed').onchange = (e) => { v.playbackRate = Number(e.target.value); };
    els.modal._onClose = () => {
      try { v.pause(); v.removeAttribute('src'); v.load(); } catch { /* abaikan */ }
      if (!server) URL.revokeObjectURL(src);
    };
    const dl = $('#pl-dl'); if (dl) dl.onclick = () => downloadBlob(r);
    $('#pl-del').onclick = () => confirmBox('Hapus rekaman?', server ? 'Berkas MP4 akan dihapus dari server.' : 'Klip akan dihapus dari browser ini.', async () => {
      try {
        if (server) await apiJson(`/api/rekaman/${encodeURIComponent(r.id)}`, { method: 'DELETE' });
        else await idbDel(r.id);
        toast('Rekaman dihapus'); renderRekaman();
      } catch (err) { toast(`Gagal menghapus: ${err.message}`, 'err'); }
    });
  }

  Object.assign(App, {
    takeSnapshot, countBrowserRecs, toggleRecording, stopRecording,
    renderRekaman, loadServerRecs, openPlayer,
  });
})();
