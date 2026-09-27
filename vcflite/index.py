"""Genomic indexes: tabix (.tbi), CSI (.csi), and an index we build ourselves.

All of them answer the same two questions:

* ``start_offset(name, pos)`` - an offset at or before the first record of
  contig ``name`` that could overlap 1-based position ``pos``.
* ``sync_points`` - a sorted list of offsets that are known line starts,
  used to page backwards.
"""

from __future__ import annotations

import array
import bisect
import gzip
import hashlib
import json
import os
import struct
import sys
from pathlib import Path


class Cancelled(Exception):
    pass


def _bin_first(level: int) -> int:
    return ((1 << (3 * level)) - 1) // 7


def _bin_parent(b: int) -> int:
    return (b - 1) >> 3


class _BinnedIndex:
    """Shared logic for tabix and CSI (both are hierarchical binning indexes)."""

    min_shift = 14
    n_lvls = 5

    def __init__(self):
        self.names: list[str] = []
        self.refs: list[dict] = []  # per ref: {"bins": {bin: [(beg,end),...]}, "loff": {...}, "lin": array}
        self._sync = None

    # -- helpers -----------------------------------------------------------
    def _bin_range(self, b: int):
        """0-based [start, end) covered by bin ``b``."""
        level = 0
        while level < self.n_lvls and b >= _bin_first(level + 1):
            level += 1
        shift = self.min_shift + 3 * (self.n_lvls - level)
        k = b - _bin_first(level)
        return k << shift, (k + 1) << shift

    def _pseudo_bin(self) -> int:
        return _bin_first(self.n_lvls + 1) + 1

    def _prepare(self):
        pseudo = self._pseudo_bin()
        for ref in self.refs:
            entries = []
            lo, hi = None, None
            for b, chunks in ref["bins"].items():
                if b >= pseudo:
                    continue
                bend = self._bin_range(b)[1]
                for beg, end in chunks:
                    entries.append((bend, beg, end))
                    lo = beg if lo is None or beg < lo else lo
                    hi = end if hi is None or end > hi else hi
            ref["entries"] = entries
            ref["first"] = lo
            ref["last"] = hi
            # htslib keeps (n_mapped, n_unmapped) in the pseudo-bin's 2nd chunk.
            meta = ref["bins"].get(pseudo)
            ref["n"] = meta[1][0] if meta and len(meta) > 1 else None

    def _min_off(self, ref: dict, beg0: int) -> int:
        raise NotImplementedError

    # -- public API ---------------------------------------------------------
    def tid(self, name: str):
        try:
            return self.names.index(name)
        except ValueError:
            return None

    def start_offset(self, name: str, pos: int):
        tid = self.tid(name)
        if tid is None:
            return None
        ref = self.refs[tid]
        if ref["first"] is None:
            return None
        beg0 = max(0, pos - 1)
        min_off = self._min_off(ref, beg0)
        best = None
        for bend, cbeg, cend in ref["entries"]:
            if bend > beg0 and cend > min_off:
                if best is None or cbeg < best:
                    best = cbeg
        if best is None:
            # Past the last record of this contig: resume where it ends.
            return ref["last"]
        return max(best, min_off)

    def contig_start(self, name: str):
        tid = self.tid(name)
        return None if tid is None else self.refs[tid]["first"]

    def record_count(self, name: str):
        """Number of records on ``name``, or None if the index doesn't say."""
        tid = self.tid(name)
        return None if tid is None else self.refs[tid].get("n")

    @property
    def sync_points(self):
        if self._sync is None:
            pts = set()
            for ref in self.refs:
                for _, beg, _ in ref["entries"]:
                    pts.add(beg)
                for v in ref.get("lin") or ():
                    if v:
                        pts.add(v)
                for v in ref.get("loff", {}).values():
                    if v:
                        pts.add(v)
            self._sync = sorted(pts)
        return self._sync


def _read_names(buf: bytes, off: int, l_nm: int):
    raw = buf[off:off + l_nm]
    return [n.decode("utf-8", "replace") for n in raw.split(b"\0") if n]


