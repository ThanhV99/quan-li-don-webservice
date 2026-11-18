// Chạy trong tab TikTok (chrome.scripting.executeScript): đọc tồn kho từ dữ liệu "skus" có sẵn trong trang.
// Hàm phải tự chứa (được gửi sang tab), không dùng biến bên ngoài.
function extractStock(htmlText, titleText) {
  const page = htmlText ?? document.documentElement.outerHTML;
  const title = titleText ?? document.title;
  if (/Security Check/i.test(title) || page.includes('id="captcha-config"') || page.includes("captcha-verify"))
    return {ok: false, captcha: true, error: "TikTok yêu cầu xác minh (captcha) – mở link này trong Chrome, xác minh rồi kiểm tra lại"};

  // đọc 1 mảng JSON bắt đầu ở vị trí "[" (đếm ngoặc, bỏ qua chuỗi)
  const arrayAt = start => {
    let depth = 0, str = false;
    for (let i = start; i < page.length; i++) {
      const c = page[i];
      if (str) { if (c === "\\") i++; else if (c === '"') str = false; continue; }
      if (c === '"') str = true;
      else if (c === "[" || c === "{") depth++;
      else if (c === "]" || c === "}") { if (--depth === 0) { try { return JSON.parse(page.slice(start, i + 1)); } catch { return null; } } }
    }
    return null;
  };
  let skus = [];
  for (const m of page.matchAll(/"skus"\s*:\s*\[/g)) {
    const arr = arrayAt(m.index + m[0].length - 1);
    if (!Array.isArray(arr)) continue;
    skus = arr.filter(s => s && typeof s === "object" && "stock" in s).map(s => ({
      name: (s.sku_sale_props || []).map(p => p && p.prop_value).filter(Boolean).join(" / ") || "Mặc định",
      stock: parseInt(s.stock) || 0,
      status: s.status ?? null,  // 1 = đang bán (đoán, TikTok không công bố)
    }));
    if (skus.length) break;
  }
  const name = (title || "").replace(/\s*[-|]\s*TikTok Shop.*$/i, "").trim();
  const sold = (page.match(/"sold_count"\s*:\s*"?(\d+)/) || [])[1];
  if (!skus.length) return {ok: false, error: "Không thấy dữ liệu tồn kho trên trang (sản phẩm ngừng bán hoặc TikTok đổi trang)", title: name};
  const total = skus.filter(s => s.status === 1 || s.status === null).reduce((a, s) => a + s.stock, 0);
  return {ok: true, title: name, sold: sold ? +sold : null, total, in_stock: total > 0, skus, url: typeof location !== "undefined" ? location.href : null};
}
