"""Đọc / xuất danh sách săn sale dạng tin nhắn:

- ram viper x1 4tr3 công 200 sl 2 shop Hải Nam
https://shop.tiktok.com/...
https://shop.tiktok.com/...

- vga 3050 4tr-4tr2 công 150-200k
https://shop.tiktok.com/...

- i5 14600KF dưới 4tr8 công 70 shop dttech
hoặc dưới 4tr5 công 100
https://vt.tiktok.com/...

- i5 14400F giảm 10% gốc 3tr2 công 50     (giá mua được = gốc giảm 10% = 2tr880)

"shop <tên>" (không bắt buộc) đứng cuối dòng. 1 sản phẩm bán ở nhiều shop: mỗi shop 1 dòng cùng tên.
Dòng "hoặc <giá> công <công>" ngay dưới món = thêm 1 mức giá khác (giá thấp hơn thì công cao hơn…).
"""
import html
import json
import re

from utils import discount_pct, money, money_short, parse_money, parse_pct, parse_range, pct_text, price_after_discount, range_short

URL_RE = re.compile(r"https?://\S+")
CONG_RE = re.compile(r"\s+c[ôo]ng\s+", re.IGNORECASE)
SHOP_RE = re.compile(r"(?:^|\s)shop\s*[:.]?\s+(.+)$", re.IGNORECASE)
QTY_RE = re.compile(r"(?<!\w)(?:sl|s\.l|số lượng|so luong)\s*[:.]?\s*(\d+)(?!\w)", re.IGNORECASE)
PRICE_RANGE_RE = re.compile(r"\s(\S+\s*[-–~]\s*\S+)$")
PRICE_UNDER_RE = re.compile(r"\s((?:dưới|duoi|tối đa|toi da|max|<=?|≤)\s*\S+)$", re.IGNORECASE)
GOC_RE = re.compile(r"(?<!\w)(?:giá gốc|gia goc|gốc|goc)\s*[:.]?\s*(\S+)", re.IGNORECASE)
PCT_TAIL_RE = re.compile(r"\s((?:giảm|giam|-|−)\s*\d+(?:[.,]\d+)?\s*%)$", re.IGNORECASE)
# cuối mức giá: "giảm 20%" hoặc "giảm 20% tối đa 1tr" (% đã nhập + số tiền giảm tối đa của mức đó)
TIER_DISC_RE = re.compile(r"\s+(?:giảm|giam)\s*(\d+(?:[.,]\d+)?\s*%)(?:\s+(?:tối đa|toi da)\s+(\S+))?\s*$", re.IGNORECASE)
# giá dòng món: "giảm 35% tối đa 2tr5" (cần giá gốc)
PCT_CAP_RE = re.compile(r"\s((?:giảm|giam|-|−)\s*\d+(?:[.,]\d+)?\s*%)\s+(?:tối đa|toi da)\s+(\S+)$", re.IGNORECASE)
TIER_PREFIX_RE = re.compile(r"^(?:hoặc|hoac|or)\s+", re.IGNORECASE)
PRICE_UP_RE = re.compile(r"\s((?:(?:từ|tu|trên|tren|>=?|≥)\s*)?\S+\s*(?:trở lên|tro len|\+)|(?:trên|tren|>=?|≥)\s*\S+)$",
                         re.IGNORECASE)


def split_shop(text: str) -> tuple[str, str | None]:
    """'i5 12400F 2tr5 shop Hải Nam' → ('i5 12400F 2tr5', 'Hải Nam')."""
    if m := SHOP_RE.search(text):
        return text[:m.start()].strip(), " ".join(m[1].split())
    return text.strip(), None


