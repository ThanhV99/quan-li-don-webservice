"""Đọc / định dạng số tiền (VND)."""
import os
import re


def parse_money(text: str) -> int | None:
    """Đọc số tiền, đơn vị mặc định là NGHÌN đồng.

    1 → 1.000đ · 150 → 150.000đ · 1.5 → 1.500đ · 1500 → 1.500.000đ
    150k → 150.000đ · 1.2tr / 1.2m → 1.200.000đ
    4tr3 → 4.300.000đ · 4tr387 → 4.387.000đ · 4tr05 → 4.050.000đ
    150.000 / 1.200.000 (có dấu chấm ngăn cách hàng nghìn) → giữ nguyên số đồng
    """
    s = text.lower().replace(" ", "").replace("đ", "").replace("vnd", "")
    m = re.fullmatch(r"(\d+)(?:tr|m)(\d{1,3})", s)  # 4tr3, 4tr387: phần sau "tr" là số lẻ của triệu
    if m:
        return int(m[1]) * 1_000_000 + int(m[2].ljust(3, "0")) * 1_000
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)(k|tr|m)", s)
    if m:
        num, unit = m.groups()
        mult = 1_000 if unit == "k" else 1_000_000
        return int(round(float(num.replace(",", ".")) * mult))
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", s):  # viết đủ số đồng: 150.000
        return int(re.sub(r"[.,]", "", s))
    if re.fullmatch(r"\d+(?:[.,]\d+)?", s):  # số trơn = nghìn đồng
        return int(round(float(s.replace(",", ".")) * 1_000))
    return None


# "dưới 4tr8" / "<4tr8" = mua được với giá bất kỳ dưới mức đó, lưu thành khoảng (0, 4tr8)
UNDER_RE = re.compile(r"^(?:dưới|duoi|tối đa|toi da|max|<=?|≤)\s*")
# "từ 70 trở lên" / "trên 70" / ">70" / "70+" = từ mức đó trở lên, lưu thành khoảng (70k, UP)
UP = -1  # giá trị cột *_max nghĩa là "trở lên" (không giới hạn trên)
UP_PREFIX_RE = re.compile(r"^(?:từ|tu|trên|tren|>=?|≥)\s*")
UP_SUFFIX_RE = re.compile(r"\s*(?:trở lên|tro len|\+)$")


def parse_range(text: str) -> tuple[int, int | None] | None:
    """Đọc 1 giá hoặc 1 khoảng giá: "4tr3" → (4300000, None); "150-200k" → (150000, 200000);
    "dưới 4tr8" → (0, 4800000); "từ 70 trở lên" → (70000, UP).

    Ở khoảng giá, nếu vế đầu không có đơn vị thì lấy đơn vị của vế sau ("150-200k" = 150k-200k).
    """
    s = text.strip().lower()
    if m := UNDER_RE.match(s):
        hi = parse_money(s[m.end():])
        return (0, hi) if hi else None
    if UP_SUFFIX_RE.search(s) or (UP_PREFIX_RE.match(s) and not re.search(r"[-–~]|đến|den|tới|toi", s)):
        lo = parse_money(UP_PREFIX_RE.sub("", UP_SUFFIX_RE.sub("", s)).strip())
        return (lo, UP) if lo else None
    s = re.sub(r"(?<!\w)(đến|den|tới|toi)(?!\w)", "-", s)  # "4tr2 đến 4tr4" = "4tr2-4tr4"
    s = re.sub(r"^[^\d]+", "", s)  # bỏ chữ phía trước: "khoảng", "tầm", "từ", "giá"...
    parts = [p.strip() for p in re.split(r"\s*[-–~]\s*", s) if p.strip()]
    if len(parts) == 1:
        v = parse_money(parts[0])
        return (v, None) if v is not None else None
    if len(parts) != 2:
        return None
    lo_txt, hi_txt = parts
    unit = re.search(r"(k|tr|m)\d*$", hi_txt.lower())
    if unit and not re.search(r"[a-z]", lo_txt.lower()):
        lo_txt += unit[1]
    lo, hi = parse_money(lo_txt), parse_money(hi_txt)
    if lo is None or hi is None:
        return None
    lo, hi = min(lo, hi), max(lo, hi)
    return (lo, hi if hi != lo else None)


def money(n) -> str:
    return f"{int(n or 0):,}".replace(",", ".")  # không ghi đơn vị (người dùng yêu cầu): 4.300.000


