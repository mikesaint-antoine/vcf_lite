"""Byte sources for VCF text: BGZF (random access), plain text, plain gzip.

Every source exposes the same small interface built around *offsets*:

* BGZF: htslib virtual offsets, ``(block_offset << 16) | offset_in_block``
* plain text / plain gzip: byte offsets into the (uncompressed) text

``iter_chunks(offset)`` yields ``(base, data)`` pairs such that the offset of
byte ``i`` in ``data`` is ``base + i`` (for every ``i < len(data)``).  All the
line iteration, searching and index building is written against that.
"""

from __future__ import annotations

import gzip
import os
import struct
import threading
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

BGZF_MAGIC = b"\x1f\x8b\x08\x04"
GZIP_MAGIC = b"\x1f\x8b"

_WORKERS = max(1, min(8, (os.cpu_count() or 2)))
_POOL: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="inflate")
    return _POOL


class FormatError(Exception):
    pass


def sniff(path: str) -> str:
    """Return 'bgzf', 'gzip' or 'text'."""
    with open(path, "rb") as f:
        head = f.read(18)
    if head[:4] == BGZF_MAGIC and len(head) >= 18:
        xlen = struct.unpack("<H", head[10:12])[0]
        with open(path, "rb") as f:
            f.seek(12)
            extra = f.read(xlen)
        if _bsize_from_extra(extra) is not None:
            return "bgzf"
    if head[:2] == GZIP_MAGIC:
        return "gzip"
    return "text"


def _bsize_from_extra(extra: bytes):
    i = 0
    while i + 4 <= len(extra):
        si1, si2, slen = extra[i], extra[i + 1], struct.unpack("<H", extra[i + 2:i + 4])[0]
        if si1 == 66 and si2 == 67 and slen == 2:
            return struct.unpack("<H", extra[i + 4:i + 6])[0]
        i += 4 + slen
    return None


def _inflate(cdata: bytes) -> bytes:
    return zlib.decompress(cdata, -15)


class Source:
    kind = "?"
    random_access = True

    def iter_chunks(self, offset: int, parallel: bool = False):
        raise NotImplementedError

    def sample_lines(self, points: int, span: int = 1 << 16):
        """Yield lists of complete lines from ``points`` evenly spaced places
        in the file.  Each sample covers the same amount of text, so records
        are sampled uniformly (unlike sampling via index entry points)."""
        raise NotImplementedError

    def progress(self, offset: int) -> float:
        """Rough fraction of the file that lies before ``offset``."""
        return min(1.0, offset / self.size) if self.size else 1.0

    def close(self):
        pass

    # -- line iteration -------------------------------------------------
    def iter_lines(self, offset: int, parallel: bool = False):
        """Yield ``(line_offset, line_bytes)`` starting at ``offset``.

        ``offset`` must be the start of a line.  Line terminators are removed.
        """
        carry = b""
        carry_off = 0
        for base, data in self.iter_chunks(offset, parallel=parallel):
            pos = 0
            if carry:
                nl = data.find(b"\n")
                if nl < 0:
                    carry += data
                    continue
                line = carry + data[:nl]
                carry = b""
                yield carry_off, line.rstrip(b"\r")
                pos = nl + 1
            find = data.find
            while True:
                nl = find(b"\n", pos)
                if nl < 0:
                    break
                if nl and data[nl - 1] == 13:
                    yield base + pos, data[pos:nl - 1]
                else:
                    yield base + pos, data[pos:nl]
                pos = nl + 1
            if pos < len(data):
                carry = data[pos:]
                carry_off = base + pos
        if carry:
            yield carry_off, carry.rstrip(b"\r")


