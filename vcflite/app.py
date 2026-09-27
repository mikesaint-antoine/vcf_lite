"""Desktop entry point: one native window hosting the web UI."""

from __future__ import annotations

import json
import os
import sys
from importlib import resources

from . import APP_NAME
from .api import Api

SMALL_SIZE = (420, 480)


def _ui_path() -> str:
    # Works both from source and from a PyInstaller bundle.
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "vcflite", "ui", "index.html")
    return str(resources.files("vcflite").joinpath("ui/index.html"))


def _file_arg(argv):
    for a in argv[1:]:
        if not a.startswith("-") and os.path.isfile(a):
            return os.path.abspath(a)
    return None


def _ensure_output():
    """Windowed builds (e.g. Windows) start with no stdout/stderr, and any
    print or traceback would then crash.  Send them to a log file instead,
    which also gives users something to send along with a bug report."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from .index import cache_dir
    try:
        log = cache_dir().parent / "log.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        f = open(log, "w", buffering=1, encoding="utf-8")
    except OSError:
        f = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def main(argv=None):
    _ensure_output()
    import webview
    from webview.dom import DOMEventHandler

    argv = sys.argv if argv is None else argv
    debug = "--debug" in argv
    initial = _file_arg(argv)

    api = Api()
    # Start as a small launcher; Api.open() grows the window once a file is open.
    width, height = (1280, 800) if initial else SMALL_SIZE
    api._small = not initial
    window = webview.create_window(
        APP_NAME,
        url=_ui_path(),
        js_api=api,
        width=width,
        height=height,
        min_size=(360, 420),
        background_color="#FFFFFF",
        text_select=True,
    )
    api._set_window(window)

    def on_drop(e):
        files = (e.get("dataTransfer") or {}).get("files") or []
        for f in files:
            p = f.get("pywebviewFullPath")
            if p:
                window.evaluate_js(f"window.app && window.app.openPath({json.dumps(p)})")
                break

    def on_loaded():
        try:
            window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        except Exception:
            pass
        if initial:
            window.evaluate_js(f"window.app && window.app.openPath({json.dumps(initial)})")

    window.events.loaded += on_loaded

    if "--selftest" in argv:
        # Smoke test for packaged builds: open the file, count rendered rows, quit.
        import threading
        import time

        def selftest():
            t0 = time.monotonic()
            loaded = window.events.loaded.wait(20)
            t_loaded = time.monotonic() - t0
            n = 0
            for _ in range(40):
                time.sleep(0.25)
                try:
                    n = window.evaluate_js("document.querySelectorAll('#body tr[data-o]').length") or 0
                except Exception:
                    n = 0
                if n:
                    break
            result = (f"SELFTEST rows={n} (page loaded {'in %.1fs' % t_loaded if loaded else 'NOT signalled'}, "
                      f"rows after {time.monotonic() - t0:.1f}s)")
            # Windowed Windows builds have no console, so also write the
            # result to a file when asked; the exit code says pass/fail.
            if os.environ.get("VCFLITE_SELFTEST_OUT"):
                with open(os.environ["VCFLITE_SELFTEST_OUT"], "w") as f:
                    f.write(result + "\n")
            if sys.stdout is not None:
                print(result, flush=True)
            window.destroy()
            os._exit(0 if n else 1)

        threading.Thread(target=selftest, daemon=True).start()
    # Default private mode = random local port, so several app instances can run at once.
    webview.start(debug=debug)


if __name__ == "__main__":
    main()