def money_short(n: int) -> str:
    """Kiểu viết tắt: 4300000 → 4tr3 · 4160000 → 4tr160 · 4000000 → 4tr · 200000 → 200 (nghìn, không ghi "k")."""
    n = int(n or 0)
    if n >= 1_000_000 and n % 1_000 == 0:
        tr, rest = divmod(n, 1_000_000)
        if not rest:
            return f"{tr}tr"
        # 4tr3 (chẵn trăm nghìn) · 4tr160 / 4tr387 (đủ 3 số như cách hay viết)
        return f"{tr}tr{rest // 100_000}" if rest % 100_000 == 0 else f"{tr}tr{rest // 1_000:03d}"
    if n >= 1_000 and n % 1_000 == 0:
        return f"{n // 1_000}"
    return money(n)


# món sale nhập 1 con số: giá = mua được dưới mức đó, công = từ mức đó trở lên
SINGLE_UNDER = {"gia_sale"}
SINGLE_UP = {"cong"}


def range_label(lo, hi, field, fmt) -> str | None:
    """Cách đọc riêng của 1 khoảng (dưới X / từ X trở lên); None = hiển thị bình thường."""
    if isinstance(fmt, str):  # bot gọi qua API gửi tên hàm định dạng ("money" / "money_short"), không gửi được hàm
        fmt = {"money": money, "money_short": money_short}[fmt]
    if hi == UP or (not hi and lo and field in SINGLE_UP):
        return f"từ {fmt(lo)} trở lên"
    if hi and not lo:
        return f"dưới {fmt(hi)}"
    if not hi and lo and field in SINGLE_UNDER:
        return f"dưới {fmt(lo)}"
    return None


def range_short(lo, hi=None, field=None) -> str:
    """Khoảng giá viết tắt: (150000, 200000) → 150-200 · (4000000, 4400000) → 4tr - 4tr4 · (0, 4800000) → dưới 4tr8.

    field="gia_sale": 1 giá → "dưới 4tr8" · field="cong": 1 mức → "từ 70 trở lên".
    """
    if label := range_label(lo, hi, field, money_short):
        return label
    if not hi or hi == lo:
        return money_short(lo)
    if hi < 1_000_000 and lo % 1_000 == 0 and hi % 1_000 == 0:
        return f"{lo // 1_000}-{hi // 1_000}"
    return f"{money_short(lo)} - {money_short(hi)}"


def social_link(kind: str, text: str | None) -> str | None:
    """Ô Zalo / Facebook của người đặt → link mở được. kind "zalo": SĐT → https://zalo.me/<số>;
    kind "facebook": tên trang / "fb.com/x" → https://facebook.com/x. Link đầy đủ giữ nguyên. None nếu để trống."""
    t = (text or "").strip()
    if not t or t == "-":
        return None
    if re.match(r"https?://", t, re.IGNORECASE):
        return t
    digits = re.sub(r"[\s.\-]", "", t)
    if kind == "zalo" and re.fullmatch(r"\+?\d{9,12}", digits):
        return "https://zalo.me/" + re.sub(r"^\+?84", "0", digits)
    if re.match(r"(www\.|m\.)?(zalo\.me|facebook\.com|fb\.com|fb\.me)/", t, re.IGNORECASE):
        return "https://" + t
    if kind == "facebook" and re.fullmatch(r"[\w.]+", t):
        return "https://facebook.com/" + t
    raise ValueError(f"Link {'Zalo' if kind == 'zalo' else 'Facebook'} không hợp lệ: {t}")


PCT_RE = re.compile(r"^(?:giảm|giam|-|−)?\s*(\d+(?:[.,]\d+)?)\s*%$", re.IGNORECASE)


def parse_pct(text: str | None) -> float | None:
    """'10%' / 'giảm 12,5%' / '-10%' → 10.0 / 12.5. None nếu không phải phần trăm 0–100."""
    m = PCT_RE.match((text or "").strip())
    v = float(m[1].replace(",", ".")) if m else None
    return v if v is not None and 0 < v < 100 else None


def price_after_discount(goc: int, pct: float, cap: int | None = None) -> int:
    """Giá gốc giảm pct% → giá mua được, làm tròn tới nghìn: 5.200.000 giảm 13% → 4.524.000.
    cap: số tiền giảm tối đa (vd giảm 35% tối đa 2tr5); 0 / None = không giới hạn."""
    off = goc * pct / 100
    if cap:
        off = min(off, cap)
    return round((goc - off) / 1000) * 1000


def discount_pct(goc: int | None, price: int | None) -> int | None:
    """% giảm so với giá gốc (làm tròn), None nếu thiếu số hoặc không rẻ hơn."""
    if not goc or not price or price >= goc:
        return None
    return round((1 - price / goc) * 100)


def pct_text(v: float) -> str:
    return f"{v:g}%"


# ---------- nhóm địa chỉ giống nhau (trang Đơn hàng, người dùng yêu cầu 11/2026) ----------
# "Ngõ 34 láng hạ" ≈ "12B 34 láng hạ"; "10 lh" ≈ "10 láng hạ"; "8 ngõ 72 nguyễn trãi" ≈ "72 nguyễn trãi thanh xuân"

