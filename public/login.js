'use strict';
(() => {
  const h = new Date().getHours();
  const part = h < 11 ? 'pagi' : h < 15 ? 'siang' : h < 18 ? 'sore' : 'malam';
  const g = document.getElementById('greeting');
  if (g) g.textContent = `Selamat ${part}`;
})();

const form = document.getElementById('login-form');
const errBox = document.getElementById('login-error');
const errText = document.getElementById('login-error-text');
const submitBtn = document.getElementById('login-submit');

const showError = (msg) => {
  if (errText) errText.textContent = msg; else errBox.textContent = msg;
  errBox.classList.add('show');
};

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  errBox.classList.remove('show');
  submitBtn.textContent = 'Memeriksa…';
  submitBtn.disabled = true;
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: document.getElementById('username').value.trim(),
        password: document.getElementById('password').value,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showError(res.status === 429
        ? (data.error || 'Terlalu banyak percobaan. Coba lagi nanti.')
        : (data.error || 'Login gagal.'));
      return;
    }
    window.location.href = '/';
  } catch {
    showError('Tidak dapat terhubung ke server.');
  } finally {
    submitBtn.textContent = 'Masuk';
    submitBtn.disabled = false;
  }
});
