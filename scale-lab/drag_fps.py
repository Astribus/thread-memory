"""Drag FPS under different rendering tweaks (desktop Chrome). Usage: python drag_fps.py PORT"""
import statistics, sys
from playwright.sync_api import sync_playwright
PORT = int(sys.argv[1])
TWEAKS = {
  "as is": "",
  "hide invisible labels": "document.querySelectorAll('g.node text').forEach(t => { if (+getComputedStyle(t).opacity < 0.05) t.style.display = 'none'; });",
  "no labels at all": "document.querySelectorAll('g.node text').forEach(t => t.style.display = 'none');",
  "no glass blur": "document.querySelectorAll('.glass, .recenter, .fab, .topnav').forEach(e => { e.style.backdropFilter = 'none'; e.style.webkitBackdropFilter = 'none'; });",
  "no CSS transitions": "void document.head.insertAdjacentHTML('beforeend', '<style>.node, .edge, path.edge, g.node * { transition: none !important; }</style>')",
  "no hover while dragging": "void nodeLayer.selectAll('g.node').on('mouseenter', null).on('mouseleave', null)",
  "both": "void (document.head.insertAdjacentHTML('beforeend', '<style>.node, .edge, path.edge, g.node * { transition: none !important; }</style>'), nodeLayer.selectAll('g.node').on('mouseenter', null).on('mouseleave', null))",
  "straight edges": "void (window.threadPath = e => `M${e.source.x},${e.source.y}L${e.target.x},${e.target.y}`)",
}
def run(pg, js):
    pg.goto(f"http://127.0.0.1:{PORT}/"); pg.wait_for_function("typeof nodes !== 'undefined' && nodes.length > 10", timeout=60000)
    pg.evaluate("activate('map')"); pg.wait_for_function("simulation.alpha() < simulation.alphaMin()", timeout=60000)
    if js: pg.evaluate(js)
    hub = pg.evaluate("""(() => { const vis = nodes.filter(x => !x.unresolved && x.degree > 5); const W = innerWidth, H = innerHeight;
      for (const h of vis) { const g = [...document.querySelectorAll('g.node')].find(e => e.__data__ === h); const r = g.querySelector('circle.dot').getBoundingClientRect();
        const x = r.x + r.width/2, y = r.y + r.height/2; if (x > 300 && x < W - 300 && y > 150 && y < H - 150) return [x, y]; } return [W/2, H/2]; })()""")
    pg.evaluate("window.__ft = []; window.__benchLast = performance.now(); window.__benchOn = true; (function loop(t) { __ft.push(t - __benchLast); __benchLast = t; if (__benchOn) requestAnimationFrame(loop); })(performance.now());")
    pg.mouse.move(*hub); pg.mouse.down()
    for i in range(1, 61): pg.mouse.move(hub[0] + 120 * (i % 20) / 20, hub[1] + 60 * ((i // 20) % 2)); pg.wait_for_timeout(33)
    pg.mouse.up(); pg.wait_for_timeout(200)
    ft = pg.evaluate("(() => { __benchOn = false; return __ft.slice(2); })()")
    active = pg.evaluate("simulation.alpha()")
    return 1000 / statistics.mean(ft), sorted(ft)[int(len(ft) * .95)], len(ft)
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome"); pg = b.new_page(viewport={"width": 1440, "height": 900})
    for name, js in TWEAKS.items():
        fps, p95, frames = run(pg, js); print(f"{PORT} {name:24s} {fps:5.1f} fps  p95 {p95:6.1f} ms  ({frames} frames)", flush=True)
    b.close()
