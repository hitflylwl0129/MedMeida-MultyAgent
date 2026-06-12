/* MedMedia 访问统计 SDK · v1.0
 * ----------------------------------------------------------------------
 * 加载方式：
 *   <script src="//162.14.76.209/static/track.js" defer></script>
 *
 * 行为：
 *   1) pageshow      → 上报 visit
 *   2) 每 30 秒      → 上报 heartbeat
 *   3) pagehide      → sendBeacon 上报 leave (含 dur_sec)
 *   4) hashchange    → SPA 内切换板块时再上报一次 visit
 *   5) 拉 footer 数据→ 渲染页脚条 #fs-strip
 *
 * 上报通道（0 CORS / 0 OPTIONS）：
 *   - visit/heartbeat：GET <BASE>/api/track/p.gif?...（1px 透明 GIF）
 *   - leave：navigator.sendBeacon(<BASE>/api/track/leave, json) 不阻塞页面卸载
 *
 * 板块字典（marketing 主板块自动下钻为 7 个子板块；其它板块靠 host 与 path 前缀匹配）。
 * 详见：访问统计_技术路线与原型.md §2
 */
(function(){
  'use strict';
  if (window.__MM_TRACK__) return; // 防重复注入
  window.__MM_TRACK__ = true;

  // ---- 配置 ---------------------------------------------------------------
  // 上报后端基址：默认就是 video-agent 公网 nginx 入口
  // 想本地联调时可在加载 script 之前 window.__MM_TRACK_BASE__ = 'http://127.0.0.1:8001'
  var BASE = window.__MM_TRACK_BASE__ || 'http://162.14.76.209';
  var HEARTBEAT_MS = 30 * 1000;

  // ---- 板块字典 -----------------------------------------------------------
  var SECTION_DICT = {
    // host:port → { default?, prefixMap? }
    '162.14.76.209':       { default: 'marketing' },
    'localhost':           { default: 'marketing' },
    '127.0.0.1':           { default: 'marketing' },
    '162.14.76.209:8000':  { prefixMap: { '/ocr':'ocr', '/asr':'asr', '/raw':'raw', '/qc':'qc' } },
    '127.0.0.1:8000':      { prefixMap: { '/ocr':'ocr', '/asr':'asr', '/raw':'raw', '/qc':'qc' } },
  };
  var SUB_BY_PATH = {
    '/':                'home',
    '/app.html':        'home',
    '/index.html':      'index',
    '/product.html':    'product',
    '/doctor.html':     'doctor',
    '/script.html':     'script',
    '/video.html':      'video',
    '/distribute.html': 'distribute',
    '/audience.html':   'audience',
    '/admin.html':      'admin',
  };

  function detectSection(){
    var host = location.host || '';
    var path = location.pathname || '/';
    var dict = SECTION_DICT[host] || SECTION_DICT[host.split(':')[0]] || null;
    if (dict && dict.default){
      // 营销中台
      var key = path.toLowerCase();
      var sub = SUB_BY_PATH[key];
      if (!sub){
        // /xxx.html 都不命中时，去掉 query/hash 重新查
        sub = '';
      }
      return { section: dict.default, subsection: sub || '' };
    }
    if (dict && dict.prefixMap){
      for (var prefix in dict.prefixMap){
        if (path.indexOf(prefix) === 0) return { section: dict.prefixMap[prefix], subsection: '' };
      }
    }
    return { section: 'unknown', subsection: '' };
  }

  // ---- session_id ---------------------------------------------------------
  function getSessionId(){
    try {
      var sid = sessionStorage.getItem('__mm_sid');
      if (!sid){
        sid = (crypto && crypto.randomUUID ? crypto.randomUUID() :
               (Date.now().toString(36) + Math.random().toString(36).slice(2,10)));
        sessionStorage.setItem('__mm_sid', sid);
      }
      return sid;
    } catch(_) {
      return 'nosess_' + Date.now();
    }
  }

  // ---- 上报 ---------------------------------------------------------------
  var ENTER_TS = Math.floor(Date.now() / 1000);

  function buildQS(extra){
    var s = detectSection();
    var p = new URLSearchParams({
      e:   extra && extra.e || 'visit',
      s:   s.section,
      ss:  s.subsection,
      p:   location.pathname || '/',
      sid: getSessionId(),
      r:   document.referrer ? document.referrer.slice(0, 500) : '',
      t:   document.title ? document.title.slice(0, 200) : '',
      d:   String(extra && extra.d || 0),
      _:   Date.now().toString(36), // 防缓存
    });
    return p.toString();
  }

  function reportPixel(extra){
    try {
      // 用 Image() 而非 fetch，0 CORS 0 OPTIONS
      var img = new Image(1, 1);
      img.referrerPolicy = 'no-referrer-when-downgrade';
      img.src = BASE + '/api/track/p.gif?' + buildQS(extra);
    } catch(_) {}
  }

  function reportLeave(){
    try {
      var s = detectSection();
      var body = JSON.stringify({
        s: s.section, ss: s.subsection,
        p: location.pathname || '/', sid: getSessionId(),
        d: Math.floor(Date.now() / 1000) - ENTER_TS,
      });
      // sendBeacon 不阻塞页面卸载；浏览器自动用 POST + Content-Type: text/plain
      if (navigator.sendBeacon){
        navigator.sendBeacon(BASE + '/api/track/leave', body);
      } else {
        // 兜底：同步 XHR（已废弃但可用）
        var xhr = new XMLHttpRequest();
        xhr.open('POST', BASE + '/api/track/leave', false);
        try { xhr.send(body); } catch(_){}
      }
    } catch(_) {}
  }

  // ---- 生命周期挂钩 -------------------------------------------------------
  // 立即上报一次 visit
  reportPixel({ e: 'visit' });

  // 心跳
  var heartbeatTimer = setInterval(function(){
    if (document.visibilityState !== 'hidden'){
      reportPixel({ e: 'heartbeat' });
    }
  }, HEARTBEAT_MS);

  // hashchange / popstate 视为 SPA 内的板块切换
  window.addEventListener('hashchange', function(){
    ENTER_TS = Math.floor(Date.now() / 1000);
    reportPixel({ e: 'visit' });
  });

  // pagehide / beforeunload：上报 leave
  window.addEventListener('pagehide', reportLeave, { capture: true });
  // 兼容老浏览器
  window.addEventListener('beforeunload', reportLeave, { capture: true });

  // ---- 渲染页脚条 #fs-strip（方案 A：32px 极简单行）-----------------------
  function ensureStrip(){
    if (document.getElementById('fs-strip')) return;
    var el = document.createElement('div');
    el.id = 'fs-strip';
    el.style.cssText = [
      'position:fixed','left:0','right:0','bottom:0','height:32px',
      'background:rgba(13,15,22,.86)','backdrop-filter:blur(8px)',
      '-webkit-backdrop-filter:blur(8px)',
      'border-top:1px solid rgba(255,255,255,.06)',
      'display:flex','justify-content:space-between','align-items:center',
      'padding:0 18px','font-size:11.5px','color:#9aa1ad',
      'font-family:ui-monospace,Menlo,monospace','z-index:99',
      'pointer-events:auto','user-select:none',
    ].join(';');
    el.innerHTML =
      '<div>' +
        '<span style="color:#7be0b3">●</span> ' +
        '<span id="fs-online" style="color:#dde">--</span> 在线 &nbsp;&nbsp; ' +
        '今日 <b id="fs-pv-today" style="color:#dde">--</b> PV / ' +
        '<b id="fs-uv-today" style="color:#dde">--</b> UV &nbsp;&nbsp; ' +
        '累计 <b id="fs-pv-total" style="color:#dde">--</b>' +
      '</div>' +
      '<div>' +
        '您的 IP <span id="fs-myip" style="color:#dde">--</span>' +
        '<span title="本站仅统计访问 IP / 浏览器类型，不存 cookie / 不识别个人" ' +
              'style="cursor:help;color:#666;margin-left:6px">ⓘ</span>' +
      '</div>';
    document.body.appendChild(el);
    // 防止主内容被遮：给 body 加 padding-bottom（不覆盖已有值）
    var pb = parseInt(getComputedStyle(document.body).paddingBottom || '0', 10);
    if (pb < 40) document.body.style.paddingBottom = '40px';
  }

  function fmt(n){
    if (n == null) return '--';
    if (n < 10000) return String(n);
    return (n / 1000).toFixed(1) + 'k';
  }

  function refreshFooter(){
    fetch(BASE + '/api/track/footer', { credentials: 'omit', cache: 'no-store' })
      .then(function(r){ return r.ok ? r.json() : null; })
      .then(function(j){
        if (!j) return;
        var el;
        el = document.getElementById('fs-online');   if (el) el.textContent  = fmt(j.online);
        el = document.getElementById('fs-pv-today'); if (el) el.textContent  = fmt(j.today_pv);
        el = document.getElementById('fs-uv-today'); if (el) el.textContent  = fmt(j.today_uv);
        el = document.getElementById('fs-pv-total'); if (el) el.textContent  = fmt(j.total_pv);
        el = document.getElementById('fs-myip');     if (el) el.textContent  = j.ip || '--';
      })
      .catch(function(){ /* 静默 */ });
  }

  // 等 body 准备好再注入条
  if (document.body){
    ensureStrip();
    refreshFooter();
  } else {
    document.addEventListener('DOMContentLoaded', function(){
      ensureStrip();
      refreshFooter();
    });
  }
  // 每 30 秒刷新页脚数字
  setInterval(refreshFooter, 30000);
})();
