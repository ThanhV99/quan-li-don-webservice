// Chạy trong trang donhang.ghn.vn (world MAIN): đọc kết quả tracking-logs mà chính trang GHN nhận về
// (sau khi người dùng qua captcha Cloudflare của GHN), chuyển cho ghn_content.js. Không gọi API thay trang.
(() => {
  const want = u => /\/order-tracking\/public-api\/client\/tracking-logs/.test(String(u || ""));
  const send = body => { try { window.postMessage({qldGhn: "resp", body}, location.origin); } catch {} };
  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    const res = await origFetch.apply(this, arguments);
    try {
      if (want(typeof input === "string" ? input : input && input.url)) res.clone().json().then(send).catch(() => {});
    } catch {}
    return res;
  };
  const open = XMLHttpRequest.prototype.open, sendXhr = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) { this.__qldUrl = url; return open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    if (want(this.__qldUrl)) this.addEventListener("load", () => {
      try { send(this.responseType === "json" ? this.response : JSON.parse(this.responseText)); } catch {}
    });
    return sendXhr.apply(this, arguments);
  };
})();