class BgzfSource(Source):
    kind = "bgzf"

    def __init__(self, path: str, cache_blocks: int = 256):
        self.path = path
        self._f = open(path, "rb")
        self._lock = threading.Lock()
        self._cache: OrderedDict[int, tuple[bytes, int]] = OrderedDict()
        self._cache_max = cache_blocks
        self.size = os.path.getsize(path)

    def close(self):
        self._f.close()

    def _read_raw(self, coffset: int):
        """Return (compressed_payload, next_coffset) or None at EOF."""
        with self._lock:
            self._f.seek(coffset)
            head = self._f.read(18)
            if len(head) < 18:
                return None
            if head[:4] != BGZF_MAGIC:
                raise FormatError(f"Not a BGZF block at offset {coffset}")
            xlen = struct.unpack("<H", head[10:12])[0]
            if xlen == 6 and head[12:14] == b"BC":
                bsize = struct.unpack("<H", head[16:18])[0]
                rest = self._f.read(bsize + 1 - 18)
                block = head + rest
            else:
                extra = head[12:18] + self._f.read(xlen - 6)
                bsize = _bsize_from_extra(extra)
                if bsize is None:
                    raise FormatError("Missing BGZF block size")
                self._f.seek(coffset)
                block = self._f.read(bsize + 1)
        hdr_len = 12 + xlen
        return block[hdr_len:len(block) - 8], coffset + bsize + 1

    def read_block(self, coffset: int):
        """Return (data, next_coffset) or None at EOF.  Cached."""
        hit = self._cache.get(coffset)
        if hit is not None:
            self._cache.move_to_end(coffset)
            return hit
        raw = self._read_raw(coffset)
        if raw is None:
            return None
        cdata, nxt = raw
        res = (_inflate(cdata), nxt)
        self._cache[coffset] = res
        if len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)
        return res

    def _bulk_blocks(self, coffset: int, buf_size: int = 8 << 20):
        """Yield (coffset, data) for consecutive blocks using parallel inflate.

        Uses its own file handle so it can run alongside interactive reads.
        """
        pool = _pool()
        with open(self.path, "rb") as f:
            pos = coffset
            pending = None
            while True:
                f.seek(pos)
                buf = f.read(buf_size)
                if not buf:
                    break
                items = []  # (coffset, cdata)
                i = 0
                n = len(buf)
                while i + 18 <= n:
                    if buf[i:i + 4] != BGZF_MAGIC:
                        raise FormatError(f"Not a BGZF block at offset {pos + i}")
                    xlen = struct.unpack_from("<H", buf, i + 10)[0]
                    if i + 12 + xlen > n:
                        break
                    bsize = _bsize_from_extra(buf[i + 12:i + 12 + xlen])
                    if bsize is None:
                        raise FormatError("Missing BGZF block size")
                    end = i + bsize + 1
                    if end > n:
                        break
                    items.append((pos + i, buf[i + 12 + xlen:end - 8]))
                    i = end
                if not items:
                    if len(buf) < buf_size:
                        break
                    buf_size *= 2
                    continue
                futs = [(co, pool.submit(_inflate, cd)) for co, cd in items]
                # Emit previous batch while this one inflates.
                if pending:
                    for co, fut in pending:
                        yield co, fut.result()
                pending = futs
                pos += i
            if pending:
                for co, fut in pending:
                    yield co, fut.result()

    def iter_chunks(self, offset: int, parallel: bool = False):
        coffset, uoff = offset >> 16, offset & 0xFFFF
        if parallel:
            first = True
            for co, data in self._bulk_blocks(coffset):
                if first:
                    first = False
                    if uoff:
                        data = data[uoff:]
                        if data:
                            yield (co << 16) + uoff, data
                        continue
                if data:
                    yield co << 16, data
            return
        while True:
            blk = self.read_block(coffset)
            if blk is None:
                return
            data, nxt = blk
            if uoff:
                data = data[uoff:]
            if data:
                yield (coffset << 16) + uoff, data
            coffset, uoff = nxt, 0

    def progress(self, offset: int) -> float:
        return min(1.0, (offset >> 16) / self.size) if self.size else 1.0

    def block_at_or_after(self, coffset: int):
        """Offset of the first BGZF block starting at or after ``coffset``,
        found by scanning for the block header (validated by the next one)."""
        with open(self.path, "rb") as f:
            pos = coffset
            while pos < self.size:
                f.seek(pos)
                buf = f.read(1 << 17)
                if not buf:
                    return None
                i = buf.find(BGZF_MAGIC)
                while i >= 0:
                    cand = pos + i
                    f.seek(cand)
                    head = f.read(18)
                    if len(head) == 18 and head[12:14] == b"BC":
                        bsize = struct.unpack("<H", head[16:18])[0]
                        nxt = cand + bsize + 1
                        f.seek(nxt)
                        if nxt >= self.size or f.read(4) == BGZF_MAGIC:
                            return cand
                    i = buf.find(BGZF_MAGIC, i + 1)
                pos += len(buf) - 3
        return None

    def sample_lines(self, points: int, span: int = 1 << 16):
        for i in range(points):
            co = self.block_at_or_after(int(self.size * (i + 0.5) / points))
            if co is None:
                continue
            blk = self.read_block(co)
            if blk is None:
                continue
            yield blk[0].split(b"\n")[1:-1]  # drop partial first/last lines

    def normalize(self, voffset: int) -> int:
        """Canonical form of a virtual offset (end of block -> start of next)."""
        co, uo = voffset >> 16, voffset & 0xFFFF
        blk = self.read_block(co)
        if blk is not None and uo >= len(blk[0]):
            return blk[1] << 16
        return voffset


