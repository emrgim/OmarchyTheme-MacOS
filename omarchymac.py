#!/usr/bin/env python3
"""OmarchyMac menu-bar app: shows the Omarchy Link pairing code (like the TV's
"Connect Omarchy Linux" screen) and the current synced theme, while the Link
server receives pushes from SuperOMI's omarchy-link and applies them to macOS.
"""
from __future__ import annotations

import threading
from pathlib import Path

import rumps

import otvmac_server as srv


def fmt_code(code: str) -> str:
    return f"{code[:3]} {code[3:]}" if code and len(code) == 6 else code or "------"


class OmarchyMacApp(rumps.App):
    def __init__(self):
        super().__init__("OmarchyMac", title="◖◗", quit_button=None)
        self.tv = srv.MacTV(name="MacLiber")
        self.httpd = srv.serve(self.tv)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.responder = srv.advertise_mdns(self.tv)

        self.i_status = rumps.MenuItem("Starting…")
        self.i_theme = rumps.MenuItem("Theme: —")
        self.i_accent = rumps.MenuItem("System accent: —")
        self.i_code = rumps.MenuItem("")
        self.i_pair = rumps.MenuItem("Pair a new computer", callback=self.pair_now)
        self.i_apply = rumps.MenuItem("Re-apply current theme", callback=self.apply_now)
        self.i_log = rumps.MenuItem("Open log", callback=self.open_log)
        self.menu = [
            self.i_status, self.i_theme, self.i_accent, None,
            self.i_code, self.i_pair, None,
            self.i_apply, self.i_log, None,
            rumps.MenuItem("Quit OmarchyMac", callback=self.quit_app),
        ]
        self.refresh(None)
        rumps.Timer(self.refresh, 2).start()

    def refresh(self, _):
        tv = self.tv
        paired = list(tv.pcs.values())
        if tv.pairing_open:
            self.title = f"◖ {fmt_code(tv.code)}"
            self.i_code.title = f"Pairing code:  {fmt_code(tv.code)}"
            self.i_code.set_callback(None)
            self.i_status.title = "Waiting to pair — enter the code on SuperOMI"
        else:
            self.title = "◖◗"
            self.i_code.title = "Not in pairing mode"
            self.i_code.set_callback(None)
            if paired:
                names = ", ".join(p.get("name", "?") for p in paired)
                when = tv.last_push_at
                extra = f" · last push {srv.time.strftime('%H:%M', srv.time.localtime(when))}" if when else ""
                self.i_status.title = f"Paired: {names}{extra}"
            else:
                self.i_status.title = "Not paired"
        lt = tv.last_theme or {}
        self.i_theme.title = f"Theme: {lt.get('name') or '—'} ({lt.get('mode', '—')})"
        ap = (lt.get("applied") or {})
        acc = lt.get("accent") or "—"
        self.i_accent.title = f"Accent {acc} → macOS {ap.get('accent_name', '—')}"

    def pair_now(self, _):
        code = self.tv.open_pairing()
        if self.responder:
            try:
                self.responder.terminate()
            except Exception:
                pass
        self.responder = srv.advertise_mdns(self.tv)
        self.refresh(None)
        rumps.notification("OmarchyMac", "Pairing open",
                           f"On SuperOMI run:  omarchy-link pair <this-mac-ip> --code {code}")

    def apply_now(self, _):
        lt = self.tv.last_theme
        if not lt:
            rumps.notification("OmarchyMac", "Nothing to apply", "No theme has been received yet.")
            return
        srv.apply_theme(lt, self.tv.wallpaper_path())
        self.refresh(None)

    def open_log(self, _):
        srv.subprocess.Popen(["open", str(srv.LOG_FILE)])

    def quit_app(self, _):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        if self.responder:
            try:
                self.responder.terminate()
            except Exception:
                pass
        rumps.quit_application()


if __name__ == "__main__":
    OmarchyMacApp().run()
