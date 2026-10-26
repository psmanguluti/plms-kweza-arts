/* ── CSRF: attach the token to every same-origin state-changing fetch ─ */
(function () {
  const meta = document.querySelector('meta[name="csrf-token"]');
  const token = meta ? meta.content : '';
  const _fetch = window.fetch.bind(window);
  window.fetch = function (input, init = {}) {
    const method = (init.method || (input && input.method) || 'GET').toUpperCase();
    if (token && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const h = new Headers(init.headers || {});
      if (!h.has('X-CSRFToken')) h.set('X-CSRFToken', token);
      init = { ...init, headers: h, credentials: init.credentials || 'same-origin' };
    }
    return _fetch(input, init);
  };
})();

document.addEventListener('DOMContentLoaded', () => {
  lucide.createIcons();
  initToasts();
  initModals();
  initScoreBars();
  initCharts();
  initSidebar();
  initSliders();
  initRollback();
  initNotes();
  initCompare();
  initVerify();
  initNotifications();
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
  t.innerHTML = `<i data-lucide="${icons[type] || 'info'}"></i><span class="toast-msg"></span>`;
  t.querySelector('.toast-msg').textContent = message;   // never inject HTML (project names are user input)
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
/* ── Generic modal helper ───────────────────────────────────── */
function plmsModal({ title, bodyHTML, confirmText = 'Confirm', danger = false, onOpen, onConfirm }) {
  const bd = document.createElement('div');
  bd.className = 'modal-backdrop open';
  bd.innerHTML = `
    <div class="modal" role="dialog" aria-modal="true">
      <h3></h3>
      <div class="plms-modal-body">${bodyHTML || ''}</div>
      <div class="modal-actions">
        <button type="button" class="btn btn-secondary btn-sm" data-act="cancel">Cancel</button>
        <button type="button" class="btn ${danger ? 'btn-danger' : 'btn-primary'} btn-sm" data-act="ok">${confirmText}</button>
      </div>
    </div>`;
  bd.querySelector('h3').textContent = title;
  document.body.appendChild(bd);
  const close = () => bd.remove();
  const ok = bd.querySelector('[data-act="ok"]');
  bd.querySelector('[data-act="cancel"]').onclick = close;
  bd.addEventListener('mousedown', e => { if (e.target === bd) close(); });
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
  });
  ok.onclick = async () => {
    ok.disabled = true;
    try { await onConfirm(bd, close); } finally { ok.disabled = false; }
  };
  if (onOpen) onOpen(bd, ok);
  return bd;
}

const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ── Rollback with confirmation modal (FR-23..FR-26) ────────── */
function initRollback() {
  document.querySelectorAll('.version-rollback').forEach(btn => {
    btn.addEventListener('click', e => {
      e.preventDefault();
      const pid  = parseInt(btn.dataset.projectId);
      const vnum = parseInt(btn.dataset.versionNum);
      plmsModal({
        title: `Roll back to version ${vnum}?`,
        confirmText: 'Roll back',
        danger: true,
        bodyHTML: `
          <p style="margin-bottom:12px">This restores the project file from the v${vnum} backup.</p>
          <ul style="margin:0 0 14px 18px;color:var(--text-2);font-size:.85rem;line-height:1.7">
            <li>Your current file is saved first, so this can be undone.</li>
            <li>The restore is recorded as a <b>new version</b> — no history is deleted.</li>
            <li>The restored file is checked against its saved checksum.</li>
          </ul>
          <label style="display:flex;gap:8px;align-items:flex-start;font-size:.85rem;color:var(--text-1)">
            <input type="checkbox" id="rb-closed" style="margin-top:3px">
            I have closed this project in FL Studio.
          </label>`,
        onOpen: (bd, ok) => {
          ok.disabled = true;
          bd.querySelector('#rb-closed').addEventListener('change', ev => ok.disabled = !ev.target.checked);
        },
        onConfirm: async (bd, close) => {
          try {
            const r = await fetch('/api/rollback', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ project_id: pid, version_num: vnum }) });
            const d = await r.json();
            showToast(d.message || d.error || 'Rollback finished.', d.success ? 'success' : 'error');
            close();
            if (d.success) setTimeout(() => location.reload(), 1600);
          } catch (err) {
            showToast('Network error during rollback.', 'error');
          }
        }
      });
    });
  });
}

