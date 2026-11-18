// Mở lần lượt từng link TikTok trong 1 tab nền (Chrome thật của người dùng), đọc tồn kho, gửi kết quả về tab web.
// Không tự chạy: chỉ kiểm tra khi bấm nút trên web. Gặp captcha thì mở tab cho người dùng tự giải, không vượt.
importScripts("extract.js");

const GAP_MS = [6000, 10000];      // nghỉ giữa 2 link (mở dồn dập → TikTok hỏi captcha)
const CAPTCHA_WAIT_MS = 120000;    // chờ người dùng giải captcha, quá thì dừng cả lượt
let running = null;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const isTikTok = u => { try { return /(^|\.)tiktok\.com$/.test(new URL(u).hostname); } catch { return false; } };

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === "ghn-resp" || msg.type === "ghn-state") { ghnEvent(msg, sender.tab && sender.tab.id); return; }
  if (msg.type === "stop") { if (running) running.stop = true; reply({ok: true}); return; }
  if (msg.type !== "check" && msg.type !== "ghn") return;
  if (running) { reply({busy: true}); return; }
  reply({started: true});
  if (msg.type === "ghn") {
    const items = (msg.items || []).filter(it => it && /^[A-Z0-9]{6,20}$/.test(it.code || "")).slice(0, 100);
    runGhn(items, sender.tab.id);
    return;
  }
  const items = (msg.items || []).filter(it => it && isTikTok(it.link)).slice(0, 200);
  run(items, sender.tab.id);
});

function loaded(tabId, timeout) {
  return new Promise(resolve => {
    const done = ok => { clearTimeout(t); chrome.tabs.onUpdated.removeListener(fn); resolve(ok); };
    const fn = (id, info) => { if (id === tabId && info.status === "complete") done(true); };
    const t = setTimeout(() => done(false), timeout);
    chrome.tabs.onUpdated.addListener(fn);
  });
}

async function read(tabId) {
  try {
    const [res] = await chrome.scripting.executeScript({target: {tabId}, func: extractStock});
    return res.result || {ok: false, error: "Không đọc được trang"};
  } catch (e) { return {ok: false, error: "Không đọc được trang: " + e.message}; }
}

// captcha → đưa tab TikTok lên trước để người dùng tự giải, đọc lại tới khi hết captcha / hết giờ / bấm dừng
async function waitCaptcha(tabId, webTab, item, send) {
  await send({qld: "captcha", ...item});
  const tab = await chrome.tabs.update(tabId, {active: true});
  await chrome.windows.update(tab.windowId, {focused: true}).catch(() => {});
  const end = Date.now() + CAPTCHA_WAIT_MS;
  let last = {ok: false, captcha: true};
  while (Date.now() < end && !running.stop) {
    await sleep(2000);
    last = await read(tabId);
    if (!last.captcha && !last.ok) {  // vừa giải xong, trang đang tải lại → đọc thêm vài lần
      for (let i = 0; i < 6 && !last.ok && !last.captcha; i++) { await sleep(1500); last = await read(tabId); }
    }
    if (!last.captcha) break;
  }
  chrome.tabs.update(webTab, {active: true}).catch(() => {});
  if (last.captcha) running.stop = running.reason = "captcha";
  return last;
}

async function checkOne(tabId, item, webTab, send) {
  const wait = loaded(tabId, 30000);
  await chrome.tabs.update(tabId, {url: item.link});
  await wait;
  // dữ liệu có thể chưa có ngay khi trang tải xong → đọc lại vài lần
  let last = {ok: false, error: "Trang không tải được"};
  for (let i = 0; i < 10; i++) {
    last = await read(tabId);
    if (last.ok) return last;
    if (last.captcha) return waitCaptcha(tabId, webTab, item, send);
    await sleep(1500);
  }
  return last;
}

async function run(items, webTab) {
  running = {stop: false};
  const send = msg => chrome.tabs.sendMessage(webTab, msg).catch(() => { running.stop = true; });
  let tab = null;
  try {
    tab = await chrome.tabs.create({url: "about:blank", active: false});
    for (let i = 0; i < items.length && !running.stop; i++) {
      let result;
      try { result = await checkOne(tab.id, items[i], webTab, send); }
      catch (e) { result = {ok: false, error: e.message}; }
      await send({qld: "progress", i: i + 1, n: items.length, ...items[i], result});
      if (i + 1 < items.length && !running.stop) await sleep(GAP_MS[0] + Math.random() * (GAP_MS[1] - GAP_MS[0]));
    }
  } finally {
    if (tab) chrome.tabs.remove(tab.id).catch(() => {});
    await send({qld: "done", stopped: !!running.stop, reason: running.reason || null});
    running = null;
  }
}