def parse_item_line(line: str) -> dict | None:
    """'- ram viper x1 4tr3 công 200 shop X' → dict món sale (chưa có link). None nếu không đọc được."""
    line, shop = split_shop(line.strip().lstrip("-•*+").strip())
    # số lượng: "sl 3" / "số lượng: 3" ở bất kỳ đâu trong dòng
    qty = None
    if m := QTY_RE.search(line):
        qty = int(m[1])
        line = (line[:m.start()] + " " + line[m.end():]).strip()
    # giá gốc: "gốc 5tr2" ở bất kỳ đâu
    goc = None
    if (m := GOC_RE.search(line)) and (goc := parse_money(m[1])):
        line = (line[:m.start()] + " " + line[m.end():]).strip()
    left, *right = CONG_RE.split(line, maxsplit=1)
    left = left.strip()
    # giá nằm cuối, bên trái chữ "công": 1 giá (4tr3), khoảng giá (4tr-4tr4, 4tr - 4tr4) hoặc "giảm 10%" (cần giá gốc)
    price, name, pct, cap = None, "", None, None
    m = PRICE_UNDER_RE.search(left) or PRICE_UP_RE.search(left) or PRICE_RANGE_RE.search(left)
    if (mp := PCT_CAP_RE.search(left)) and goc and (pct := parse_pct(mp[1])) and (cap := parse_money(mp[2])):
        price, name = (price_after_discount(goc, pct, cap), None), left[:mp.start()]
    elif (mp := PCT_TAIL_RE.search(left)) and goc and (pct := parse_pct(mp[1])):
        price, name = (price_after_discount(goc, pct), None), left[:mp.start()]
    elif m and (price := parse_range(m[1])):
        name = left[:m.start()]
    elif len(tokens := left.split()) >= 2 and (price := parse_range(tokens[-1])):
        name = " ".join(tokens[:-1])
    name = " ".join(name.split())
    if not price or not name:
        return None
    cong = (0, None)
    if right:
        cong = parse_range(right[0].strip())
        if not cong:
            return None
    return {
        "ten_sp": name,
        "shop": shop,
        "gia_sale": price[0], "gia_sale_max": price[1],
        "cong": cong[0], "cong_max": cong[1],
        "so_luong": qty,
        "gia_goc": goc,
        "giam_pt": pct,
        "giam_toi_da": cap,
        "links": [],
    }


def parse_tier(text: str) -> dict | None:
    """'dưới 4tr5 công 100 [giảm 20% tối đa 1tr]' → {gia_sale, gia_sale_max, cong, cong_max, giam_pt, giam_toi_da}.
    None nếu không đọc được."""
    text = " " + TIER_PREFIX_RE.sub("", text.strip().lstrip("-•*+").strip())
    pct = cap = None
    if m := TIER_DISC_RE.search(text):
        pct, cap = parse_pct(m[1]), parse_money(m[2]) if m[2] else None
        if not pct or (m[2] and not cap):
            return None
        text = text[:m.start()]
    left, *right = CONG_RE.split(text, maxsplit=1)
    left = re.sub(r"^(?:giá|gia)\s+", "", left.strip(), flags=re.IGNORECASE)
    price = parse_range(left) if left else None
    if not price:
        return None
    cong = parse_range(right[0]) if right else (0, None)
    if not cong:
        return None
    return {"gia_sale": price[0], "gia_sale_max": price[1], "cong": cong[0], "cong_max": cong[1],
            "giam_pt": pct, "giam_toi_da": cap}


def tier_text(t: dict) -> str:
    """Dạng lưu / dạng dán của 1 mức: 'dưới 4tr5 công từ 100 trở lên [giảm 20% tối đa 1tr]'."""
    line = range_short(t["gia_sale"], t["gia_sale_max"], "gia_sale")
    if t["cong"] or t["cong_max"]:
        line += f" công {range_short(t['cong'], t['cong_max'], 'cong')}"
    if t.get("giam_pt"):
        line += f" giảm {pct_text(t['giam_pt'])}" + (f" tối đa {money_short(t['giam_toi_da'])}" if t.get("giam_toi_da") else "")
    return line


def rediscount_tiers(muc_gia: str | None, goc: int | None) -> str | None:
    """Đổi giá gốc → tính lại giá của các mức có % giảm (giữ % và giảm tối đa)."""
    if not muc_gia or not goc:
        return muc_gia
    out = []
    for t in (parse_tier(line) for line in muc_gia.splitlines()):
        if t and t["giam_pt"]:
            t.update(gia_sale=price_after_discount(goc, t["giam_pt"], t["giam_toi_da"]), gia_sale_max=None)
        if t:
            out.append(tier_text(t))
    return "\n".join(out) or None


def parse_tiers(text: str | None) -> tuple[str | None, list[str]]:
    """Ô "mức giá khác" (mỗi dòng hoặc mỗi đoạn cách nhau ';' là 1 mức) → (chuỗi chuẩn để lưu, dòng lỗi)."""
    tiers, errors = [], []
    for part in re.split(r"[\n;]+", text or ""):
        if not part.strip() or part.strip() == "-":
            continue
        if t := parse_tier(part):
            tiers.append(tier_text(t))
        else:
            errors.append(part.strip())
    return "\n".join(tiers) or None, errors