class TabixIndex(_BinnedIndex):
    kind = "tbi"

    @classmethod
    def load(cls, path: str) -> "TabixIndex":
        with gzip.open(path, "rb") as f:
            buf = f.read()
        if buf[:4] != b"TBI\x01":
            raise ValueError("not a tabix index")
        self = cls()
        n_ref, fmt, col_seq, col_beg, col_end, meta, skip, l_nm = struct.unpack_from("<8i", buf, 4)
        p = 36
        self.names = _read_names(buf, p, l_nm)
        p += l_nm
        for _ in range(n_ref):
            (n_bin,) = struct.unpack_from("<i", buf, p)
            p += 4
            bins = {}
            for _ in range(n_bin):
                b, n_chunk = struct.unpack_from("<Ii", buf, p)
                p += 8
                ch = array.array("Q")
                ch.frombytes(buf[p:p + 16 * n_chunk])
                if sys.byteorder != "little":
                    ch.byteswap()
                p += 16 * n_chunk
                bins[b] = list(zip(ch[0::2], ch[1::2]))
            (n_intv,) = struct.unpack_from("<i", buf, p)
            p += 4
            lin = array.array("Q")
            lin.frombytes(buf[p:p + 8 * n_intv])
            if sys.byteorder != "little":
                lin.byteswap()
            p += 8 * n_intv
            self.refs.append({"bins": bins, "lin": lin})
        self._prepare()
        return self

    def _min_off(self, ref, beg0):
        lin = ref["lin"]
        if not lin:
            return 0
        i = beg0 >> 14
        return lin[i] if i < len(lin) else lin[-1]


class CsiIndex(_BinnedIndex):
    kind = "csi"

    @classmethod
    def load(cls, path: str, header_contigs: list[str] | None = None) -> "CsiIndex":
        with gzip.open(path, "rb") as f:
            buf = f.read()
        if buf[:4] != b"CSI\x01":
            raise ValueError("not a CSI index")
        self = cls()
        self.min_shift, depth, l_aux = struct.unpack_from("<3i", buf, 4)
        self.n_lvls = depth
        p = 16
        aux = buf[p:p + l_aux]
        p += l_aux
        if l_aux >= 28:
            l_nm = struct.unpack_from("<i", aux, 24)[0]
            self.names = _read_names(aux, 28, l_nm)
        (n_ref,) = struct.unpack_from("<i", buf, p)
        p += 4
        for _ in range(n_ref):
            (n_bin,) = struct.unpack_from("<i", buf, p)
            p += 4
            bins, loff = {}, {}
            for _ in range(n_bin):
                b, lo, n_chunk = struct.unpack_from("<IQi", buf, p)
                p += 16
                ch = array.array("Q")
                ch.frombytes(buf[p:p + 16 * n_chunk])
                if sys.byteorder != "little":
                    ch.byteswap()
                p += 16 * n_chunk
                bins[b] = list(zip(ch[0::2], ch[1::2]))
                loff[b] = lo
            self.refs.append({"bins": bins, "loff": loff})
        if not self.names:
            # BCF-style CSI: tids follow the header's contig order.
            self.names = list(header_contigs or [])[:n_ref]
        self._prepare()
        return self

    def _min_off(self, ref, beg0):
        bins = ref["bins"]
        b = _bin_first(self.n_lvls) + (beg0 >> self.min_shift)
        while b:
            if b in bins:
                break
            first = (_bin_parent(b) << 3) + 1
            b = b - 1 if b > first else _bin_parent(b)
        return ref["loff"].get(b, 0)


# ---------------------------------------------------------------------------
# Our own index, for files that have none (plain .vcf, gzip, unindexed bgzf).
# ---------------------------------------------------------------------------

_SPACING = 64 * 1024  # roughly one checkpoint per 64 KB of text


