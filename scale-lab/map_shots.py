"""Screenshots of the map at fixed zooms, phone (WebKit, 440x956 @3x) and desktop, light and dark.
Usage: python map_shots.py PORT LABEL OUT_DIR   (server from serve.py on a COPY of the notes)"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
PORT, LABEL, OUT = int(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
ZOOMS = [0.25, 0.5, 1, 2, 4]
DEVICES = {"iphone": dict(engine="webkit", viewport={"width": 440, "height": 956}, device_scale_factor=3, is_mobile=True, has_touch=True),
           "desktop": dict(engine="chromium", viewport={"width": 1440, "height": 900}, device_scale_factor=2)}
def wait(pg, expr):
    for _ in range(600):
        if pg.evaluate(expr): return
        pg.wait_for_timeout(100)
with sync_playwright() as p:
    for dev, d in DEVICES.items():
        d = dict(d); engine = d.pop("engine")
        b = p.webkit.launch() if engine == "webkit" else p.chromium.launch(channel="chrome")
        for scheme in ("light", "dark"):
            ctx = b.new_context(color_scheme=scheme, bypass_csp=True, **d); pg = ctx.new_page()
            pg.goto(f"http://127.0.0.1:{PORT}/"); wait(pg, "typeof nodes !== 'undefined' && nodes.length > 10")
            pg.evaluate("activate('map')"); wait(pg, "simulation.alpha() < simulation.alphaMin()"); pg.wait_for_timeout(800)
            for k in ZOOMS:
                pg.evaluate("""k => { const el = document.getElementById('map'), r = el.getBoundingClientRect(), me = byId.get('me');
                  d3.select(el).call(zoom.transform, d3.zoomIdentity.translate(r.width / 2, r.height / 2).scale(k).translate(-me.x, -me.y)); }""", k)
                pg.wait_for_timeout(700)
                pg.screenshot(path=str(OUT / f"{LABEL}-{dev}-{scheme}-zoom{k}.png"))
            ctx.close()
        b.close()
print("done")