def tiers_of(r) -> list[dict]:
    """Các mức giá khác đã lưu của 1 dòng sales."""
    keys = r.keys()
    return [t for line in ((r["muc_gia"] if "muc_gia" in keys else None) or "").splitlines()
            if (t := parse_tier(line))]


GROUP_HEADER_RE = re.compile(r"\s*nh[oó]m\s*(\d{1,2})\s*(?:\n|$)", re.IGNORECASE)


def split_group(text: str) -> tuple[int | None, str]:
    """Dòng đầu "nhóm 1" của danh sách dán → (1, phần còn lại); không có → (None, text)."""
    m = GROUP_HEADER_RE.match(text or "")
    return (int(m[1]), text[m.end():]) if m and int(m[1]) > 0 else (None, text)


def parse_block(text: str) -> tuple[list[dict], list[str]]:
    """Đọc cả khối tin nhắn. Trả về (danh sách món, các dòng không đọc được)."""
    items, errors = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        urls = URL_RE.findall(line)
        if urls and not URL_RE.sub("", line).strip(" -•"):
            # dòng chỉ có link → thuộc về món phía trên
            if items:
                items[-1]["links"] += urls
            else:
                errors.append(line)
            continue
        if TIER_PREFIX_RE.match(line) and items and (tier := parse_tier(URL_RE.sub("", line))):
            items[-1].setdefault("tiers", []).append(tier_text(tier))  # "hoặc dưới 4tr5 công 100"
            items[-1]["links"] += urls
            continue
        item = parse_item_line(URL_RE.sub("", line))
        if item:
            item["links"] += urls  # link nằm cùng dòng với tên
            items.append(item)
        else:
            errors.append(line)
    for it in items:
        it["link"] = "\n".join(it.pop("links")) or None
        it["muc_gia"] = rediscount_tiers("\n".join(it.pop("tiers", [])) or None, it["gia_goc"])
    return items, errors


def item_line(r) -> str:
    """Dòng tiêu đề 1 món theo đúng định dạng nhập: '- tên giá công X'."""
    get = r.get if isinstance(r, dict) else (lambda k: r[k] if k in r.keys() else None)
    if get("giam_pt") and get("gia_goc"):  # dán lại sẽ tính lại từ giá gốc
        price = f"giảm {pct_text(get('giam_pt'))}" + (f" tối đa {money_short(get('giam_toi_da'))}" if get("giam_toi_da") else "")
    else:
        price = range_short(r['gia_sale'], r['gia_sale_max'], 'gia_sale')
    line = f"- {r['ten_sp']} {price}"
    if r["cong"] or r["cong_max"]:
        line += f" công {range_short(r['cong'], r['cong_max'], 'cong')}"
    if get("gia_goc"):
        line += f" gốc {money_short(get('gia_goc'))}"
    if get("so_luong"):
        line += f" sl {r['so_luong']}"
    if get("shop"):
        line += f" shop {r['shop']}"
    return line


def sale_pct(r, goc: int | None) -> float | None:
    """% giảm của 1 mức giá so với giá gốc: % đã nhập (giam_pt), không thì tính từ giá (khoảng / "dưới X" → X)."""
    keys = r.keys()
    if "giam_pt" in keys and r["giam_pt"]:
        return r["giam_pt"]
    hi = r["gia_sale_max"] if r["gia_sale_max"] and r["gia_sale_max"] > 0 else None
    return discount_pct(goc, hi or r["gia_sale"])


def goc_note(r, goc: int | None) -> str:
    """' (gốc 5tr2, giảm 13%)' sau giá mua được; '' nếu chưa có giá gốc."""
    if not goc:
        return ""
    pct = sale_pct(r, goc)
    cap = r["giam_toi_da"] if "giam_toi_da" in r.keys() and pct else None
    return (f" <i>(gốc {money_short(goc)}" + (f", giảm {pct_text(pct)}" if pct else "")
            + (f" tối đa {money_short(cap)}" if cap else "") + ")</i>")


def pct_note(r, goc: int | None) -> str:
    """/sale (gửi khách, không ghi giá gốc): ' (mã -35%)' sau giá mua được; '' nếu không biết % giảm."""
    pct = sale_pct(r, goc)
    return f" <i>(mã -{pct_text(pct)})</i>" if pct else ""


