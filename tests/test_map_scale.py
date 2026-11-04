"""Map sizing (app.js): radius and titles scale with √zoom, line width is constant, titles fade with
log2(zoom), and high-DPR screens get smaller dots. Evaluates the app.js formulas in Node."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "thread" / "web"
APP = (WEB / "app.js").read_text()


def run_formulas(script: str):
    settings = re.search(r"^const SETTINGS = \{.*?\};", APP, re.S | re.M).group(0)
    map_scale = re.search(r"^const MAP_SCALE = .*?;", APP, re.M).group(0)
    block = APP[APP.index("const OBS = "):]
    block = block[:block.index("\n", block.index("const titleFade = "))]
    src = "\n".join([settings, map_scale, "let transform = { k: 1 }, dpr = 2;",
                     "const neighbors = new Map([['hub', new Set([...Array(99).keys()])], ['leaf', new Set()]]);",
                     block, script])
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs Node")


@needs_node
def test_dots_grow_with_the_square_root_of_zoom():
    r = run_formulas("""
      const leaf = { id: 'leaf' }; leaf._size = obsSize(leaf);
      console.log(JSON.stringify({ at1: dotPx(leaf, 1), at4: dotPx(leaf, 4), at025: dotPx(leaf, 0.25) }));""")
    assert r["at4"] / r["at1"] == pytest.approx(2)
    assert r["at1"] / r["at025"] == pytest.approx(2)


@needs_node
def test_sizes_are_obsidians_clamped_by_links():
    r = run_formulas("""
      console.log(JSON.stringify({ leaf: obsSize({ id: 'leaf' }), hub: obsSize({ id: 'hub' }), unknown: obsSize({ id: 'x' }), mult: SETTINGS.nodeSize }));""")
    assert r["leaf"] == pytest.approx(8 * r["mult"])       # max(8, 3·√1)
    assert r["unknown"] == pytest.approx(8 * r["mult"])
    assert r["hub"] == pytest.approx(30 * r["mult"])        # min(3·√100, 30)


@needs_node
def test_a_3x_phone_gets_smaller_dots_than_a_2x_laptop():
    r = run_formulas("""
      const leaf = { id: 'leaf' }; leaf._size = obsSize(leaf);
      dpr = 2; const laptop = dotPx(leaf, 1), laptopLine = lineWidthPx();
      dpr = 3; const phone = dotPx(leaf, 1), phoneLine = lineWidthPx();
      console.log(JSON.stringify({ laptop, phone, laptopLine, phoneLine }));""")
    assert r["phone"] / r["laptop"] == pytest.approx((2 / 3) ** 0.5)
    assert r["phoneLine"] < r["laptopLine"]


@needs_node
def test_lines_keep_their_width_and_titles_fade_with_log_zoom():
    r = run_formulas("""
      transform.k = 0.2; const w1 = lineWidthPx(); transform.k = 4; const w2 = lineWidthPx();
      console.log(JSON.stringify({ w1, w2, far: titleFade(0.05), near: titleFade(3), fade: SETTINGS.textFade,
        mid: titleFade(Math.pow(2, 0.5 + SETTINGS.textFade - 1) / (dpr * MAP_SCALE)) }));""")
    assert r["w1"] == r["w2"]
    assert r["far"] == 0 and r["near"] == 1
    assert r["mid"] == pytest.approx(0.5)


def test_the_map_is_one_canvas_not_svg():
    html = (WEB / "index.html").read_text()
    assert '<canvas id="map"' in html and '<svg id="map"' not in html
    assert "g.node" not in APP and "path.edge" not in APP
