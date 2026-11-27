/* ==========================================================================
   CCTipi System — Fitur Jepretan AI, Katalog Kelas & Modal Perjelas Malam
   ========================================================================== */
(() => {
  'use strict';

  const App = window.CCTipi;
  const { $, $$, ic, esc, fmtTime, fmtDateTime, state, els, toast, apiJson, camKey, refreshAiStatus, openModal, confirmBox } = App;

  const JP_LABEL = {
    person: 'Orang', bicycle: 'Sepeda', car: 'Mobil', motorcycle: 'Motor',
    bus: 'Bus', truck: 'Truk', bird: 'Burung', cat: 'Kucing', dog: 'Anjing',
    horse: 'Kuda', sheep: 'Domba', cow: 'Sapi', elephant: 'Gajah',
    bear: 'Beruang', zebra: 'Zebra', giraffe: 'Jerapah',
    backpack: 'Ransel', handbag: 'Tas', suitcase: 'Koper',
  };
  const AI_CATALOG = [
    { key: 'orang', label: 'Orang', objects: ['person'] },
    { key: 'kendaraan', label: 'Kendaraan', objects: ['bicycle', 'car', 'motorcycle', 'bus', 'truck'] },
    { key: 'hewan', label: 'Hewan', objects: ['bird', 'cat', 'dog', 'horse', 'sheep', 'cow', 'elephant', 'bear', 'zebra', 'giraffe'] },
    { key: 'barang', label: 'Barang bawaan', objects: ['backpack', 'handbag', 'suitcase'] },
  ].map((c) => ({ ...c, objects: c.objects.map((n) => ({ name: n, label: JP_LABEL[n] || n })) }));

  const aiCatalog = () => (
    state.aiSettings && Array.isArray(state.aiSettings.catalog) && state.aiSettings.catalog.length
      ? state.aiSettings.catalog
      : AI_CATALOG
  );

  function clsPicker(s) {
    const cls = s.classes || {}, objs = s.objects || {};
    return `<div class="cls-list">${aiCatalog().map((c) => {
      const on = cls[c.key] ?? c.default ?? true;
      return `<div class="cls-row${on ? '' : ' off'}" data-cat="${esc(c.key)}"><label class="chk cls-cat"><input type="checkbox" data-cls="${esc(c.key)}"${on ? ' checked' : ''}> ${esc(c.label)}${c.key === 'barang' ? '<span class="cls-new">Baru</span>' : ''}</label>
        <div class="cls-objs">${c.objects.map((o) => `<label class="obj-chip"><input type="checkbox" data-obj="${esc(o.name)}"${objs[o.name] !== false ? ' checked' : ''}${on ? '' : ' disabled'}><span>${esc(o.label)}</span></label>`).join('')}</div></div>`;
    }).join('')}</div>`;
  }

  function bindClsPicker(root) {
    $$('[data-cls]', root).forEach((c) => c.onchange = () => {
      const row = c.closest('.cls-row');
      row.classList.toggle('off', !c.checked);
      $$('[data-obj]', row).forEach((o) => { o.disabled = !c.checked; });
    });
    $$('[data-obj]', root).forEach((o) => o.onchange = () => {
      const row = o.closest('.cls-row');
      if (!$$('[data-obj]', row).some((x) => x.checked)) {
        const cat = $('[data-cls]', row);
        cat.checked = false; cat.onchange();
        $$('[data-obj]', row).forEach((x) => { x.checked = true; });
      }
    });
  }

  const catLabel = (k) => (aiCatalog().find((c) => c.key === k) || {}).label || k;
  const jpLabel = (n) => JP_LABEL[n] || n;
  const jpDate = (ts) => new Date(ts * 1000);

  function jpCard(j, i) {
    return `<button class="jp-card" data-jp="${esc(j.id)}" style="--i:${i}"><img data-fade loading="lazy" alt="Jepretan ${esc(jpLabel(j.class_name))}" src="${esc(j.image_url)}"><span class="jp-meta"><span class="jp-name">${esc(j.camera_name)}</span><span class="jp-sub">${fmtTime(jpDate(j.ts)).slice(0, 5)} · <span class="cat-chip ${esc(j.category)}">${esc(jpLabel(j.class_name))}</span> ${Math.round(j.confidence * 100)}%${j.enhanced ? `<span class="src-chip" title="Diperjelas sebelum deteksi">${ic('moon', 'ic-sm')}</span>` : ''}</span></span></button>`;
  }

  function jpTile(j, i) {
    const label = jpLabel(j.class_name);
    return `<button type="button" class="jp-tile" data-jp="${esc(j.id)}" style="--i:${i}" title="${esc(j.camera_name)} · ${esc(label)} ${Math.round(j.confidence * 100)}%"><img data-fade loading="lazy" alt="Jepretan ${esc(label)} di ${esc(j.camera_name)}" src="${esc(j.image_url)}"><span class="jp-cap"><span class="jp-cap-main"><span class="jp-cap-name">${esc(j.camera_name)}</span><span class="jp-cap-time">${fmtTime(jpDate(j.ts)).slice(0, 5)} · ${Math.round(j.confidence * 100)}%</span></span><span class="cat-chip ${esc(j.category)}">${esc(label)}</span></span></button>`;
  }

  function bindJpCards(root, items) {
    const map = new Map(items.map((j) => [j.id, j]));
    $$('[data-jp]', root).forEach((b) => b.addEventListener('click', () => openJp(map.get(b.dataset.jp))));
  }

  async function loadJepretan() {
    const f = state.jpFilter, p = new URLSearchParams();
    if (f.date) p.set('date', f.date); if (f.cam) p.set('camera', f.cam); if (f.cat) p.set('category', f.cat);
    try { state.jepretan = (await apiJson(`/api/jepretan?${p}`)).jepretan || []; state.jpError = ''; }
    catch (err) { state.jepretan = []; state.jpError = err.message; }
    if (state.view === 'jepretan') renderJepretanBody();
  }

  function renderJepretan() {
    const f = state.jpFilter, ai = state.aiStatus || {};
    const camOpts = state.cameras
      .map((c) => [camKey(c), c.name])
      .filter(([k]) => (state.aiSettings?.cameras || []).includes(k) || k === f.cam);
    els.page.innerHTML = `<div class="page">
      <div class="card"><div class="filter-bar">
        <label class="source-field"><span>Tanggal</span><input type="date" id="jp-date" value="${esc(f.date)}"></label>
        <label class="source-field"><span>Kamera</span><select id="jp-cam"><option value="">Semua kamera</option>${camOpts.map(([k, n]) => `<option value="${esc(k)}"${f.cam === k ? ' selected' : ''}>${esc(n)}</option>`).join('')}</select></label>
        <label class="source-field"><span>Kategori</span><select id="jp-cat"><option value="">Semua kategori</option>${aiCatalog().map((c) => `<option value="${esc(c.key)}"${f.cat === c.key ? ' selected' : ''}>${esc(c.label)}</option>`).join('')}</select></label>
        <span class="spacer"></span>
        <span class="state-chip${ai.running ? ' on' : ''}"><span class="ai-dot"></span>${!ai.available ? 'AI tidak tersedia' : ai.running ? 'Worker AI berjalan' : 'Worker AI berhenti'}</span>
        <button class="action-btn" id="jp-reload">${ic('rotate')}<span>Muat ulang</span></button>
        <button class="action-btn" data-goto="pengaturan" data-tab="ai" data-perm="settings.ai">${ic('sliders')}<span>Atur AI</span></button>
      </div>
      <p class="field-note" id="jp-note" style="margin-top:12px"></p></div>
      <div id="jp-body"><div class="empty-state">Memuat jepretan…</div></div>
    </div>`;
    $('#jp-date').onchange = (e) => { f.date = e.target.value; loadJepretan(); };
    $('#jp-cam').onchange = (e) => { f.cam = e.target.value; loadJepretan(); };
    $('#jp-cat').onchange = (e) => { f.cat = e.target.value; loadJepretan(); };
    $('#jp-reload').onclick = () => loadJepretan();
    loadJepretan();
  }

  function renderJepretanBody() {
    const body = $('#jp-body'); if (!body) return;
    const ai = state.aiStatus || {};
    $('#jp-note').innerHTML = `${ic('shield', 'ic-sm')} Jepretan dibuat otomatis oleh worker AI &amp; Live AI — <b>hanya untuk kamera opt-in</b> (${ai.monitored_cameras || 0} kamera). ${state.jepretan.length} jepretan pada filter ini.`;
    if (state.jpError) { body.innerHTML = `<div class="empty-card">${ic('alert')}<b>Jepretan tidak dapat dimuat</b><span>${esc(state.jpError)}</span></div>`; return; }
    body.innerHTML = state.jepretan.length
      ? `<div class="jp-grid">${state.jepretan.map((j, i) => jpCard(j, i)).join('')}</div>`
      : `<div class="empty-card">${ic('image')}<b>Belum ada jepretan pada filter ini</b><span>Aktifkan AI dan pilih kamera opt-in di Pengaturan, lalu tunggu objek melintas.</span></div>`;
    bindJpCards(body, state.jepretan);
  }

  function openJp(j) {
    if (!j) return;
    const cam = state.byKey.get(j.camera_key);
    openModal(
      `<span class="sheet-handle" aria-hidden="true"></span>
       <div class="modal-header"><div class="modal-heading"><h2 id="modal-title">${esc(jpLabel(j.class_name))} — ${Math.round(j.confidence * 100)}%</h2><p class="modal-subtitle">${esc(j.camera_name)} · ${esc(j.folder || '')} · ${esc(fmtDateTime(jpDate(j.ts)))}</p></div>
         <button class="icon-btn close-btn" data-close-modal aria-label="Tutup">${ic('x')}</button></div>
       <div class="jp-view">
         <div class="card-row" style="margin-bottom:10px"><div class="segmented"><button class="layout-btn active" data-ver="asli">Asli</button><button class="layout-btn" data-ver="enh">${ic('moon', 'ic-sm')} Diperjelas</button></div>
           <span class="field-note" id="jp-vnote" style="margin:0">${j.enhanced ? 'Foto asli. Jepretan ini dideteksi dengan Perjelas Malam aktif.' : 'Foto asli, apa adanya dari kamera.'}</span></div>
         <img id="jp-big" data-fade alt="Jepretan ${esc(j.camera_name)}" src="${esc(j.image_url)}">
         <div class="dets">${(j.detections || []).map((d) => `<div class="det"><span><span class="cat-chip ${esc(d.category)}">${esc(catLabel(d.category))}</span> ${esc(jpLabel(d.class_name))}</span><b>${Math.round(d.confidence * 100)}%</b></div>`).join('')}</div>
       </div>
       <div class="modal-footer"><button class="action-btn danger-btn" id="jp-del" data-perm="jepretan.delete">${ic('trash')}<span>Hapus jepretan</span></button>
         <a class="action-btn" id="jp-open" href="${esc(j.image_url)}" target="_blank" rel="noopener">${ic('maximize')}<span>Buka gambar penuh</span></a>
         <button class="action-btn" id="jp-rec" data-perm="rekaman.view">${ic('film')}<span>Lihat rekaman</span></button>
         ${cam ? `<button class="action-btn primary-btn" data-play-key="${esc(j.camera_key)}">${ic('play')}<span>Buka kamera</span></button>` : ''}</div>`,
      'jp-dialog'
    );
    $$('[data-ver]', els.dialog).forEach((b) => b.onclick = () => {
      const enh = b.dataset.ver === 'enh';
      $$('[data-ver]', els.dialog).forEach((x) => x.classList.toggle('active', x === b));
      const url = enh ? `${j.image_url}?enhanced=1` : j.image_url;
      $('#jp-big').src = url; $('#jp-open').href = url;
      $('#jp-vnote').textContent = enh
        ? 'Versi Perjelas Malam (CLAHE + gamma) — dihasilkan dari foto asli.'
        : (j.enhanced ? 'Foto asli. Jepretan ini dideteksi dengan Perjelas Malam aktif.' : 'Foto asli, apa adanya dari kamera.');
    });
    $('#jp-rec').onclick = async (e) => {
      const b = e.currentTarget; b.disabled = true;
      try {
        const r = await apiJson(`/api/rekaman/find?camera=${encodeURIComponent(j.camera_key)}&ts=${j.ts}`);
        if (r.found) {
          App.openPlayer(r.recording, 'server', { startAt: r.offset });
          toast(`Diputar dari ${fmtTime(new Date((r.recording.started_at + r.offset) * 1000))} · 3 dtk sebelum jepretan`);
        } else toast(r.message || 'Rekaman tidak ditemukan', 'err');
      } catch (err) { toast(`Gagal mencari rekaman: ${err.message}`, 'err'); }
      b.disabled = false;
    };
    $('#jp-del').onclick = () => confirmBox('Hapus jepretan?', 'Jepretan ini akan dihapus dari server.', async () => {
      try {
        await apiJson(`/api/jepretan/${encodeURIComponent(j.id)}`, { method: 'DELETE' });
        toast('Jepretan dihapus'); await refreshAiStatus();
        if (state.view === 'jepretan') loadJepretan(); else App.renderView?.();
      } catch (err) { toast(`Gagal menghapus: ${err.message}`, 'err'); }
    });
  }

  Object.assign(App, {
    clsPicker, bindClsPicker, jpCard, jpTile, bindJpCards,
    loadJepretan, renderJepretan, openJp,
  });
})();
