const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const http = require('node:http');
const child = require('node:child_process');
const { once } = require('node:events');

function hash(bytes) { return crypto.createHash('sha256').update(bytes).digest('hex'); }
function write(file, value) { fs.writeFileSync(file, JSON.stringify(value, null, 2) + '\n'); }
async function delay(ms) { await new Promise((resolve) => setTimeout(resolve, ms)); }

async function browserProbe(chrome, directory, url) {
  const profile = path.join(directory, 'chrome-profile');
  fs.mkdirSync(profile);
  const err = fs.openSync(path.join(directory, 'chrome.log'), 'w');
  const process = child.spawn(chrome, ['--headless', '--no-sandbox', '--disable-gpu', '--remote-debugging-port=0',
    '--no-first-run', '--user-data-dir=' + profile, 'about:blank'], { stdio: ['ignore', 'ignore', err] });
  let launchError;
  process.once('error', (error) => { launchError = error; });
  let socket;
  try {
    const active = path.join(profile, 'DevToolsActivePort');
    for (let i = 0; i < 600 && !fs.existsSync(active); i++) {
      if (launchError) throw launchError;
      if (process.exitCode !== null) throw new Error('Headless Chrome exited before DevTools');
      await delay(50);
    }
    assert.ok(fs.existsSync(active), 'Chrome did not expose DevTools');
    const port = fs.readFileSync(active, 'utf8').trim().split('\n')[0];
    const target = await (await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' })).json();
    socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
    let next = 0;
    const pending = new Map();
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) {
        const { resolve, reject, timer } = pending.get(message.id);
        pending.delete(message.id); clearTimeout(timer);
        if (message.error) reject(new Error(JSON.stringify(message.error))); else resolve(message.result);
      }
    });
    const call = (method, params) => new Promise((resolve, reject) => {
      const id = ++next;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('DevTools command timed out: ' + method)); }, 120000);
      pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }));
    });
    const runtime = await call('Browser.getVersion', {});
    await call('Runtime.enable', {});
    const response = await call('Runtime.evaluate', {
      expression: `(async()=>{while(!window.issue5538Done)await new Promise(r=>setTimeout(r,25));return await window.issue5538Done;})()`,
      awaitPromise: true, returnByValue: true,
    });
    assert.ok(!response.exceptionDetails, JSON.stringify(response.exceptionDetails));
    const value = response.result.value;
    assert.ok(value.ok, JSON.stringify(value));
    assert.equal(value.result.measurementComplete, true);
    return { observations: value.result, runtime };
  } finally {
    socket?.close();
    process.kill('SIGTERM');
    if (process.exitCode === null && !launchError) await Promise.race([once(process, 'exit'), delay(5000)]);
    if (process.exitCode === null) process.kill('SIGKILL');
    fs.closeSync(err);
    fs.rmSync(profile, { recursive: true, force: true });
  }
}

