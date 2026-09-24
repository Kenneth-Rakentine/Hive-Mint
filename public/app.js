const $ = selector => document.querySelector(selector);
const stageNames = { idea: 'Idea tray', research: 'Research desk', design: 'Design floor', qa: 'Inspection', cost: 'Ledger', approval: 'Manager review', listing_draft: 'Listing draft' };
const stations = [
  { stage: 'research', x: 59, y: 81, name: 'RESEARCH', kind: 'tent' },
  { stage: 'design', x: 168, y: 70, name: 'DESIGN', kind: 'workshop' },
  { stage: 'qa', x: 273, y: 83, name: 'INSPECTION', kind: 'tower' },
  { stage: 'approval', x: 376, y: 72, name: 'MANAGER', kind: 'office' },
  { stage: 'cost', x: 183, y: 161, name: 'LEDGER', kind: 'ledger' },
  { stage: 'listing_draft', x: 338, y: 165, name: 'LISTINGS', kind: 'post' },
];
let state = { jobs: [] }, selected = null, model = '', filter = null, busy = false;
const canvas = $('#map'), ctx = canvas.getContext('2d');
ctx.imageSmoothingEnabled = false;
const esc = str => String(str ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const money = amount => `$${Number(amount).toFixed(2)}`;
const current = () => state.jobs.find(j => j.id === selected);
const fmt = date => new Date(date).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
function toast(message, bad = false) {
  const el = $('#toast'); el.textContent = message; el.className = `show${bad ? ' bad' : ''}`;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.className = '', 4500);
}
async function api(path, method = 'GET', data) {
  const res = await fetch(path, { method, headers: data ? { 'content-type': 'application/json' } : {}, body: data ? JSON.stringify(data) : undefined });
  const json = await res.json();
  if (!res.ok) throw new Error(json.error || 'Request failed');
  return json;
}
async function refresh() {
  state = await api('/api/state');
  if (selected && !current()) selected = null;
  render();
}
async function act(fn) {
  if (busy) return;
  busy = true;
  try { await fn(); await refresh(); }
  catch (error) { toast(error.message, true); }
  finally { busy = false; }
}
async function checkOllama() {
  const info = await api('/api/ollama');
  const label = $('#ollama-status');
  label.textContent = info.online ? `● OLLAMA ONLINE · ${info.models.length} MODELS` : '○ OLLAMA OFFLINE · MANUAL MODE';
  label.classList.toggle('on', info.online);
  if (info.models.length) model = info.models.includes(model) ? model : info.models[0];
  renderDetail();
}
function renderQueue() {
  const jobs = filter ? state.jobs.filter(j => j.stage === filter) : state.jobs;
  $('#queue').innerHTML = jobs.length ? jobs.map(job => `<button class="queue-item ${selected === job.id ? 'selected' : ''}" data-job="${job.id}">
    <span class="hex">${job.stage === 'approval' ? '!' : '◆'}</span><span><strong>${esc(job.title || 'Untitled idea')}</strong><small>${esc(job.product || 'Product not chosen')} · ${esc(job.audience || 'Audience not chosen')}</small></span><span class="chip">${esc(stageNames[job.stage])}</span>
  </button>`).join('') : `<div class="empty">${filter ? `No jobs at ${esc(stageNames[filter])}. Click the station again to see all jobs.` : 'Nothing is on the floor yet. Create a job to begin.'}</div>`;
  $('#queue').querySelectorAll('[data-job]').forEach(el => el.addEventListener('click', () => { selected = el.dataset.job; render(); }));
}
function numberField(key, label, value, step = '0.01') {
  return `<div class="field"><label for="cost-${key}">${label}</label><input id="cost-${key}" name="${key}" type="number" min="0" step="${step}" value="${Number(value)}"></div>`;
}
function textField(key, label, value, multiline = false) {
  return `<div class="field"><label for="${key}">${label}</label>${multiline ? `<textarea id="${key}" name="${key}">${esc(value)}</textarea>` : `<input id="${key}" name="${key}" value="${esc(value)}">`}</div>`;
}
function costEstimate(c) {
  const total = +c.price + +c.shippingCharged;
  const fees = +c.listingFee + total * (+c.transactionPct + +c.processingPct + +c.adsPct) / 100 + +c.processingFixed;
  return total - +c.production - +c.fulfillmentShipping - +c.refundsAllowance - +c.aiCost - fees;
}
function renderDetail() {
  const job = current(), el = $('#detail');
  if (!job) {
    el.innerHTML = `<div class="detail-hero"><span class="eyebrow">OWNER'S DESK</span><h2>Make something worth buying.</h2><p>A visible workflow for original artwork, clear costs, and decisions you control.</p></div>
      <div class="intro-grid"><div>01 / RESEARCH<small>Audience and evidence</small></div><div>02 / DESIGN<small>Brief and real art file</small></div><div>03 / INSPECT<small>Print and originality review</small></div><div>04 / DECIDE<small>Costs and approval</small></div></div><p class="hint" style="margin-top:26px">Create a job to start. There are no sample workers or simulated sales in this view.</p>`;
    return;
  }
  const c = job.costs, estimate = costEstimate(c);
  el.innerHTML = `<div class="detail-hero"><span class="eyebrow">JOB / ${esc(job.id.slice(0, 8).toUpperCase())}</span><h2>${esc(job.title || 'Untitled idea')}</h2><p>Current station: <strong>${esc(stageNames[job.stage])}</strong> · Updated ${esc(fmt(job.updatedAt))}</p></div>
    <div class="detail-block"><h3>01 / PRODUCT IDEA</h3>
      ${textField('title', 'Working title', job.title)}${textField('audience', 'Who buys this?', job.audience)}${textField('product', 'Product / blank', job.product)}
      <button class="button small" id="save-job">SAVE DETAILS</button></div>
    <div class="detail-block"><h3>02 / RESEARCH</h3><p class="hint">Ollama can suggest hypotheses. Check live Etsy listings yourself and add links or evidence here.</p>
      ${textField('research', 'Evidence, sources and hypotheses', job.research, true)}<div class="row"><button class="button small" data-save="research">SAVE</button>${generator('research', 'BRAINSTORM')}</div></div>
    <div class="detail-block"><h3>03 / DESIGN</h3><p class="hint">A text brief is not a finished logo. Upload actual artwork before inspection. PNG, JPG or WebP; 7 MB maximum.</p>
      ${textField('designBrief', 'Original design brief', job.designBrief, true)}<div class="row"><button class="button small" data-save="designBrief">SAVE</button>${generator('design', 'DRAFT BRIEF')}</div>
      ${job.artwork ? `<img class="artwork" src="/assets/${esc(job.artwork.filename)}" alt="Uploaded design artwork"><p class="hint">Artwork: ${esc(job.artwork.name)}. Uploading a replacement resets QA and approval.</p>` : '<p class="hint error-hint">No artwork attached.</p>'}
      <input type="file" id="artwork" accept="image/png,image/jpeg,image/webp"></div>
    <div class="detail-block"><h3>04 / INSPECTION</h3><p class="hint">Check spelling, transparency, actual pixel dimensions, print placement, visual similarity to other marks, and the product mockup. This checkbox records your review; it does not certify legal clearance.</p>
      ${textField('qaNotes', 'Findings and fixes', job.qaNotes, true)}<label class="checkbox"><input id="qaPassed" type="checkbox" ${job.qaPassed ? 'checked' : ''}> I inspected this artwork and its print placement</label><button class="button small" data-save="qaNotes">SAVE QA</button></div>
    <div class="detail-block"><h3>05 / UNIT ECONOMICS</h3><div class="grid-2">
      ${numberField('price', 'Item price', c.price)}${numberField('shippingCharged', 'Shipping charged to buyer', c.shippingCharged)}
      ${numberField('production', 'Product + printing', c.production)}${numberField('fulfillmentShipping', 'Fulfillment shipping', c.fulfillmentShipping)}
      ${numberField('listingFee', 'Listing fee', c.listingFee)}${numberField('transactionPct', 'Transaction %', c.transactionPct)}
      ${numberField('processingPct', 'Processing %', c.processingPct)}${numberField('processingFixed', 'Processing fixed', c.processingFixed)}
      ${numberField('adsPct', 'Ads % (if applicable)', c.adsPct)}${numberField('refundsAllowance', 'Refunds allowance', c.refundsAllowance)}
      ${numberField('aiCost', 'AI cost allocation', c.aiCost)}</div>
      <div class="row" style="justify-content:space-between"><button class="button small" id="save-costs">SAVE COSTS</button><span id="margin" class="cost-total ${estimate < 0 ? 'negative' : ''}">${money(estimate)}</span></div><p class="hint">Estimated amount left per order before taxes and any costs you have not entered. Fees are editable assumptions.</p></div>
    ${job.stage === 'approval' ? `<div class="approval"><strong>YOUR DECISION</strong><p class="hint">Approval accepts the attached design for a listing draft. It does not publish, buy, or send anything.</p><div class="row"><button class="button primary" id="approve">APPROVE DESIGN</button><button class="button danger" id="reject">RETURN TO DESIGN</button></div></div>` : ''}
    <div class="detail-block"><h3>06 / LISTING DRAFT</h3><p class="hint">Available after design approval. Review every claim and disclose AI use and the production partner when applicable.</p>${textField('listingDraft', 'Title and description', job.listingDraft, true)}<div class="row"><button class="button small" data-save="listingDraft">SAVE</button>${job.stage === 'listing_draft' ? generator('listing', 'DRAFT LISTING') : ''}</div><p class="hint">There is no Etsy publishing connection yet.</p></div>
    <div class="detail-block"><h3>07 / CUSTOMER REPLY PAD</h3>${textField('customerDraft', 'Draft reply (manual copy only)', job.customerDraft, true)}<button class="button small" data-save="customerDraft">SAVE DRAFT</button></div>
    <div class="detail-block"><h3>ACTIVITY RECORD</h3><ul class="events">${job.events.map(event => `<li><time>${esc(fmt(event.at))}</time>${esc(event.action)}</li>`).join('')}</ul></div>
    <div class="row"><button id="advance" class="button primary" ${['approval', 'listing_draft'].includes(job.stage) ? 'disabled' : ''}>MOVE TO NEXT STATION →</button></div>`;

  const save = keys => act(async () => { const values = Object.fromEntries(keys.map(k => [k, $(`#${k}`).value])); if (keys.includes('qaNotes')) values.qaPassed = $('#qaPassed').checked; await api(`/api/jobs/${job.id}`, 'PATCH', values); toast('Saved locally'); });
  $('#save-job').onclick = () => save(['title', 'audience', 'product']);
  el.querySelectorAll('[data-save]').forEach(button => button.onclick = () => save([button.dataset.save]));
  $('#save-costs').onclick = () => act(async () => { const costs = Object.fromEntries(Object.keys(c).map(key => [key, Number($(`#cost-${key}`).value)])); await api(`/api/jobs/${job.id}`, 'PATCH', { costs }); toast('Costs saved'); });
  el.querySelectorAll('input[id^="cost-"]').forEach(input => input.addEventListener('input', () => {
    const values = Object.fromEntries(Object.keys(c).map(key => [key, Number($(`#cost-${key}`).value) || 0]));
    const value = costEstimate(values); $('#margin').textContent = money(value); $('#margin').classList.toggle('negative', value < 0);
  }));
  $('#advance').onclick = () => act(async () => { await api(`/api/jobs/${job.id}/advance`, 'POST'); toast('Job moved'); });
  if ($('#approve')) $('#approve').onclick = () => act(async () => { await api(`/api/jobs/${job.id}/approve`, 'POST'); toast('Artwork approved for draft'); });
  if ($('#reject')) $('#reject').onclick = () => act(async () => { await api(`/api/jobs/${job.id}/reject`, 'POST'); toast('Returned to design'); });
  el.querySelectorAll('[data-generate]').forEach(button => button.onclick = () => act(async () => {
    const task = button.dataset.generate;
    button.textContent = 'WORKING…';
    await api(`/api/jobs/${job.id}/generate`, 'POST', { task, model: $('#model-picker').value });
    toast(`${task} draft saved`);
  }));
  $('#artwork').onchange = event => act(async () => {
    const file = event.target.files[0]; if (!file) return;
    if (file.size > 7_000_000) throw new Error('Image must be under 7 MB');
    const data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
    await api(`/api/jobs/${job.id}/artwork`, 'POST', { data, name: file.name }); toast('Artwork attached; QA reset');
  });
}
function generator(task, label) {
  const online = $('#ollama-status').classList.contains('on');
  const models = window.ollamaModels || [];
  return online && models.length ? `<select id="model-picker" aria-label="Ollama model">${models.map(name => `<option ${name === model ? 'selected' : ''}>${esc(name)}</option>`).join('')}</select><button class="button small" data-generate="${task}">${label}</button>` : '';
}
function render() { renderQueue(); renderDetail(); $('#map-caption').textContent = filter ? `Showing ${stageNames[filter]} jobs · click again to clear` : `${state.jobs.length} actual job${state.jobs.length === 1 ? '' : 's'} in the hive`; }