def _offer_lines(r, show_status: bool, show_goc: bool = False) -> tuple[str, list[str]]:
    """1 dòng sales (1 shop): (dòng shop · SL, các dòng chi tiết: giá · công, link).
    show_goc: kèm "(gốc …, giảm …%)" sau giá (/saleall); không thì chỉ "(mã -…%)" (/sale gửi khách)."""
    shop = f"🏪 <b>{html.escape(r['shop'])}</b>" if r["shop"] else "🏪 <i>chưa ghi shop</i>"
    if "id" in r.keys():  # id để /suasale, /xoasale
        shop += f"  <code>#{r['id']}</code>"
    qty = r["so_luong"]
    if qty is not None:  # ghi rõ để khách không hiểu nhầm SL 2 = 2 đơn
        shop += f" · 📦 Mua 1 đơn, số lượng {qty}"
    if show_goc and "uu_tien" in r.keys() and r["uu_tien"]:  # /saleall: thứ tự ưu tiên (khách không cần thấy)
        shop += f" · 🔝 ưu tiên {r['uu_tien']}"
    if show_goc and "nhom" in r.keys() and r["nhom"]:  # /saleall: nhóm sale (/sale N)
        shop += f" · 🗂 nhóm {r['nhom']}"
    if show_status and not r["active"]:
        shop = f"<s>{shop}</s>  ⛔ <i>hết sale</i>"
    goc = r["gia_goc"] if "gia_goc" in r.keys() else None
    # /saleall: "(gốc …, giảm …%)"; /sale: chỉ "(mã -35%)", không lộ giá gốc
    note = (lambda x: goc_note(x, goc)) if show_goc else (lambda x: pct_note(x, goc))
    whole = " (cả đơn)" if qty and qty > 1 else ""
    info = [f"💰 Giá mua được{whole} <b>{range_short(r['gia_sale'], r['gia_sale_max'], 'gia_sale')}</b>" + note(r)]
    if r["cong"] or r["cong_max"]:
        info.append(f"🛠 công {range_short(r['cong'], r['cong_max'], 'cong')}")
    extra = []
    for t in tiers_of(r):
        line = f"💰 Hoặc giá <b>{range_short(t['gia_sale'], t['gia_sale_max'], 'gia_sale')}</b>" + note(t)
        if t["cong"] or t["cong_max"]:
            line += f" · 🛠 công {range_short(t['cong'], t['cong_max'], 'cong')}"
        extra.append(line)
    # link đầy đủ (mỗi link 1 dòng) để copy text sang Zalo/FB vẫn giữ được link, kèm số còn lại lần kiểm tra gần nhất
    stock = json.loads(r["ton_kho"] or "{}") if "ton_kho" in r.keys() else {}
    chosen = json.loads(r["phan_loai"] or "{}") if "phan_loai" in r.keys() else {}
    text = f"{r['ten_sp'] or ''} {r['shop'] or ''}"
    links = [f"🔗 {html.escape(u)}" + stock_note(stock.get(u), show_status, chosen.get(u), text)
             for u in (r["link"] or "").split()]
    return shop, [" · ".join(info), *extra, *links]


# ---------- còn hàng theo phân loại ----------
# Link TikTok có nhiều phân loại (vd "1x16GB 5600" / "1x16GB 6000"): chỉ tính phân loại cần mua.
#  - "chosen": người dùng bấm 🎯 chọn trên web (sales.phan_loai = {link: [tên phân loại]})
#  - "auto":   chưa chọn → đoán theo tên sản phẩm + shop của món sale (từ riêng của phân loại có trong tên, vd "6000")
#  - "total":  không đoán được → cộng mọi phân loại đang bán (như trước)
SKU_SELLING = (1, None)  # status TikTok: 1 = đang bán, 2 = hết hàng, 0 = không bán được (đoán, TikTok không công bố)


def _tokens(text: str) -> set[str]:
    """Chữ thường, tách cả ranh giới chữ ↔ số ("6000Mhz" → 6000, mhz); bỏ từ 1 ký tự (số lẻ trong giá, "x1"…)."""
    parts = re.findall(r"[^\W\d_]+|\d+", (text or "").lower())
    return {p for p in parts if len(p) > 1}


def guess_variants(skus: list[dict], text: str) -> list[str]:
    """Tên các phân loại khớp tên món sale nhất (theo từ chỉ phân loại đó có); [] nếu không đoán được."""
    if len(skus) < 2:
        return []
    toks = [_tokens(k.get("name", "")) for k in skus]
    common = set.intersection(*toks)
    want = _tokens(text)
    scores = [len((t - common) & want) for t in toks]
    best = max(scores)
    picked = [k["name"] for k, sc in zip(skus, scores) if best and sc == best]
    return picked if 0 < len(picked) < len(skus) else []