async function main() {
  assert.equal(process.env.CI, 'true', 'CI=true is required');
  assert.ok(Number(process.versions.node.split('.')[0]) >= 22, 'Node 22+ with native WebSocket is required');
  const options = {};
  for (let i = 2; i < process.argv.length; i += 2) options[process.argv[i].replace(/^--/, '')] = process.argv[i + 1];
  const root = path.resolve(options.repo);
  const upstreamCommit = child.execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
  assert.equal(upstreamCommit, '683df93e986af19bbcefd22606a417083116847b', 'Upstream source identity changed');
  child.execFileSync('git', ['diff', '--exit-code'], { cwd: root, encoding: 'utf8' });
  const output = path.resolve(options.output);
  assert.ok(!fs.existsSync(output), 'Output must be new');
  fs.mkdirSync(output, { recursive: true });
  const workerFile = path.join(root, 'packages/pdfrx/assets/pdfium_worker.js');
  const worker = fs.readFileSync(workerFile, 'utf8');
  const audit = fs.readFileSync(path.join(__dirname, 'internal_upstream_audit.js'), 'utf8');
  const wasm = fs.readFileSync(path.join(root, 'packages/pdfrx/assets/pdfium.wasm'));
  const encrypted = fs.readFileSync(path.join(root, 'packages/pdfrx_engine/test/assets/encrypted.pdf'));
  const corpus = fs.readFileSync(options.corpus);
  const requests = [];
  const server = http.createServer((request, response) => {
    const requestPath = request.url.split('?')[0];
    requests.push({ path: requestPath, method: request.method, range: request.headers.range ?? null });
    if (requestPath === '/probe.html') {
      response.setHeader('Content-Type', 'text/html');
      response.end(`<script>window.issue5538Done=new Promise((resolve,reject)=>{const w=new Worker('/worker.js');w.onmessage=e=>{resolve(e.data);w.terminate()};w.onerror=e=>reject(new Error(e.message));w.postMessage({})});</script>`);
    } else if (requestPath === '/worker.js') {
      response.setHeader('Content-Type', 'application/javascript');
      response.end(worker + '\n' + audit + `\nonmessage=async()=>{try{const inputs={wasm:await(await fetch('/pdfium.wasm')).arrayBuffer(),encrypted:await(await fetch('/encrypted.pdf')).arrayBuffer(),corpus:await(await fetch('/corpus.pdf')).arrayBuffer(),encryptedUrl:new URL('/encrypted.pdf',self.location.href).href,corpusUrl:new URL('/corpus.pdf',self.location.href).href};postMessage({ok:true,result:await issue5538Audit(inputs)});}catch(e){postMessage({ok:false,error:String(e),stack:e.stack});}};`);
    } else {
      const data = requestPath === '/pdfium.wasm' ? wasm : requestPath === '/encrypted.pdf' ? encrypted : requestPath === '/corpus.pdf' ? corpus : null;
      if (!data) { response.statusCode = 404; response.end(); return; }
      response.setHeader('Content-Type', requestPath.endsWith('.wasm') ? 'application/wasm' : 'application/pdf');
      response.setHeader('Content-Length', data.length);
      response.end(data);
    }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const context = { self: { location: { href: base + '/worker.js' } }, console: { log() {}, warn() {}, error() {} },
      setTimeout, clearTimeout, TextEncoder, TextDecoder, Uint8Array, Float32Array, ArrayBuffer, AbortController,
      performance, onmessage: null, postMessage() {}, fetch,
      input: { wasm, encrypted: Uint8Array.from(encrypted).buffer, corpus: Uint8Array.from(corpus).buffer,
        encryptedUrl: base + '/encrypted.pdf', corpusUrl: base + '/corpus.pdf' } };
    vm.createContext(context);
    vm.runInContext(worker + '\n' + audit, context);
    const nodeResult = await vm.runInContext('issue5538Audit(input)', context);
    assert.equal(nodeResult.measurementComplete, true);
    write(path.join(output, 'node.json'), nodeResult);
    const browserResult = options.chrome ? await browserProbe(options.chrome, output, base + '/probe.html') : null;
    if (browserResult) write(path.join(output, 'chrome.json'), browserResult.observations);
    else assert.equal(options['node-only-validation'], 'true', 'Chrome is required outside explicit local validation');
    write(path.join(output, 'provenance.json'), { node: process.version, workerSha256: hash(worker), auditSha256: hash(audit),
      wasmSha256: hash(wasm), encryptedSha256: hash(encrypted), corpusSha256: hash(corpus), corpusBytes: corpus.length,
      browserExecuted: !!browserResult, browserRuntime: browserResult?.runtime ?? null, upstreamCommit, requests });
    write(path.join(output, 'summary.json'), { measurementComplete: true, sections: ['R01', 'S15'],
      runtimes: browserResult ? ['node', 'chrome-worker'] : ['node'], localValidationOnly: !browserResult });
  } catch (error) {
    write(path.join(output, 'failure.json'), { measurementComplete: false, error: String(error), stack: error.stack });
    throw error;
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
