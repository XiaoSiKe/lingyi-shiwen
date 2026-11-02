/**
 * 零一拾文 · 公众号自适应采集引擎 ↔ 零一智译 browser-core 的桥。
 *
 * 不重写指纹注入：直接复用 `browser-core/src/{kernel,profiles}.mjs`
 * （CDP Emulation + document_start 注入 + persona 一致性 + 独占 user-data-dir），
 * 只把「打开一个 URL 并把 HTML/状态/cookie 交回去」包装成按行 JSON 协议，
 * 供 Python 侧 `wxrc.kernel_bridge.KernelBridge` 调用。
 *
 * 协议（stdin/stdout 各一行 JSON）：
 *   {"id":1,"cmd":"open","profileId":"p_ab12cd34","seed":123,"locale":"zh-CN",
 *    "url":"https://mp.weixin.qq.com/s/xxx","referer":"https://mp.weixin.qq.com/",
 *    "proxy":{"mode":"http","host":"127.0.0.1","port":7890},
 *    "waitHumanMs":90000,"timeoutMs":20000}
 *   {"id":2,"cmd":"eval","expr":"navigator.webdriver"}
 *   {"id":3,"cmd":"cookies","url":"https://mp.weixin.qq.com/"}
 *   {"id":4,"cmd":"close"} / {"id":5,"cmd":"shutdown"} / {"id":6,"cmd":"ping"}
 *
 * 用法：
 *   node bridge/wx_kernel_bridge.mjs --core "C:/.../零一智译/browser-core" \
 *        --data-root "C:/Users/me/AppData/Local/wxrc/browser-profiles" --headless
 */
import readline from 'node:readline';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { existsSync } from 'node:fs';

/* ---------------------------------------------------------------------- */
/* 参数                                                                    */
/* ---------------------------------------------------------------------- */
function parseArgs(argv) {
  const out = { headless: true };
  for (let i = 2; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--core') out.core = argv[++i];
    else if (a === '--data-root') out.dataRoot = argv[++i];
    else if (a === '--chrome') out.chrome = argv[++i];
    else if (a === '--headed') out.headless = false;
    else if (a === '--headless') out.headless = true;
  }
  return out;
}

const opts = parseArgs(process.argv);
const coreRoot = path.resolve(
  opts.core || process.env.LINGYI_BROWSER_CORE || path.join(process.cwd(), '..', '零一智译', 'browser-core'),
);

function fail(message) {
  process.stdout.write(JSON.stringify({ ok: false, fatal: true, error: message }) + '\n');
  process.exit(1);
}

if (!existsSync(path.join(coreRoot, 'src', 'kernel.mjs'))) {
  fail(`找不到 browser-core：${coreRoot}（用 --core 指定 browser-core 目录）`);
}

const imp = (rel) => import(pathToFileURL(path.join(coreRoot, 'src', rel)).href);
const { BrowserKernel } = await imp('kernel.mjs');
const { ProfileStore } = await imp('profiles.mjs');

/* ---------------------------------------------------------------------- */
/* 状态                                                                    */
/* ---------------------------------------------------------------------- */
let kernel = null;
let store = null;
let session = null;
let sessionProfileId = null;
let sessionProxy = null;

const CAPTCHA_MARKERS = ['环境异常', '完成验证后即可继续访问', 'wappoc_appmsgcaptcha', '请输入验证码'];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function ensureRuntime() {
  if (!kernel) {
    kernel = new BrowserKernel({ headless: opts.headless, bin: opts.chrome || undefined });
    await kernel.initialize();
  }
  if (!store) {
    const dataRoot =
      opts.dataRoot ||
      process.env.WXRC_BROWSER_DATA_ROOT ||
      path.join(process.env.LOCALAPPDATA || path.join(process.env.USERPROFILE || '.', 'AppData', 'Local'), 'wxrc', 'browser-profiles');
    store = new ProfileStore({ root: dataRoot, chromeMajor: kernel.chromeMajor });
    await store.init();
  }
  return { kernel, store };
}

async function getHtml() {
  return session.evaluate('document.documentElement.outerHTML', { timeoutMs: 20000 });
}

async function readFingerprint() {
  return session.evaluate(
    `(() => {
      const nav = typeof performance !== 'undefined' && performance.getEntriesByType
        ? (performance.getEntriesByType('navigation')[0] || {}) : {};
      const canvasHash = (() => {
        try {
          const c = document.createElement('canvas');
          c.width = 240; c.height = 60;
          const ctx = c.getContext('2d');
          if (!ctx) return null;
          ctx.textBaseline = 'top';
          ctx.font = '14px Arial';
          ctx.fillStyle = '#f60';
          ctx.fillRect(0, 0, 120, 30);
          ctx.fillStyle = '#069';
          ctx.fillText('lingyi-shiwen \\u6307\\u7eb9', 2, 2);
          const data = c.toDataURL();
          let h = 5381;
          for (let i = 0; i < data.length; i += 1) h = ((h << 5) + h + data.charCodeAt(i)) >>> 0;
          return h.toString(16);
        } catch (e) { return null; }
      })();
      return {
        ua: navigator.userAgent,
        platform: navigator.platform,
        languages: navigator.languages,
        language: navigator.language,
        timezone: (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return null; } })(),
        hardwareConcurrency: navigator.hardwareConcurrency,
        deviceMemory: navigator.deviceMemory === undefined ? null : navigator.deviceMemory,
        maxTouchPoints: navigator.maxTouchPoints,
        webdriver: navigator.webdriver,
        screen: { width: screen.width, height: screen.height, availWidth: screen.availWidth, availHeight: screen.availHeight },
        window: { outerWidth: window.outerWidth, outerHeight: window.outerHeight, innerWidth: window.innerWidth, innerHeight: window.innerHeight },
        webglVendor: (() => { try { const gl = document.createElement('canvas').getContext('webgl'); const d = gl.getExtension('WEBGL_debug_renderer_info'); return gl.getParameter(d.UNMASKED_VENDOR_WEBGL); } catch (e) { return null; } })(),
        canvasHash,
        responseStatus: nav.responseStatus || 0,
        title: document.title,
      };
    })()`,
    { timeoutMs: 15000 },
  );
}

