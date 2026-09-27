"""The VCF document model used by the UI."""

from __future__ import annotations

import bisect
import os
import re
import threading
import time

from .bgzf import FormatError, open_source
from .index import (Cancelled, CsiIndex, ScanIndex, TabixIndex, find_index_file,
                    load_cached_scan, save_cached_scan)

REF_ONLY_ALTS = {b".", b"<NON_REF>", b"<*>", b"<X>"}
FIXED = ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO"]
_META_RE = re.compile(r'([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|[^,]*)')
_END_RE = re.compile(rb"(?:^|;)END=(\d+)")
_WORD = re.compile(rb"[A-Za-z0-9_]")


def parse_meta(line: str):
    """``##KEY=<ID=x,Description="...">`` -> (KEY, {ID: x, ...}); else (KEY, value)."""
    body = line[2:]
    key, _, val = body.partition("=")
    if val.startswith("<") and val.endswith(">"):
        d = {}
        for k, v in _META_RE.findall(val[1:-1]):
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1].replace('\\"', '"')
            d[k] = v
        return key, d
    return key, val


def is_nonvariant(cols, sample=None) -> bool:
    """True for reference-only records and records where no sample carries
    an ALT allele (hom-ref / no-call genotypes).  With ``sample`` (0-based),
    only that sample's genotype counts."""
    if len(cols) < 5:
        return False
    if cols[4] in REF_ONLY_ALTS:
        return True
    if len(cols) > 9 and cols[8][:2] == b"GT":
        if sample is not None:
            if 9 + sample >= len(cols):
                return False
            samples = cols[9 + sample:10 + sample]
        else:
            samples = cols[9:]
        for s in samples:
            gt = s.split(b":", 1)[0]
            for a in gt.replace(b"|", b"/").split(b"/"):
                if a != b"0" and a != b".":
                    return False
        return True
    return False


def record_end(cols) -> int:
    pos = int(cols[1])
    end = pos + max(len(cols[3]), 1) - 1
    if len(cols) > 7 and b"END=" in cols[7]:
        m = _END_RE.search(cols[7])
        if m:
            end = max(end, int(m.group(1)))
    return end


def _natural_key(name: str):
    n = name[3:] if name.lower().startswith("chr") else name
    if n.isdigit():
        return (0, int(n), "")
    order = {"X": 1, "Y": 2, "M": 3, "MT": 3}
    if n.upper() in order:
        return (0, 100 + order[n.upper()], "")
    return (1, 0, name)


