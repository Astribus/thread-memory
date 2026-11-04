"""Browser-side timings for Thread's map at several sizes (Chrome desktop + WebKit iPhone).
Usage: python bench_client.py LABEL  (servers on 8811/8812/8813 = 300/1000/3000 notes)"""
import json, statistics, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

LABEL = sys.argv[1] if len(sys.argv) > 1 else "run"
SHOTS = Path.home() / "projects/thread/physics-lab/shots/scale"; SHOTS.mkdir(parents=True, exist_ok=True)
SIZES = {300: 8811, 1000: 8812, 3000: 8813}

def measure(page, n, port, device):
    url = f"http://127.0.0.1:{port}/"
    page.goto(url); page.evaluate("localStorage.clear()")
    t0 = page.evaluate("performance.now()")
    page.goto(url)
    page.wait_for_function("typeof nodes !== 'undefined' && nodes.length > 10", timeout=120000)
    page.evaluate("activate('map')")
    loaded = page.evaluate("performance.now()")
    # settle: the simulation stops (alpha < alphaMin)
    page.wait_for_function("simulation.alpha() < simulation.alphaMin()", timeout=120000)
    settled = page.evaluate("performance.now()")
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / f"{LABEL}-{device}-{n}.png"))
    # drag the biggest hub for ~3 s, record frame times
    hub = page.evaluate("""(() => { const vis = nodes.filter(x => !x.unresolved && x.degree > 3); const W = innerWidth, H = innerHeight;
      for (const h of vis) { const g = [...document.querySelectorAll('g.node')].find(e => e.__data__ === h); const r = g.querySelector('circle.dot').getBoundingClientRect();
        const x = r.x + r.width/2, y = r.y + r.height/2; if (x > W * .2 && x < W * .8 && y > H * .25 && y < H * .75) return [x, y, h.id]; } return [W/2, H/2, null]; })()""")
    page.evaluate("""window.__ft = []; window.__last = performance.now(); window.__benchOn = true;
      (function loop(t) { __ft.push(t - __last); __last = t; if (__benchOn) requestAnimationFrame(loop); })(performance.now());""")
    page.mouse.move(hub[0], hub[1]); page.mouse.down()
    for i in range(1, 91):
        page.mouse.move(hub[0] + 80 * (i % 30) / 30, hub[1] + 40 * ((i // 30) % 2)); page.wait_for_timeout(33)
    page.mouse.up()
    page.wait_for_timeout(300)
    ft = page.evaluate("(() => { __benchOn = false; return __ft.slice(2); })()")
    fps = 1000 / statistics.mean(ft) if ft else 0
    p95 = sorted(ft)[int(len(ft) * 0.95)] if ft else 0
    cull = page.evaluate("(() => { const t = performance.now(); cullLabels(); return performance.now() - t; })()")
    # pop: one new note arrives while at rest
    page.wait_for_function("simulation.alpha() < simulation.alphaMin()", timeout=120000)
    pop = page.evaluate("""(async () => {
      const d = await (await fetch('/api/graph')).json(); const parent = d.nodes.find(x => x.degree > 3) || d.nodes[0];
      d.nodes.push({...parent, id: 'inbox/pop-scale', title: 'Pop test', degree: 1}); d.edges.push({source: parent.id, target: 'inbox/pop-scale'});
      const ft = []; let last = performance.now(), on = true; (function loop(t) { ft.push(t - last); last = t; if (on) requestAnimationFrame(loop); })(last);
      const t = performance.now(); setGraph(d); const setMs = performance.now() - t;
      await new Promise(r => setTimeout(r, 1500)); on = false;
      return [setMs, Math.max(...ft.slice(2)), ft.length]; })()""")
    mem = page.evaluate("performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1e6) : null")
    nodes_n = page.evaluate("nodes.length")
    # Home + search responsiveness
    t = page.evaluate("performance.now()"); page.evaluate("activate('home')"); page.wait_for_timeout(50)
    home_ms = page.evaluate(f"performance.now() - {t}")
    search_ms = page.evaluate("""(async () => { const t = performance.now(); const box = document.querySelector('#search, .search input');
      if (!box) return null; box.value = 'exam'; box.dispatchEvent(new Event('input', {bubbles: true}));
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); return performance.now() - t; })()""")
    return {"nodes": nodes_n, "load_ms": round(loaded - t0), "settle_ms": round(settled - loaded), "drag_fps": round(fps, 1),
            "drag_p95_ms": round(p95, 1), "cull_ms": round(cull, 1), "pop_setgraph_ms": round(pop[0], 1), "pop_worst_frame_ms": round(pop[1], 1),
            "js_heap_mb": mem, "home_ms": round(home_ms), "search_ms": round(search_ms, 1) if search_ms is not None else None}

with sync_playwright() as p:
    out = {}
    for device in (sys.argv[2:] or ["desktop", "iphone"]):
        if device == "desktop":
            b = p.chromium.launch(channel="chrome"); ctx = b.new_context(viewport={"width": 1440, "height": 900})
        else:
            b = p.webkit.launch(); ctx = b.new_context(**{k: v for k, v in p.devices["iPhone 15 Pro Max"].items() if k != "default_browser_type"})
        for n, port in SIZES.items():
            page = ctx.new_page(); errs = []; page.on("pageerror", lambda e: errs.append((e.stack or str(e))[:900]))
            try:
                r = measure(page, n, port, device)
            except Exception as e:
                r = {"error": str(e)[:200]}
            r["errors"] = errs[:2]; out[f"{device}-{n}"] = r; print(device, n, r, flush=True); page.close()
        b.close()
    json.dump(out, open(SHOTS / f"{LABEL}-client.json", "w"), indent=1)
