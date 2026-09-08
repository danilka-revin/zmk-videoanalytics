#!/usr/bin/env node
// =============================================================================
// ZMK Vision — проверка каскада превью камеры (WebRTC → MSE → HLS → MJPEG).
//
// Воспроизводит браузер без H.264-декодера (Firefox на Ubuntu), у которого
// недоступен go2rtc-сигналинг: WebSocket /rtc/api/ws всегда падает, WebRTC
// никогда не соединяется. В таком режиме карточка камеры ОБЯЗАНА дойти до
// живого MJPEG и показать картинку — это регрессионный тест бага
// «система определяет нарушения, но картинки в карточке нет».
//
// Требования:
//   1. Запущенный API (например, docker compose up api) с хотя бы одной
//      включённой камерой, у которой есть живые кадры (работающий inference
//      worker или симуляция live-frame). Парольная аутентификация должна быть
//      выключена либо передайте API-ключ через переменную окружения API_KEY.
//   2. В каталоге frontend:  npm i --no-save jsdom
//
// Запуск (из каталога frontend):
//   CID=cam_1 node scripts/preview-cascade-check.mjs
//
// Коды выхода: 0 — картинка появилась (каскад работает), 1 — карточка
// осталась пустой/тёмной (регрессия), 2 — ошибка окружения.
// =============================================================================
import {createRequire} from 'node:module';

const API_BASE = process.env.API_BASE || 'http://127.0.0.1:8000';
const CID = process.env.CID || '';
if (!CID) { console.error('Задайте переменную CID (id камеры), например: CID=cam_1 node scripts/preview-cascade-check.mjs'); process.exit(2); }

let JSDOM;
try { ({JSDOM} = await import('jsdom')); }
catch { console.error('jsdom не установлен. Выполните: npm i --no-save jsdom'); process.exit(2); }

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div><div id="preview-host"></div></body></html>', {
  url: API_BASE + '/', pretendToBeVisual: true,
});
const w = dom.window;

// --- глобалы, которые ждёт компонент в Node-контексте -----------------------
globalThis.window = w;
globalThis.document = w.document;
globalThis.location = w.location;
globalThis.localStorage = w.localStorage;
globalThis.getComputedStyle = w.getComputedStyle.bind(w);
w.matchMedia = q => ({matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}});
globalThis.matchMedia = w.matchMedia.bind(w);
globalThis.requestAnimationFrame = cb => setTimeout(cb, 16);
globalThis.cancelAnimationFrame = id => clearTimeout(id);
for (const name of ['HTMLElement', 'Element', 'Node', 'SVGElement', 'DocumentFragment', 'Event', 'CustomEvent', 'KeyboardEvent', 'MouseEvent'])
  globalThis[name] = w[name];

let blobSeq = 0;
w.URL.createObjectURL = () => `blob:mock-${++blobSeq}`;
w.URL.revokeObjectURL = () => {};

const apiKey = process.env.API_KEY || '';
const realFetch = globalThis.fetch.bind(globalThis);
const wrappedFetch = (input, init) => {
  const url = typeof input === 'string' && input.startsWith('/') ? API_BASE + input : input;
  const next = init ? {...init} : {};
  if (apiKey) next.headers = {...(init && init.headers), 'X-API-Key': apiKey};
  return realFetch(url, next);
};
w.fetch = wrappedFetch;
globalThis.fetch = wrappedFetch;

// Сигналинг go2rtc всегда недоступен — ключевой элемент сценария.
class FakeWebSocket {
  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
  constructor(url) { this.url = String(url); this.readyState = 0; this.binaryType = 'blob';
    setTimeout(() => { this.readyState = 3;
      try { this.onerror && this.onerror({}); } catch {}
      try { this.onclose && this.onclose({code: 1006}); } catch {}
    }, 25);
  }
  send() {} close() { this.readyState = 3; }
}
globalThis.WebSocket = FakeWebSocket;
w.WebSocket = FakeWebSocket;