class ScanIndex:
    kind = "scan"
    VERSION = 3

    def __init__(self):
        self.names: list[str] = []
        self.pos: dict[str, list[int]] = {}
        self.off: dict[str, list[int]] = {}
        self.unsorted = False
        self.count: dict[str, int] = {}
        self._sync = None

    def _add(self, chrom: bytes, pos: int, off: int, new: bool):
        name = chrom.decode("utf-8", "replace")
        if new:
            if name in self.pos:
                self.unsorted = True
                return
            self.names.append(name)
            self.pos[name] = []
            self.off[name] = []
        elif name not in self.pos:
            return
        ps = self.pos[name]
        if ps and pos < ps[-1]:
            self.unsorted = True
            return
        ps.append(pos)
        self.off[name].append(off)

    @classmethod
    def build(cls, source, data_start: int, progress=None, cancel=None) -> "ScanIndex":
        idx = cls()
        cur = None  # current contig (bytes)
        cur_tag = b""
        carry = b""
        carry_off = 0
        n_chunks = 0
        counts: dict[bytes, int] = {}
        for base, data in source.iter_chunks(data_start, parallel=True):
            n_chunks += 1
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if progress is not None and n_chunks % 64 == 0:
                progress(source.progress(base))
            carry_len = len(carry)
            text = carry + data if carry_len else data
            last_nl = text.rfind(b"\n")
            if last_nl < 0:
                if not carry_len:
                    carry_off = base
                carry = text
                continue

            def off(p, carry_len=carry_len, carry_off=carry_off, base=base):
                return carry_off if p < carry_len else base + (p - carry_len)

            n_lines = text.count(b"\n", 0, last_nl + 1)
            same = -1
            if cur is not None:
                same = (1 if text.startswith(cur_tag) else 0) + text.count(b"\n" + cur_tag, 0, last_nl)
            if same == n_lines:
                # Whole chunk is one contig: drop a checkpoint every ~64 KB.
                counts[cur] = counts.get(cur, 0) + n_lines
                p = 0
                while p <= last_nl:
                    start = p + len(cur_tag)
                    tab2 = text.find(b"\t", start, last_nl)
                    try:
                        pos = int(text[start:tab2])
                    except ValueError:
                        pos = None
                    if pos is not None:
                        idx._add(cur, pos, off(p), False)
                    nxt = text.find(b"\n", p + _SPACING, last_nl + 1)
                    if nxt < 0 or nxt >= last_nl:
                        break
                    p = nxt + 1
            else:
                p = 0
                first = True
                while p <= last_nl:
                    nl = text.find(b"\n", p)
                    tab = text.find(b"\t", p, nl)
                    if tab > p and text[p] != 35:  # skip blank / '#' lines
                        chrom = text[p:tab]
                        counts[chrom] = counts.get(chrom, 0) + 1
                        new = chrom != cur
                        if new or first:
                            tab2 = text.find(b"\t", tab + 1, nl)
                            try:
                                pos = int(text[tab + 1:tab2 if tab2 > 0 else nl])
                            except ValueError:
                                pos = 0
                            if new:
                                cur = chrom
                                cur_tag = chrom + b"\t"
                            idx._add(chrom, pos, off(p), new)
                            first = False
                    p = nl + 1
            if last_nl + 1 < len(text):
                carry_off = off(last_nl + 1)
                carry = text[last_nl + 1:]
            else:
                carry = b""
        tab = carry.find(b"\t")
        if tab > 0 and not carry.startswith(b"#"):  # last line had no newline
            counts[carry[:tab]] = counts.get(carry[:tab], 0) + 1
        idx.count = {k.decode("utf-8", "replace"): v for k, v in counts.items()}
        return idx

    # -- query --------------------------------------------------------------
    def start_offset(self, name: str, pos: int):
        ps = self.pos.get(name)
        if not ps:
            return None
        i = bisect.bisect_right(ps, pos) - 1
        i = max(0, i - 1)  # one step back for records spanning ``pos``
        return self.off[name][i]

    def contig_start(self, name: str):
        o = self.off.get(name)
        return o[0] if o else None

    def tid(self, name):
        return self.names.index(name) if name in self.pos else None

    def record_count(self, name: str):
        return self.count.get(name)

    @property
    def sync_points(self):
        if self._sync is None:
            pts = []
            for n in self.names:
                pts.extend(self.off[n])
            pts.sort()
            self._sync = pts
        return self._sync

    # -- persistence ----------------------------------------------------------
    def to_json(self):
        return {"v": self.VERSION, "names": self.names, "pos": self.pos, "off": self.off,
                "unsorted": self.unsorted, "count": self.count}

    @classmethod
    def from_json(cls, d):
        if d.get("v") != cls.VERSION:
            raise ValueError("stale index cache")
        idx = cls()
        idx.names = d["names"]
        idx.pos = d["pos"]
        idx.off = d["off"]
        idx.unsorted = d.get("unsorted", False)
        idx.count = d.get("count", {})
        return idx


def cache_dir() -> Path:
    if os.environ.get("VCFLITE_CACHE"):
        return Path(os.environ["VCFLITE_CACHE"]) / "index"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "VCF Lite" / "index"


def _cache_path(path: str) -> Path:
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
    return cache_dir() / (hashlib.sha1(key.encode()).hexdigest() + ".json.gz")


def load_cached_scan(path: str):
    try:
        with gzip.open(_cache_path(path), "rt") as f:
            return ScanIndex.from_json(json.load(f))
    except Exception:
        return None


def save_cached_scan(path: str, idx: ScanIndex):
    try:
        p = _cache_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as f:
            json.dump(idx.to_json(), f)
        os.replace(tmp, p)
    except Exception:
        pass


def find_index_file(path: str):
    """Locate a .tbi/.csi next to ``path``; returns (kind, path) or None."""
    cands = [(path + ".tbi", "tbi"), (path + ".csi", "csi")]
    stem = path[:-3] if path.endswith(".gz") else None
    if stem:
        cands += [(stem + ".tbi", "tbi"), (stem + ".csi", "csi")]
    for p, kind in cands:
        if os.path.exists(p):
            return kind, p
    return None
