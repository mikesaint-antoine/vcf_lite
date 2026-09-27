"""The bridge between the web UI and the VCF model.

Every public method is callable from JavaScript as
``await pywebview.api.method(...)``.  Everything returned must be JSON-able.
"""

from __future__ import annotations

import json
import os
import re
import threading
import traceback

from . import __version__, detect
from .bgzf import FormatError
from .index import Cancelled, cache_dir
from .vcf import VcfFile

# "12345", "1.5M", "200kb", "" (= start of the chromosome)
POS_RE = re.compile(r"^(?:(\d+(?:\.\d+)?)\s*([kKmM])?[bB]?)?$")
MAX_RECENT = 8


def _settings_path():
    return cache_dir().parent / "settings.json"


class Task:
    """A cancellable background job whose state the UI polls."""

    def __init__(self, kind, fn):
        self.kind = kind
        self.cancel = threading.Event()
        self.progress = 0.0
        self.result = None
        self.error = None
        self.done = False
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()

    def _run(self, fn):
        try:
            self.result = fn(self)
        except Cancelled:
            self.error = "cancelled"
        except Exception as e:
            traceback.print_exc()
            self.error = str(e)
        finally:
            self.done = True

    def state(self):
        return {"kind": self.kind, "progress": round(self.progress, 3), "done": self.done,
                "result": self.result, "error": self.error}


