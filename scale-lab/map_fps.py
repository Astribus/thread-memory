"""Map smoothness: pan / zoom / drag frame rate and time-to-map, on desktop and a throttled phone profile.
Works on both the old SVG map and the canvas map (it only uses `nodes`, `d3` and #map).
Usage: python map_fps.py PORT LABEL   (server from serve.py on a COPY of the notes)"""
import json, statistics, sys
from playwright.sync_api import sync_playwright
PORT, LABEL = int(sys.argv[1]), sys.argv[2]
PROFILES = {
    "desktop": dict(viewport={"width": 1440, "height": 900}, device_scale_factor=2, throttle=1),
    "phone 4x-throttled": dict(viewport={"width": 440, "height": 956}, device_scale_factor=3, throttle=4, mobile=True),
    "phone webkit": dict(viewport={"width": 440, "height": 956}, device_scale_factor=3, throttle=1, engine="webkit"),
}
SCREEN = """(() => { const el = document.getElementById('map'), t = d3.zoomTransform(el), r = el.getBoundingClientRect();
  return { r: [r.left, r.top, r.width, r.height], pts: nodes.filter(n => !n.unresolved).map(n => [r.left + t.applyX(n.x), r.top + t.applyY(n.y), n.degree]) }; })()"""
START = "window.__ft = []; window.__last = performance.now(); window.__on = true; (function loop(t) { __ft.push(t - __last); __last = t; if (__on) requestAnimationFrame(loop); })(performance.now());"
STOP = "(() => { __on = false; return __ft.slice(2); })()"

def wait(pg, expr, timeout=120000):
    for _ in range(timeout // 100):
        if pg.evaluate(expr): return
        pg.wait_for_timeout(100)
    raise TimeoutError(expr)

def stats(ft):
    return {"fps": round(1000 / statistics.mean(ft), 1), "p95_ms": round(sorted(ft)[int(len(ft) * .95)], 1), "frames": len(ft)}

def empty_spot(s):
    L, T, W, H = s["r"]
    best, bd = (L + W / 2, T + H / 2), -1
    for gx in range(1, 12):
        for gy in range(2, 11):
            x, y = L + W * gx / 12, T + H * gy / 12
            d = min(((x - p[0]) ** 2 + (y - p[1]) ** 2 for p in s["pts"]), default=1e9)
            if d > bd: best, bd = (x, y), d
    return best

def run(p, prof):
    webkit = prof.get("engine") == "webkit"
    b = p.webkit.launch() if webkit else p.chromium.launch(channel="chrome")
    ctx = b.new_context(viewport=prof["viewport"], device_scale_factor=prof["device_scale_factor"], is_mobile=prof.get("mobile", False), has_touch=prof.get("mobile", False), bypass_csp=True)
    pg = ctx.new_page()
    if not webkit:
        ctx.new_cdp_session(pg).send("Emulation.setCPUThrottlingRate", {"rate": prof["throttle"]})
    out = {}
    pg.goto(f"http://127.0.0.1:{PORT}/")
    wait(pg, "typeof nodes !== 'undefined' && nodes.length > 10")
    out["open_map_ms"] = pg.evaluate("""(async () => { const t0 = performance.now(); await activate('map');
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); return Math.round(performance.now() - t0); })()""")
    wait(pg, "simulation.alpha() < simulation.alphaMin()")
    pg.wait_for_timeout(600)
    W, H = prof["viewport"]["width"], prof["viewport"]["height"]
    # pan: drag on empty space
    s = pg.evaluate(SCREEN); x, y = empty_spot(s)
    pg.evaluate(START); pg.mouse.move(x, y); pg.mouse.down()
    for i in range(1, 61): pg.mouse.move(x + 90 * ((i % 20) / 20 - .5), y + 60 * ((i // 20) % 2 - .5)); pg.wait_for_timeout(16)
    pg.mouse.up(); out["pan"] = stats(pg.evaluate(STOP)); pg.wait_for_timeout(400)
    # zoom: wheel in and out around the middle
    pg.mouse.move(W / 2, H / 2); pg.evaluate(START)
    for i in range(60): pg.mouse.wheel(0, -40 if (i // 15) % 2 == 0 else 40); pg.wait_for_timeout(16)
    out["zoom"] = stats(pg.evaluate(STOP)); pg.wait_for_timeout(600)
    # drag a well-linked visible note (warms the physics, the hardest case)
    s = pg.evaluate(SCREEN); L, T, Wm, Hm = s["r"]
    cand = sorted([q for q in s["pts"] if L + 60 < q[0] < L + Wm - 140 and T + 80 < q[1] < T + Hm - 120], key=lambda q: -q[2])
    hx, hy = cand[0][:2] if cand else (W / 2, H / 2)
    pg.evaluate(START); pg.mouse.move(hx, hy); pg.mouse.down()
    for i in range(1, 61): pg.mouse.move(hx + 100 * (i % 20) / 20, hy + 50 * ((i // 20) % 2)); pg.wait_for_timeout(33)
    pg.mouse.up(); pg.wait_for_timeout(300); out["drag"] = stats(pg.evaluate(STOP))
    out["nodes"] = pg.evaluate("nodes.length"); out["dom_elements"] = pg.evaluate("document.querySelectorAll('*').length")
    b.close()
    return out

with sync_playwright() as p:
    res = {name: run(p, prof) for name, prof in PROFILES.items()}
print(json.dumps({"label": LABEL, "port": PORT, **res}))
