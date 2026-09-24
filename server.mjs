import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import crypto from 'node:crypto';

const root = path.dirname(fileURLToPath(import.meta.url));
const dataDir = process.env.HIVE_MINT_DATA_DIR || (process.platform === 'win32'
  ? path.join(process.env.APPDATA || os.homedir(), 'HiveMint')
  : path.join(os.homedir(), '.hive-mint'));
const dataFile = path.join(dataDir, 'jobs.json');
const assetDir = path.join(dataDir, 'assets');
const port = Number(process.env.PORT || 3741);
const ollama = process.env.OLLAMA_URL || 'http://127.0.0.1:11434';
const stages = ['idea', 'research', 'design', 'qa', 'cost', 'approval', 'listing_draft'];
const mime = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8' };
let db = { version: 1, jobs: [] };
let writeQueue = Promise.resolve();

export function margin(job) {
  const c = job.costs;
  const total = Number(c.price) + Number(c.shippingCharged);
  const fees = Number(c.listingFee) + total * (Number(c.transactionPct) + Number(c.processingPct) + Number(c.adsPct)) / 100 + Number(c.processingFixed);
  return Math.round((total - Number(c.production) - Number(c.fulfillmentShipping) - Number(c.refundsAllowance) - Number(c.aiCost) - fees) * 100) / 100;
}