class Api:
    def __init__(self):
        self._window = None
        self._vcf: VcfFile | None = None
        self._gen = 0  # bumps on every open, so stale background jobs drop out
        self._index_task: Task | None = None
        self._task: Task | None = None
        self._info = {}
        self._small = False  # True while the window is the small launcher

    # -- plumbing ------------------------------------------------------------
    def _set_window(self, window):
        self._window = window

    def _grow(self):
        """Enlarge the small launcher window to viewer size (first open only)."""
        if not self._small:
            return
        self._small = False
        try:
            import webview
            scr = webview.screens[0]
            w = min(1400, int(scr.width * 0.9))
            h = min(800, int(scr.height * 0.85))
            self._window.resize(w, h)
            self._window.move(scr.x + (scr.width - w) // 2, scr.y + (scr.height - h) // 2)
        except Exception:
            traceback.print_exc()

    def _push(self, fn, *args):
        """Call ``window.app[fn](...args)`` in the UI."""
        if self._window is not None:
            self._window.evaluate_js(f"window.app && window.app.{fn}(...{json.dumps(list(args))})")

    def _settings(self):
        p = _settings_path()
        if not p.exists():
            # The app used to be called "VCF Viewer"; carry its settings over.
            p = p.parent.parent / "VCF Viewer" / p.name
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}

    def _save_settings(self, s):
        try:
            p = _settings_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(s))
        except Exception:
            pass

    # -- app level -----------------------------------------------------------
    def version(self):
        return __version__

    def recent(self):
        return [p for p in self._settings().get("recent", []) if os.path.exists(p)]

    def forget_recent(self):
        s = self._settings()
        s["recent"] = []
        self._save_settings(s)

    def choose_file(self):
        import webview
        types = ("VCF files (*.vcf;*.vcf.gz;*.vcf.bgz;*.gz;*.bgz)", "All files (*.*)")
        try:
            res = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=types)
        except Exception:
            res = self._window.create_file_dialog(webview.FileDialog.OPEN)
        if not res:
            return None
        return res[0] if isinstance(res, (list, tuple)) else res

    # -- opening -------------------------------------------------------------
    def open(self, path):
        path = os.path.expanduser(str(path))
        if not os.path.isfile(path):
            return {"error": f"File not found: {path}"}
        if path.endswith((".tbi", ".csi")):
            return {"error": "That's an index file. Open the .vcf.gz next to it instead."}
        try:
            vcf = VcfFile(path)
        except FormatError as e:
            return {"error": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {"error": f"Couldn't read this file: {e}"}

        for t in (self._index_task, self._task):
            if t is not None:
                t.cancel.set()
        old, self._vcf = self._vcf, vcf
        if old is not None:
            old.close()
        self._gen += 1
        gen = self._gen
        self._task = None
        self._info = {"build": None, "kind": None, "coverage": None, "phased": None}

        if self._window is not None:
            try:
                self._window.set_title(f"VCF Lite - {vcf.name}")
            except Exception:
                pass
            self._grow()

        s = self._settings()
        rec = [p for p in s.get("recent", []) if p != vcf.path]
        s["recent"] = [vcf.path] + rec[:MAX_RECENT - 1]
        self._save_settings(s)

        if not vcf.index_ready and vcf.data_start is not None:
            self._index_task = Task("index", lambda t: self._build_index(vcf, t))
        else:
            self._index_task = None
        threading.Thread(target=self._describe, args=(vcf, gen), daemon=True).start()

        rows, nxt, eof = vcf.read_rows(vcf.data_start, 150)
        return {
            "path": vcf.path,
            "name": vcf.name,
            "dir": os.path.dirname(vcf.path),
            "size": vcf.size,
            "compression": vcf.source.kind,
            "index": vcf.index_kind,
            "indexNote": vcf.index_error,
            "columns": vcf.columns,
            "samples": vcf.samples,
            "tableSamples": min(len(vcf.samples), VcfFile.TABLE_SAMPLES),
            "meta": vcf.meta_lines,
            "info": vcf.info_defs,
            "format": vcf.format_defs,
            "filter": vcf.filter_defs,
            "contigs": vcf.contigs,
            "page": {"rows": rows, "next": nxt, "eof": eof, "atStart": True},
        }

    def _build_index(self, vcf, task):
        def watch():
            while not task.done:
                task.progress = vcf.index_progress
                if task.cancel.wait(0.2):
                    break
        threading.Thread(target=watch, daemon=True).start()
        vcf.build_index(cancel=task.cancel)
        task.progress = 1.0
        return {"contigs": vcf.contigs}

    def _describe(self, vcf, gen):
        """Classify content and detect the build (may wait for the index)."""
        try:
            kind = detect.classify(vcf)
            if gen != self._gen:
                return
            self._info["kind"] = kind
            self._info["phased"] = detect.phasing(vcf)
            b = detect.build_from_header(vcf)
            if not b[0] and not vcf.index_ready and self._index_task is not None:
                while not self._index_task.done and gen == self._gen:
                    self._index_task.cancel.wait(0.25)
            if gen != self._gen:
                return
            self._info["build"] = detect.detect_build(vcf)
        except Exception as e:
            traceback.print_exc()
            self._info["build"] = {"build": None, "why": f"detection failed: {e}"}
        try:
            # Coverage needs the index's record counts.
            while not vcf.index_ready and self._index_task is not None and not self._index_task.done \
                    and gen == self._gen:
                self._index_task.cancel.wait(0.25)
            if gen != self._gen:
                return
            cov = detect.estimate_coverage(vcf, self._info["build"].get("build"), self._info.get("kind"))
            self._info["coverage"] = cov or {"label": "unknown", "why": "couldn't read the file index"}
        except Exception as e:
            traceback.print_exc()
            self._info["coverage"] = {"label": "unknown", "why": f"estimate failed: {e}"}

    def status(self):
        v = self._vcf
        if v is None:
            return {}
        idx = self._index_task
        out = {
            "indexReady": v.index_ready,
            "indexProgress": 1.0 if v.index_ready else round(v.index_progress, 3),
            "indexError": idx.error if idx and idx.error and idx.error != "cancelled" else None,
            "build": self._info.get("build"),
            "kind": self._info.get("kind"),
            "coverage": self._info.get("coverage"),
            "phased": self._info.get("phased"),
            "task": self._task.state() if self._task else None,
        }
        if v.index_ready and idx is not None and idx.done and idx.result:
            out["contigs"] = idx.result["contigs"]
        return out

    # -- paging ----------------------------------------------------------------
    # ``sample``: index of the sample picked in the sidebar (None = all).
    def rows(self, offset, n=150, hide_ref=False, sample=None):
        v = self._vcf
        rows, nxt, eof = v.read_rows(offset, n, hide_ref=hide_ref, sample=sample)
        return {"rows": rows, "next": nxt, "eof": eof}

    def before(self, offset, n=150, hide_ref=False, sample=None):
        v = self._vcf
        if not v.index_ready:
            return {"rows": [], "atStart": False, "pending": True}
        rows, at_start = v.read_before(offset, n, hide_ref=hide_ref, sample=sample)
        return {"rows": rows, "atStart": at_start}

    def row(self, offset):
        return self._vcf.row_full(offset)

    def page_at(self, offset, n=150, hide_ref=False, keep_first=False, sample=None):
        """Rows starting at ``offset`` (None = file start)."""
        v = self._vcf
        if offset is None:
            offset = v.data_start
        rows, nxt, eof = v.read_rows(offset, n, hide_ref=hide_ref, keep_first=keep_first, sample=sample)
        return {"rows": rows, "next": nxt, "eof": eof, "atStart": offset == v.data_start}

    # -- navigation ------------------------------------------------------------
    def goto(self, contig, pos="", hide_ref=False, sample=None):
        """Jump to ``pos`` (e.g. "1,000,000", "1.5M"; empty = start) on ``contig``."""
        v = self._vcf
        c = v.resolve_contig(str(contig or "").strip())
        if c is None:
            return {"error": f"No chromosome “{contig}” in this file", "field": "chrom"}
        m = POS_RE.match(str(pos or "").replace(",", "").replace("_", "").strip())
        if not m:
            return {"error": f"“{pos}” isn't a position. Try something like 1,000,000", "field": "pos"}
        p = float(m.group(1) or 1)
        if m.group(2):
            p *= 1000 if m.group(2) in "kK" else 1_000_000
        p = max(1, int(p))
        if not v.index_ready:
            return {"error": "Still indexing this file, one moment…", "indexing": True}
        off = v.locate(c, p)
        if off is None:
            return {"error": f"No records on {c} at or after {p:,}", "field": "pos"}
        rows, nxt, eof = v.read_rows(off, 150, hide_ref=hide_ref, sample=sample)
        note = None
        if rows and rows[0]["c"][0] != c:
            note = f"Nothing on {c} at or after {p:,}. Showing what comes next."
        elif rows and m.group(1) and int(rows[0]["c"][1]) > p:  # (not for "start of chromosome")
            note = f"No record at {c}:{p:,}. Showing the next one, at {c}:{int(rows[0]['c'][1]):,}."
        return {"page": {"rows": rows, "next": nxt, "eof": eof, "atStart": off == v.data_start},
                "target": {"contig": c, "pos": p}, "note": note}

    def find_id(self, text, after_offset=None):
        """Start a background search of the ID column.  "123" means "rs123"."""
        v = self._vcf
        text = str(text or "").strip()
        if not text:
            return {"error": "Type an rsID, e.g. rs3121393"}
        if text.isdigit():
            text = "rs" + text
        if self._task is not None and not self._task.done:
            self._task.cancel.set()
        start = None
        if after_offset is not None:
            start = v.next_line_offset(after_offset)
            if start is None:
                return {"error": f"No more matches for {text}"}
        gen = self._gen

        def run(task):
            off = v.search_text(text, start=start, cancel=task.cancel, column=2,
                                progress=lambda x: setattr(task, "progress", x))
            if gen != self._gen:
                return None
            return {"offset": off, "text": text}

        self._task = Task("search", run)
        return {"searching": True, "text": text}

    def cancel(self):
        if self._task is not None:
            self._task.cancel.set()
        return True

    # -- misc ----------------------------------------------------------------------
    def reveal(self):
        """Show the open file in Finder / Explorer."""
        import subprocess
        import sys
        v = self._vcf
        if v is None:
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", v.path])
        elif os.name == "nt":
            subprocess.Popen(["explorer", "/select,", v.path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(v.path)])
