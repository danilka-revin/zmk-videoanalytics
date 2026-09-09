#!/usr/bin/env node
// =============================================================================
// ZMK Vision — регрессионный тест «чёрная карточка при РАБОТАЮЩЕМ транспорте».
//
// Чем отличается от preview-cascade-check.mjs
// -----------------------------------------------------------------------------
// В preview-cascade-check.mjs сигналинг go2rtc всегда падает — каскад видит
// явную ошибку и честно уходит на MJPEG. Но самый неприятный случай другой:
// СИГНАЛИНГ УСПЕШЕН. WebSocket открывается, WebRTC доходит до connected,
// приходит видеотрек — и ни одного кадра не декодируется (кодек, который
// браузер не умеет; сломанный ffmpeg-вариант; поток без ключевого кадра).
//
// Раньше карточка в этот момент объявляла себя live, снимок-подложка
// убирался, а health-check видел liveRef.current === true и принципиально не
// перезапускал транспорт. Результат — вечно чёрный прямоугольник, поверх
// которого исправно рисуются рамки детекции: «система определяет нарушения,
// но картинки нет».
//
// Здесь <video> намеренно НИКОГДА не начинает декодировать (readyState = 0,
// currentTime не растёт, totalVideoFrames = 0). Карточка обязана это заметить
// и всё равно дойти до живого MJPEG.
//
// Требования:
//   1. Запущенный API с включённой камерой, у которой есть живые кадры.
//      Парольная аутентификация выключена либо задан API_KEY.
//   2. В каталоге frontend:  npm i --no-save jsdom
//
// Запуск (из каталога frontend):
//   CID=cam_1 node scripts/preview-silent-track-check.mjs
//
// Коды выхода: 0 — картинка появилась, 1 — карточка осталась чёрной
// (регрессия), 2 — ошибка окружения.
// =============================================================================
import {createRequire} from 'node:module';

const API_BASE = process.env.API_BASE || 'http://127.0.0.1:8000';
const CID = process.env.CID || '';
if (!CID) { console.error('Задайте переменную CID (id камеры), например: CID=cam_1 node scripts/preview-silent-track-check.mjs'); process.exit(2); }

let JSDOM;
try { ({JSDOM} = await import('jsdom')); }
catch { console.error('jsdom не установлен. Выполните: npm i --no-save jsdom'); process.exit(2); }

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div><div id="preview-host"></div></body></html>', {
  url: API_BASE + '/', pretendToBeVisual: true,
});
const w = dom.window;

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

// --- «мёртвый» <video>: транспорт подключён, декодер молчит ------------------
// Именно это состояние и должен распознавать компонент.
Object.defineProperty(w.HTMLMediaElement.prototype, 'readyState', {configurable: true, get: () => 0});
Object.defineProperty(w.HTMLMediaElement.prototype, 'currentTime', {configurable: true, get: () => 0, set() {}});
w.HTMLMediaElement.prototype.getVideoPlaybackQuality = () => ({totalVideoFrames: 0, droppedVideoFrames: 0});
w.HTMLMediaElement.prototype.play = () => Promise.resolve();
w.HTMLMediaElement.prototype.pause = () => {};
w.HTMLMediaElement.prototype.load = () => {};

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

// --- УСПЕШНЫЙ сигналинг go2rtc ----------------------------------------------
// WebSocket открывается и отвечает корректным SDP-ответом, как настоящий
// go2rtc. Ошибок нет — единственный признак беды в том, что кадры не идут.
class FakeWebSocket {
  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
  constructor(url) {
    this.url = String(url); this.readyState = 0; this.binaryType = 'blob';
    setTimeout(() => { this.readyState = 1; try { this.onopen && this.onopen({}); } catch {} }, 15);
  }
  send(raw) {
    let msg = {};
    try { msg = JSON.parse(raw); } catch {}
    if (msg.type === 'webrtc/offer') {
      setTimeout(() => {
        try { this.onmessage && this.onmessage({data: JSON.stringify({type: 'webrtc/answer', value: 'v=0\r\n'})}); } catch {}
      }, 20);
    }
    // На запрос MSE отвечаем согласованным MIME и «сегментами», которые
    // декодер молча проглатывает, не выдавая ни одного кадра.
    if (msg.type === 'mse') {
      setTimeout(() => {
        try { this.onmessage && this.onmessage({data: JSON.stringify({type: 'mse', value: 'video/mp4; codecs="avc1.640029"'})}); } catch {}
        try { this.onmessage && this.onmessage({data: new Uint8Array(64).fill(7).buffer}); } catch {}
      }, 25);
    }
  }
  close() { this.readyState = 3; try { this.onclose && this.onclose({code: 1000}); } catch {} }
}
globalThis.WebSocket = FakeWebSocket;
w.WebSocket = FakeWebSocket;

