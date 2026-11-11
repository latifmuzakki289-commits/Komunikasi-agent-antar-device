// ==UserScript==
// @name         Qwen Chat Token -> relay (auto-sync + bridge + template)
// @namespace    dsh-go
// @version      1.6.2
// @description  Push token + cookies chat.qwen.ai ke relay2.py, jadi jembatan eksekusi dari dalam tab, dan memakai bentuk request yang sama dengan app Qwen.
// @match        https://chat.qwen.ai/*
// @match        https://chat.qwenlm.ai/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==
//
// v1.6.0 - CHAT DIBUAT DULU, BODY DISAMAKAN DENGAN TANGKAPAN ASLI
// ------------------------------------------------------------------------
// Dua temuan dari request app yang tertangkap (dan dari bundle 0.3.40):
//
// 1. App membuat chat lebih dulu: POST /api/v2/chats/new
//      {chatId:"", models:[model], timestamp:<ms>, chat_type:"t2t", chat_mode:"normal"}
//    lalu memakai data.id sebagai chat_id. Mengirim pesan ke chat_id yang
//    tidak ada di akun membuat server gagal menyimpan -> "Internal error...".
//    Skrip ini sekarang selalu membuat chat baru per request.
//
// 2. Body tidak punya field "headers". Field itu ada di kode app tetapi
//    dipakai client-nya sendiri, tidak ikut terkirim. Field yang benar-benar
//    dikirim: stream, version, incremental_output, chatId, parentId, chat_id,
//    chat_mode, model, parent_id, messages, timestamp.
//
// Pesan juga membawa files:[], extra:{meta:{subChatType}}, sub_chat_type,
// models:[model], dan feature_config lengkap (auto_thinking, thinking_mode,
// auto_search) seperti tangkapan asli.
//
// Tanggung jawab lain tetap: session push + bridge (v1.2.0), pemilihan token
// berdasarkan exp (v1.3.0), dan sniffer di unsafeWindow (v1.5.0).