def stock_view(res: dict | None, chosen: list[str] | None = None, text: str = "") -> dict | None:
    """Kết quả còn hàng theo phân loại cần mua: {mode, names, total, in_stock, missing}; None nếu chưa kiểm tra / lỗi."""
    if not res or not res.get("ok"):
        return None
    skus = res.get("skus") or []
    if chosen:
        mode, names = "chosen", list(chosen)
    elif names := guess_variants(skus, text):
        mode = "auto"
    else:
        mode, names = "total", []
    pick = [k for k in skus if k.get("name") in names] if names else skus
    total = sum(k.get("stock") or 0 for k in pick if k.get("status") in SKU_SELLING)
    return {"mode": mode, "names": names, "total": total, "in_stock": total > 0,
            "missing": [n for n in names if n not in {k.get("name") for k in skus}]}


def stock_note(res: dict | None, show_error: bool = False, chosen: list[str] | None = None, text: str = "") -> str:
    """Kết quả nút 📦 Kiểm tra còn hàng trên web (tiện ích Chrome) → ' · 📦 còn 33 (15:12 03/11)', theo phân loại cần
    mua nếu đã chọn / đoán được (' · ⛔ hết hàng – 1x16GB 6000'). Lỗi / captcha chỉ hiện khi show_error (/saleall)."""
    if not res:
        return ""
    at = res.get("at") or ""
    when = f" <i>({at[11:16]} {at[8:10]}/{at[5:7]})</i>" if at else ""
    if res.get("ok"):
        v = stock_view(res, chosen, text)
        which = f" – {html.escape(', '.join(v['names']))}" if v["names"] else ""
        return (f" · 📦 còn <b>{v['total']}</b>" if v["in_stock"] else " · ⛔ <b>hết hàng</b>") + which + when
    return f" · ⚠️ chưa đọc được tồn kho{when}" if show_error else ""


INDENT = "\u00a0" * 4  # khoảng trắng không ngắt để Telegram không bỏ thụt lề


def format_card(rows, index: int, show_status: bool = False, actions=None, show_goc: bool = False) -> str:
    """1 sản phẩm dạng cây (HTML): tên in đậm, mỗi shop 1 nhánh ├ / └, chi tiết thụt vào dưới shop.

    3. I5 14400F
    ├ 🏪 dttech  #4 · 📦 Mua 1 đơn, số lượng 1
    │   💰 Giá mua được dưới 2tr9 · 🛠 công từ 50 trở lên
    │   🔗 https://...
    └ 🏪 dttech 2  #6 · 📦 Mua 1 đơn, số lượng 2
        💰 Giá mua được (cả đơn) dưới 5tr8 · 🛠 công từ 100 trở lên

    actions(row) → HTML thêm 1 dòng thao tác dưới mỗi shop (vd link ➕ Thêm link · ⛔ Tắt sale ở /saleall);
    hoặc dict {id sale: HTML} (bot gọi qua API, không gửi được hàm).
    """
    lines = [f"<b>{index}. {html.escape(rows[0]['ten_sp']).upper()}</b>"]
    for i, row in enumerate(rows):
        last = i == len(rows) - 1
        shop, details = _offer_lines(row, show_status, show_goc)
        if isinstance(actions, dict):
            if actions.get(row["id"]):
                details.append(actions[row["id"]])
        elif actions:
            details.append(actions(row))
        lines.append(("└ " if last else "├ ") + shop)
        lines += [("\u00a0\u00a0" if last else "│") + INDENT + d for d in details]
    return "\n".join(lines) + "\n"


SUB = "  - "  # thụt lề không ngắt (Telegram bỏ dấu cách đầu dòng)


def _price_full(lo, hi, field) -> str:
    """Giá / công viết đủ số (4.000.000) cho /sale gọn: 1 giá = tối đa, khoảng = "X - Y", "từ X trở lên" = X."""
    if field == "gia_sale":
        if hi and not lo:
            return money(hi)
        if hi and hi != lo:
            return f"{money(lo)} - {money(hi)}"
        return money(lo)
    if hi and hi != lo and hi > 0 and lo:
        return f"{money(lo)} - {money(hi)}"
    return money(lo or (hi if hi and hi > 0 else 0))


