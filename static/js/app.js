document.addEventListener('DOMContentLoaded', () => {
  lucide.createIcons();
  initToasts();
  initModals();
  initScoreBars();
  initCharts();
  initSidebar();
  initSliders();
  initRollback();
});

/* ── Toasts ─────────────────────────────────────────────────── */
function initToasts() {
  document.querySelectorAll('[data-flash]').forEach(el =>
    showToast(el.dataset.flash, el.dataset.flashType || 'info')
  );
}

function showToast(message, type = 'info') {
  let c = document.getElementById('toast-container');
  if (!c) {
    c = document.createElement('div');
    c.id = 'toast-container';
    c.className = 'toast-container';
    document.body.appendChild(c);
  }
  const icons = { success: 'check-circle', error: 'x-circle', info: 'info' };
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<i data-lucide="${icons[type] || 'info'}"></i><span class="toast-msg">${message}</span>`;
  c.appendChild(t);
  lucide.createIcons({ nodes: [t] });
  setTimeout(() => {
    t.style.cssText = 'opacity:0;transform:translateY(8px);transition:all .3s ease';
    setTimeout(() => t.remove(), 300);
  }, 5000);
}

/* ── Modals ─────────────────────────────────────────────────── */
function initModals() {
  document.querySelectorAll('[data-modal-open]').forEach(b =>
    b.addEventListener('click', () => openModal(b.dataset.modalOpen))
  );
  document.querySelectorAll('[data-modal-close]').forEach(b =>
    b.addEventListener('click', () => {
      const bd = b.closest('.modal-backdrop');
      if (bd) closeModal(bd.id);
    })
  );
  document.querySelectorAll('.modal-backdrop').forEach(bd =>
    bd.addEventListener('click', e => { if (e.target === bd) closeModal(bd.id); })
  );
}
function openModal(id)  { const el = document.getElementById(id); if (el) el.classList.add('open'); }
function closeModal(id) { const el = document.getElementById(id); if (el) el.classList.remove('open'); }

/* ── Score bars ─────────────────────────────────────────────── */
function initScoreBars() {
  document.querySelectorAll('.score-bar-fill').forEach(bar => {
    const pct = parseFloat(bar.dataset.score) || 0;
    bar.style.width = '0%';
    requestAnimationFrame(() =>
      setTimeout(() => { bar.style.width = pct + '%'; }, 80)
    );
  });
}

/* ── Charts ─────────────────────────────────────────────────── */
/*
  WINDOW_SIZE: max data points shown at once.
  If there are more versions, the chart shows the last WINDOW_SIZE only
  and the user sees a "Showing last N versions" label.
  This stops the x-axis labels squishing on large projects.
*/
const WINDOW_SIZE = 20;

function initCharts() {
  const dataEl = document.getElementById('chart-data');
  if (!dataEl) return;

  let data;
  try { data = JSON.parse(dataEl.textContent); } catch { return; }
  if (!data.labels || !data.labels.length) return;

  /* Apply window — take the LAST WINDOW_SIZE entries */
  const total = data.labels.length;
  const start = total > WINDOW_SIZE ? total - WINDOW_SIZE : 0;

  const labels   = data.labels.slice(start);
  const scores   = data.scores.slice(start);
  const channels = data.channels.slice(start);
  const tempos   = data.tempos.slice(start);
  const patterns = data.patterns.slice(start);
  const samples  = (data.samples || []).slice(start);

  if (total > WINDOW_SIZE) {
    document.querySelectorAll('.chart-window-note').forEach(el => {
      el.textContent = `Showing last ${WINDOW_SIZE} of ${total} versions`;
      el.style.display = 'block';
    });
  }

  /* Shared Chart.js defaults */
  Chart.defaults.color        = '#8888aa';
  Chart.defaults.borderColor  = '#191930';
  Chart.defaults.font.family  = "'JetBrains Mono', monospace";
  Chart.defaults.font.size    = 11;

  const grid = { color: '#191930', drawBorder: false };
  const tip  = {
    backgroundColor : '#0f0f1c',
    borderColor     : '#252548',
    borderWidth     : 1,
    titleColor      : '#eeeef8',
    bodyColor       : '#8888aa',
    padding         : 12,
    cornerRadius    : 8,
  };

  const makeChart = (id, type, label, vals, color, opts = {}) => {
    const ctx = document.getElementById(id);
    if (!ctx) return;
    return new Chart(ctx, {
      type,
      data: {
        labels,
        datasets: [{
          label,
          data: vals,
          borderColor       : color,
          backgroundColor   : type === 'line' ? color + '14' : color + '70',
          borderWidth       : type === 'line' ? 2 : 1,
          fill              : type === 'line',
          tension           : 0.35,
          pointBackgroundColor: color,
          pointRadius       : type === 'line' ? (vals.length > 12 ? 2 : 4) : undefined,
          pointHoverRadius  : type === 'line' ? 6 : undefined,
          borderRadius      : type === 'bar' ? 4 : undefined,
        }],
      },
      options: {
        responsive   : true,
        animation    : { duration: 400 },
        plugins      : { legend: { display: false }, tooltip: tip },
        scales       : { x: { grid }, y: { grid, ...opts } },
      },
    });
  };

  makeChart('chart-score',    'line', 'Quality Score', scores,   '#c8920a', { min: 0, max: 100 });
  makeChart('chart-channels', 'bar',  'Channels',      channels, '#0aaeb8', { beginAtZero: true });
  makeChart('chart-tempo',    'line', 'Tempo',         tempos,   '#a78bfa', {});
  makeChart('chart-patterns', 'bar',  'Patterns',      patterns, '#22c55e', { beginAtZero: true });

  /* Sample count chart — only show if we have data */
  if (samples.some(v => v > 0)) {
    makeChart('chart-samples', 'bar', 'Samples', samples, '#f59e0b', { beginAtZero: true });
  } else {
    const wrap = document.getElementById('chart-samples-wrap');
    if (wrap) wrap.style.display = 'none';
  }
}

/* ── Sidebar ────────────────────────────────────────────────── */
function initSidebar() {
  const toggle  = document.getElementById('sidebar-toggle');
  const sidebar = document.getElementById('sidebar');
  if (!toggle || !sidebar) return;
  toggle.addEventListener('click', () => sidebar.classList.toggle('open'));
  document.addEventListener('click', e => {
    if (sidebar.classList.contains('open') &&
        !sidebar.contains(e.target) &&
        e.target !== toggle) {
      sidebar.classList.remove('open');
    }
  });
}

/* ── Range sliders ──────────────────────────────────────────── */
function initSliders() {
  document.querySelectorAll('input[type="range"]').forEach(s => {
    const d = document.getElementById(`val-${s.id}`);
    if (d) { d.textContent = s.value; s.addEventListener('input', () => d.textContent = s.value); }
  });
}

/* ── Rollback ───────────────────────────────────────────────── */
function initRollback() {
  document.querySelectorAll('.version-rollback').forEach(btn => {
    btn.addEventListener('click', e => {
      e.preventDefault();
      const pid  = btn.dataset.projectId;
      const vnum = btn.dataset.versionNum;
      if (!confirm(
        `Roll back to version ${vnum}?\n\n` +
        `This will overwrite the .flp file on disk. ` +
        `Close the project in FL Studio first, then reopen after rollback.`
      )) return;

      btn.disabled = true;
      btn.textContent = 'Rolling back…';

      fetch('/api/rollback', {
        method  : 'POST',
        headers : { 'Content-Type': 'application/json' },
        body    : JSON.stringify({ project_id: parseInt(pid), version_num: parseInt(vnum) }),
      })
      .then(r => r.json())
      .then(d => {
        showToast(d.message, d.success ? 'success' : 'error');
        btn.disabled    = false;
        btn.textContent = 'Rollback';
      })
      .catch(() => {
        showToast('Network error during rollback.', 'error');
        btn.disabled    = false;
        btn.textContent = 'Rollback';
      });
    });
  });
}

/* ── Confirm delete ─────────────────────────────────────────── */
function confirmDelete(name, formId) {
  if (confirm(`Delete project "${name}"? This is permanent.`)) {
    document.getElementById(formId).submit();
  }
}