class VcfFile:
    TABLE_SAMPLES = 12  # sample columns sent per row in the table

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.name = os.path.basename(path)
        self.size = os.path.getsize(path)
        self.source = open_source(path)
        self.meta_lines: list[str] = []
        self.columns: list[str] = []
        self.samples: list[str] = []
        self.data_start = 0
        self.info_defs: dict[str, dict] = {}
        self.format_defs: dict[str, dict] = {}
        self.filter_defs: dict[str, dict] = {}
        self.header_contigs: list[dict] = []
        self._read_header()

        self.index = None
        self.index_kind = None
        self.index_error = None
        self.index_progress = 0.0
        self._index_lock = threading.Lock()
        self._load_index()

    # -- header ----------------------------------------------------------------
    def _read_header(self):
        first = True
        for off, line in self.source.iter_lines(0):
            if first:
                first = False
                if line.startswith(b"BCF"):
                    raise FormatError("This is a BCF file. BCF support is coming; for now convert it with "
                                      "`bcftools view -Oz -o out.vcf.gz in.bcf`.")
                if not line.startswith(b"#"):
                    raise FormatError("This doesn't look like a VCF (no header).")
            if line.startswith(b"##"):
                s = line.decode("utf-8", "replace")
                self.meta_lines.append(s)
                key, val = parse_meta(s)
                if isinstance(val, dict) and "ID" in val:
                    if key == "INFO":
                        self.info_defs[val["ID"]] = val
                    elif key == "FORMAT":
                        self.format_defs[val["ID"]] = val
                    elif key == "FILTER":
                        self.filter_defs[val["ID"]] = val
                    elif key == "contig":
                        self.header_contigs.append(val)
                continue
            if line.startswith(b"#"):
                self.columns = line[1:].decode("utf-8", "replace").split("\t")
                self.samples = self.columns[9:]
                continue
            self.data_start = off
            break
        else:
            # Header only, no records.
            self.data_start = None
        if not self.columns:
            raise FormatError("This doesn't look like a VCF (missing the #CHROM line).")

    # -- index -------------------------------------------------------------------
    def _load_index(self):
        if self.source.kind == "bgzf":
            found = find_index_file(self.path)
            if found:
                kind, ipath = found
                try:
                    if kind == "tbi":
                        self.index = TabixIndex.load(ipath)
                    else:
                        self.index = CsiIndex.load(ipath, [c.get("ID") for c in self.header_contigs])
                    self.index_kind = kind
                    self.index_progress = 1.0
                    return
                except Exception as e:  # corrupt / unsupported index: fall back
                    self.index_error = f"Couldn't read {os.path.basename(ipath)} ({e}); indexing instead."
        cached = load_cached_scan(self.path)
        if cached is not None:
            self.index = cached
            self.index_kind = "scan"
            self.index_progress = 1.0

    @property
    def index_ready(self) -> bool:
        return self.index is not None

    def build_index(self, cancel=None):
        """Scan the file to build our own index (runs in a worker thread)."""
        with self._index_lock:
            if self.index is not None or self.data_start is None:
                self.index_progress = 1.0
                return

            def prog(x):
                self.index_progress = x

            idx = ScanIndex.build(self.source, self.data_start, progress=prog, cancel=cancel)
            self.index = idx
            self.index_kind = "scan"
            self.index_progress = 1.0
            save_cached_scan(self.path, idx)

    @property
    def contigs(self) -> list[str]:
        if self.index is not None:
            names = [n for n in self.index.names if self.index.contig_start(n) is not None]
        else:
            names = [c["ID"] for c in self.header_contigs if "ID" in c]
        return sorted(names, key=_natural_key)

    def resolve_contig(self, q: str):
        names = self.index.names if self.index is not None else [c.get("ID", "") for c in self.header_contigs]
        if q in names:
            return q
        low = {n.lower(): n for n in names}
        ql = q.lower()
        bare = ql[3:] if ql.startswith("chr") else ql
        for cand in (ql, bare, "chr" + bare):
            if cand in low:
                return low[cand]
        if bare in ("m", "mt"):
            for cand in ("mt", "m", "chrm", "chrmt"):
                if cand in low:
                    return low[cand]
        return None

    # -- rows ----------------------------------------------------------------------
    def _row(self, off, line, full=False, sample=None):
        """One record for the UI.  With ``sample`` (0-based) the row carries
        just that sample's column (as c[9]) and "r" refers to that sample."""
        cols = line.split(b"\t")
        nonvar = is_nonvariant(cols, sample)
        if not full:
            if sample is not None:
                cols = cols[:9] + cols[9 + sample:10 + sample]
            elif len(cols) > 9 + self.TABLE_SAMPLES:
                cols = cols[:9 + self.TABLE_SAMPLES]
        return {"o": off, "c": [c.decode("utf-8", "replace") for c in cols], "r": nonvar}

    @staticmethod
    def _hidden(line, sample):
        """Should hide-hom-ref skip this line?  (Split only as far as needed.)"""
        if sample is None:
            return is_nonvariant(line.split(b"\t"))
        return is_nonvariant(line.split(b"\t", 10 + sample), sample)

    def read_rows(self, offset, n=100, hide_ref=False, budget=1.5, keep_first=False, sample=None):
        """Rows starting at ``offset``.  Returns (rows, next_offset, eof).

        ``keep_first`` shows the first record even if ``hide_ref`` would hide it
        (used when jumping to a search hit)."""
        if offset is None:
            return [], None, True
        rows = []
        t0 = time.monotonic()
        scanned = 0
        for off, line in self.source.iter_lines(offset):
            if len(rows) >= n:
                return rows, off, False
            if not line or line[0] == 35:
                continue
            if hide_ref and not (keep_first and not rows and off == offset):
                scanned += 1
                if self._hidden(line, sample):
                    if scanned % 4096 == 0 and time.monotonic() - t0 > budget:
                        return rows, off, False
                    continue
            rows.append(self._row(off, line, sample=sample))
        return rows, None, True

    def row_full(self, offset):
        for off, line in self.source.iter_lines(offset):
            return self._row(off, line, full=True)
        return None

    def locate(self, contig: str, pos: int, cancel=None):
        """Offset of the first record on ``contig`` overlapping ``pos`` (or the
        next record after it).  Requires the index."""
        start = self.index.start_offset(contig, pos)
        if start is None:
            return None
        n = 0
        for off, line in self.source.iter_lines(start):
            n += 1
            if cancel is not None and n % 10000 == 0 and cancel.is_set():
                raise Cancelled()
            cols = line.split(b"\t", 8)
            if len(cols) < 5 or line[0] == 35:
                continue
            if cols[0].decode("utf-8", "replace") != contig:
                return off  # ran past the contig: land on whatever follows
            try:
                if int(cols[1]) >= pos or record_end(cols) >= pos:
                    return off
            except ValueError:
                continue
        return None

    def records_at(self, contig: str, pos: int, limit=64):
        """Raw column lists of records starting exactly at ``pos``."""
        start = self.index.start_offset(contig, pos)
        out = []
        if start is None:
            return out
        for i, (off, line) in enumerate(self.source.iter_lines(start)):
            cols = line.split(b"\t", 8)
            if len(cols) < 5:
                continue
            if cols[0].decode("utf-8", "replace") != contig:
                break
            p = int(cols[1])
            if p == pos:
                out.append(cols)
            elif p > pos or i > limit + 5000:
                break
        return out

    def read_before(self, offset, n=100, hide_ref=False, budget=1.5, sample=None):
        """Up to ``n`` rows ending just before ``offset``.

        Returns (rows, reached_start).
        """
        if self.index is None or offset is None:
            return [], True
        sync = self.index.sync_points
        floor = self.data_start
        i = bisect.bisect_left(sync, offset)
        step = 1
        t0 = time.monotonic()
        while True:
            j = i - step
            start = sync[j] if j >= 0 else floor
            if start < floor:
                start = floor
            rows = []
            for off, line in self.source.iter_lines(start):
                if off >= offset:
                    break
                if not line or line[0] == 35:
                    continue
                if hide_ref and self._hidden(line, sample):
                    continue
                rows.append((off, line))
            if len(rows) >= n or start == floor or time.monotonic() - t0 > budget:
                at_start = start == floor and len(rows) <= n
                return [self._row(o, l, sample=sample) for o, l in rows[-n:]], at_start
            step *= 4

    # -- text search -------------------------------------------------------------
    def search_text(self, query: str, start=None, cancel=None, progress=None, column=None):
        """Find the next record containing ``query`` (case-insensitive, whole
        token when the query is alphanumeric).  ``column`` (0-based) restricts
        matches to one field, e.g. 2 for ID.  Returns an offset or None."""
        needle = query.strip().lower().encode()
        if not needle or self.data_start is None:
            return None
        word = bool(re.fullmatch(rb"[A-Za-z0-9_]+", needle))
        start = self.data_start if start is None else start
        carry = b""
        carry_off = 0
        n = 0
        for base, data in self.source.iter_chunks(start, parallel=True):
            n += 1
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if progress is not None and n % 32 == 0:
                progress(self.source.progress(base))
            carry_len = len(carry)
            text = (carry + data) if carry_len else data
            last_nl = text.rfind(b"\n")
            if last_nl < 0:
                if not carry_len:
                    carry_off = base
                carry = text
                continue
            body = text[:last_nl + 1].lower()
            p = body.find(needle)
            while p >= 0:
                ok = True
                if word:
                    if p > 0 and _WORD.match(body, p - 1):
                        ok = False
                    e = p + len(needle)
                    if e < len(body) and _WORD.match(body, e):
                        ok = False
                if ok:
                    ls = body.rfind(b"\n", 0, p) + 1
                    if body[ls:ls + 1] != b"#" and (column is None or body.count(b"\t", ls, p) == column):
                        return carry_off if ls < carry_len else base + (ls - carry_len)
                p = body.find(needle, p + 1)
            if last_nl + 1 < len(text):
                carry_off = carry_off if last_nl + 1 < carry_len else base + (last_nl + 1 - carry_len)
                carry = text[last_nl + 1:]
            else:
                carry = b""
        if carry and not carry.startswith(b"#"):
            # Last line without a trailing newline.
            tail = carry.lower()
            if column is not None:
                fields = tail.split(b"\t")
                tail = fields[column] if len(fields) > column else b""
            if (re.search(rb"(?<![a-z0-9_])" + re.escape(needle) + rb"(?![a-z0-9_])", tail)
                    if word else needle in tail):
                return carry_off
        return None

    def next_line_offset(self, offset):
        it = self.source.iter_lines(offset)
        next(it, None)
        nxt = next(it, None)
        return nxt[0] if nxt else None

    def close(self):
        self.source.close()