function log(job, action) { job.events.unshift({ at: new Date().toISOString(), action }); }
function save() {
  writeQueue = writeQueue.catch(() => {}).then(async () => {
    await fs.mkdir(dataDir, { recursive: true });
    const temp = `${dataFile}.${process.pid}.tmp`;
    await fs.writeFile(temp, JSON.stringify(db, null, 2));
    await fs.rename(temp, dataFile);
  });
  return writeQueue;
}
function send(res, status, value, type = 'application/json; charset=utf-8') {
  res.writeHead(status, { 'content-type': type, 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' });
  res.end(type.startsWith('application/json') ? JSON.stringify(value) : value);
}
async function body(req, limit = 10_000_000) {
  let chunks = [], size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw Object.assign(new Error('Request too large'), { status: 413 });
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw Object.assign(new Error('Invalid JSON'), { status: 400 }); }
}
function bad(message, status = 400) { throw Object.assign(new Error(message), { status }); }
function jobById(id) { const job = db.jobs.find(j => j.id === id); if (!job) bad('Job not found', 404); return job; }
function checkOrigin(req) {
  const origin = req.headers.origin;
  if (origin && new URL(origin).host !== req.headers.host) bad('Origin rejected', 403);
}
const editable = ['title', 'audience', 'product', 'research', 'designBrief', 'qaNotes', 'listingDraft', 'customerDraft'];
function update(job, input) {
  const designChanged = ['title', 'audience', 'product', 'designBrief'].some(key => key in input && input[key] !== job[key]);
  for (const key of editable) if (key in input) {
    if (typeof input[key] !== 'string' || input[key].length > 20000) bad(`Invalid ${key}`);
    job[key] = input[key].trim();
  }
  if (designChanged && stages.indexOf(job.stage) >= stages.indexOf('qa')) {
    job.stage = 'design'; job.qaPassed = false; job.approvedAt = null;
    log(job, 'Design details changed; returned to design and reset QA');
  }
  if ('qaPassed' in input) job.qaPassed = input.qaPassed === true;
  if ('costs' in input) {
    if (!input.costs || typeof input.costs !== 'object' || Array.isArray(input.costs)) bad('Invalid costs');
    for (const [key, value] of Object.entries(input.costs)) {
      if (!(key in job.costs) || typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 100000) bad(`Invalid cost ${key}`);
      job.costs[key] = value;
    }
  }
  job.updatedAt = new Date().toISOString();
  log(job, 'Saved job details');
}
function advance(job) {
  const current = job.stage;
  const checks = {
    idea: [job.title, job.audience, job.product],
    research: [job.research],
    design: [job.designBrief, job.artwork],
    qa: [job.qaPassed],
    cost: [job.costs.price > 0, job.costs.production > 0],
  };
  if (current === 'approval' || current === 'listing_draft') bad('Use the approval action for this stage');
  if (!checks[current].every(Boolean)) bad({ idea: 'Add a title, audience, and product', research: 'Add research notes', design: 'Add a design brief and upload artwork', qa: 'Mark QA as passed', cost: 'Enter a sale price and production cost' }[current]);
  job.stage = stages[stages.indexOf(current) + 1];
  job.updatedAt = new Date().toISOString();
  log(job, `Moved to ${job.stage}`);
}
function promptFor(job, task) {
  const context = `Product: ${job.product}\nAudience: ${job.audience}\nWorking title: ${job.title}\nResearch notes: ${job.research}\nDesign brief: ${job.designBrief}`;
  if (task === 'research') return `Act as a product research assistant. Brainstorm three specific customer motivations, three search phrase hypotheses, and three things to verify on live Etsy listings for this idea. You have NO live web access. Do not fabricate citations, sales figures, or trademark clearance. Label all claims as hypotheses.\n\n${context}`;
  if (task === 'design') return `Write a concise, original design brief for an imaginary emblem that appeals to this audience. Specify composition, typography to set manually, colors, print size, and what to avoid. Do not imitate a known mark or claim trademark clearance. This is a brief, not a finished image.\n\n${context}`;
  if (task === 'listing') return `Draft an Etsy listing title and description for this seller-directed design. Do not invent material, shipping, production, or sizing claims. Include a placeholder for the required AI-use disclosure and production partner disclosure. Do not claim the design is trademark-cleared.\n\n${context}`;
  bad('Unknown task');
}
async function generate(job, input) {
  if (!['research', 'design', 'listing'].includes(input.task)) bad('Unknown generation task');
  if (input.task === 'listing' && job.stage !== 'listing_draft') bad('Approve the design first');
  const model = typeof input.model === 'string' ? input.model.trim() : '';
  if (!/^[\w.:/-]{1,100}$/.test(model)) bad('Select an Ollama model');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120000);
  try {
    const response = await fetch(new URL('/api/chat', ollama), { method: 'POST', signal: controller.signal,
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ model, stream: false, messages: [{ role: 'user', content: promptFor(job, input.task) }], options: { num_predict: 1100 } }) });
    if (!response.ok) bad(`Ollama returned ${response.status}`, 502);
    const result = await response.json();
    const output = result.message?.content?.trim();
    if (!output) bad('Ollama returned no text', 502);
    const field = { research: 'research', design: 'designBrief', listing: 'listingDraft' }[input.task];
    job[field] = output;
    job.updatedAt = new Date().toISOString();
    log(job, `Generated ${input.task} draft with ${model}`);
    await save();
    return job;
  } finally { clearTimeout(timer); }
}
async function route(req, res) {
  const url = new URL(req.url, 'http://localhost');
  const method = req.method;
  if (!['GET', 'HEAD'].includes(method)) checkOrigin(req);
  if (url.pathname === '/api/state' && method === 'GET') return send(res, 200, db);
  if (url.pathname === '/api/ollama' && method === 'GET') {
    try {
      const response = await fetch(new URL('/api/tags', ollama), { signal: AbortSignal.timeout(2500) });
      if (!response.ok) throw new Error('Ollama unavailable');
      const models = (await response.json()).models?.map(m => m.name) || [];
      return send(res, 200, { online: true, models });
    } catch { return send(res, 200, { online: false, models: [] }); }
  }
  if (url.pathname === '/api/jobs' && method === 'POST') {
    const input = await body(req);
    const job = { id: crypto.randomUUID(), title: '', audience: '', product: '', stage: 'idea', research: '', designBrief: '', qaNotes: '', qaPassed: false,
      artwork: null, listingDraft: '', customerDraft: '', approvedAt: null, costs: { price: 0, shippingCharged: 0, production: 0, fulfillmentShipping: 0, listingFee: 0.2, transactionPct: 6.5, processingPct: 3, processingFixed: 0.25, adsPct: 0, refundsAllowance: 0, aiCost: 0 },
      events: [], createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() };
    update(job, input); db.jobs.unshift(job); log(job, 'Created job'); await save(); return send(res, 201, job);
  }
  const match = url.pathname.match(/^\/api\/jobs\/([a-f0-9-]+)(?:\/(advance|approve|reject|generate|artwork))?$/);
  if (match) {
    const job = jobById(match[1]), action = match[2];
    if (!action && method === 'PATCH') { update(job, await body(req)); await save(); return send(res, 200, job); }
    if (action === 'advance' && method === 'POST') { advance(job); await save(); return send(res, 200, job); }
    if (action === 'approve' && method === 'POST') {
      if (job.stage !== 'approval') bad('Job is not awaiting approval');
      job.approvedAt = new Date().toISOString(); job.stage = 'listing_draft'; job.updatedAt = job.approvedAt;
      log(job, 'Owner approved artwork for listing draft'); await save(); return send(res, 200, job);
    }
    if (action === 'reject' && method === 'POST') {
      if (job.stage !== 'approval') bad('Job is not awaiting approval');
      job.stage = 'design'; job.qaPassed = false; job.updatedAt = new Date().toISOString();
      log(job, 'Owner returned artwork to design'); await save(); return send(res, 200, job);
    }
    if (action === 'generate' && method === 'POST') return send(res, 200, await generate(job, await body(req)));
    if (action === 'artwork' && method === 'POST') {
      const input = await body(req);
      const found = /^data:(image\/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)$/.exec(input.data || '');
      if (!found) bad('Upload a PNG, JPG, or WebP image');
      const buffer = Buffer.from(found[2], 'base64');
      if (!buffer.length || buffer.length > 7_000_000) bad('Image must be under 7 MB');
      const valid = found[1] === 'image/png' ? buffer.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex')) :
        found[1] === 'image/jpeg' ? buffer.subarray(0, 3).equals(Buffer.from('ffd8ff', 'hex')) :
        buffer.subarray(0, 4).toString() === 'RIFF' && buffer.subarray(8, 12).toString() === 'WEBP';
      if (!valid) bad('Image contents do not match the selected format');
      await fs.mkdir(assetDir, { recursive: true });
      const ext = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp' }[found[1]];
      const filename = `${job.id}-${crypto.randomUUID()}.${ext}`;
      await fs.writeFile(path.join(assetDir, filename), buffer);
      job.artwork = { filename, name: String(input.name || 'Artwork').slice(0, 120), type: found[1] };
      job.qaPassed = false; job.approvedAt = null; job.updatedAt = new Date().toISOString();
      if (stages.indexOf(job.stage) > stages.indexOf('design')) job.stage = 'design';
      log(job, 'Uploaded artwork; QA must be repeated'); await save(); return send(res, 200, job);
    }
  }
  if (url.pathname.startsWith('/assets/') && method === 'GET') {
    const filename = path.basename(url.pathname);
    if (!db.jobs.some(j => j.artwork?.filename === filename)) bad('Asset not found', 404);
    const ext = path.extname(filename).toLowerCase();
    const type = { '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp' }[ext];
    return send(res, 200, await fs.readFile(path.join(assetDir, filename)), type);
  }
  if (method === 'GET' && ['/', '/index.html', '/app.js', '/style.css'].includes(url.pathname)) {
    const filename = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    return send(res, 200, await fs.readFile(path.join(root, 'public', filename)), mime[path.extname(filename)]);
  }
  bad('Not found', 404);
}

export async function start(options = {}) {
  const file = options.dataFile || dataFile;
  if (options.dataFile) {
    // Tests use a separate process and override the data directory through the environment.
    throw new Error('Use HIVE_MINT_DATA_DIR for an isolated data directory');
  }
  await fs.mkdir(dataDir, { recursive: true });
  try { db = JSON.parse(await fs.readFile(file, 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const server = http.createServer((req, res) => route(req, res).catch(error => {
    if (error.name === 'AbortError') return send(res, 504, { error: 'Ollama timed out' });
    console.error(error);
    send(res, error.status || 500, { error: error.status ? error.message : 'Internal error' });
  }));
  await new Promise(resolve => server.listen(options.port ?? port, '127.0.0.1', resolve));
  return server;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  start().then(() => console.log(`Hive Mint is running at http://127.0.0.1:${port}`)).catch(error => { console.error(error); process.exitCode = 1; });
}