// Браузер без H.264 (Firefox/Ubuntu): MSE недоступен, WebRTC не соединяется.
class FakeRTCRtpReceiver {
  static getCapabilities() { return {codecs: [{mimeType: 'video/VP8'}, {mimeType: 'video/VP9'}, {mimeType: 'video/rtx'}, {mimeType: 'video/ulpfec'}]}; }
}
class FakeRTCPeerConnection {
  constructor() { this.connectionState = 'new'; }
  addTransceiver(kind, init) { return {direction: (init && init.direction) || 'sendrecv', currentDirection: undefined, receiver: {track: null}, setCodecPreferences() {}}; }
  getTransceivers() { return []; }
  createOffer() { return Promise.resolve({type: 'offer', sdp: 'v=0\r\n'}); }
  setLocalDescription() {
    this.connectionState = 'checking';
    setTimeout(() => { this.connectionState = 'failed';
      try { this.onconnectionstatechange && this.onconnectionstatechange(); } catch {} }, 120);
    return Promise.resolve();
  }
  get localDescription() { return {sdp: 'v=0\r\n'}; }
  setRemoteDescription() { return Promise.resolve(); }
  addIceCandidate() { return Promise.resolve(); }
  close() { this.connectionState = 'closed'; }
}
class FakeMediaStream {
  constructor() { this._t = []; }
  addTrack(t) { this._t.push(t); }
  getVideoTracks() { return this._t; }
  getTracks() { return this._t; }
}
globalThis.RTCRtpReceiver = FakeRTCRtpReceiver;
globalThis.RTCPeerConnection = FakeRTCPeerConnection;
globalThis.MediaStream = FakeMediaStream;
w.RTCPeerConnection = FakeRTCPeerConnection;
w.MediaStream = FakeMediaStream;

// --- загрузка настоящего компонента через vite -------------------------------
const req = createRequire(new URL('../package.json', import.meta.url));
const {createServer} = req('vite');
const vite = await createServer({
  root: new URL('..', import.meta.url).pathname,
  server: {middlewareMode: true}, appType: 'custom', logLevel: 'silent',
  ssr: {external: ['react', 'react-dom', 'react-dom/client']},
});
const mod = await vite.ssrLoadModule('/src/main.tsx');
if (typeof mod.CameraPreview !== 'function') { console.error('FAIL: CameraPreview не экспортирован из src/main.tsx'); process.exit(1); }
const React = req('react');
const {createRoot} = req('react-dom/client');

const host = document.getElementById('preview-host');
createRoot(host).render(React.createElement(mod.CameraPreview, {
  id: CID, status: 'online', age: 1, telemetryStale: false, previewMode: 'auto',
}));

const t0 = Date.now();
const seen = new Set();
const result = await new Promise(resolve => {
  const timer = setInterval(() => {
    const html = host.innerHTML || '';
    const hasPicture = /<img[^>]+class="camera-snapshot/.test(html);
    const mjpegLive = html.includes('● MJPEG') && !html.includes('MJPEG…');
    seen.add(mjpegLive ? 'MJPEGLIVE' : hasPicture ? 'PICTURE' : 'EMPTY');
    if (mjpegLive && hasPicture) { clearInterval(timer); resolve({ok: true, ms: Date.now() - t0, html}); }
    else if (Date.now() - t0 > 30000) { clearInterval(timer); resolve({ok: false, ms: Date.now() - t0, html}); }
  }, 200);
});

console.log('состояния карточки:', [...seen].join(' → '));
if (result.ok) {
  console.log(`OK: живая MJPEG-картинка через ${result.ms} мс — каскад превью работает`);
  process.exit(0);
}
console.log(`FAIL: картинка не появилась за ${result.ms} мс — каскад превью сломан`);
console.log('HTML карточки:', result.html.replace(/\s+/g, ' ').slice(0, 400));
process.exit(1);
