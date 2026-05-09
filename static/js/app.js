document.addEventListener('DOMContentLoaded', () => {
  lucide.createIcons();
  initToasts(); initModals(); initScoreBars(); initCharts(); initSidebar(); initSliders(); initRollback();
});

function initToasts() {
  document.querySelectorAll('[data-flash]').forEach(el => showToast(el.dataset.flash, el.dataset.flashType || 'info'));
}
function showToast(message, type = 'info') {
  let c = document.getElementById('toast-container');
  if (!c) { c = document.createElement('div'); c.id='toast-container'; c.className='toast-container'; document.body.appendChild(c); }
  const icons = {success:'check-circle',error:'x-circle',info:'info'};
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<i data-lucide="${icons[type]||'info'}"></i><span class="toast-msg">${message}</span>`;
  c.appendChild(t); lucide.createIcons({nodes:[t]});
  setTimeout(() => { t.style.cssText='opacity:0;transform:translateY(8px);transition:all .3s ease'; setTimeout(()=>t.remove(),300); }, 4000);
}

function initModals() {
  document.querySelectorAll('[data-modal-open]').forEach(b => b.addEventListener('click', () => openModal(b.dataset.modalOpen)));
  document.querySelectorAll('[data-modal-close]').forEach(b => b.addEventListener('click', () => { const bd=b.closest('.modal-backdrop'); if(bd) closeModal(bd.id); }));
  document.querySelectorAll('.modal-backdrop').forEach(bd => bd.addEventListener('click', e => { if(e.target===bd) closeModal(bd.id); }));
}
function openModal(id)  { const el=document.getElementById(id); if(el) el.classList.add('open'); }
function closeModal(id) { const el=document.getElementById(id); if(el) el.classList.remove('open'); }

function initScoreBars() {
  document.querySelectorAll('.score-bar-fill').forEach(bar => {
    const pct = bar.dataset.score || '0'; bar.style.width='0%';
    requestAnimationFrame(() => setTimeout(() => { bar.style.width=pct+'%'; }, 80));
  });
}

function initCharts() {
  const dataEl = document.getElementById('chart-data');
  if (!dataEl) return;
  let data; try { data = JSON.parse(dataEl.textContent); } catch { return; }
  if (!data.labels || !data.labels.length) return;
  Chart.defaults.color='#8888aa'; Chart.defaults.borderColor='#191930';
  Chart.defaults.font.family="'JetBrains Mono', monospace"; Chart.defaults.font.size=11;
  const grid = {color:'#191930',drawBorder:false};
  const tip  = {backgroundColor:'#0f0f1c',borderColor:'#252548',borderWidth:1,titleColor:'#eeeef8',bodyColor:'#8888aa',padding:12,cornerRadius:8};
  const mkLine = (id, label, data, color) => {
    const ctx = document.getElementById(id); if(!ctx) return;
    new Chart(ctx, {type:'line',data:{labels:data.labels,datasets:[{label,data:data[label.toLowerCase().replace(/ /g,'_')]||data,borderColor:color,backgroundColor:color+'14',borderWidth:2,fill:true,tension:.35,pointBackgroundColor:color,pointRadius:4,pointHoverRadius:6}]},options:{responsive:true,plugins:{legend:{display:false},tooltip:tip},scales:{x:{grid},y:{grid}}}});
  };
  const mkBar = (id, label, vals, color) => {
    const ctx = document.getElementById(id); if(!ctx) return;
    new Chart(ctx,{type:'bar',data:{labels:data.labels,datasets:[{label,data:vals,backgroundColor:color+'80',borderColor:color,borderWidth:1,borderRadius:4}]},options:{responsive:true,plugins:{legend:{display:false},tooltip:tip},scales:{x:{grid},y:{grid,beginAtZero:true}}}});
  };
  // Score line
  const scoreCtx = document.getElementById('chart-score');
  if(scoreCtx) new Chart(scoreCtx,{type:'line',data:{labels:data.labels,datasets:[{label:'Score',data:data.scores,borderColor:'#c8920a',backgroundColor:'rgba(200,146,10,.08)',borderWidth:2,fill:true,tension:.35,pointBackgroundColor:'#c8920a',pointRadius:4,pointHoverRadius:6}]},options:{responsive:true,plugins:{legend:{display:false},tooltip:tip},scales:{x:{grid},y:{grid,min:0,max:100}}}});
  // Channels bar
  const chanCtx = document.getElementById('chart-channels');
  if(chanCtx) new Chart(chanCtx,{type:'bar',data:{labels:data.labels,datasets:[{label:'Channels',data:data.channels,backgroundColor:'rgba(10,174,184,.5)',borderColor:'#0aaeb8',borderWidth:1,borderRadius:4}]},options:{responsive:true,plugins:{legend:{display:false},tooltip:tip},scales:{x:{grid},y:{grid,beginAtZero:true}}}});
  // Tempo line
  const tempoCtx = document.getElementById('chart-tempo');
  if(tempoCtx) new Chart(tempoCtx,{type:'line',data:{labels:data.labels,datasets:[{label:'Tempo',data:data.tempos,borderColor:'#a78bfa',backgroundColor:'rgba(167,139,250,.07)',borderWidth:2,fill:true,tension:.35,pointBackgroundColor:'#a78bfa',pointRadius:4,pointHoverRadius:6}]},options:{responsive:true,plugins:{legend:{display:false},tooltip:tip},scales:{x:{grid},y:{grid}}}});
  // Patterns bar
  const pattCtx = document.getElementById('chart-patterns');
  if(pattCtx) new Chart(pattCtx,{type:'bar',data:{labels:data.labels,datasets:[{label:'Patterns',data:data.patterns,backgroundColor:'rgba(34,197,94,.4)',borderColor:'#22c55e',borderWidth:1,borderRadius:4}]},options:{responsive:true,plugins:{legend:{display:false},tooltip:tip},scales:{x:{grid},y:{grid,beginAtZero:true}}}});
}

function initSidebar() {
  const toggle=document.getElementById('sidebar-toggle'), sidebar=document.getElementById('sidebar');
  if(!toggle||!sidebar) return;
  toggle.addEventListener('click',()=>sidebar.classList.toggle('open'));
  document.addEventListener('click',e=>{ if(sidebar.classList.contains('open')&&!sidebar.contains(e.target)&&e.target!==toggle) sidebar.classList.remove('open'); });
}

function initSliders() {
  document.querySelectorAll('input[type="range"]').forEach(s=>{
    const d=document.getElementById(`val-${s.id}`);
    if(d){ d.textContent=s.value; s.addEventListener('input',()=>d.textContent=s.value); }
  });
}

function initRollback() {
  document.querySelectorAll('.version-rollback').forEach(btn=>{
    btn.addEventListener('click', e=>{
      e.preventDefault();
      const pid=btn.dataset.projectId, vnum=btn.dataset.versionNum;
      if(!confirm(`Roll back to version ${vnum}? This cannot be undone.`)) return;
      fetch('/api/rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:parseInt(pid),version_num:parseInt(vnum)})})
        .then(r=>r.json()).then(d=>showToast(d.message,d.success?'success':'error'))
        .catch(()=>showToast('Network error.','error'));
    });
  });
}

function confirmDelete(name, formId) {
  if(confirm(`Delete project "${name}"? This action is permanent.`)) document.getElementById(formId).submit();
}