(function () {
  'use strict';
  if (window.__qwenRelayLoaded) return;
  window.__qwenRelayLoaded = true;

  const RELAY = 'http://127.0.0.1:20232/session';
  const BRIDGE = RELAY.replace(/\/session$/, '') + '/qwen/bridge';
  const PROVIDER = 'qwen';
  const HEARTBEAT = 45000;
  const TICK = 5000;
  const POLL = 1000;
  const MANUAL_KEY = 'qwen_relay_manual_token';
  const TEMPLATE_KEY = 'qwen_relay_template';
  const TEMPLATE_TTL = 7 * 24 * 3600 * 1000;
  const PING_MODEL = 'qwen3.7-plus';
  const CHAT_TYPE = 't2t';

  const JWT_RE = /eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/;

  const rid = () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });

  // App mengirim header Timezone berbentuk "Thu Nov 12 2026 00:12:34 GMT+0700"
  // (JS Date.toString() tanpa kurung zona).
  const tzString = () => {
    try { return new Date().toString().replace(/\s*\(.+\)$/, ''); } catch (e) { return ''; }
  };

  // Header "Version" WAJIB. Tanpa itu server Qwen membalas
  // "Bad_Request - Internal error..." untuk body yang sebenarnya benar.
  // Nilainya diambil dari request app yang tertangkap kalau ada, supaya
  // versi baru app otomatis terpakai.
  function appVersion(t) {
    const h = (t && t.headers) || {};
    for (const k of Object.keys(h)) {
      if (k.toLowerCase() === 'version' && h[k]) return String(h[k]);
    }
    return '0.3.40';
  }

  // ---- cari token ----
  const matchJwt = v => { const m = v && v.match(JWT_RE); return m ? m[0] : ''; };

  function unwrap(v) {
    if (!v) return '';
    if (v[0] === '{' || v[0] === '"') {
      try {
        const j = JSON.parse(v);
        if (typeof j === 'string') return j;
        if (j && typeof j === 'object') {
          for (const k of ['value', 'token', 'access_token', 'accessToken', 'jwt']) {
            if (typeof j[k] === 'string') return j[k];
          }
        }
      } catch (e) { /* bukan JSON */ }
    }
    return v;
  }

  const jwtExp = t => {
    try {
      let p = t.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
      while (p.length % 4) p += '=';
      return JSON.parse(atob(p)).exp || 0;
    } catch (e) { return 0; }
  };

  // Semua nilai berbentuk JWT di kedua storage. Satu browser bisa menyimpan
  // sisa token akun lama di kunci lain, jadi "yang pertama ketemu" tidak aman.
  function candidates() {
    const out = [];
    const seen = new Set();
    const add = (token, key) => {
      if (!token || seen.has(token)) return;
      seen.add(token);
      out.push({ token: token, key: key, exp: jwtExp(token) });
    };
    add(matchJwt(localStorage.getItem(MANUAL_KEY)), '__manual');
    for (const [name, store] of [['local', localStorage], ['session', sessionStorage]]) {
      let n = 0;
      try { n = store.length; } catch (e) { continue; }
      for (let i = 0; i < n; i++) {
        let k, v;
        try { k = store.key(i); v = store.getItem(k); } catch (e) { continue; }
        if (!k || !v) continue;
        add(matchJwt(unwrap(v)), name + ':' + k);
      }
    }
    return out;
  }

  // Pilih berdasarkan exp, bukan urutan. Override manual hanya menang selama
  // belum kedaluwarsa.
  function findToken() {
    const all = candidates();
    if (!all.length) return null;
    const now = Date.now() / 1000;
    const manual = all.find(c => c.key === '__manual' && c.exp > now);
    if (manual) return manual;
    const live = all.filter(c => c.key !== '__manual' && c.exp > now)
                    .sort((a, b) => b.exp - a.exp);
    if (live.length) return live[0];
    return all.sort((a, b) => b.exp - a.exp)[0];
  }

  // ---- cookies ----
  const baseDomain = (() => {
    const h = location.hostname.split('.');
    return '.' + h.slice(-2).join('.');
  })();

  const getCookies = () => document.cookie.split(';').map(s => s.trim()).filter(Boolean).map(kv => {
    const i = kv.indexOf('=');
    return { name: kv.slice(0, i), value: kv.slice(i + 1), domain: baseDomain };
  });

  const isWafCookie = n => /^(acw|ssxmod|tfstk|x5sec|cdn_sec)/i.test(n);
  const mask = t => (t ? t.slice(0, 6) + '...' + t.slice(-6) : '(kosong)');

  // Token hasil refresh sendiri (lihat refreshViaAuth). App hanya me-refresh
  // token saat ada aktivitas, jadi tab diam terus mendorong token basi.
  let refreshed = null;   // { token, exp }
  let refreshing = false;

  async function refreshViaAuth() {
    if (refreshing) return false;
    refreshing = true;
    isSelf = true;
    try {
      const r = await fetch('https://auth.qwen.ai/api/v2/auths/refresh', {
        method: 'GET',
        credentials: 'include',
        headers: {
          'Accept': 'application/json',
          'source': 'web',
          'Version': appVersion(loadTemplate()),
          'Timezone': tzString(),
          'x-request-origin': location.origin,
          'x-request-id': rid(),
        },
      });
      const j = await r.json();
      const tok = j && j.data && j.data.access_token;
      if (typeof tok === 'string' && tok.split('.').length === 3) {
        refreshed = { token: tok, exp: jwtExp(tok) };
        return true;
      }
      return false;
    } catch (e) {
      return false;
    } finally {
      isSelf = false;
      refreshing = false;
    }
  }

  function bestToken() {
    let f = findToken();
    if (refreshed && (!f || refreshed.exp > f.exp)) {
      f = { token: refreshed.token, key: 'auth-refresh', exp: refreshed.exp };
    }
    return f;
  }

  const snapshot = () => {
    const f = bestToken();
    return {
      provider: PROVIDER,
      token: f ? f.token : '',
      tokenSource: f ? f.key : '',
      cookies: getCookies(),
      ua: navigator.userAgent,
      lang: navigator.language,
      origin: location.origin,
      ts: Date.now(),
    };
  };

  // ======================================================================
  // TEMPLATE - rekam request chat asli app Qwen (lapisan kedua)
  // ======================================================================

  let captured = null;      // template terakhir yang terlihat di tab ini
  let isSelf = false;       // true selama request KITA sendiri jalan
  let onCapture = null;     // diisi boot() supaya baris panel ikut diperbarui
  const seen = [];          // riwayat POST /api/ untuk diagnosa

  // Header rahasia tidak pernah disimpan ke localStorage.
  const SECRET_HEADER = /^(authorization|cookie|set-cookie)$/i;

  function headerObj(h) {
    const o = {};
    if (!h) return o;
    try {
      if (typeof h.forEach === 'function' && !Array.isArray(h)) {
        h.forEach((v, k) => { o[k] = v; });
        return o;
      }
      if (Array.isArray(h)) {
        for (const p of h) if (p && p.length === 2) o[p[0]] = p[1];
        return o;
      }
      Object.assign(o, h);
    } catch (e) { /* bentuk aneh, abaikan */ }
    return o;
  }

  const stripSecrets = o => {
    const out = {};
    for (const k of Object.keys(o || {})) {
      if (!SECRET_HEADER.test(k)) out[k] = o[k];
    }
    return out;
  };

  const isChatUrl = u => /\/api\.*\/.*chat\/completions/.test(String(u || ''));

  function recordChat(url, method, headers, rawBody) {
    try {
      if (isSelf) return;
      let body = rawBody;
      if (typeof body === 'string') {
        try { body = JSON.parse(body); } catch (e) { return; }
      }
      if (!body || typeof body !== 'object' || Array.isArray(body)) return;

      const isChat = isChatUrl(url) || Array.isArray(body.messages);

      // riwayat singkat: cukup nama field, jangan isi pesannya
      if (/\/api\//.test(String(url))) {
        seen.push({
          url: String(url).slice(0, 120),
          keys: Object.keys(body).slice(0, 14).join(','),
          size: String(String(rawBody || '')).length,
          t: Date.now(),
        });
        while (seen.length > 8) seen.shift();
      }

      if (!isChat || !Array.isArray(body.messages)) return;

      captured = {
        url: String(url),
        method: String(method || 'POST').toUpperCase(),
        headers: stripSecrets(headers),
        body: body,
        ts: Date.now(),
      };
      try {
        localStorage.setItem(TEMPLATE_KEY, JSON.stringify(captured));
      } catch (e) { /* storage penuh, template tetap dipakai dari memori */ }
      if (onCapture) onCapture();
    } catch (e) { /* jangan pernah mengganggu app */ }
  }

  function loadTemplate() {
    if (captured) return captured;
    try {
      const t = JSON.parse(localStorage.getItem(TEMPLATE_KEY) || 'null');
      if (t && t.body && t.body.messages && (Date.now() - (t.ts || 0)) < TEMPLATE_TTL) {
        captured = t;
        return t;
      }
    } catch (e) { /* rusak, biarkan kosong */ }
    return null;
  }

  // window halaman, bukan sandbox userscript. Ini sebabnya v1.4.0 tidak
  // pernah menangkap apa pun: di Firefox, `window.fetch` di dalam sandbox
  // adalah fetch milik sandbox, bukan milik halaman.
  const PAGE = (function () {
    try { if (typeof unsafeWindow !== 'undefined' && unsafeWindow) return unsafeWindow; } catch (e) {}
    return window;
  })();

  function installSniffer() {
    let hooked = false;
    try {
      const origFetch = PAGE.fetch;
      if (typeof origFetch === 'function') {
        const patched = function (input, init) {
          try {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (!isSelf) {
              if (init && init.body != null) {
                recordChat(url, init.method, headerObj(init.headers), init.body);
              } else if (input && typeof input.clone === 'function') {
                const rq = input;
                rq.clone().text()
                  .then(t => recordChat(url, rq.method, headerObj(rq.headers), t))
                  .catch(() => {});
              }
            }
          } catch (e) { /* lanjut saja */ }
          return origFetch.apply(this, arguments);
        };
        try {
          PAGE.fetch = patched;
          // Di Firefox, fungsi yang menyeberang sandbox -> halaman dibungkus
          // Xray, jadi perbandingan identitas tidak bisa dipercaya. Yang penting
          // assignment-nya tidak melempar.
          hooked = (PAGE === window) ? (PAGE.fetch === patched) : true;
        } catch (e) { hooked = false; }
      }
    } catch (e) { /* fetch tidak bisa dipatch */ }
    return hooked;
  }

  // Nama model: fasad relay memakai "qwen-chat"/"qwen" yang tidak dikenal
  // upstream. Kalau ada template, pakai model yang dipakai app sendiri.
  function pickModel(asked, tpl) {
    const fromApp = tpl && tpl.body && tpl.body.model;
    if (!asked) return fromApp || PING_MODEL;
    const a = String(asked).trim();
    if (fromApp && (/^qwen(-chat)?$/i.test(a) || a === PING_MODEL)) return fromApp;
    return a;
  }

  // ---- body canonical, hasil pembacaan bundle Qwen 0.3.40 ----
  // Bentuknya disamakan dengan request app yang tertangkap di panel:
  // tidak ada field "headers", pesan membawa files/extra/sub_chat_type/models.
  function canonicalBody(prompt, model, cid) {
    const now = Math.floor(Date.now() / 1000);   // DETIK
    return {
      stream: true,
      version: '2.1',
      incremental_output: true,
      chatId: cid,
      parentId: '',
      chat_id: cid,
      chat_mode: 'normal',
      model: model,
      parent_id: null,
      timestamp: now,
      messages: [{
        id: null,
        fid: rid(),
        parentId: null,
        parent_id: null,
        childrenIds: [],
        role: 'user',
        content: prompt,
        user_action: 'chat',
        files: [],
        timestamp: now,
        models: [model],
        model: '',
        chat_type: CHAT_TYPE,
        feature_config: {
          thinking_enabled: false,
          output_schema: 'phase',
          research_mode: 'normal',
          auto_thinking: false,
          thinking_mode: 'Fast',
          auto_search: false,
        },
        extra: { meta: { subChatType: CHAT_TYPE } },
        sub_chat_type: CHAT_TYPE,
      }],
    };
  }

  // Chat harus DIBUAT dulu: POST /api/v2/chats/new. Kalau chat_id tidak ada
  // di akun, server Qwen menjawab "Bad_Request - Internal error..." - bukan
  // pesan validasi. timestamp di sini milidetik (app memakai Date.now()).
  function chatCreateBody(model) {
    return {
      chatId: '',
      models: [model],
      timestamp: Date.now(),
      chat_type: CHAT_TYPE,
      chat_mode: 'normal',
    };
  }

  function buildBody(prompt, model, cid) {
    const t = loadTemplate();
    const body = canonicalBody(prompt, model, cid);
    body.model = pickModel(model, t);
    if (!t || !t.body) return body;

    // Template dipakai HANYA untuk menyesuaikan diri dengan akun ini:
    // model yang valid, chat_mode/version yang dipakai app, dan bentuk
    // feature_config / chat_type pesan. Field khas app (share_id, turn_id,
    // modelIdx, origin_branch_message_id) sengaja tidak dibawa.
    const tb = t.body;
    if (typeof tb.chat_mode === 'string') body.chat_mode = tb.chat_mode;
    if (typeof tb.version === 'string') body.version = tb.version;

    const tm = (Array.isArray(tb.messages) ? tb.messages : [])
      .find(m => m && m.role === 'user') || (tb.messages || [])[0];
    if (tm && typeof tm === 'object') {
      if (typeof tm.chat_type === 'string') body.messages[0].chat_type = tm.chat_type;
      if (tm.feature_config && typeof tm.feature_config === 'object') {
        body.messages[0].feature_config = JSON.parse(JSON.stringify(tm.feature_config));
      }
      if (typeof tm.sub_chat_type === 'string') {
        body.messages[0].sub_chat_type = tm.sub_chat_type;
      }
    }
    // Template datang dari chat manual (auto_search: true). Untuk agent, tool
    // bawaan Qwen (web_search, code_interpreter) hanya memperlambat, dan
    // bridge membalas sekali di akhir sehingga proses berpikir terbuang.
    const fc = body.messages[0].feature_config;
    fc.auto_search = false;
    fc.thinking_enabled = false;
    fc.auto_thinking = false;
    fc.thinking_mode = 'Fast';
    return body;
  }

  function buildQuery(t, cid) {
    const q = new URLSearchParams();
    if (t) {
      try {
        new URL(t.url, location.origin).searchParams.forEach((v, k) => {
          if (k !== 'chat_id') q.set(k, v);
        });
      } catch (e) { /* url relatif aneh, abaikan */ }
    }
    q.set('chat_id', cid);
    return '?' + q.toString();
  }

  // ======================================================================
  // PANEL + SESSION PUSH + BRIDGE
  // ======================================================================

  const hooked = installSniffer();

  function boot() {
    if (document.getElementById('qwen-token-panel')) return;
    if (!document.body) return;

    const p = document.createElement('div');
    p.id = 'qwen-token-panel';
    p.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;width:410px;background:#111;color:#eee;' +
      'font:12px/1.5 monospace;padding:12px;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.5)';
    p.innerHTML =
      '<b>Qwen bridge</b> <span id="qw-x" style="float:right;cursor:pointer">[x]</span>' +
      '<div id="qw-status" style="margin:6px 0;color:#9cf"></div>' +
      '<div id="qw-bridge" style="margin:0 0 4px;color:#8f8"></div>' +
      '<div id="qw-cap" style="margin:0 0 6px;color:#fc6"></div>' +
      '<textarea id="qw-out" readonly style="width:100%;height:56px;background:#000;color:#8f8;border:1px solid #333;' +
      'font:11px monospace;box-sizing:border-box"></textarea>' +
      '<div id="qw-reply" style="display:none;margin-top:6px;padding:6px;background:#000;border:1px solid #333;' +
      'color:#eee;font:11px/1.45 monospace;max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-word"></div>' +
      '<div style="display:flex;gap:6px;margin-top:8px">' +
      '<input id="qw-manual" placeholder="tempel token manual kalau kosong" ' +
      'style="flex:1;min-width:0;padding:5px;background:#000;color:#eee;border:1px solid #333;font:11px monospace">' +
      '<button id="qw-set" style="padding:5px 8px;cursor:pointer">Pakai</button></div>' +
      '<div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">' +
      '<button id="qw-ping" style="flex:1;padding:6px;cursor:pointer;font-weight:bold">Ping</button>' +
      '<button id="qw-copy" style="flex:1;padding:6px;cursor:pointer">Salin</button>' +
      '<button id="qw-save" style="flex:1;padding:6px;cursor:pointer">JSON</button>' +
      '<button id="qw-verify" style="flex:1;padding:6px;cursor:pointer">Verifikasi</button>' +
      '<button id="qw-tpl" style="flex:1;padding:6px;cursor:pointer">Tangkap</button>' +
      '<button id="qw-tpl-x" style="flex:1;padding:6px;cursor:pointer">Hapus tpl</button></div>';
    document.body.appendChild(p);

    const $ = id => document.getElementById(id);
    const status = t => { $('qw-status').textContent = t; };
    const reply = t => { const b = $('qw-reply'); b.style.display = 'block'; b.textContent = t; };
    const bridgeLine = t => { $('qw-bridge').textContent = t; };

    function refreshCaptureLine() {
      const el = $('qw-cap');
      if (!el) return;
      const t = loadTemplate();
      if (!t) {
        el.style.color = '#fc6';
        el.textContent = 'template: belum ada (tidak wajib) - sniffer ' +
          (hooked ? 'aktif' : 'GAGAL menempel') + ' - lihat tombol Tangkap';
        return;
      }
      const age = Math.max(0, Math.round((Date.now() - (t.ts || 0)) / 60000));
      el.style.color = '#8f8';
      el.textContent = 'template: ok, ' + Object.keys(t.body).length + ' field, model ' +
        (t.body.model || '?') + ', ' + age + ' mnt lalu';
    }
    onCapture = refreshCaptureLine;
    refreshCaptureLine();

    let lastPushed = '';
    let lastErr = '';
    let lastModelUsed = PING_MODEL;
    let pushes = 0;
    let jobsDone = 0;
    let polls = 0;

    function push(reason, afterRefresh) {
      let s = snapshot();
      const left = s.token ? jwtExp(s.token) - Date.now() / 1000 : 0;
      if (!afterRefresh && s.token && left < 120 && !refreshing) {
        refreshViaAuth().then(() => push(reason, true));
        return;
      }
      s = snapshot();
      if (!s.token) {
        status('token tidak ketemu - login dulu, atau tempel manual di kotak bawah');
        return;
      }
      $('qw-out').value = JSON.stringify(s, null, 2);
      GM_xmlhttpRequest({
        method: 'POST',
        url: RELAY,
        data: JSON.stringify(s),
        headers: { 'Content-Type': 'application/json' },
        onload: r => {
          if (r.status === 200) {
            lastPushed = s.token;
            lastErr = '';
            pushes++;
            const waf = s.cookies.filter(c => isWafCookie(c.name)).length;
            status('sesi tersinkron #' + pushes + ' (' + reason + ') ' + mask(s.token) +
              ' - ' + s.cookies.length + ' cookie (' + waf + ' WAF) - ' + new Date().toLocaleTimeString());
          } else {
            lastErr = 'http' + r.status;
            status('relay menolak: HTTP ' + r.status + ' - ' + String(r.responseText || '').slice(0, 90));
          }
        },
        onerror: () => {
          if (lastErr !== 'down') {
            lastErr = 'down';
            status('relay2.py tidak jalan - jalankan dulu, lalu push otomatis nyambung');
          }
        },
      });
    }

    // ---- chat di dalam tab ----
    // Chat dibuat dulu lewat /api/v2/chats/new. Tanpa ini, chat_id karangan
    // membuat server gagal menyimpan pesan -> "Internal error...".
    async function ensureChat(model, token) {
      isSelf = true;
      try {
        const r = await fetch('/api/v2/chats/new', {
          method: 'POST',
          credentials: 'include',
          headers: {
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'source': 'web',
            'Version': appVersion(loadTemplate()),
            'Timezone': tzString(),
            'x-request-id': rid(),
          },
          body: JSON.stringify(chatCreateBody(model)),
        });
        const txt = await r.text();
        let j = null;
        try { j = JSON.parse(txt); } catch (e) { /* bukan JSON */ }
        const id = j && ((j.data && j.data.id) || j.id);
        if (typeof id === 'string' && id) return { id: id, status: r.status, error: '' };
        return {
          id: '',
          status: r.status,
          error: (j && j.data && (j.data.code || j.data.details)) || txt.slice(0, 200) || 'respons kosong',
        };
      } catch (e) {
        return { id: '', status: 0, error: String(e.message || e) };
      } finally {
        isSelf = false;
      }
    }

    async function sendChat(prompt, model) {
      let f = bestToken();
      if (f && f.exp - Date.now() / 1000 < 60) {
        await refreshViaAuth();
        f = bestToken();
      }
      if (!f) throw new Error('token hilang dari storage');

      const t = loadTemplate();
      const chosen = pickModel(model, t);
      // chat_id kiriman relay tidak dipakai: id itu tidak ada di akun.
      const chat = await ensureChat(chosen, f.token);
      const cid = chat.id || rid();

      const body = buildBody(prompt, model, cid);
      const url = '/api/v2/chat/completions' + buildQuery(t, cid);
      lastModelUsed = body.model;

      let r;
      isSelf = true;
      try {
        r = await fetch(url, {
          method: 'POST',
          credentials: 'include',
          headers: {
            'Authorization': 'Bearer ' + f.token,
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream',
            'X-Accel-Buffering': 'no',
            'source': 'web',
            'Version': appVersion(t),
            'Timezone': tzString(),
            'x-request-id': rid(),
          },
          body: JSON.stringify(body),
        });
      } finally {
        isSelf = false;
      }

      const raw = await r.text();
      if (!r.ok) throw new Error('HTTP ' + r.status + ' - ' + raw.slice(0, 300));
      if (/x5secdata|_____tmd_____/.test(raw))
        throw new Error('WAF challenge - reload tab ini dan selesaikan verifikasi');

      const fail = pickError(raw);
      if (fail) {
        throw new Error('Qwen menolak: ' + fail +
          '\n\nchat baru: ' + (chat.id ? 'ok (' + chat.id.slice(0, 8) + '...)' : 'GAGAL - ' + chat.error) +
          '\ntemplate: ' + (t ? Object.keys(t.body).join(', ') : 'TIDAK ADA (tidak wajib)') +
          '\nbody kita: ' + Object.keys(body).join(', ') +
          '\nraw: ' + raw.slice(0, 400));
      }

      const text = pullText(raw);
      if (!text) throw new Error('balasan kosong (' + raw.length + ' B) - raw: ' + raw.slice(0, 700));
      return text;
    }

    function pullText(raw) {
      let out = '';
      for (const line of raw.split(/\r?\n/)) {
        const s = line.trim();
        if (!s.startsWith('data:')) continue;
        const payload = s.slice(5).trim();
        if (!payload || payload === '[DONE]') continue;
        try { out += pickContent(JSON.parse(payload)); } catch (e) { /* potongan belum utuh */ }
      }
      if (!out) {
        try { out = pickContent(JSON.parse(raw)); } catch (e) { /* bukan JSON */ }
      }
      return out;
    }

    function pickError(raw) {
      try {
        const j = JSON.parse(raw);
        if (j && j.success === false) {
          const d = j.data || {};
          return [d.code, d.details || d.message].filter(Boolean).join(' - ').slice(0, 400);
        }
      } catch (e) { /* bukan JSON */ }
      return '';
    }

    // Bentuk balasan Qwen tidak persis satu bentuk, jadi semua jalur dicoba.
    // delta.phase "think" dilewati supaya yang terbaca hanya jawaban akhir.
    function pickContent(j) {
      if (typeof j === 'string') return j;
      if (!j || typeof j !== 'object') return '';
      const partText = v => {
        if (typeof v === 'string') return v;
        if (Array.isArray(v)) return v.map(x => (x && (x.text || x.content)) || '').join('');
        return '';
      };
      const c = (j.choices && j.choices[0]) || (j.data && j.data.choices && j.data.choices[0]);
      if (c) {
        const d = c.delta || c.message || {};
        if (d.phase === 'think' || d.phase === 'thinking') return '';
        const t = partText(d.content) || partText(c.text);
        if (t) return t;
      }
      for (const k of ['content', 'response', 'text', 'answer']) {
        const t = partText(j[k]);
        if (t) return t;
      }
      if (j.data && typeof j.data === 'object') {
        for (const k of ['content', 'response', 'text', 'answer']) {
          const t = partText(j.data[k]);
          if (t) return t;
        }
      }
      if (j.output) {
        if (typeof j.output.text === 'string') return j.output.text;
        if (Array.isArray(j.output) && j.output[0]) {
          const t = partText(j.output[0].content);
          if (t) return t;
        }
      }
      return '';
    }

    // ---- bridge loop: ambil job relay, kerjakan, kirim hasil ----
    async function bridgeTick() {
      polls++;
      GM_xmlhttpRequest({
        method: 'GET',
        url: BRIDGE + '/pull',
        onload: async r => {
          if (r.status === 204 || r.status === 404) {
            bridgeLine('bridge ok - menunggu job (poll #' + polls + ', selesai ' + jobsDone + ')');
            return;
          }
          if (r.status !== 200) {
            bridgeLine('bridge: relay balas HTTP ' + r.status);
            return;
          }
          let job;
          try { job = JSON.parse(r.responseText); } catch (e) { return; }
          if (!job || !job.id) return;
          const body = job.body || {};
          bridgeLine('bridge: mengerjakan job ' + String(job.id).slice(0, 8) + '...');
          let out;
          const t0 = performance.now();
          try {
            const text = await sendChat(body.prompt, body.model);
            out = { id: job.id, ok: true, text: text };
            bridgeLine('bridge ok - job ' + String(job.id).slice(0, 8) + ' - ' +
              Math.round(performance.now() - t0) + ' ms - ' + text.length + ' char');
          } catch (e) {
            out = { id: job.id, ok: false, error: String(e.message || e) };
            bridgeLine('bridge GAGAL - job ' + String(job.id).slice(0, 8) + ': ' + out.error.slice(0, 160));
          }
          jobsDone++;
          GM_xmlhttpRequest({
            method: 'POST',
            url: BRIDGE + '/push',
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify(out),
          });
        },
        onerror: () => bridgeLine('bridge GAGAL - relay tidak menjawab, relay2.py jalan?'),
      });
    }

    $('qw-ping').onclick = async () => {
      const btn = $('qw-ping');
      btn.disabled = true;
      reply('mengirim "ping"...');
      status('ping...');
      const t0 = performance.now();
      try {
        const text = await sendChat('ping', PING_MODEL);
        reply(text);
        status('pong ok - ' + Math.round(performance.now() - t0) + ' ms - ' + lastModelUsed +
          (loadTemplate() ? ' (template app)' : ' (body canonical)'));
      } catch (e) {
        reply(String(e.message || e));
        status('ping gagal');
      } finally { btn.disabled = false; }
    };

    // 1. heartbeat sesi
    const hb = setInterval(() => push('heartbeat'), HEARTBEAT);
    // 2. deteksi rotasi token
    const tk = setInterval(() => {
      const f = bestToken();
      if (f && f.token !== lastPushed) push('token berubah');
    }, TICK);
    // 3. tab aktif lagi
    const onVis = () => { if (!document.hidden) { push('tab aktif'); bridgeTick(); } };
    document.addEventListener('visibilitychange', onVis);
    // 4. bridge poller
    const bp = setInterval(bridgeTick, POLL);
    bridgeTick();

    // ---- tombol lain ----
    $('qw-x').onclick = () => {
      clearInterval(hb); clearInterval(tk); clearInterval(bp);
      document.removeEventListener('visibilitychange', onVis);
      p.remove();
      window.__qwenRelayLoaded = false;
    };

    $('qw-set').onclick = () => {
      const rawv = $('qw-manual').value.trim();
      if (!rawv) {
        localStorage.removeItem(MANUAL_KEY);
        status('override manual dihapus - kembali ke deteksi otomatis');
        return;
      }
      const t = matchJwt(rawv) || matchJwt(unwrap(rawv));
      if (!JWT_RE.test(t)) {
        status('itu bukan JWT (harusnya diawali eyJ) - periksa lagi');
        return;
      }
      localStorage.setItem(MANUAL_KEY, t);
      $('qw-manual').value = '';
      status('token manual disimpan');
      push('manual');
    };

    $('qw-copy').onclick = async () => {
      const f = findToken();
      if (!f) { status('tidak ada token'); return; }
      try {
        await navigator.clipboard.writeText(f.token);
        status('token disalin (' + f.key + ')');
      } catch (e) {
        const ta = $('qw-out');
        ta.focus(); ta.select();
        status('pilih manual lalu Ctrl+C');
      }
    };

    $('qw-save').onclick = () => {
      const blob = new Blob([JSON.stringify(snapshot(), null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'websession-qwen.json';
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      status('websession-qwen.json diunduh');
    };

    // diagnosa: template + riwayat POST /api/ yang terlihat
    $('qw-tpl').onclick = () => {
      const t = loadTemplate();
      const lines = [];
      lines.push('sniffer: ' + (hooked ? 'aktif di window halaman' : 'GAGAL menempel di window halaman'));
      lines.push('');
      if (t) {
        lines.push('template: ' + t.url + ' (' + Object.keys(t.body).length + ' field)');
        lines.push(JSON.stringify(t.body, null, 2).slice(0, 1500));
      } else {
        lines.push('template: belum ada.');
        lines.push('Tidak wajib - Ping memakai body canonical dari bundle Qwen.');
        lines.push('Kalau mau template, kirim 1 pesan di UI Qwen lalu klik Tangkap lagi.');
      }
      if (seen.length) {
        lines.push('');
        lines.push('POST /api/ terakhir:');
        for (const s of seen) {
          lines.push('  ' + (s.url.split('?')[0]) + '  [' + s.keys + ']  ' + s.size + 'B');
        }
      }
      reply(lines.join('\n'));
    };

    $('qw-tpl-x').onclick = () => {
      captured = null;
      localStorage.removeItem(TEMPLATE_KEY);
      refreshCaptureLine();
      status('template dihapus - akan direkam lagi saat kamu kirim pesan');
    };

    // verifikasi + pemindaian token
    $('qw-verify').onclick = async () => {
      const all = candidates();
      const f = findToken();
      const now = Date.now() / 1000;
      const fmt = e => !e ? 'tanpa exp'
        : (e > now ? '+' + Math.round((e - now) / 60) + 'mnt'
                   : '-' + Math.round((now - e) / 3600) + 'jam');
      reply(all.length
        ? 'kandidat (' + all.length + '):\n' + all.map(c =>
            ((f && c.token === f.token) ? '> ' : '  ') + c.key + '  ' + fmt(c.exp) +
            '  ' + c.token.slice(0, 10) + '...').join('\n') +
            '\n\ntemplate: ' + (loadTemplate() ? 'ada' : 'belum ada (tidak wajib)')
        : 'tidak ada JWT di storage - belum login?');

      if (!f) { status('tidak ada token'); return; }

      const local = f.exp ? fmt(f.exp) : 'tanpa exp';
      status('memeriksa ' + f.key + ' (' + local + ')...');
      for (const path of ['/api/v1/auths/', '/api/models']) {
        try {
          const r = await fetch(path, {
            headers: { 'Authorization': 'Bearer ' + f.token, 'Accept': 'application/json', 'x-request-id': rid() },
            credentials: 'include',
          });
          if (r.status === 401 || r.status === 403) {
            status('ditolak ' + r.status + ' (' + f.key + ') - token ini mati. ' +
              'Kosongkan kotak lalu klik Pakai untuk hapus override, lalu reload tab');
            return;
          }
          if (r.ok) { status('valid (' + f.key + ') - ' + local + ' - ' + path); return; }
        } catch (e) { /* lanjut ke endpoint berikutnya */ }
      }
      status('tidak ada endpoint yang 2xx - lihat daftar kandidat di kotak atas');
    };

    push('mulai');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
