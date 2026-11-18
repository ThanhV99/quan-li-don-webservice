"""Mã hóa / giải mã JSON cho lời gọi hàm từ bot sang web (/api/svc/<module>.<hàm>).

Giữ đúng kiểu Python mà JSON thường làm mất: sqlite3.Row (→ RemoteRow, dùng như Row), tuple, set,
dict có khóa không phải chuỗi (vd {order_id: ...}), bytes, datetime/date, regex đã compile, generator (→ list),
dataclass (vd tracking.TrackResult: phía có lớp đã register() → dựng lại đúng lớp, phía không có → RemoteObj).
File này có 2 bản giống hệt nhau: web/rpc.py và bot/rpc.py – sửa thì chép sang bản kia.
"""
import base64
import dataclasses
import json
import re
import sqlite3
import types
from datetime import date, datetime

_CLASSES: dict[str, type] = {}


def register(*classes: type):
    """Dataclass được dựng lại đúng lớp khi giải mã (web đăng ký TrackResult, TrackEvent)."""
    for c in classes:
        _CLASSES[c.__name__] = c


class RemoteObj:
    """Dataclass của phía bên kia (bot không có lớp TrackResult): đọc / ghi thuộc tính như object thường."""

    def __init__(self, cls_name: str, fields: dict):
        object.__setattr__(self, "_cls", cls_name)
        self.__dict__.update(fields)

    def __repr__(self):
        return f"{self._cls}({', '.join(f'{k}={v!r}' for k, v in self.__dict__.items() if k != '_cls')})"

    def __eq__(self, other):  # so theo nội dung như dataclass (bản sao qua API không bao giờ `is` bản gốc)
        if isinstance(other, RemoteObj):
            return self.__dict__ == other.__dict__
        if dataclasses.is_dataclass(other) and not isinstance(other, type):
            return self._cls == type(other).__name__ and all(
                self.__dict__.get(f.name) == getattr(other, f.name) for f in dataclasses.fields(other))
        return NotImplemented

    __hash__ = None  # như dataclass mặc định (eq=True): không dùng làm khóa dict


class RemoteRow:
    """Bản sao sqlite3.Row sau khi qua API: r["cột"], r[0], r.keys(), dict(r), duyệt = các giá trị.
    Cố ý không phải dict, để code phân biệt bản ghi đã lưu (Row) với dữ liệu nháp (dict) vẫn đúng."""
    __slots__ = ("_keys", "_values", "_index")

    def __init__(self, keys, values):
        self._keys = list(keys)
        self._values = list(values)
        self._index = {k: i for i, k in enumerate(self._keys)}

    def keys(self):
        return list(self._keys)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._index[key]]
        except KeyError:
            raise IndexError("No item with that key") from None  # giống sqlite3.Row

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        return isinstance(other, RemoteRow) and self._keys == other._keys and self._values == other._values

    def __hash__(self):
        return hash(tuple(self._values))

    def __repr__(self):
        return f"RemoteRow({dict(zip(self._keys, self._values))!r})"


def _is_tag(k) -> bool:
    return isinstance(k, str) and k.startswith("__") and k.endswith("__")


def encode(o):
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, (sqlite3.Row, RemoteRow)):
        return {"__row__": [list(o.keys()), [encode(v) for v in o]]}
    if isinstance(o, (list, types.GeneratorType, map, filter, type({}.keys()), type({}.values()))):
        return [encode(v) for v in o]
    if isinstance(o, tuple):
        return {"__tuple__": [encode(v) for v in o]}
    if isinstance(o, (set, frozenset)):
        return {"__set__": [encode(v) for v in o]}
    if isinstance(o, dict):
        if all(isinstance(k, str) and not _is_tag(k) for k in o):
            return {k: encode(v) for k, v in o.items()}
        return {"__dict__": [[encode(k), encode(v)] for k, v in o.items()]}
    if isinstance(o, (bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(o)).decode()}
    if isinstance(o, datetime):
        return {"__dt__": o.isoformat()}
    if isinstance(o, date):
        return {"__date__": o.isoformat()}
    if isinstance(o, re.Pattern):
        return {"__re__": [o.pattern, o.flags]}
    if isinstance(o, RemoteObj):
        return {"__obj__": [o._cls, {k: encode(v) for k, v in o.__dict__.items() if k != "_cls"}]}
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {"__obj__": [type(o).__name__, {f.name: encode(getattr(o, f.name)) for f in dataclasses.fields(o)}]}
    raise TypeError(f"Không gửi được kiểu {type(o).__name__} qua API")


def _hashable(v):
    return tuple(v) if isinstance(v, list) else v


def decode(o):
    if isinstance(o, list):
        return [decode(v) for v in o]
    if not isinstance(o, dict):
        return o
    if len(o) == 1:
        (tag, val), = o.items()
        if tag == "__row__":
            return RemoteRow(val[0], [decode(v) for v in val[1]])
        if tag == "__tuple__":
            return tuple(decode(v) for v in val)
        if tag == "__set__":
            return {_hashable(decode(v)) for v in val}
        if tag == "__dict__":
            return {_hashable(decode(k)): decode(v) for k, v in val}
        if tag == "__bytes__":
            return base64.b64decode(val)
        if tag == "__dt__":
            return datetime.fromisoformat(val)
        if tag == "__date__":
            return date.fromisoformat(val)
        if tag == "__re__":
            return re.compile(val[0], val[1])
        if tag == "__obj__":
            fields = {k: decode(v) for k, v in val[1].items()}
            cls = _CLASSES.get(val[0])
            return cls(**fields) if cls else RemoteObj(val[0], fields)
    return {k: decode(v) for k, v in o.items()}


def dumps(o) -> bytes:
    return json.dumps(encode(o), ensure_ascii=False).encode()


def loads(data: bytes | str):
    return decode(json.loads(data))
