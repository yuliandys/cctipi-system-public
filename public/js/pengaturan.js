/* ==========================================================================
   CCTipi System — Fitur Kamera Saya (ONVIF/Tuya) & Pengaturan Sistem
   ========================================================================== */
(() => {
  'use strict';

  const App = window.CCTipi;
  const {
    $, $$, ic, esc, fmtTime, fmtBytes, isoDay, LS, K, state, els,
    saveFolders, savePresets, normalizeFolders, toast, apiJson, postJson,
    camKey, camType, camsIn, can, SET_TAB_PERM,
    loadCameras, refreshAiStatus, refreshRecStatus, refreshMonStatus,
    openModal, closeModal, confirmBox, promptBox,
  } = App;

  // --------------------------------------------------------- Kamera Saya
  let lastOnvif = null;
  async function renderKamera() {
    let manual = [], tuya = null;
    try { manual = (await apiJson('/api/manual-cameras')).cameras || []; } catch (err) { toast(`Gagal memuat kamera manual: ${err.message}`, 'err'); }
    if (can('settings.system')) {
      try { tuya = (await apiJson('/api/tuya/settings')).settings || null; } catch { /* abaikan */ }
    }
    if (state.view !== 'kamera') return;
    const aiSet = new Set(state.aiSettings?.cameras || []), recSet = new Set(state.recSettings?.continuous_cameras || []);
    const tuyaCams = state.cameras.filter((c) => c.provider === 'tuya');
    els.page.innerHTML = `<div class="page">
      <section class="card"><h3>${ic('plus')}Tambah kamera manual</h3><p class="card-sub">HLS (.m3u8), MJPEG, HTTP Snapshot, RTSP (jembatan ffmpeg → HLS), atau ONVIF (penemuan profil otomatis).</p>
        <div class="form-grid" style="margin-top:14px">
          <label class="source-field"><span>Nama kamera</span><input type="text" id="mc-name" placeholder="CCTV Gerbang Depan" maxlength="120"></label>
          <label class="source-field"><span>Folder / area</span><input type="text" id="mc-folder" value="Lokal" placeholder="Lokal" maxlength="60"></label>
          <label class="source-field"><span>Tipe stream</span><select id="mc-type"><option value="hls">HLS (.m3u8)</option><option value="mjpeg">MJPEG (HTTP Stream)</option><option value="http">HTTP Snapshot (auto-refresh 1 detik)</option><option value="rtsp">RTSP (bridge ffmpeg → HLS)</option><option value="onvif">ONVIF (auto-discover URI)</option></select></label>
          <label class="source-field"><span>URL / IP device</span><input type="text" id="mc-url" placeholder="https://stream.contoh.id/live.m3u8" autocomplete="off"></label>
          <label class="source-field"><span>Latitude (opsional)</span><input type="text" id="mc-lat" placeholder="-6.5886" inputmode="decimal"></label>
          <label class="source-field"><span>Longitude (opsional)</span><input type="text" id="mc-lng" placeholder="110.6684" inputmode="decimal"></label>
        </div>
        <div id="mc-onvif" class="hidden" style="margin-top:12px"><div class="form-grid"><label class="source-field"><span>Username ONVIF</span><input type="text" id="mc-ou" placeholder="admin (opsional)" autocomplete="off"></label><label class="source-field"><span>Password ONVIF</span><input type="password" id="mc-op" placeholder="opsional" autocomplete="off"></label></div>
          <div class="card-row" style="margin-top:10px"><button class="action-btn primary-btn" id="mc-discover">${ic('scan')}<span>Cek ONVIF</span></button><span class="small muted" id="mc-ostat"></span></div><div class="onvif-prof" id="mc-prof"></div></div>
        <div class="card-row" style="margin-top:12px"><label class="chk"><input type="checkbox" id="mc-ai"> Langsung opt-in AI Jepretan</label><label class="chk"><input type="checkbox" id="mc-rec"> Rekam server berkelanjutan</label><span style="flex:1"></span><button class="action-btn primary-btn" id="mc-save">${ic('plus')}<span>Tambah &amp; simpan</span></button></div>
      </section>
      <section class="card"><h3>${ic('video')}Kamera tersimpan <span class="folder-count">${manual.length}</span></h3><p class="card-sub">Ikut muncul di Beranda, Pantau, pemilih slot, dan peta (bila ada koordinat). Kamera ONVIF punya pad PTZ di tile.</p>
        <div class="manual-grid">${manual.map((c) => { const k = `m:${c.manualId}`; return `<div class="manual-card"><span class="manual-ico">${ic(c.onvif ? 'scan' : 'video')}</span><div class="grow"><strong>${esc(c.name)}</strong><div class="m">${esc(c.folder || 'Lokal')} · ${esc((c.type || 'hls').toUpperCase())}${c.onvif ? ' · ONVIF + PTZ' : ''}${typeof c.lat === 'number' ? ' · berkoordinat' : ''}</div><div class="m url">${esc(c.url || '')}</div>
          <div class="row-actions">${aiSet.has(k) ? '<span class="tile-ai-tag">AI</span>' : ''}${recSet.has(k) ? '<span class="tile-ai-tag rec">REC</span>' : ''}<button class="rec-mini-btn" data-play-key="${esc(k)}">${ic('play')}Putar</button><button class="rec-mini-btn" data-act="delete" data-mdel="${esc(c.manualId)}">${ic('trash')}Hapus</button></div></div></div>`; }).join('') || '<div class="empty-state">Belum ada kamera manual. Tambahkan lewat formulir di atas.</div>'}</div>
      </section>
      ${tuya ? `<section class="card">
        <div class="card-row" style="justify-content:space-between;flex-wrap:wrap;gap:8px">
          <div><h3>${ic('cloud')}Tuya IoT Cloud (Smart Life)</h3><p class="card-sub">Ambil kamera pintar Tuya lewat OpenAPI (<code>iot.tuya.com</code>) dan putar RTSP/HLS di Pantau.</p></div>
          <label class="switch"><input type="checkbox" id="ty-en"${tuya.enabled ? ' checked' : ''}><span class="track"></span><span>Aktifkan Tuya</span></label>
        </div>
        <div class="form-grid" style="margin-top:14px">
          <label class="source-field"><span>Region</span><select id="ty-reg">${(tuya.regions || []).map((r) => `<option value="${esc(r.id)}"${tuya.region === r.id ? ' selected' : ''}>${esc(r.label)}</option>`).join('')}</select></label>
          <label class="source-field"><span>Tipe stream</span><select id="ty-stype"><option value="rtsp"${tuya.stream_type === 'rtsp' ? ' selected' : ''}>RTSP (bridge ffmpeg → HLS)</option><option value="hls"${tuya.stream_type === 'hls' ? ' selected' : ''}>HLS langsung</option></select></label>
          <label class="source-field"><span>Access ID (Client ID)</span><input type="text" id="ty-aid" value="${esc(tuya.access_id || '')}" placeholder="xxxxxxxxxxxxxxxxxxxx" autocomplete="off"></label>
          <label class="source-field"><span>Access Secret</span><input type="password" id="ty-sec" value="" placeholder="${tuya.has_secret ? 'Tersimpan (kosongkan jika tetap)' : 'Masukkan Access Secret'}" autocomplete="off"></label>
          <label class="source-field"><span>User UID (Smart Life)</span><input type="text" id="ty-uid" value="${esc(tuya.uid || '')}" placeholder="az168... (opsional bila isi Device ID)" autocomplete="off"></label>
          <label class="source-field"><span>Device IDs (opsional, pisah koma)</span><input type="text" id="ty-devs" value="${esc(tuya.device_ids || '')}" placeholder="bf123..., bf456..." autocomplete="off"></label>
        </div>
        <div class="card-row" style="margin-top:12px;gap:10px;flex-wrap:wrap">
          <button class="action-btn primary-btn" id="ty-save">${ic('save')}<span>Simpan</span></button>
          <button class="action-btn" id="ty-test">${ic('rotate')}<span>Tes &amp; sinkronkan</span></button>
          <a class="action-btn" href="https://protect-us.ismartlife.me/" target="_blank" rel="noopener noreferrer">${ic('arrow-ur')}<span>Buka Tuya Web (HD)</span></a>
          <span class="small muted" id="ty-stat">${tuyaCams.length ? `${tuyaCams.length} kamera Tuya aktif di folder Tuya` : ''}</span>
        </div>
      </section>` : ''}</div>`;
    $('#mc-type').onchange = (e) => $('#mc-onvif').classList.toggle('hidden', e.target.value !== 'onvif');
    $('#mc-discover').onclick = async () => {
      const url = $('#mc-url').value.trim(), stat = $('#mc-ostat'), prof = $('#mc-prof');
      if (!url) { stat.textContent = 'Isi URL perangkat ONVIF dulu.'; return; }
      stat.textContent = 'Menghubungi perangkat ONVIF…'; prof.innerHTML = '';
      try {
        const data = await postJson('/api/onvif/discover', { url, username: $('#mc-ou').value.trim(), password: $('#mc-op').value });
        const profiles = data.profiles || [];
        stat.textContent = `${profiles.length} profil ditemukan. Pilih salah satu URI untuk dipakai.`;
        const rows = [];
        for (const p of profiles) {
          if (p.streamUri) rows.push({ p, uri: p.streamUri, kind: p.streamUri.startsWith('http') ? (p.streamUri.includes('.m3u8') ? 'hls' : 'mjpeg') : 'rtsp', label: p.streamUri.startsWith('http') ? 'Stream' : 'RTSP', ptz: true });
          if (p.snapshotUri) rows.push({ p, uri: p.snapshotUri, kind: 'http', label: 'HTTP Snapshot', ptz: false });
        }
        prof.innerHTML = rows.map((r, i) => `<label><input type="radio" name="mc-prof" value="${i}"><span><b>[${esc(r.p.name)}] ${esc(r.label)}</b><br><code>${esc(r.uri)}</code></span></label>`).join('');
        $$('input[name="mc-prof"]', prof).forEach((inp) => inp.onchange = () => {
          const r = rows[Number(inp.value)];
          $('#mc-type').value = r.kind; $('#mc-url').value = r.uri;
          lastOnvif = r.ptz ? { deviceUrl: data.mediaUrl || url, username: $('#mc-ou').value.trim(), password: $('#mc-op').value, profileToken: r.p.token } : null;
          stat.textContent = `Profil “${r.p.name}” dipilih${r.ptz ? ' — pad PTZ aktif di Pantau' : ''}. Beri nama lalu simpan.`;
        });
      } catch (err) { stat.textContent = `ONVIF gagal: ${err.message}`; }
    };
    $('#mc-save').onclick = async (e) => {
      const btn = e.currentTarget;
      const name = $('#mc-name').value.trim(), url = $('#mc-url').value.trim(), type = $('#mc-type').value;
      if (!name || !url) { toast('Nama kamera dan URL wajib diisi', 'err'); return; }
      if (type === 'onvif') { toast('Pilih dulu salah satu URI hasil penemuan ONVIF, atau ganti tipe stream', 'err'); return; }
      const body = { name, url, type, folder: $('#mc-folder').value.trim() || 'Lokal', onvif: lastOnvif };
      const lat = parseFloat($('#mc-lat').value), lng = parseFloat($('#mc-lng').value);
      if (Number.isFinite(lat) && Number.isFinite(lng)) { body.lat = lat; body.lng = lng; }
      btn.disabled = true;
      try {
        const res = await postJson('/api/manual-cameras', body);
        const key = `m:${res.manualId}`;
        lastOnvif = null;
        if ($('#mc-ai').checked) await addOptin('ai', key);
        if ($('#mc-rec').checked) await addOptin('rec', key);
        toast(`Kamera “${name}” tersimpan`);
        await loadCameras(); renderKamera();
      } catch (err) { toast(`Gagal menyimpan kamera: ${err.message}`, 'err'); btn.disabled = false; }
    };
    $$('[data-mdel]', els.page).forEach((b) => b.onclick = () => { const c = manual.find((x) => x.manualId === b.dataset.mdel); deleteManual(c); });
    if (tuya) {
      const readTuya = () => ({
        enabled: $('#ty-en').checked, region: $('#ty-reg').value, stream_type: $('#ty-stype').value,
        access_id: $('#ty-aid').value.trim(), access_secret: $('#ty-sec').value.trim(),
        uid: $('#ty-uid').value.trim(), device_ids: $('#ty-devs').value.trim(),
      });
      $('#ty-save').onclick = async (e) => {
        const b = e.currentTarget; b.disabled = true;
        try { await postJson('/api/tuya/settings', { settings: readTuya() }); toast('Pengaturan Tuya disimpan'); await loadCameras(); renderKamera(); }
        catch (err) { toast(`Gagal menyimpan Tuya: ${err.message}`, 'err'); b.disabled = false; }
      };
      $('#ty-test').onclick = async (e) => {
        const b = e.currentTarget, st = $('#ty-stat'); b.disabled = true; st.textContent = 'Menghubungi Tuya Cloud…';
        try {
          await postJson('/api/tuya/settings', { settings: readTuya() });
          const res = await postJson('/api/tuya/test', {});
          st.textContent = `Terhubung — ${res.count} kamera ditemukan: ${(res.devices || []).map((d) => d.name).join(', ') || '-'}`;
          toast(`Tuya terhubung · ${res.count} kamera`);
          await loadCameras();
        } catch (err) { st.textContent = `Gagal: ${err.message}`; toast(err.message, 'err'); }
        b.disabled = false;
      };
    }
  }

  function deleteManual(c) {
    if (!c) return;
    confirmBox('Hapus kamera manual?', `“${c.name}” akan dihapus dan izin proxy host-nya ikut dicabut.`, async () => {
      try { await apiJson(`/api/manual-cameras?manualId=${encodeURIComponent(c.manualId)}`, { method: 'DELETE' }); toast(`Kamera “${c.name}” dihapus`); await loadCameras(); App.renderView?.(); }
      catch (err) { toast(`Gagal menghapus: ${err.message}`, 'err'); }
    });
  }
  async function addOptin(kind, key) {
    try {
      if (kind === 'ai') {
        const s = (await apiJson('/api/ai/settings')).settings || {};
        s.cameras = [...new Set([...(s.cameras || []), key])];
        state.aiSettings = (await postJson('/api/ai/settings', { settings: s })).settings || s;
      } else {
        const s = (await apiJson('/api/rekaman/settings')).settings || {};
        s.continuous_cameras = [...new Set([...(s.continuous_cameras || []), key])];
        state.recSettings = (await postJson('/api/rekaman/settings', { settings: s })).settings || s;
      }
    } catch (err) { toast(`Opt-in ${kind === 'ai' ? 'AI' : 'rekaman'} gagal: ${err.message}`, 'err'); }
  }

  // ---------------------------------------------------------- Pengaturan
  function optinBox(id, title) {
    return `<div class="optin-box" id="${id}"><div class="optin-head"><span>${esc(title)} — <b data-cnt>0 dipilih</b></span><button class="rec-mini-btn" data-only-sel>Tampilkan terpilih</button></div>
      <div class="search-box"><svg class="ic"><use href="#i-search"/></svg><input type="search" placeholder="Cari kamera untuk daftar opt-in…" data-osearch></div><div class="optin-list" data-olist></div><p class="field-note" data-onote style="padding:0 12px 10px"></p></div>`;
  }
  function bindOptin(root, set) {
    const list = $('[data-olist]', root), q = $('[data-osearch]', root);
    let onlySel = false;
    const draw = () => {
      const s = q.value.trim().toLowerCase();
      const matches = state.cameras.filter((c) => camType(c) !== 'youtube' && (!s || `${c.name} ${c.folder}`.toLowerCase().includes(s)) && (!onlySel || set.has(camKey(c))));
      matches.sort((a, b) => set.has(camKey(b)) - set.has(camKey(a)));
      const shown = matches.slice(0, 150);
      list.innerHTML = shown.map((c) => { const k = camKey(c); return `<label class="optin-item"><input type="checkbox" value="${esc(k)}"${set.has(k) ? ' checked' : ''}><span class="grow">${esc(c.name)}</span><span class="provider-tag ${esc(c.provider || 'manual')}">${esc(c.folder || '')}</span></label>`; }).join('') || '<div class="empty-state">Tidak ada kamera.</div>';
      $('[data-cnt]', root).textContent = `${set.size} dipilih`;
      $('[data-onote]', root).textContent = matches.length > shown.length ? `Menampilkan ${shown.length} dari ${matches.length} — persempit dengan pencarian. Kamera yang dicentang tetap terpilih walau tidak tampil.` : `${matches.length} kamera cocok.`;
    };
    q.oninput = draw;
    $('[data-only-sel]', root).onclick = (e) => { onlySel = !onlySel; e.currentTarget.textContent = onlySel ? 'Tampilkan semua' : 'Tampilkan terpilih'; draw(); };
    list.onchange = (e) => { const k = e.target.value; if (e.target.checked) set.add(k); else set.delete(k); $('[data-cnt]', root).textContent = `${set.size} dipilih`; };
    draw();
  }
  const sw = (id, label, on, disabled = false) => `<label class="switch"><input type="checkbox" id="${id}"${on ? ' checked' : ''}${disabled ? ' disabled' : ''}><span class="track"></span>${label}</label>`;

  async function renderPengaturan() {
    const allTabs = [
      ['ai', 'scan', 'AI & Telegram'], ['rekaman', 'hdd', 'Rekaman server'], ['alarm', 'bell', 'Alarm'],
      ['sumber', 'folder', 'Sumber stream'], ['pengguna', 'users', 'Pengguna & role'],
      ['log', 'history', 'Log aktivitas'], ['akun', 'lock', 'Akun saya'], ['backup', 'save', 'Backup'],
    ].filter(([k]) => !SET_TAB_PERM[k] || can(SET_TAB_PERM[k]));
    const tabs = state.mustChange ? [['akun', 'lock', 'Akun saya (Wajib Ganti Password)']] : allTabs;
    if (!tabs.some(([k]) => k === state.pageTab.pengaturan)) state.pageTab.pengaturan = tabs[0][0];
    const tab = state.pageTab.pengaturan;
    els.page.innerHTML = `<div class="page"><div class="page-tabs" role="tablist">${tabs.map(([k, i, l]) => `<button class="page-tab${tab === k ? ' active' : ''}" data-ptab="${k}">${ic(i)}${l}</button>`).join('')}</div><div id="set-body"><div class="empty-state">Memuat…</div></div></div>`;
    $$('[data-ptab]', els.page).forEach((b) => b.onclick = () => { state.pageTab.pengaturan = b.dataset.ptab; renderPengaturan(); });
    const body = $('#set-body');
    if (tab === 'ai') await renderSetAi(body);
    else if (tab === 'rekaman') await renderSetRec(body);
    else if (tab === 'sumber') await renderSetSources(body);
    else if (tab === 'akun') renderSetAccount(body);
    else if (tab === 'pengguna') await renderSetUsers(body);
    else if (tab === 'alarm') await renderSetAlarm(body);
    else if (tab === 'log') await renderSetAudit(body);
    else renderSetBackup(body);
  }

  async function renderSetAi(body) {
    await refreshAiStatus();
    try { state.aiSettings = (await apiJson('/api/ai/settings')).settings || {}; } catch (err) { body.innerHTML = `<div class="empty-card">${ic('alert')}<b>Pengaturan AI tidak tersedia</b><span>${esc(err.message)}</span></div>`; return; }
    try { state.liveStatus = await apiJson('/api/live-ai/status'); } catch { state.liveStatus = null; }
    if (state.view !== 'pengaturan' || state.pageTab.pengaturan !== 'ai') return;
    const st = state.aiStatus || {}, s = state.aiSettings, model = st.model || {}, live = state.liveStatus || {};
    const optin = new Set(s.cameras || []);
    const pct = Math.round((s.threshold ?? 0.45) * 100);
    body.innerHTML = `<div class="page" style="animation:none">
      <section class="card"><div class="card-row" style="justify-content:space-between"><div><h3>${ic('scan')}AI Jepretan</h3><p class="card-sub">Deteksi objek lokal (YOLO11n ONNX, CPU). <b>Opt-in:</b> hanya kamera yang dicentang yang dipindai.</p></div>
        <div class="card-row"><span class="state-chip${st.running ? ' on' : ''}"><span class="ai-dot"></span>${!st.available ? 'Tidak tersedia' : st.running ? 'Berjalan' : 'Berhenti'}</span>${st.available ? `<button class="action-btn ${st.enabled ? 'danger-btn' : 'primary-btn'}" id="ai-toggle">${ic(st.enabled ? 'stop' : 'play')}<span>${st.enabled ? 'Berhenti' : 'Mulai'}</span></button>` : ''}<button class="action-btn" id="ai-refresh" title="Segarkan status">${ic('rotate')}</button></div></div>
        ${!st.available ? `<div class="empty-card" style="margin-top:14px">${ic('alert')}<b>AI tidak tersedia di server ini</b><span>${esc(st.unavailable_reason || '')}</span><span class="small">Fungsi lain tetap berjalan. Untuk AI: pasang dependensi di venv, unduh model resmi <code>python ai.py --download-model</code>, lalu jalankan <code>.venv/bin/python server.py</code>.</span></div>` : `
        <div class="ai-status"><div class="health-stat is-online"><strong>${st.monitored_cameras || 0}</strong><span>Kamera dipantau</span></div><div class="health-stat"><strong>${st.today_count || 0}</strong><span>Jepretan hari ini</span></div><div class="health-stat"><strong>${(st.cycles || 0).toLocaleString('id-ID')}</strong><span>Siklus pindai</span></div><div class="health-stat"><strong>${esc(model.name || '-')}</strong><span>${model.size_bytes ? fmtBytes(model.size_bytes) : 'model'}${model.loaded ? ' · dimuat' : ''}</span></div></div>
        ${st.last_error ? `<p class="field-note" style="color:var(--danger-ink)">Catatan terakhir: ${esc(st.last_error)}</p>` : ''}`}
        <div class="form-grid" style="margin-top:16px">
          <label class="source-field"><span>Ambang confidence: <b id="th-val">${pct}%</b></span><input type="range" id="ai-th" min="5" max="95" value="${pct}"></label>
          <label class="source-field"><span>Interval pindai (detik)</span><input type="number" id="ai-int" min="3" max="3600" value="${esc(s.interval ?? 10)}"></label>
          <label class="source-field"><span>Cooldown per kamera (detik)</span><input type="number" id="ai-cd" min="0" max="86400" value="${esc(s.cooldown ?? 60)}"></label>
          <label class="source-field"><span>Retensi jepretan (hari)</span><input type="number" id="ai-ret" min="1" max="365" value="${esc(s.retention_days ?? 7)}"></label>
        </div>
        <div class="cls-block"><div class="cls-head"><span class="small" style="font-weight:700;color:var(--ink-2)">Kelas yang dideteksi</span><span class="field-note" style="margin:0">Centang kategori, lalu pilih objek yang mau dijepret.</span></div>${App.clsPicker(s)}</div>
        <div style="margin-top:8px">${sw('ai-enh', `${ic('moon', 'ic-sm')} Perjelas frame gelap sebelum deteksi`, s.enhance_before_detect)}<p class="field-note">CLAHE + gamma adaptif. Jepretan yang disimpan tetap frame asli; bandingkan lewat tombol Asli | Diperjelas di galeri.</p></div>
        <div style="margin-top:14px">${optinBox('ai-optin', 'Kamera yang dipantau AI (opt-in)')}</div>
      </section>
      ${zoneCard(s)}
      <section class="card"><h3>${ic('send')}Notifikasi Telegram</h3><p class="card-sub">Jepretan worker &amp; Live AI dikirim ke bot milik sendiri (buat lewat @BotFather). Token tidak pernah ditampilkan kembali. Env <code>CCTIPI_TG_BOT_TOKEN</code> / <code>CCTIPI_TG_CHAT_ID</code> didahulukan.</p>
        <div style="margin-top:10px">${sw('tg-on', 'Kirim jepretan ke Telegram', s.telegram_enabled)}</div>
        <div class="form-grid" style="margin-top:10px"><label class="source-field"><span>Chat ID</span><input type="text" id="tg-chat" value="${esc(s.telegram_chat_id || '')}" placeholder="mis. 123456789" autocomplete="off"></label><label class="source-field"><span>Token bot (rahasia)</span><input type="password" id="tg-token" placeholder="${s.telegram_token_set ? 'Tersimpan · kosongkan bila tidak diganti' : 'Token dari @BotFather'}" autocomplete="off"></label></div>
        <div class="card-row" style="margin-top:12px"><span class="state-chip${st.telegram_configured ? ' on' : ''}"><span class="ai-dot"></span>${st.telegram_configured ? 'Terkonfigurasi' + (s.telegram_env ? ' (env)' : '') : 'Belum dikonfigurasi'}</span><span style="flex:1"></span><button class="action-btn primary-btn" id="ai-save">${ic('save')}<span>Simpan pengaturan AI</span></button></div>
      </section>
      <section class="card"><h3>${ic('activity')}Live AI realtime (pilot)</h3><p class="card-sub">Dinyalakan dari tombol <b>Live AI</b> di tile Pantau. Maks 1 kamera · berhenti sendiri setelah ${Math.round((live.auto_stop_seconds || 600) / 60)} menit, ${live.no_viewer_seconds || 45} detik tanpa penonton, atau stream putus. Jepretan Live AI memakai opt-in, ambang, dan cooldown yang sama dengan worker.</p>
        <div class="card-row" style="margin-top:10px"><span class="state-chip${live.active ? ' on' : ''}"><span class="ai-dot"></span>${live.available === false ? 'Tidak tersedia' : live.active ? 'Aktif · ' + esc(live.camera_name || '') : 'Tidak aktif'}</span>${live.active ? '<button class="action-btn danger-btn" id="la-stop">Stop Live AI</button>' : '<button class="action-btn" data-goto="pantau">Buka Pantau</button>'}${live.ended_reason && !live.active ? `<span class="small muted">Terakhir berhenti: ${esc(live.ended_reason)}</span>` : ''}</div></section>
    </div>`;
    bindOptin($('#ai-optin'), optin);
    App.bindClsPicker(body);
    bindZoneCard(body);
    $('#ai-th').oninput = (e) => { $('#th-val').textContent = `${e.target.value}%`; };
    $('#ai-refresh').onclick = () => renderPengaturan();
    const collect = () => {
      const out = {
        ...s,
        interval: Number($('#ai-int').value) || 10, threshold: (Number($('#ai-th').value) || 45) / 100,
        cooldown: Number($('#ai-cd').value) || 0, retention_days: Number($('#ai-ret').value) || 7,
        classes: Object.fromEntries($$('[data-cls]', body).map((c) => [c.dataset.cls, c.checked])),
        objects: Object.fromEntries($$('[data-obj]', body).map((o) => [o.dataset.obj, o.checked])),
        enhance_before_detect: $('#ai-enh').checked, cameras: [...optin],
        telegram_enabled: $('#tg-on').checked, telegram_chat_id: $('#tg-chat').value.trim(),
      };
      delete out.telegram_bot_token; delete out.telegram_token_set; delete out.telegram_configured; delete out.catalog;
      const tok = $('#tg-token').value.trim(); if (tok) out.telegram_bot_token = tok;
      return out;
    };
    const save = async (extra = {}, msg = 'Pengaturan AI disimpan') => {
      try { state.aiSettings = (await postJson('/api/ai/settings', { settings: { ...collect(), ...extra } })).settings; await refreshAiStatus(); toast(msg); renderPengaturan(); }
      catch (err) { toast(`Gagal menyimpan pengaturan AI: ${err.message}`, 'err'); }
    };
    $('#ai-save').onclick = () => save();
    const tg = $('#ai-toggle'); if (tg) tg.onclick = () => save({ enabled: !st.enabled }, st.enabled ? 'Worker AI dihentikan' : 'Worker AI dimulai (hanya kamera opt-in)');
    const ls = $('#la-stop'); if (ls) ls.onclick = async () => { try { await postJson(`/api/live-ai/${encodeURIComponent(live.camera_key)}/stop`); } catch { /* abaikan */ } state.liveAi = null; toast('Live AI dihentikan'); renderPengaturan(); };
  }

  async function renderSetRec(body) {
    await refreshRecStatus();
    try { state.recSettings = (await apiJson('/api/rekaman/settings')).settings || {}; } catch (err) { body.innerHTML = `<div class="empty-card">${ic('alert')}<b>Pengaturan rekaman tidak tersedia</b><span>${esc(err.message)}</span></div>`; return; }
    if (state.view !== 'pengaturan' || state.pageTab.pengaturan !== 'rekaman') return;
    const rs = state.recStatus || {}, s = state.recSettings;
    const optin = new Set(s.continuous_cameras || []);
    const quotaGb = s.quota_gb ?? 20;
    body.innerHTML = `<section class="card"><div class="card-row" style="justify-content:space-between"><div><h3>${ic('hdd')}Rekaman server</h3><p class="card-sub">Perekaman MP4 di server (ffmpeg <code>-c copy</code>). Opt-in seperti AI; YouTube tidak bisa direkam.</p></div>${sw('rec-on', 'Saklar utama', s.record_enabled, rs.available === false)}</div>
      ${rs.available === false ? `<div class="empty-card" style="margin-top:14px">${ic('alert')}<b>Rekaman Server tidak tersedia</b><span>${esc(rs.unavailable_reason || 'ffmpeg tidak ditemukan di server.')}</span></div>` : `
      <div class="ai-status"><div class="health-stat${rs.enabled ? ' is-online' : ''}"><strong>${(rs.recording || []).length}</strong><span>Sedang merekam</span></div><div class="health-stat"><strong>${rs.recordings_count || 0}</strong><span>File rekaman</span></div><div class="health-stat"><strong>${fmtBytes(rs.total_size_bytes || 0)}</strong><span>Terpakai</span></div><div class="health-stat"><strong>${fmtBytes(rs.quota_bytes || 0)}</strong><span>Kuota</span></div></div>
      ${rs.last_error ? `<p class="field-note" style="color:var(--danger-ink)">Catatan terakhir: ${esc(rs.last_error)}</p>` : ''}`}
      <div class="form-grid" style="margin-top:16px">
        <label class="source-field"><span>Durasi segmen berkelanjutan (menit)</span><input type="number" id="rs-seg" min="1" max="60" value="${esc(s.segment_minutes ?? 5)}"></label>
        <label class="source-field"><span>Durasi klip deteksi (detik)</span><input type="number" id="rs-clip" min="5" max="600" value="${esc(s.clip_seconds ?? 30)}"></label>
        <label class="source-field"><span>Retensi rekaman (hari)</span><input type="number" id="rs-ret" min="1" max="365" value="${esc(s.retention_days ?? 7)}"></label>
        <label class="source-field"><span>Kuota folder rekaman (GB)</span><input type="number" id="rs-quota" min="1" max="10000" value="${esc(quotaGb)}"></label>
      </div>
      <div style="margin-top:10px">${sw('rs-clipon', 'Rekam klip saat AI mendeteksi (butuh AI aktif + kamera opt-in AI)', s.clip_on_detect !== false)}</div>
      <p class="field-note">Terukur ±180–240 MB/jam per kamera 640×360 → kuota ${esc(quotaGb)} GB ≈ ${Math.round((quotaGb * 1024) / 210)} jam rekaman satu kamera.</p>
      <div style="margin-top:14px">${optinBox('rec-optin', 'Kamera direkam berkelanjutan (opt-in)')}</div>
      <div class="card-row end" style="margin-top:14px"><button class="action-btn" data-goto="rekaman">${ic('film')}<span>Lihat rekaman</span></button><button class="action-btn primary-btn" id="rs-save">${ic('save')}<span>Simpan pengaturan rekaman</span></button></div></section>`;
    bindOptin($('#rec-optin'), optin);
    $('#rs-save').onclick = async () => {
      const out = { ...s, record_enabled: $('#rec-on').checked, continuous_cameras: [...optin], segment_minutes: Number($('#rs-seg').value) || 5, clip_on_detect: $('#rs-clipon').checked, clip_seconds: Number($('#rs-clip').value) || 30, retention_days: Number($('#rs-ret').value) || 7, quota_gb: Number($('#rs-quota').value) || 20 };
      try { state.recSettings = (await postJson('/api/rekaman/settings', { settings: out })).settings; toast('Pengaturan rekaman disimpan'); renderPengaturan(); }
      catch (err) { toast(`Gagal menyimpan pengaturan rekaman: ${err.message}`, 'err'); }
    };
  }

  async function renderSetSources(body) {
    try { state.sources = (await apiJson('/api/sources')).sources || []; } catch (err) { body.innerHTML = `<div class="empty-card">${ic('alert')}<b>Sumber tidak dapat dimuat</b><span>${esc(err.message)}</span></div>`; return; }
    const draw = () => {
      body.innerHTML = `<section class="card"><div class="card-row" style="justify-content:space-between"><div><h3>${ic('folder')}Sumber stream daerah</h3><p class="card-sub">Dibaca server lewat <code>/api/sources</code>. Menyimpan memicu sinkron ulang &amp; menghitung ulang izin host proxy.</p></div>
        <div class="card-row"><button class="action-btn" id="src-add">${ic('plus')}<span>Tambah</span></button><button class="action-btn danger-btn" id="src-reset">Reset default</button><button class="action-btn primary-btn" id="src-save">Simpan &amp; Sync</button></div></div>
        <div class="src-row-grid" style="margin-top:14px">${state.sources.map((src, i) => `<div class="source-card" data-i="${i}"><div class="source-card-top"><span class="source-card-title">${ic('folder')}${esc(src.title || src.folder || 'Sumber baru')}</span><div class="source-card-tools"><span class="folder-count">${camsIn(src.folder).length} kamera</span>
          <button class="folder-act-btn folder-move-btn" data-mv="-1" title="Naikkan" aria-label="Naikkan">${ic('chev-up')}</button><button class="folder-act-btn folder-move-btn" data-mv="1" title="Turunkan" aria-label="Turunkan">${ic('chev-down')}</button><button class="folder-act-btn del" data-del title="Hapus sumber" aria-label="Hapus sumber">${ic('trash')}</button></div></div>
          <div class="source-card-fields">${[['title', 'Nama daerah', 'Kabupaten Kendal'], ['folder', 'Nama folder', 'Kendal'], ['portalUrl', 'Sumber web', 'https://'], ['apiUrl', 'Endpoint / API', 'https://'], ['streamHosts', 'Host stream (pisahkan koma)', 'stream.contoh.go.id'], ['format', 'Format', 'HLS (.m3u8)']].map(([k, l, ph]) => `<label class="source-field"><span>${l}</span><input type="text" data-f="${k}" value="${esc(src[k] || '')}" placeholder="${esc(ph)}"></label>`).join('')}</div></div>`).join('')}</div></section>`;
      $$('.source-card input', body).forEach((inp) => inp.oninput = () => { const i = Number(inp.closest('.source-card').dataset.i); state.sources[i][inp.dataset.f] = inp.value; });
      $$('[data-mv]', body).forEach((b) => b.onclick = () => { const i = Number(b.closest('.source-card').dataset.i), j = i + Number(b.dataset.mv); if (j < 0 || j >= state.sources.length) return; [state.sources[i], state.sources[j]] = [state.sources[j], state.sources[i]]; draw(); });
      $$('[data-del]', body).forEach((b) => b.onclick = () => { state.sources.splice(Number(b.closest('.source-card').dataset.i), 1); draw(); });
      $('#src-add').onclick = () => { state.sources.push({ title: '', folder: '', portalUrl: '', apiUrl: '', streamHosts: '', format: 'HLS (.m3u8)' }); draw(); };
      $('#src-save').onclick = async (e) => { const b = e.currentTarget; b.disabled = true; try { state.sources = (await postJson('/api/sources', { sources: state.sources })).sources || state.sources; toast('Sumber disimpan · daftar kamera disinkronkan ulang'); await loadCameras(true); draw(); } catch (err) { toast(`Gagal menyimpan sumber: ${err.message}`, 'err'); } b.disabled = false; };
      $('#src-reset').onclick = () => confirmBox('Reset sumber?', 'Sumber kustom akan hilang dan kembali ke bawaan.', async () => { try { state.sources = (await postJson('/api/sources/reset')).sources || []; toast('Sumber dikembalikan ke bawaan'); await loadCameras(true); draw(); } catch (err) { toast(`Reset gagal: ${err.message}`, 'err'); } });
    };
    draw();
  }

  function renderSetAccount(body) {
    body.innerHTML = `<div class="page" style="animation:none"><section class="card"><h3>${ic('lock')}Akun login</h3><p class="card-sub">${state.mustChange ? '<b>Wajib ganti password terlebih dahulu:</b> demi keamanan, semua fitur operasional dikunci selama akun masih memakai password bawaan atau password awal dari admin. ' : ''}Wajib isi password saat ini. Password baru minimal ${state.minPass} karakter dan bukan password bawaan. Setelah disimpan, semua perangkat lain otomatis keluar.</p>
      <form class="form-grid" style="margin-top:14px" id="acc-form"><label class="source-field"><span>Username</span><input type="text" id="acc-u" value="${esc(state.user || '')}" autocomplete="username" maxlength="64" required></label><label class="source-field"><span>Password saat ini</span><input type="password" id="acc-c" autocomplete="current-password" required></label><label class="source-field"><span>Password baru</span><input type="password" id="acc-p" autocomplete="new-password" minlength="${state.minPass}" placeholder="Minimal ${state.minPass} karakter" required></label><label class="source-field"><span>Ulangi password baru</span><input type="password" id="acc-p2" autocomplete="new-password" required></label>
      <div class="card-row end" style="grid-column:1/-1"><button type="submit" class="action-btn primary-btn" id="acc-save">Simpan akun</button></div></form></section>
      <section class="card"><h3>${ic('shield')}Keamanan</h3><div class="status-list">
        ${App.statusRow('users', `Role: ${state.roleName || '-'}`, state.regions.length ? `Wilayah: ${state.regions.join(', ')}` : 'Semua wilayah kamera', true, 'pengaturan', 'akun')}
        ${App.statusRow('lock', 'Sesi sisi-server', 'Token acak 256-bit, berlaku 7 hari · Keluar mencabut sesi', true, 'pengaturan', 'akun')}
        ${App.statusRow('alert', 'Pembatas tebak-password', '5× gagal / 15 menit → kunci 30 dtk, berlipat sampai 15 menit', true, 'pengaturan', 'akun')}</div></section></div>`;
    $('#acc-form').onsubmit = async (e) => {
      e.preventDefault();
      const username = $('#acc-u').value.trim(), currentPassword = $('#acc-c').value, password = $('#acc-p').value;
      if (!username) return toast('Username wajib diisi', 'err');
      if (password.length < state.minPass) return toast(`Password baru minimal ${state.minPass} karakter`, 'err');
      if (password !== $('#acc-p2').value) return toast('Ulangi password tidak sama', 'err');
      try {
        const wasLocked = state.mustChange;
        const data = await postJson('/api/auth/password', { username, password, currentPassword });
        state.mustChange = false;
        $('#default-pass-banner').classList.add('hidden');
        $('[data-dismiss="default-pass-banner"]')?.classList.remove('hidden');
        state.user = data.username || username;
        $('#user-name').textContent = state.user; $('#user-initial').textContent = state.user.charAt(0).toUpperCase();
        $('#acc-c').value = ''; $('#acc-p').value = ''; $('#acc-p2').value = '';
        toast('Akun diperbarui · sesi perangkat lain dicabut');
        if (wasLocked) {
          await App.loadInitialOperationalData?.();
          App.go?.('home');
        }
      } catch (err) { toast(`Gagal mengubah akun: ${err.message}`, 'err'); }
    };
  }

  // ---------------------------------------------- Zona deteksi & jadwal
  const DAYS = ['Sen', 'Sel', 'Rab', 'Kam', 'Jum', 'Sab', 'Min'];
  function zoneSummary(z) {
    if (!z) return 'Seluruh gambar · 24 jam';
    const n = (z.polygons || []).length, sc = z.schedule || {};
    const days = sc.days && sc.days.length < 7 ? ` · ${sc.days.map((d) => DAYS[d]).join(', ')}` : '';
    return `${n ? `${n} zona` : 'Seluruh gambar'} · ${sc.enabled ? `${sc.start}–${sc.end}${days}` : '24 jam'}`;
  }
  function zoneCard(s) {
    const keys = (s.cameras || []).filter((k) => state.byKey.has(k));
    const zones = s.zones || {};
    return `<section class="card" id="zone-card"><div class="card-head"><h3>${ic('layout')}Zona &amp; jadwal deteksi</h3><span class="head-chip${Object.keys(zones).length ? ' on' : ''}"><b>${Object.keys(zones).length}</b> kamera diatur</span></div>
      <p class="card-sub">Gambar area yang dijaga (mis. pintu gudang, pagar belakang) dan jam aktifnya. Jepretan &amp; Telegram hanya untuk objek yang <b>titik kakinya</b> masuk zona, pada jam aktif. Tanpa zona = seluruh gambar.</p>
      ${keys.length ? `<div class="zone-list">${keys.map((k, i) => { const c = state.byKey.get(k), z = zones[k]; return `<div class="status-row" style="--i:${i}"><span class="stat-ico${z ? '' : ' navy'}">${ic(z ? 'layout' : 'video')}</span><div class="grow"><strong>${esc(c.name)}</strong><span class="sub">${esc(c.folder || '')} · ${esc(zoneSummary(z))}</span></div><button class="action-btn" data-zone="${esc(k)}">${ic('pencil')}<span>Atur</span></button></div>`; }).join('')}</div>`
        : `<div class="empty-state" style="margin-top:10px">Belum ada kamera opt-in AI. Centang kamera di daftar opt-in di atas, simpan, lalu atur zonanya di sini.</div>`}</section>`;
  }
  function bindZoneCard(body) { $$('[data-zone]', body).forEach((b) => b.onclick = () => openZoneEditor(b.dataset.zone)); }
  function openZoneEditor(key) {
    const cam = state.byKey.get(key); if (!cam) return;
    const z = (state.aiSettings?.zones || {})[key] || null;
    const polys = (z?.polygons || []).map((p) => p.map((pt) => pt.slice()));
    let cur = [];
    const sc = z?.schedule || { enabled: false, start: '18:00', end: '06:00', days: [0, 1, 2, 3, 4, 5, 6] };
    openModal(`<span class="sheet-handle" aria-hidden="true"></span>
      <div class="modal-header"><div class="modal-heading"><h2 id="modal-title">Zona &amp; jadwal · ${esc(cam.name)}</h2><p class="modal-subtitle">Klik di gambar untuk menaruh titik. Klik titik pertama (atau “Tutup zona”) untuk menyelesaikan. Maks. 8 zona.</p></div><button class="icon-btn close-btn" data-close-modal aria-label="Tutup">${ic('x')}</button></div>
      <div class="zone-body">
        <div class="zone-stage" id="zn-stage"><img id="zn-img" alt="Gambar kamera ${esc(cam.name)}"><canvas id="zn-canvas"></canvas><div class="zone-loading" id="zn-load">${ic('loader', 'spin')}<span>Mengambil gambar kamera…</span></div></div>
        <div class="card-row" style="flex-wrap:wrap;gap:8px;margin-top:10px"><button class="action-btn" id="zn-close">${ic('check')}<span>Tutup zona</span></button><button class="action-btn" id="zn-undo">${ic('rotate')}<span>Batalkan titik</span></button><button class="action-btn danger-btn" id="zn-clear">${ic('trash')}<span>Hapus semua zona</span></button><span class="field-note grow" id="zn-info" style="margin:0"></span></div>
        <div class="zone-sched">${sw('zn-on', 'Aktif hanya pada jam tertentu', sc.enabled)}
          <div class="form-grid zone-times${sc.enabled ? '' : ' is-off'}"><label class="source-field"><span>Mulai</span><input type="time" id="zn-start" value="${esc(sc.start)}"></label><label class="source-field"><span>Selesai</span><input type="time" id="zn-end" value="${esc(sc.end)}"></label></div>
          <div class="cls-objs zone-days${sc.enabled ? '' : ' is-off'}">${DAYS.map((d, i) => `<label class="obj-chip"><input type="checkbox" data-day="${i}"${(sc.days || []).includes(i) ? ' checked' : ''}><span>${d}</span></label>`).join('')}</div>
          <p class="field-note">Jam melewati tengah malam didukung (mis. 18:00–06:00); hari mengikuti jam mulai. Waktu mengikuti jam server.</p></div>
      </div>
      <div class="modal-footer"><button class="action-btn" data-close-modal>Batal</button><button class="action-btn primary-btn" id="zn-save">${ic('save')}<span>Simpan zona</span></button></div>`, 'zone-dialog');
    els.dialog.style.width = 'min(860px,100%)';
    const img = $('#zn-img'), cv = $('#zn-canvas'), ctx = cv.getContext('2d');
    const fit = () => { const r = img.getBoundingClientRect(); cv.width = Math.round(r.width * devicePixelRatio); cv.height = Math.round(r.height * devicePixelRatio); draw(); };
    const P = (pt) => [pt[0] * cv.width, pt[1] * cv.height];
    function draw() {
      ctx.clearRect(0, 0, cv.width, cv.height);
      const lw = 2 * devicePixelRatio;
      for (const poly of polys) {
        ctx.beginPath(); poly.forEach((pt, i) => { const [x, y] = P(pt); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); }); ctx.closePath();
        ctx.fillStyle = 'rgba(255,196,0,.22)'; ctx.fill(); ctx.lineWidth = lw; ctx.strokeStyle = '#ffc400'; ctx.stroke();
      }
      if (cur.length) {
        ctx.beginPath(); cur.forEach((pt, i) => { const [x, y] = P(pt); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
        ctx.lineWidth = lw; ctx.setLineDash([6 * devicePixelRatio, 5 * devicePixelRatio]); ctx.strokeStyle = '#fff'; ctx.stroke(); ctx.setLineDash([]);
        cur.forEach((pt, i) => { const [x, y] = P(pt); ctx.beginPath(); ctx.arc(x, y, (i ? 4 : 7) * devicePixelRatio, 0, Math.PI * 2); ctx.fillStyle = i ? '#fff' : '#ffc400'; ctx.fill(); });
      }
      $('#zn-info').textContent = `${polys.length} zona${cur.length ? ` · sedang menggambar (${cur.length} titik)` : ''}${polys.length ? '' : ' · tanpa zona = seluruh gambar'}`;
    }
    const closePoly = () => { if (cur.length >= 3) { polys.push(cur); cur = []; draw(); } else if (cur.length) toast('Zona butuh minimal 3 titik', 'err'); };
    cv.addEventListener('pointerdown', (e) => {
      const r = cv.getBoundingClientRect();
      const pt = [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
      if (cur.length >= 3) { const [fx, fy] = cur[0]; if (Math.hypot((pt[0] - fx) * r.width, (pt[1] - fy) * r.height) < 14) { closePoly(); return; } }
      if (polys.length >= 8) { toast('Maksimal 8 zona per kamera', 'err'); return; }
      if (cur.length >= 40) { toast('Maksimal 40 titik per zona', 'err'); return; }
      cur.push(pt); draw();
    });
    $('#zn-close').onclick = closePoly;
    $('#zn-undo').onclick = () => { if (cur.length) cur.pop(); else polys.pop(); draw(); };
    $('#zn-clear').onclick = () => { polys.length = 0; cur = []; draw(); };
    $('#zn-on').onchange = (e) => $$('.zone-times, .zone-days', els.dialog).forEach((x) => x.classList.toggle('is-off', !e.target.checked));
    img.onload = () => { $('#zn-load').remove(); fit(); };
    img.onerror = () => { $('#zn-load').innerHTML = `${ic('alert')}<span>Gambar kamera tidak bisa diambil saat ini — zona tetap bisa digambar.</span>`; $('#zn-stage').classList.add('no-img'); fit(); };
    img.src = `/api/ai/snapshot?key=${encodeURIComponent(key)}&t=${Date.now()}`;
    const ro = new ResizeObserver(fit); ro.observe($('#zn-stage'));
    els.modal._onClose = () => ro.disconnect();
    $('#zn-save').onclick = async () => {
      if (cur.length >= 3) closePoly();
      const days = $$('[data-day]', els.dialog).filter((c) => c.checked).map((c) => Number(c.dataset.day));
      if ($('#zn-on').checked && !days.length) return toast('Pilih minimal satu hari', 'err');
      const payload = { polygons: polys, schedule: { enabled: $('#zn-on').checked, start: $('#zn-start').value || '18:00', end: $('#zn-end').value || '06:00', days } };
      try {
        const res = await postJson(`/api/ai/zones/${encodeURIComponent(key)}`, payload);
        state.aiSettings = { ...(state.aiSettings || {}), zones: { ...((state.aiSettings || {}).zones || {}) } };
        if (res.zone) state.aiSettings.zones[key] = res.zone; else delete state.aiSettings.zones[key];
        toast(res.zone ? `Zona ${cam.name} disimpan · ${zoneSummary(res.zone)}` : `Zona ${cam.name} dihapus · seluruh gambar, 24 jam`);
        closeModal();
        const card = $('#zone-card'); if (card) { card.outerHTML = zoneCard(state.aiSettings); bindZoneCard($('#set-body')); }
      } catch (err) { toast(`Gagal menyimpan zona: ${err.message}`, 'err'); }
    };
  }

  // ---------------------------------------------- Alarm kamera & disk
  async function renderSetAlarm(body) {
    let s;
    try { [s] = await Promise.all([apiJson('/api/monitor/settings').then((d) => d.settings), refreshMonStatus()]); }
    catch (err) { body.innerHTML = `<div class="empty-card">${ic('alert')}<b>Pengaturan alarm tidak tersedia</b><span>${esc(err.message)}</span></div>`; return; }
    if (state.view !== 'pengaturan' || state.pageTab.pengaturan !== 'alarm') return;
    const st = state.monStatus || {}, disk = st.disk || {}, ai = state.aiStatus || {};
    const optin = new Set(s.cameras || []);
    const bar = (pct, label, sub) => `<div class="disk-row"><div class="card-row" style="justify-content:space-between"><b>${esc(label)}</b><span class="small">${esc(sub)}</span></div><div class="disk-bar${pct >= s.disk_alert_percent ? ' warn' : ''}"><span style="width:${Math.min(100, pct || 0)}%"></span><i style="left:${s.disk_alert_percent}%"></i></div></div>`;
    const rows = (st.cameras || []).filter((c) => c.status !== 'online');
    body.innerHTML = `<div class="page" style="animation:none"><section class="card"><div class="card-row" style="justify-content:space-between"><div><h3>${ic('bell')}Alarm kamera &amp; penyimpanan</h3><p class="card-sub">Server mengecek kamera terpilih secara berkala. Bila tidak merespons terus-menerus, kirim Telegram sekali — lalu sekali lagi saat kembali online. Juga memantau disk server &amp; kuota rekaman.</p></div>${sw('mn-on', 'Saklar utama', s.enabled)}</div>
      <div class="ai-status"><div class="health-stat${st.running ? ' is-online' : ''}"><strong>${st.monitored || 0}</strong><span>Kamera dipantau</span></div><div class="health-stat${st.offline ? ' is-offline' : ''}"><strong>${st.offline || 0}</strong><span>Offline sekarang</span></div><div class="health-stat"><strong>${disk.disk_percent != null ? disk.disk_percent + '%' : '–'}</strong><span>Disk server</span></div><div class="health-stat"><strong>${disk.quota_percent ?? '–'}${disk.quota_percent != null ? '%' : ''}</strong><span>Kuota rekaman</span></div></div>
      ${disk.disk_total ? bar(disk.disk_percent, 'Disk server', `${fmtBytes(disk.disk_used)} / ${fmtBytes(disk.disk_total)} · sisa ${fmtBytes(disk.disk_free)}`) : ''}
      ${disk.quota_bytes ? bar(disk.quota_percent, 'Kuota rekaman', `${fmtBytes(disk.quota_used)} / ${fmtBytes(disk.quota_bytes)}`) : ''}
      ${rows.length ? `<div class="status-list" style="margin-top:12px">${rows.map((c) => `<div class="status-row"><span class="stat-ico danger">${ic('alert')}</span><div class="grow"><strong>${esc(c.name)}</strong><span class="sub">${esc(c.folder)} · tidak merespons sejak ${fmtTime(new Date(c.fail_since * 1000)).slice(0, 5)}</span></div><span class="state-chip${c.status === 'offline' ? ' bad' : ' warn'}">${c.status === 'offline' ? 'Offline' : 'Gangguan'}</span></div>`).join('')}</div>` : st.last_run ? `<p class="field-note">${ic('check', 'ic-sm')} Semua kamera terpantau merespons · cek terakhir ${fmtTime(new Date(st.last_run * 1000)).slice(0, 5)}</p>` : ''}
      ${st.last_error ? `<p class="field-note" style="color:var(--danger-ink)">Catatan terakhir: ${esc(st.last_error)}</p>` : ''}
      <div class="form-grid" style="margin-top:16px">
        <label class="source-field"><span>Cek setiap (detik)</span><input type="number" id="mn-int" min="30" max="3600" value="${esc(s.interval)}"></label>
        <label class="source-field"><span>Dianggap offline setelah (menit)</span><input type="number" id="mn-off" min="1" max="1440" value="${esc(s.offline_minutes)}"></label>
        <label class="source-field"><span>Alarm penyimpanan pada (%)</span><input type="number" id="mn-disk" min="50" max="99" value="${esc(s.disk_alert_percent)}"></label>
      </div>
      <div style="margin-top:10px;display:grid;gap:6px">${sw('mn-tg', `Kirim ke Telegram${ai.telegram_configured ? '' : ' (Telegram belum dikonfigurasi di tab AI &amp; Telegram)'}`, s.telegram_enabled)}${sw('mn-rec', 'Kirim juga saat kamera kembali online', s.notify_recovery)}</div>
      <p class="field-note">Kamera RTSP dicek dengan membuka port-nya (ringan, tanpa ffmpeg); HLS/MJPEG dengan membaca awal stream. Semua alarm juga tercatat di Log aktivitas.</p>
      <div style="margin-top:14px">${optinBox('mn-optin', 'Kamera yang dipantau alarm')}</div>
      <div class="card-row end" style="margin-top:14px"><button class="action-btn primary-btn" id="mn-save">${ic('save')}<span>Simpan pengaturan alarm</span></button></div></section></div>`;
    bindOptin($('#mn-optin'), optin);
    $('#mn-save').onclick = async () => {
      const out = { enabled: $('#mn-on').checked, cameras: [...optin], interval: Number($('#mn-int').value) || 120, offline_minutes: Number($('#mn-off').value) || 5, disk_alert_percent: Number($('#mn-disk').value) || 90, telegram_enabled: $('#mn-tg').checked, notify_recovery: $('#mn-rec').checked };
      try { await postJson('/api/monitor/settings', { settings: out }); toast(out.enabled ? `Alarm aktif · ${out.cameras.length} kamera dipantau` : 'Pengaturan alarm disimpan (alarm mati)'); setTimeout(() => { if (state.pageTab.pengaturan === 'alarm') renderPengaturan(); }, 400); }
      catch (err) { toast(`Gagal menyimpan alarm: ${err.message}`, 'err'); }
    };
  }

  // ---------------------------------------------- Log aktivitas
  async function renderSetAudit(body) {
    const f = state.auditFilter || (state.auditFilter = { date: '', user: '', action: '', q: '' });
    let data;
    try { data = await apiJson(`/api/audit?${new URLSearchParams({ ...f, limit: 500 })}`); }
    catch (err) { body.innerHTML = `<div class="empty-card">${ic('alert')}<b>Log aktivitas tidak dapat dimuat</b><span>${esc(err.message)}</span></div>`; return; }
    const groups = [['auth.', 'Login & akun'], ['user.', 'Pengguna'], ['role.', 'Role & izin'], ['jepretan.', 'Jepretan'], ['rekaman.', 'Rekaman'], ['ai.', 'Pengaturan AI & zona'], ['liveai.', 'Live AI'], ['camera.', 'Kamera manual'], ['sources.', 'Sumber stream'], ['allowlist.', 'Allowlist host'], ['alarm.', 'Alarm sistem']];
    const tone = (it) => (!it.ok || it.action === 'alarm.offline' || it.action === 'alarm.disk' || it.action.endsWith('.delete') ? 'bad' : it.action.startsWith('alarm.') ? 'ok' : '');
    const when = (ts) => { const d = new Date(ts * 1000); return `${d.toLocaleDateString('id-ID', { day: '2-digit', month: 'short' })} ${fmtTime(d)}`; };
    body.innerHTML = `<section class="card"><div class="card-head"><h3>${ic('history')}Log aktivitas</h3><span class="head-chip"><b>${data.items.length}</b> catatan${data.items.length >= 500 ? ' (500 terbaru)' : ''}</span></div>
      <p class="card-sub">Siapa melakukan apa: login, perubahan pengaturan, hapus jepretan/rekaman, kelola pengguna, allowlist host, dan alarm sistem. Disimpan ${data.retentionDays} hari di server.</p>
      <div class="form-grid audit-filter" style="margin-top:12px">
        <label class="source-field"><span>Tanggal</span><input type="date" id="au-date" value="${esc(f.date)}"></label>
        <label class="source-field"><span>Pengguna</span><select id="au-user"><option value="">Semua pengguna</option>${data.users.map((u) => `<option${u === f.user ? ' selected' : ''}>${esc(u)}</option>`).join('')}</select></label>
        <label class="source-field"><span>Jenis aktivitas</span><select id="au-act"><option value="">Semua aktivitas</option>${groups.map(([k, l]) => `<option value="${k}"${k === f.action ? ' selected' : ''}>${l}</option>`).join('')}</select></label>
        <label class="source-field"><span>Cari target / detail</span><input type="search" id="au-q" value="${esc(f.q)}" placeholder="mis. Gerbang, satpam…"></label>
      </div>
      <div class="card-row end" style="margin-top:10px"><button class="action-btn" id="au-reset">Reset filter</button><button class="action-btn" id="au-csv">${ic('download')}<span>Ekspor CSV</span></button><button class="action-btn primary-btn" id="au-go">${ic('search')}<span>Terapkan</span></button></div>
      ${data.items.length ? `<div class="perm-wrap"><table class="audit-table"><thead><tr><th>Waktu</th><th>Pengguna</th><th>Aktivitas</th><th>Target</th><th>Detail</th><th>IP</th></tr></thead><tbody>${data.items.map((it) => `<tr><td class="mono">${esc(when(it.ts))}</td><td><b>${esc(it.username)}</b></td><td><span class="audit-chip ${tone(it)}">${esc(it.label)}</span></td><td>${esc(it.target)}</td><td class="muted">${esc(it.detail)}</td><td class="mono muted">${esc(it.ip)}</td></tr>`).join('')}</tbody></table></div>`
        : `<div class="empty-state" style="margin-top:14px">Tidak ada catatan pada filter ini.</div>`}</section>`;
    const apply = () => { Object.assign(f, { date: $('#au-date').value, user: $('#au-user').value, action: $('#au-act').value, q: $('#au-q').value.trim() }); renderSetAudit(body); };
    $('#au-go').onclick = apply; $('#au-q').onkeydown = (e) => { if (e.key === 'Enter') apply(); };
    $('#au-reset').onclick = () => { state.auditFilter = null; renderSetAudit(body); };
    $('#au-csv').onclick = () => {
      // Cegah CSV/formula injection: nilai yang diawali = + - @ TAB CR (mis. username login gagal) diberi awalan '
      const q = (v) => { let s = String(v ?? ''); if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`; return `"${s.replace(/"/g, '""')}"`; };
      const lines = [['Waktu', 'Pengguna', 'Aktivitas', 'Target', 'Detail', 'IP', 'Berhasil'].join(',')].concat(data.items.map((it) => [new Date(it.ts * 1000).toISOString(), it.username, it.label, it.target, it.detail, it.ip, it.ok ? 'ya' : 'tidak'].map(q).join(',')));
      const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' }));
      a.download = `log-aktivitas-${f.date || new Date().toISOString().slice(0, 10)}.csv`; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    };
  }

  // ---------------------------------------------- Pengguna & role
  const fmtAgo = (ts) => {
    if (!ts) return 'belum pernah';
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return 'baru saja';
    if (s < 3600) return `${Math.round(s / 60)} menit lalu`;
    if (s < 86400) return `${Math.round(s / 3600)} jam lalu`;
    return new Date(ts * 1000).toLocaleDateString('id-ID', { day: 'numeric', month: 'short', year: 'numeric' });
  };
  const genPassword = () => { const a = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'; const r = crypto.getRandomValues(new Uint32Array(12)); return Array.from(r, (x) => a[x % a.length]).join(''); };
  async function renderSetUsers(body) {
    let data;
    try { data = await apiJson('/api/users'); } catch (err) { body.innerHTML = `<div class="empty-card">${ic('alert')}<b>Data pengguna tidak dapat dimuat</b><span>${esc(err.message)}</span></div>`; return; }
    const { users, roles, permissions, regions, me } = data;
    const roleById = new Map(roles.map((r) => [r.id, r]));
    const groups = [...new Set(permissions.map((p) => p.group))];
    const regionText = (u) => (u.regions.length ? u.regions.join(', ') : 'Semua wilayah');
    body.innerHTML = `<div class="page" style="animation:none">
      <section class="card"><div class="card-head"><h3>${ic('users')}Pengguna</h3><button class="action-btn primary-btn" id="us-add">${ic('plus')}<span>Tambah pengguna</span></button></div>
        <p class="card-sub">Tiap pengguna punya satu role (menentukan fitur) dan daftar wilayah (menentukan kamera yang terlihat — termasuk jepretan &amp; rekamannya).</p>
        <div class="user-list">${users.map((u, i) => `<div class="user-row${u.disabled ? ' is-off' : ''}" style="--i:${i}">
          <span class="avatar">${esc(u.username.charAt(0).toUpperCase())}</span>
          <div class="grow"><strong>${esc(u.username)}${u.username === me ? ' <span class="you-chip">Anda</span>' : ''}</strong>
            <span class="sub">${ic('map', 'ic-sm')}${esc(regionText(u))} · login ${esc(fmtAgo(u.lastLogin))}</span></div>
          <span class="role-chip role-${esc(u.role)}">${esc(u.roleName)}</span>
          ${u.disabled ? '<span class="state-chip">Nonaktif</span>' : u.mustChange ? '<span class="state-chip warn">Wajib ganti password</span>' : '<span class="state-chip on"><span class="ai-dot"></span>Aktif</span>'}
          <button class="icon-btn" data-uedit="${esc(u.username)}" title="Ubah ${esc(u.username)}" aria-label="Ubah ${esc(u.username)}">${ic('pencil')}</button>
        </div>`).join('')}</div></section>
      <section class="card"><div class="card-head"><h3>${ic('shield')}Role &amp; izin</h3><button class="action-btn" id="rl-add">${ic('plus')}<span>Role baru</span></button></div>
        <p class="card-sub">Centang izin per role. <b>Admin</b> selalu memegang semua izin. Melihat kamera &amp; Pantau selalu diizinkan untuk semua role.</p>
        <div class="perm-wrap"><table class="perm-table"><thead><tr><th>Izin</th>${roles.map((r) => `<th><span class="perm-role">${esc(r.name)}</span><span class="perm-count">${r.userCount} pengguna</span>${r.builtin ? '' : `<button class="perm-del" data-rdel="${esc(r.id)}" title="Hapus role ${esc(r.name)}" aria-label="Hapus role ${esc(r.name)}">${ic('trash', 'ic-sm')}</button>`}</th>`).join('')}</tr></thead>
          <tbody>${groups.map((g) => `<tr class="perm-group"><td colspan="${roles.length + 1}">${esc(g)}</td></tr>${permissions.filter((p) => p.group === g).map((p) => `<tr><td>${esc(p.label)}</td>${roles.map((r) => `<td><input type="checkbox" class="perm-chk" data-role="${esc(r.id)}" data-p="${esc(p.key)}"${r.permissions.includes(p.key) ? ' checked' : ''}${r.id === 'admin' ? ' disabled' : ''} aria-label="${esc(p.label)} untuk ${esc(r.name)}"></td>`).join('')}</tr>`).join('')}`).join('')}</tbody></table></div>
        <div class="card-row end" style="margin-top:12px"><span class="field-note grow" id="rl-dirty" style="margin:0"></span><button class="action-btn primary-btn" id="rl-save" disabled>${ic('save')}<span>Simpan izin</span></button></div></section></div>`;

    const dirty = new Set();
    $$('.perm-chk', body).forEach((c) => c.onchange = () => { dirty.add(c.dataset.role); $('#rl-save').disabled = false; $('#rl-dirty').textContent = `${dirty.size} role berubah — belum disimpan`; });
    $('#rl-save').onclick = async () => {
      try {
        for (const id of dirty) {
          const r = roleById.get(id);
          const perms = $$(`.perm-chk[data-role="${id}"]`, body).filter((c) => c.checked).map((c) => c.dataset.p);
          await postJson('/api/roles', { id, name: r.name, permissions: perms });
        }
        toast('Izin role disimpan'); renderSetUsers(body);
      } catch (err) { toast(`Gagal menyimpan izin: ${err.message}`, 'err'); }
    };
    $('#rl-add').onclick = () => promptBox('Role baru', 'Nama role (mis. Satpam, Supervisor)', '', async (name) => {
      try { await postJson('/api/roles', { name, permissions: ['jepretan.view'] }); toast(`Role “${name}” dibuat · atur izinnya di tabel`); renderSetUsers(body); }
      catch (err) { toast(`Gagal membuat role: ${err.message}`, 'err'); }
    });
    $$('[data-rdel]', body).forEach((b) => b.onclick = () => { const r = roleById.get(b.dataset.rdel); confirmBox(`Hapus role ${r.name}?`, r.userCount ? `Masih dipakai ${r.userCount} pengguna — pindahkan dulu ke role lain.` : 'Role ini akan dihapus.', async () => {
      try { await apiJson(`/api/roles/${encodeURIComponent(r.id)}`, { method: 'DELETE' }); toast('Role dihapus'); renderSetUsers(body); }
      catch (err) { toast(err.message, 'err'); }
    }); });

    const userForm = (u) => {
      const isNew = !u, self = u && u.username === me;
      const regs = new Set(u ? u.regions : []);
      openModal(`<span class="sheet-handle" aria-hidden="true"></span>
        <div class="modal-header"><div class="modal-heading"><h2 id="modal-title">${isNew ? 'Tambah pengguna' : `Ubah ${esc(u.username)}`}</h2><p class="modal-subtitle">${isNew ? 'Pengguna wajib mengganti password saat login pertama.' : `Dibuat ${esc(fmtAgo(u.createdAt))} · login ${esc(fmtAgo(u.lastLogin))}`}</p></div><button class="icon-btn close-btn" data-close-modal aria-label="Tutup">${ic('x')}</button></div>
        <form id="uf" class="uf-body">
          <div class="form-grid">
            <label class="source-field"><span>Username</span><input type="text" id="uf-u" value="${esc(u ? u.username : '')}" ${isNew ? '' : 'disabled'} autocomplete="off" maxlength="32" placeholder="mis. satpam.pos1" required></label>
            <label class="source-field"><span>Role</span><select id="uf-r"${self ? ' disabled' : ''}>${roles.map((r) => `<option value="${esc(r.id)}"${(u ? u.role : 'viewer') === r.id ? ' selected' : ''}>${esc(r.name)}</option>`).join('')}</select></label>
            <label class="source-field" style="grid-column:1/-1"><span>${isNew ? 'Password awal' : 'Reset password (kosongkan bila tidak diganti)'}</span><div class="pw-row"><input type="text" id="uf-p" autocomplete="new-password" minlength="${state.minPass}" ${isNew ? 'required' : ''} placeholder="minimal ${state.minPass} karakter"><button type="button" class="action-btn" id="uf-gen">${ic('rotate')}<span>Buat acak</span></button></div></label>
          </div>
          <div class="uf-regions"><span class="small" style="font-weight:700;color:var(--ink-2)">Wilayah kamera yang boleh dilihat</span>
            <div class="seg-row"><label class="chk"><input type="radio" name="uf-scope" value="all"${regs.size ? '' : ' checked'}> Semua wilayah</label><label class="chk"><input type="radio" name="uf-scope" value="some"${regs.size ? ' checked' : ''}> Pilih wilayah</label></div>
            <div class="cls-objs uf-reg-list${regs.size ? '' : ' hidden'}">${regions.map((f) => `<label class="obj-chip"><input type="checkbox" data-reg="${esc(f)}"${regs.has(f) ? ' checked' : ''}><span>${esc(f)}</span></label>`).join('')}</div>
          </div>
          ${!isNew && !self ? `<div style="margin-top:12px">${sw('uf-off', 'Nonaktifkan akun (tidak bisa login, sesi dicabut)', u.disabled)}</div>` : ''}
        </form>
        <div class="modal-footer">${!isNew && !self ? `<button class="action-btn danger-btn" id="uf-del">${ic('trash')}<span>Hapus</span></button><span style="flex:1"></span>` : ''}<button class="action-btn" data-close-modal>Batal</button><button class="action-btn primary-btn" id="uf-save">${ic('save')}<span>Simpan</span></button></div>`, 'user-dialog');
      els.dialog.style.width = 'min(560px,100%)';
      const list = $('.uf-reg-list', els.dialog);
      $$('[name="uf-scope"]', els.dialog).forEach((r) => r.onchange = () => list.classList.toggle('hidden', r.value === 'all' ? r.checked : !r.checked));
      $('#uf-gen').onclick = () => { $('#uf-p').value = genPassword(); };
      const save = async () => {
        const some = $('[name="uf-scope"][value="some"]', els.dialog).checked;
        const regionsSel = some ? $$('[data-reg]', els.dialog).filter((c) => c.checked).map((c) => c.dataset.reg) : [];
        if (some && !regionsSel.length) return toast('Pilih minimal satu wilayah, atau pilih “Semua wilayah”', 'err');
        const pw = $('#uf-p').value;
        if ((isNew || pw) && pw.length < state.minPass) return toast(`Password minimal ${state.minPass} karakter`, 'err');
        const payload = { regions: regionsSel };
        if (!self) payload.role = $('#uf-r').value;
        if (pw) payload.password = pw;
        if ($('#uf-off')) payload.disabled = $('#uf-off').checked;
        try {
          if (isNew) { payload.username = $('#uf-u').value.trim(); await postJson('/api/users', payload); toast(`Pengguna “${payload.username}” dibuat${pw ? ' · berikan password awalnya' : ''}`); }
          else { await postJson(`/api/users/${encodeURIComponent(u.username)}`, payload); toast(pw ? 'Disimpan · password direset, sesi pengguna dicabut' : 'Perubahan pengguna disimpan'); }
          closeModal(); renderSetUsers(body);
        } catch (err) { toast(err.message, 'err'); }
      };
      $('#uf-save').onclick = save;
      $('#uf').onsubmit = (e) => { e.preventDefault(); save(); };
      const del = $('#uf-del');
      if (del) del.onclick = () => confirmBox(`Hapus ${u.username}?`, 'Akun dihapus permanen dan semua sesinya dicabut.', async () => {
        try { await apiJson(`/api/users/${encodeURIComponent(u.username)}`, { method: 'DELETE' }); toast('Pengguna dihapus'); renderSetUsers(body); }
        catch (err) { toast(err.message, 'err'); }
      });
      (isNew ? $('#uf-u') : $('#uf-r'))?.focus();
    };
    $('#us-add').onclick = () => userForm(null);
    $$('[data-uedit]', body).forEach((b) => b.onclick = () => userForm(users.find((x) => x.username === b.dataset.uedit)));
  }

  function renderSetBackup(body) {
    body.innerHTML = `<section class="card"><h3>${ic('save')}Backup &amp; restore</h3><p class="card-sub">Satu berkas .json berisi sumber daerah &amp; kamera manual (dari server) plus folder custom, urutan folder, folder hide-offline, dan preset tata letak (dari browser ini).</p>
      <div class="card-actions start" style="margin-top:14px"><button class="action-btn" id="bk-exp">${ic('download')}<span>Export backup</span></button><label class="action-btn" style="cursor:pointer">${ic('upload')}<span>Import backup</span><input type="file" accept=".json,application/json" class="hidden" id="bk-imp"></label></div>
      <p class="field-note">Pengaturan AI, rekaman server, dan token Telegram tidak ikut — tersimpan di folder <code>data/</code> server.</p></section>`;
    $('#bk-exp').onclick = async () => {
      try {
        const [src, man] = await Promise.all([apiJson('/api/sources'), apiJson('/api/manual-cameras')]);
        const payload = { app: 'CCTipi System', version: 2, exportedAt: new Date().toISOString(), sources: src.sources || [], manualCameras: man.cameras || [], customFolders: state.customFolders, folderOrder: state.folderOrder, hideOfflineFolders: [...state.hideOfflineFolders], layoutPresets: state.layoutPresets };
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }));
        a.download = `cctipi-backup-${isoDay(new Date())}.json`; a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 4000);
        toast('Backup diekspor');
      } catch (err) { toast(`Ekspor gagal: ${err.message}`, 'err'); }
    };
    $('#bk-imp').onchange = async (e) => {
      const file = e.target.files[0]; if (!file) return;
      try {
        const d = JSON.parse(await file.text());
        const srv = {};
        if (Array.isArray(d.sources)) srv.sources = d.sources;
        if (Array.isArray(d.manualCameras)) srv.manualCameras = d.manualCameras;
        if (srv.sources || srv.manualCameras) await postJson('/api/backup/import', srv);
        await loadCameras(true);
        if (Array.isArray(d.customFolders)) { const inc = normalizeFolders(d.customFolders); const ids = new Set(inc.map((f) => f.id)); state.customFolders = [...state.customFolders.filter((f) => !ids.has(f.id)), ...inc]; saveFolders(); }
        if (Array.isArray(d.folderOrder)) { state.folderOrder = d.folderOrder.slice(); LS.set(K.order, state.folderOrder); }
        if (Array.isArray(d.hideOfflineFolders)) { state.hideOfflineFolders = new Set(d.hideOfflineFolders); LS.set(K.hideF, [...state.hideOfflineFolders]); }
        if (d.layoutPresets && typeof d.layoutPresets === 'object') { state.layoutPresets = { ...state.layoutPresets, ...d.layoutPresets }; savePresets(); App.renderLpSelect?.(); }
        App.renderPresetOptions?.(); App.renderFillSelect?.(); App.renderSidebar?.();
        toast('Backup berhasil diimpor');
      } catch (err) { toast(`Impor gagal: ${err.message}`, 'err'); }
      e.target.value = '';
    };
  }

  Object.assign(App, {
    renderKamera, deleteManual, addOptin, renderPengaturan,
  });
})();