/* ── Version notes (annotations) ────────────────────────────── */
function initNotes() {
  document.querySelectorAll('.version-note-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const vid = btn.dataset.versionId, vnum = btn.dataset.versionNum;
      plmsModal({
        title: `Note for version ${vnum}`,
        confirmText: 'Save note',
        bodyHTML: `<div class="modal-form-fields" style="margin-bottom:0">
            <textarea id="note-text" maxlength="1000" placeholder="What changed? Why does this version matter?">${esc(btn.dataset.note)}</textarea>
            <small style="color:var(--text-3)"><span id="note-count">0</span>/1000</small></div>`,
        onOpen: bd => {
          const t = bd.querySelector('#note-text'), c = bd.querySelector('#note-count');
          const upd = () => c.textContent = t.value.length; upd(); t.addEventListener('input', upd); t.focus();
        },
        onConfirm: async (bd, close) => {
          const notes = bd.querySelector('#note-text').value;
          const r = await fetch(`/api/version/${vid}/notes`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ notes }) });
          const d = await r.json();
          if (!d.ok) { showToast(d.error || 'Could not save note.', 'error'); return; }
          btn.dataset.note = d.notes;
          const line = document.getElementById(`note-line-${vid}`);
          if (line) line.textContent = d.notes || '';
          btn.querySelector('span').textContent = d.notes ? 'Edit note' : 'Add note';
          showToast('Note saved.', 'success');
          close();
        }
      });
    });
  });
}

