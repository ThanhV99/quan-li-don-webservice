// Chạy trên trang web quản lí đơn: báo cho trang biết đã cài tiện ích, chuyển lệnh kiểm tra ↔ background.js
document.documentElement.dataset.qldExt = chrome.runtime.getManifest().version;

window.addEventListener("message", e => {
  if (e.source !== window || !e.data || !["check", "stop", "ghn"].includes(e.data.qld)) return;
  chrome.runtime.sendMessage({type: e.data.qld, items: e.data.items}, resp => {
    if (chrome.runtime.lastError) resp = {error: "Tiện ích không trả lời, tải lại trang rồi thử lại"};
    if (resp && (resp.busy || resp.error)) window.postMessage({qld: "error", error: resp.error || "Đang kiểm tra, chờ xong lượt trước"}, location.origin);
  });
});
chrome.runtime.onMessage.addListener(msg => {
  if (msg && msg.qld) window.postMessage(msg, location.origin);
});