// MSE «работает»: SourceBuffer принимает данные, но картинки от этого нет.
class FakeSourceBuffer {
  constructor() { this.updating = false; this._l = {}; }
  addEventListener(t, fn) { this._l[t] = fn; }
  removeEventListener() {}
  appendBuffer() {}
  remove() {}
}
class FakeMediaSource {
  static isTypeSupported() { return true; }
  constructor() { this.readyState = 'closed'; this.sourceBuffers = []; this._l = {};
    setTimeout(() => { this.readyState = 'open'; this._l.sourceopen && this._l.sourceopen(); }, 10); }
  addEventListener(t, fn) { this._l[t] = fn; }
  removeEventListener() {}
  addSourceBuffer() { const sb = new FakeSourceBuffer(); this.sourceBuffers.push(sb); return sb; }
  removeSourceBuffer() {}
  endOfStream() {}
}
globalThis.MediaSource = FakeMediaSource;
w.MediaSource = FakeMediaSource;

// WebRTC доходит до connected и отдаёт видеотрек — «всё хорошо», кроме картинки.
class FakeRTCRtpReceiver {
  static getCapabilities() { return {codecs: [{mimeType: 'video/H264'}, {mimeType: 'video/VP8'}]}; }
}
class FakeTrack {
  constructor() { this.kind = 'video'; this.id = 'silent'; }
  stop() {}
}
class FakeRTCPeerConnection {
  constructor() { this.connectionState = 'new'; this._tr = []; }
  addTransceiver() {
    const tr = {direction: 'recvonly', currentDirection: 'recvonly', receiver: {track: new FakeTrack()}, setCodecPreferences() {}};
    this._tr.push(tr); return tr;
  }
  getTransceivers() { return this._tr; }
  createOffer() { return Promise.resolve({type: 'offer', sdp: 'v=0\r\n'}); }
  setLocalDescription() { return Promise.resolve(); }
  get localDescription() { return {sdp: 'v=0\r\n'}; }
  setRemoteDescription() {
    this.connectionState = 'connected';
    setTimeout(() => {
      try { this.ontrack && this.ontrack({track: this._tr[0]?.receiver.track, streams: []}); } catch {}
      try { this.onconnectionstatechange && this.onconnectionstatechange(); } catch {}
    }, 10);
    return Promise.resolve();
  }
  addIceCandidate() { return Promise.resolve(); }
  close() { this.connectionState = 'closed'; }
}
class FakeMediaStream {
  constructor(tracks) { this._t = tracks ? [...tracks] : []; }
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
  previewResolution: '720', previewFps: '30', previewContentHint: 'motion',
}));

const t0 = Date.now();
const seen = new Set();
const result = await new Promise(resolve => {
  const timer = setInterval(() => {
    const html = host.innerHTML || '';
    // Картинка = <img>, а не <video>: <video> здесь принципиально пуст.
    const hasPicture = /<img[^>]+class="camera-snapshot/.test(html);
    seen.add(hasPicture ? 'PICTURE' : 'BLACK');
    if (hasPicture) { clearInterval(timer); resolve({ok: true, ms: Date.now() - t0, html}); }
    else if (Date.now() - t0 > 45000) { clearInterval(timer); resolve({ok: false, ms: Date.now() - t0, html}); }
  }, 200);
});

console.log('состояния карточки:', [...seen].join(' → '));
if (result.ok) {
  console.log(`OK: картинка появилась через ${result.ms} мс, хотя транспорт «подключился» и не декодировал ни кадра`);
  process.exit(0);
}
console.log(`FAIL: карточка осталась чёрной ${result.ms} мс — «молчащий» транспорт снова считается живым`);
console.log('HTML карточки:', result.html.replace(/\s+/g, ' ').slice(0, 400));
process.exit(1);