/* ── Compare any two versions ───────────────────────────────── */
function initCompare() {
  const root = document.getElementById('compare-panel');
  if (!root) return;
  const pid = root.dataset.projectId;
  const selA = document.getElementById('cmp-a'), selB = document.getElementById('cmp-b');
  const out = document.getElementById('cmp-result');
  const arrow = { increased: '▲', decreased: '▼', unchanged: '–', 'n/a': '' };

  async function run() {
    if (selA.value === selB.value) { out.innerHTML = '<p class="text-muted">Choose two different versions.</p>'; return; }
    out.innerHTML = '<p class="text-muted">Comparing…</p>';
    try {
      const r = await fetch(`/api/project/${pid}/compare?a=${selA.value}&b=${selB.value}`);
      const d = await r.json();
      if (!d.ok) { out.innerHTML = `<p class="text-muted">${esc(d.error)}</p>`; return; }
      const rows = d.metrics.map(m => `
        <tr class="cmp-${m.direction}">
          <td>${esc(m.label)}</td>
          <td class="mono">${m.available ? m.before : '—'}</td>
          <td class="mono">${m.available ? m.after : '—'}</td>
          <td class="mono">${m.available ? `${arrow[m.direction]} ${m.delta > 0 ? '+' : ''}${m.delta} ${esc(m.unit)}` : 'no arrangement data'}</td>
        </tr>`).join('');
      const list = (items, fmt) => items.length ? items.map(fmt).join(', ') : '—';
      const a = d.arrangement;
      const arr = a.available ? `
        <div class="cmp-arr">
          <div><b>Patterns added:</b> ${list(a.patterns_added, p => `#${p.id} (${p.count}×)`)}</div>
          <div><b>Patterns removed:</b> ${list(a.patterns_removed, p => `#${p.id}`)}</div>
          <div><b>Pattern usage changed:</b> ${list(a.patterns_changed, p => `#${p.id} ${p.before}→${p.after}`)}</div>
          <div><b>Playlist tracks added:</b> ${list(a.tracks_added, t => `T${t.id}`)}
               &nbsp; <b>removed:</b> ${list(a.tracks_removed, t => `T${t.id}`)}</div>
        </div>` : '<p class="text-muted" style="font-size:.8rem">Arrangement changes need versions captured with playlist data (newer captures).</p>';
      const bars = (bd) => bd.dimensions.map(x => `
        <div style="display:flex;align-items:center;gap:8px;font-size:.75rem;margin:2px 0">
          <span style="width:140px;color:var(--text-2)">${esc(x.label)}</span>
          <div style="flex:1;height:6px;background:var(--bg-elevated);border-radius:4px;overflow:hidden">
            <div style="width:${x.max ? Math.round(x.points / x.max * 100) : 0}%;height:100%;background:var(--gold)"></div></div>
          <span class="mono" style="width:70px;text-align:right">${x.points}/${x.max}</span>
        </div>`).join('');
      const noteBlock = (v, n) => n ? `<div class="text-muted" style="font-size:.78rem;margin-top:6px">v${v} note: ${esc(n)}</div>` : '';
      out.innerHTML = `
        <p style="margin:10px 0;color:var(--text-1)">${esc(d.summary)}</p>
        <div class="table-scroll"><table class="cmp-table">
          <thead><tr><th>Metric</th><th>v${d.a.version_num}</th><th>v${d.b.version_num}</th><th>Change</th></tr></thead>
          <tbody>
            <tr class="cmp-score"><td>Quality score</td><td class="mono">${d.a.quality_score}</td>
                <td class="mono">${d.b.quality_score}</td>
                <td class="mono">${d.score_delta > 0 ? '+' : ''}${d.score_delta}</td></tr>
            ${rows}
          </tbody></table></div>
        ${arr}
        <div class="cmp-break" style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:14px">
          <div><b style="font-size:.8rem">v${d.a.version_num} score breakdown</b>${bars(d.breakdown.a)}</div>
          <div><b style="font-size:.8rem">v${d.b.version_num} score breakdown</b>${bars(d.breakdown.b)}</div>
        </div>${noteBlock(d.a.version_num, d.notes.a)}${noteBlock(d.b.version_num, d.notes.b)}`;
    } catch (e) { out.innerHTML = '<p class="text-muted">Could not load comparison.</p>'; }
  }
  document.getElementById('cmp-run').addEventListener('click', run);
  selA.addEventListener('change', run); selB.addEventListener('change', run);
  if (root.dataset.auto === '1') run();
}

/* ── Verify backups ─────────────────────────────────────────── */
function initVerify() {
  const btn = document.getElementById('verify-btn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const r = await fetch(`/api/project/${btn.dataset.projectId}/verify`, { method: 'POST' });
      const d = await r.json();
      const bad = Object.keys(d.problems || {});
      if (bad.length) showToast(`Backup problems in: ${bad.map(v => 'v' + v).join(', ')}`, 'error');
      else showToast(`Backups checked — ${d.counts.ok || 0} intact, ${d.counts.no_backup || 0} without backup.`, 'success');
    } catch (e) { showToast('Could not verify backups.', 'error'); }
    btn.disabled = false;
  });
}

/* ── In-app notifications (FR-27..FR-31) ────────────────────── */
function initNotifications() {
  if (document.body.dataset.auth !== '1') return;
  const tick = async () => {
    try {
      const r = await fetch('/api/notifications');
      if (!r.ok) return;
      const d = await r.json();
      d.notifications.forEach(n =>
        showToast(n.message, n.level === 'error' ? 'error' : (n.level === 'info' ? 'info' : 'success')));
    } catch (e) { /* server restarting — ignore */ }
  };
  tick();
  setInterval(tick, 5000);
}

/* ── Confirm delete ─────────────────────────────────────────── */
function confirmDelete(name, formId) {
  if (confirm(`Delete project "${name}"? This is permanent.`)) {
    document.getElementById(formId).submit();
  }
}