ADDR_FILLER = {"toa", "so", "sn", "ngo", "ngach", "hem", "kiet", "duong", "pho", "d", "p", "dc", "dia", "chi"}


def _fold(text: str | None) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", (text or "").lower().replace("đ", "d"))
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _parse_aliases(raw: str) -> dict[str, str]:
    """.env ADDR_ALIASES = "tên gọi khác=địa chỉ thật|…" (vd "toa abc=12 lang ha") → {khóa đã chuẩn hóa: địa chỉ}."""
    out = {}
    for item in raw.split("|"):
        name, sep, addr = item.partition("=")
        words = [t for t in re.split(r"[^0-9a-z]+", _fold(name)) if t and t not in ADDR_FILLER]
        if sep and words and addr.strip():
            out[" ".join(words)] = addr.strip()
    return out


# tên gọi khác của 1 địa chỉ → địa chỉ thật (thông tin riêng của người dùng: đặt trong .env, không ghi trong code)
ADDR_ALIASES = _parse_aliases(os.getenv("ADDR_ALIASES", ""))


def addr_core(text: str | None) -> tuple[str | None, list[str]]:
    """(số nhà / số ngõ, các từ tên đường) – lấy số thuần cuối cùng đứng ngay trước tên đường
    ("8 ngõ 72 nguyễn trãi" → ("72", [nguyen, trai])). Không có số → (None, mọi từ)."""
    toks = [t for t in re.split(r"[^0-9a-z]+", _fold(text)) if t]
    for i in range(len(toks) - 2, -1, -1):
        if toks[i].isdigit():
            street = [t for t in toks[i + 1:] if t not in ADDR_FILLER]
            if street and not street[0].isdigit() and toks[i + 1] not in ADDR_FILLER:
                return toks[i], street
    words = [t for t in toks if t not in ADDR_FILLER] or toks
    alias = ADDR_ALIASES.get(" ".join(words))
    return addr_core(alias) if alias else (None, words)


def _acronym_of(short: str, words: list[str]) -> bool:
    return len(short) >= 2 and len(words) >= len(short) and "".join(w[0] for w in words[:len(short)]) == short


def _street_match(a: list[str], b: list[str]) -> bool:
    if len(a) > len(b):
        a, b = b, a
    if a == b[:len(a)]:  # "nguyen trai" ⊂ "nguyen trai thanh xuan"
        return True
    return len(a) >= 1 and _acronym_of(a[0], b) and a[1:] == b[len(a[0]):len(a[0]) + len(a) - 1]  # "ntt" = nguyen thi thap


def addr_groups(addresses) -> dict[str, tuple[str, str]]:
    """Gom các địa chỉ giống nhau → {địa chỉ gốc: (khóa nhóm, tên nhóm)}. Địa chỉ trống → ("-", "")."""
    raw = {}
    for a in addresses:
        a = (a or "").strip()
        raw[a] = raw.get(a, 0) + 1
    items = [a for a in raw if a]
    cores = {a: addr_core(a) for a in items}
    parent = {a: a for a in items}

    def root(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, a in enumerate(items):
        na, sa = cores[a]
        for b in items[i + 1:]:
            nb, sb = cores[b]
            same = (na == nb and sa and sb and _street_match(sa, sb)) if na else (not nb and sa == sb)
            if same:
                parent[root(a)] = root(b)
    groups: dict[str, list[str]] = {}
    for a in items:
        groups.setdefault(root(a), []).append(a)
    out = {"": ("-", "")}
    for members in groups.values():
        # tên nhóm: địa chỉ viết đủ (không phải viết tắt) dùng nhiều nhất, số nhà đứng đầu, ngắn hơn
        def rank(a):
            n, s = cores[a]
            short = len(s) == 1 and any(_acronym_of(s[0], cores[b][1]) for b in members)
            lead = _fold(a).split().index(n) if n and n in _fold(a).split() else 0
            alias = not cores[a][0] or cores[a][0] not in _fold(a).split()  # tên gọi khác → không làm tên nhóm
            return alias or short, -raw[a], lead, len(a)
        label = min(members, key=rank)
        # khóa nhóm (lọc ?dc=): số nhà + tên đường ngắn nhất viết đủ ("72 nguyen trai"), ít đổi khi thêm đơn
        # tên gọi khác (ADDR_ALIASES) đã đổi sang địa chỉ thật nên dùng được làm khóa; chỉ bỏ tên tắt ("ntt")
        full = [a for a in members if not (len(cores[a][1]) == 1 and any(_acronym_of(cores[a][1][0], cores[x][1])
                                                                       for x in members))] or members
        n, s = min((cores[a] for a in full), key=lambda c: (len(c[1]), c[1]))
        key = " ".join(([n] if n else []) + s)
        for a in members:
            out[a] = (key, label)
    return out