// The canvas is a view of persisted jobs. Bees appear only for real jobs and move only within their station.
function rect(x, y, w, h, color) { ctx.fillStyle = color; ctx.fillRect(Math.round(x), Math.round(y), Math.round(w), Math.round(h)); }
function label(x, y, text, color = '#e5d9a8') { ctx.fillStyle = color; ctx.font = 'bold 6px monospace'; ctx.textAlign = 'center'; ctx.fillText(text, x, y); }
function building(s, count, t) {
  const { x, y, kind } = s;
  rect(x - 35, y + 21, 70, 4, '#19291f');
  if (kind === 'tent') {
    rect(x - 22, y - 3, 44, 24, '#8e6845'); rect(x - 26, y + 2, 52, 4, '#c09560');
    for (let i = 0; i < 8; i++) rect(x - 25 + i * 7, y + 1 - i * 2.2, 7, 4, '#b7824f');
    rect(x - 6, y + 9, 12, 12, '#33291c');
  } else if (kind === 'tower') {
    rect(x - 19, y - 12, 38, 34, '#706f4b'); rect(x - 24, y - 16, 48, 7, '#a39565');
    for (let i = 0; i < 5; i++) rect(x - 23 + i * 10, y - 21, 6, 6, '#b6a67c');
    rect(x - 5, y - 4, 10, 12, '#242e23'); rect(x - 3, y - 2, 6, 8, '#eac273');
  } else if (kind === 'office') {
    rect(x - 24, y - 6, 48, 28, '#926d51'); rect(x - 28, y - 13, 56, 9, '#766044');
    rect(x - 5, y + 6, 10, 16, '#293725'); rect(x - 19, y + 1, 9, 8, '#e6ba75'); rect(x + 10, y + 1, 9, 8, '#e6ba75');
    rect(x - 1, y - 21, 3, 8, '#d3a361');
  } else if (kind === 'workshop') {
    rect(x - 27, y - 3, 54, 25, '#6f6b51'); rect(x - 31, y - 10, 62, 8, '#965c47');
    rect(x - 21, y + 3, 13, 12, '#ddbb76'); rect(x + 7, y + 2, 11, 20, '#2b3124');
    rect(x + 13, y - 21, 7, 12, '#625d4a');
  } else if (kind === 'ledger') {
    rect(x - 24, y - 5, 48, 25, '#605b43'); rect(x - 27, y - 9, 54, 6, '#a08756');
    rect(x - 14, y + 3, 28, 9, '#c9ab69'); rect(x - 8, y + 5, 16, 2, '#64553e');
  } else {
    rect(x - 24, y - 4, 48, 24, '#696850'); rect(x - 27, y - 9, 54, 7, '#927d5c');
    rect(x - 15, y + 3, 30, 11, '#cdb277'); rect(x - 10, y + 6, 20, 2, '#746449');
    rect(x + 20, y - 21, 3, 14, '#eac77e');
  }
  if (count > 0) { rect(x + 22, y - 27, 15, 11, '#eac77e'); label(x + 29, y - 19, String(count), '#263725'); }
  label(x, y + 34, s.name, filter === s.stage ? '#f4d587' : '#d8d1ad');
  if (count && !['approval', 'listing_draft'].includes(s.stage)) {
    for (let i = 0; i < Math.min(count, 3); i++) {
      const bx = x - 13 + i * 11 + Math.sin(t / 850 + i * 2) * 2;
      const by = y + 16 + Math.sin(t / 250 + i) * 2;
      rect(bx - 3, by, 7, 4, '#e5bf6d'); rect(bx - 1, by - 2, 2, 2, '#d9e9c1'); rect(bx + 3, by + 1, 1, 1, '#222a1d');
    }
  }
}
function draw(t) {
  rect(0, 0, 440, 215, '#1b3028');
  for (let y = 0; y < 215; y += 10) for (let x = 0; x < 440; x += 10) {
    let n = (x * 37 + y * 73) % 11;
    rect(x, y, 10, 10, n < 4 ? '#20382b' : n < 8 ? '#233b2f' : '#274032');
    if (n === 1) rect(x + 3, y + 4, 2, 2, '#3b5838');
  }
  ctx.strokeStyle = '#857955'; ctx.lineWidth = 9; ctx.beginPath(); ctx.moveTo(5, 117); ctx.lineTo(380, 115); ctx.moveTo(180, 118); ctx.lineTo(185, 175); ctx.moveTo(335, 117); ctx.lineTo(339, 175); ctx.stroke();
  ctx.strokeStyle = '#ad9964'; ctx.lineWidth = 5; ctx.stroke();
  for (let i = 0; i < 13; i++) { const x = (i * 109 + 28) % 440, y = (i * 67 + 21) % 210; rect(x, y, 5, 8, '#14271f'); rect(x - 3, y - 4, 11, 6, '#426342'); rect(x - 1, y - 7, 7, 5, '#527247'); }
  for (const station of stations) building(station, state.jobs.filter(j => j.stage === station.stage).length, t);
  label(50, 190, `IDEA TRAY: ${state.jobs.filter(j => j.stage === 'idea').length}`, '#eac77e');
  requestAnimationFrame(draw);
}
canvas.addEventListener('click', event => {
  const bounds = canvas.getBoundingClientRect(), x = (event.clientX - bounds.left) * canvas.width / bounds.width, y = (event.clientY - bounds.top) * canvas.height / bounds.height;
  const station = stations.find(s => Math.abs(s.x - x) < 38 && Math.abs(s.y - y) < 40);
  if (station) { filter = filter === station.stage ? null : station.stage; render(); }
});
$('#new-job').onclick = () => act(async () => {
  const job = await api('/api/jobs', 'POST', {}); selected = job.id; filter = null; toast('New job created');
});
async function init() {
  try { await refresh(); requestAnimationFrame(draw); const info = await api('/api/ollama'); window.ollamaModels = info.models; model = info.models[0] || '';
    $('#ollama-status').textContent = info.online ? `● OLLAMA ONLINE · ${info.models.length} MODELS` : '○ OLLAMA OFFLINE · MANUAL MODE';
    $('#ollama-status').classList.toggle('on', info.online); renderDetail();
  } catch (error) { toast(error.message, true); }
}
init();