class TextSource(Source):
    kind = "text"
    CHUNK = 1 << 20

    def __init__(self, path: str):
        self.path = path
        self.size = os.path.getsize(path)

    def iter_chunks(self, offset: int, parallel: bool = False):
        with open(self.path, "rb") as f:
            f.seek(offset)
            pos = offset
            while True:
                data = f.read(self.CHUNK)
                if not data:
                    return
                yield pos, data
                pos += len(data)

    def normalize(self, off: int) -> int:
        return off

    def sample_lines(self, points: int, span: int = 1 << 16):
        with open(self.path, "rb") as f:
            for i in range(points):
                f.seek(int(self.size * (i + 0.5) / points))
                yield f.read(span).split(b"\n")[1:-1]


class GzipSource(Source):
    """Plain (non-block) gzip.  Seeking means decompressing from the start,
    so random access is slow; we keep one warm reader to make forward paging
    cheap."""

    kind = "gzip"
    random_access = False
    CHUNK = 1 << 20

    def __init__(self, path: str):
        self.path = path
        self.size = os.path.getsize(path)
        self._lock = threading.Lock()
        self._raw_pos = 0
        # (handle, base, data): handle is positioned at base + len(data) and
        # ``data`` is the last chunk it produced, so we can resume inside it.
        self._warm = None

    def close(self):
        if self._warm:
            self._warm[0].close()

    def iter_chunks(self, offset: int, parallel: bool = False):
        with self._lock:
            warm, self._warm = self._warm, None
        g = None
        tail = b""
        last = None
        pos = offset
        if warm is not None:
            wg, wbase, wdata = warm
            if wbase <= offset <= wbase + len(wdata):
                g = wg
                tail = wdata[offset - wbase:]
                last = (wbase, wdata)
                pos = wbase + len(wdata)
            elif wg.tell() <= offset:
                g = wg
            else:
                wg.close()
        if g is None:
            g = gzip.open(self.path, "rb")
        try:
            if tail:
                yield offset, tail
            if g.tell() != pos:
                g.seek(pos)
            while True:
                data = g.read(self.CHUNK)
                if not data:
                    return
                last = (pos, data)
                try:
                    self._raw_pos = g.fileobj.tell()
                except Exception:
                    pass
                yield pos, data
                pos += len(data)
        finally:
            with self._lock:
                if self._warm is None and last is not None:
                    self._warm = (g, last[0], last[1])
                else:
                    g.close()

    def progress(self, offset: int) -> float:
        return min(1.0, self._raw_pos / self.size) if self.size else 1.0

    def sample_lines(self, points: int, span: int = 1 << 16):
        # One pass through the file; sample positions by compressed progress.
        with gzip.open(self.path, "rb") as g:
            raw = g.fileobj
            nxt = 0
            while nxt < points:
                target = self.size * (nxt + 0.5) / points
                chunk = g.read(span)
                if not chunk:
                    return
                if raw.tell() >= target:
                    nxt += 1
                    yield chunk.split(b"\n")[1:-1]

    def normalize(self, off: int) -> int:
        return off


def open_source(path: str) -> Source:
    kind = sniff(path)
    if kind == "bgzf":
        return BgzfSource(path)
    if kind == "gzip":
        return GzipSource(path)
    return TextSource(path)
