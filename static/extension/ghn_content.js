// Chạy trên donhang.ghn.vn (world riêng của tiện ích): điền sẵn 4 số cuối SĐT người nhận (#qld4=1234 trên link do
// tiện ích mở, GHN đọc sessionStorage.receiver_phone), chuyển kết quả hành trình + trạng thái captcha về background.js.
const m = location.hash.match(/qld4=(\d{4})/);
if (m) {
  try { sessionStorage.setItem("receiver_phone", m[1]); } catch {}
  history.replaceState(null, "", location.pathname + location.search);
}
const toBg = msg => { try { chrome.runtime.sendMessage(msg).catch(() => {}); } catch {} };
window.addEventListener("message", e => {
  if (e.source === window && e.data && e.data.qldGhn === "resp") toBg({type: "ghn-resp", body: e.data.body});
});
// ô captcha (Cloudflare Turnstile) đang hiện → báo để đưa tab lên trước cho người dùng tự bấm
setInterval(() => {
  const cap = document.querySelector(".captcha-modal, .captcha-widget, iframe[src*='challenges.cloudflare.com']");
  toBg({type: "ghn-state", captcha: !!(cap && cap.getClientRects().length)});
}, 1500);