// ---------- GHN: API GHN bắt captcha Cloudflare → mở trang donhang.ghn.vn, người dùng tự giải, đọc hành trình ----------
const GHN_GAP_MS = [3000, 5000];       // nghỉ giữa 2 đơn
const GHN_LOAD_MS = 25000;             // chờ trang trả hành trình (không hỏi captcha)
const GHN_CAPTCHA_SHOW_MS = 4000;      // captcha hiện quá lâu (không tự qua) → đưa tab lên trước
let ghnWait = null;                    // {tabId, resolve, captcha, captchaSince}

function ghnEvent(msg, tabId) {
  const w = ghnWait;
  if (!w || tabId !== w.tabId) return;
  if (msg.type === "ghn-state") {
    w.captcha = msg.captcha;
    if (msg.captcha && !w.captchaSince) w.captchaSince = Date.now();
    if (!msg.captcha) w.captchaSince = 0;
    return;
  }
  const b = msg.body || {};
  const got = String((b.data && b.data.order_info && b.data.order_info.order_code) || "").toUpperCase();
  if (got && got !== w.code) return;  // phản hồi muộn của đơn trước
  if (b.code === 200 && b.data) return w.resolve({ok: true, data: b.data});
  const cm = String(b.code_message || "");
  if (/^CAPTCHA_/.test(cm)) return;  // trang tự hiện captcha rồi gọi lại
  if (/^PHONE_VERIFY/.test(cm)) return w.resolve({ok: false, error: "GHN không nhận 4 số cuối SĐT (sai SĐT người nhận?)"});
  w.resolve({ok: false, error: b.message || cm || "GHN trả lỗi"});
}

async function ghnOne(tabId, item, webTab, send) {
  const result = new Promise(resolve => { ghnWait = {tabId, resolve, code: item.code, captcha: false, captchaSince: 0}; });
  const url = `https://donhang.ghn.vn/?order_code=${encodeURIComponent(item.code)}${item.phone4 ? "#qld4=" + item.phone4 : ""}`;
  await chrome.tabs.update(tabId, {url});
  let end = Date.now() + GHN_LOAD_MS, shown = false, out = null;
  result.then(r => { out = r; });
  while (!out && !running.stop && Date.now() < end) {
    await sleep(500);
    const w = ghnWait;
    if (!shown && w && w.captcha && Date.now() - w.captchaSince > GHN_CAPTCHA_SHOW_MS) {
      shown = true;  // captcha không tự qua → đưa tab GHN lên cho người dùng bấm, chờ tối đa 2 phút
      end = Date.now() + CAPTCHA_WAIT_MS;
      await send({qld: "ghn-captcha", ...item});
      const tab = await chrome.tabs.update(tabId, {active: true});
      await chrome.windows.update(tab.windowId, {focused: true}).catch(() => {});
    }
  }
  ghnWait = null;
  if (shown) chrome.tabs.update(webTab, {active: true}).catch(() => {});
  if (out) return out;
  if (shown && !running.stop) { running.stop = running.reason = "captcha"; }
  return {ok: false, error: shown ? "Chưa giải captcha GHN" : running.stop ? "Đã dừng" : "Trang GHN không trả hành trình"};
}

async function runGhn(items, webTab) {
  running = {stop: false};
  const send = msg => chrome.tabs.sendMessage(webTab, msg).catch(() => { running.stop = true; });
  let tab = null;
  try {
    tab = await chrome.tabs.create({url: "about:blank", active: false});
    for (let i = 0; i < items.length && !running.stop; i++) {
      let result;
      try { result = await ghnOne(tab.id, items[i], webTab, send); }
      catch (e) { result = {ok: false, error: e.message}; }
      await send({qld: "ghn-progress", i: i + 1, n: items.length, ...items[i], result});
      if (i + 1 < items.length && !running.stop) await sleep(GHN_GAP_MS[0] + Math.random() * (GHN_GAP_MS[1] - GHN_GAP_MS[0]));
    }
  } finally {
    ghnWait = null;
    if (tab) chrome.tabs.remove(tab.id).catch(() => {});
    await send({qld: "ghn-done", stopped: !!running.stop, reason: running.reason || null});
    running = null;
  }
}