async function cmdOpen(msg) {
  const { store: st } = await ensureRuntime();
  const profileId = msg.profileId;
  const proxyKey = JSON.stringify(msg.proxy || null);
  if (session && (sessionProfileId !== profileId || sessionProxy !== proxyKey)) {
    await session.close();
    session = null;
  }
  const profile = await st.ensure({
    id: profileId,
    name: msg.name || profileId,
    localeProfile: msg.locale || 'zh-CN',
    seed: msg.seed,
    proxy: msg.proxy || null,
    chromeMajor: kernel.chromeMajor,
  });
  if (!session) {
    session = await kernel.launch(profile);
    sessionProfileId = profileId;
    sessionProxy = proxyKey;
  }

  const nav = await session.navigate(msg.url, { timeoutMs: msg.timeoutMs || 20000 });
  let html = '';
  let waitedMs = 0;
  const waitHuman = Number(msg.waitHumanMs || 0);

  // 验证页：给人工留时间（headed 时窗口可见），页面变了就继续
  for (;;) {
    try {
      html = await getHtml();
    } catch {
      html = '';
    }
    const blocked = CAPTCHA_MARKERS.some((m) => html.includes(m));
    if (!blocked || waitedMs >= waitHuman) break;
    await sleep(1000);
    waitedMs += 1000;
  }

  const fp = await readFingerprint().catch(() => ({}));
  let cookies = [];
  try {
    const got = await session.cdp.send('Network.getCookies', { urls: [msg.url] });
    cookies = (got && got.cookies) || [];
  } catch {
    cookies = [];
  }

  return {
    status: fp.responseStatus || (nav.errorText ? 0 : 200),
    finalUrl: await session.evaluate('location.href').catch(() => nav.url),
    url: nav.url,
    errorText: nav.errorText || null,
    title: fp.title || '',
    html,
    waitedMs,
    cookies: cookies.map((c) => ({ name: c.name, value: c.value, domain: c.domain, path: c.path })),
    fingerprint: {
      ua: fp.ua,
      platform: fp.platform,
      languages: fp.languages,
      timezone: fp.timezone,
      hardwareConcurrency: fp.hardwareConcurrency,
      deviceMemory: fp.deviceMemory,
      maxTouchPoints: fp.maxTouchPoints,
      webdriver: fp.webdriver,
      screen: fp.screen,
      window: fp.window,
      webglVendor: fp.webglVendor,
      canvasHash: fp.canvasHash,
    },
    profileId,
    chromeVersion: kernel.version,
    pid: session.pid,
  };
}

async function handle(msg) {
  switch (msg.cmd) {
    case 'ping':
      return { pong: true, node: process.version, core: coreRoot, headless: opts.headless };
    case 'open':
      return cmdOpen(msg);
    case 'eval': {
      if (!session) throw new Error('还没有打开任何页面');
      return { value: await session.evaluate(msg.expr, { awaitPromise: !!msg.awaitPromise }) };
    }
    case 'cookies': {
      if (!session) throw new Error('还没有打开任何页面');
      const got = await session.cdp.send('Network.getCookies', { urls: msg.urls || [] });
      return { cookies: (got && got.cookies) || [] };
    }
    case 'close':
      if (session) {
        await session.close();
        session = null;
        sessionProfileId = null;
      }
      return { closed: true };
    case 'shutdown': {
      if (session) {
        await session.close();
        session = null;
      }
      if (kernel) await kernel.closeAll();
      setTimeout(() => process.exit(0), 50);
      return { bye: true };
    }
    default:
      throw new Error(`未知 cmd: ${msg.cmd}`);
  }
}

/* ---------------------------------------------------------------------- */
/* 主循环                                                                  */
/* ---------------------------------------------------------------------- */
const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on('line', async (line) => {
  const text = line.trim();
  if (!text) return;
  let msg;
  try {
    msg = JSON.parse(text);
  } catch {
    process.stdout.write(JSON.stringify({ ok: false, error: '非法 JSON 行' }) + '\n');
    return;
  }
  const id = msg.id ?? null;
  try {
    const data = await handle(msg);
    process.stdout.write(JSON.stringify({ id, ok: true, data }) + '\n');
  } catch (err) {
    process.stdout.write(
      JSON.stringify({ id, ok: false, error: String(err && err.message ? err.message : err) }) + '\n',
    );
  }
});

rl.on('close', async () => {
  try {
    if (session) await session.close();
    if (kernel) await kernel.closeAll();
  } catch {
    /* noop */
  }
  process.exit(0);
});