def _price_label(lo, hi) -> str:
    """'Giá sale tối đa' khi chỉ có 1 mức (mua được dưới mức đó), 'Giá sale' khi là khoảng."""
    return "Giá sale" if lo and hi and hi != lo else "Giá sale tối đa"


def compact_card(rows, show_status: bool = False) -> str:
    """/sale gọn (người dùng yêu cầu 11/2026), mỗi shop:
    • <b>I5 14600KF DTECH</b>
      - Giá sale tối đa: <b>4.000.000</b> | Công: 50.000 | SL: 1
      - Hoặc giá tối đa: 3.700.000 | Công: 200.000
      - Shop/Ghi chú: dttech
      - 🔗 https://… · còn 17
    SL > 1: giá là của cả đơn ("(cả đơn)") để khách không hiểu nhầm SL 2 = 2 đơn."""
    lines = [f"• <b>{html.escape(rows[0]['ten_sp']).upper()}</b>"]
    for r in rows:
        goc = r["gia_goc"] if "gia_goc" in r.keys() else None
        qty = r["so_luong"]
        whole = " (cả đơn)" if qty and qty > 1 else ""
        head = (f"{_price_label(r['gia_sale'], r['gia_sale_max'])}{whole}: "
                f"<b>{_price_full(r['gia_sale'], r['gia_sale_max'], 'gia_sale')}</b>" + pct_note(r, goc))
        if r["cong"] or r["cong_max"]:
            head += f" | Công: {_price_full(r['cong'], r['cong_max'], 'cong')}"
        if qty is not None:
            head += f" | SL: {qty}"
        lines.append(SUB + head)
        for t in tiers_of(r):
            line = (f"Hoặc {_price_label(t['gia_sale'], t['gia_sale_max']).replace('Giá sale', 'giá')}{whole}: "
                    f"<b>{_price_full(t['gia_sale'], t['gia_sale_max'], 'gia_sale')}</b>" + pct_note(t, goc))
            if t["cong"] or t["cong_max"]:
                line += f" | Công: {_price_full(t['cong'], t['cong_max'], 'cong')}"
            lines.append(SUB + line)
        if r["shop"]:
            lines.append(SUB + f"Shop/Ghi chú: {html.escape(r['shop'])}")
        if show_status and not r["active"]:  # /sale all
            lines.append(SUB + "⛔ <i>hết sale</i>")
        stock = json.loads(r["ton_kho"] or "{}") if "ton_kho" in r.keys() else {}
        chosen = json.loads(r["phan_loai"] or "{}") if "phan_loai" in r.keys() else {}
        text = f"{r['ten_sp'] or ''} {r['shop'] or ''}"
        for u in (r["link"] or "").split():
            res, note = stock.get(u), ""
            if res and res.get("ok"):
                v = stock_view(res, chosen.get(u), text)
                note = f" · còn {v['total']}" if v["in_stock"] else " · ⛔ hết hàng"
            lines.append(SUB + f"🔗 {html.escape(u)}{note}")
    return "\n".join(lines) + "\n"


def compact_cards(rows, show_status: bool = False) -> list[str]:
    return [compact_card(g, show_status) for g in group_by_product(rows)]


def group_by_product(rows) -> list[list]:
    """Gộp các dòng cùng tên sản phẩm (không phân biệt hoa thường / khoảng trắng), giữ thứ tự xuất hiện."""
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(" ".join(r["ten_sp"].lower().split()), []).append(r)
    return list(groups.values())


def format_cards(rows, show_status: bool = False, actions=None, show_goc: bool = False) -> list[str]:
    return [format_card(g, i + 1, show_status, actions, show_goc) for i, g in enumerate(group_by_product(rows))]


def format_block(rows, show_status: bool = False) -> list[str]:
    """Xuất danh sách món theo định dạng tin nhắn gốc (đã escape HTML), mỗi phần tử là 1 món."""
    blocks = []
    for r in (row for g in group_by_product(rows) for row in g):  # các shop cùng sản phẩm đứng liền nhau
        head = html.escape(item_line(r))
        if show_status and not r["active"]:
            head += " ⛔ <i>hết sale</i>"
        links = [html.escape(u) for u in (r["link"] or "").split() if u]
        tiers = [html.escape("hoặc " + tier_text(t)) for t in tiers_of(r)]
        blocks.append("\n".join([head, *tiers, *links]) + "\n")
    return blocks
