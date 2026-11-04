"""Server-side timings + peak memory for Thread at several brain sizes. Usage: python bench_server.py DIR [label]
DIR holds brain-300/, brain-1000/, brain-3000/ (+ -chats) made by gen.py. Prints a markdown table."""
import gc, json, shutil, sys, tempfile, time, tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from starlette.testclient import TestClient  # noqa: E402

from thread.brain import Brain  # noqa: E402
from thread.chats import ChatBackup  # noqa: E402
from thread.home import build_app  # noqa: E402
from thread.server import build_server  # noqa: E402


def measure(fn, repeat=3):
    gc.collect()
    best, peak = 1e9, 0
    for _ in range(repeat):
        tracemalloc.start()
        t = time.perf_counter()
        out = fn()
        dt = time.perf_counter() - t
        _, p = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        best, peak = min(best, dt), max(peak, p)
    size = len(out) if isinstance(out, (str, bytes)) else len(json.dumps(out, default=str)) if out is not None else 0
    return best * 1000, peak / 1e6, size


def run(base: Path, n: int):
    src = base / f"brain-{n}"
    work = Path(tempfile.mkdtemp()) / "brain"   # work on a copy
    shutil.copytree(src, work)
    brain = Brain(work)
    chats = ChatBackup(base / f"brain-{n}-chats")
    client = TestClient(build_app(brain))
    server = build_server(brain)
    rows = {}
    rows["/api/graph"] = measure(lambda: client.get("/api/graph").content)
    rows["/api/tree"] = measure(lambda: client.get("/api/tree").content)
    rows["/api/profile"] = measure(lambda: client.get("/api/profile").content)
    rows["/api/setup"] = measure(lambda: client.get("/api/setup").content)
    rows["/ (page)"] = measure(lambda: client.get("/").content)
    rows["search_memory"] = measure(lambda: brain.search("exam matrix regression"))
    rows["list_memory"] = measure(lambda: brain.tree())
    some = next(p for p in work.rglob("*.md") if "classes" in p.parts and p.stem != p.parent.name)
    rows["read_note"] = measure(lambda: brain.read(str(some.relative_to(work))))
    rows["remember (+related)"] = measure(lambda: brain.remember(
        f"Scale test note {time.time_ns()}", "content", "inbox",
        related=[r["path"] for r in brain.search("exam matrix")][:3]), repeat=3)
    rows["search_chats"] = measure(lambda: chats.search("lecture deadline"))
    rows["instructions()"] = measure(lambda: server._lowlevel_server.instructions)
    rows["/api/export"] = measure(lambda: client.get("/api/export").content, repeat=1)
    shutil.rmtree(work.parent)
    return rows


if __name__ == "__main__":
    base = Path(sys.argv[1]); label = sys.argv[2] if len(sys.argv) > 2 else ""
    sizes = [300, 1000, 3000]
    results = {n: run(base, n) for n in sizes}
    keys = list(results[sizes[0]])
    print(f"### Server {label}\n")
    print("| call | " + " | ".join(f"{n} notes: ms / peak MB / out KB" for n in sizes) + " |")
    print("|---|" + "---|" * len(sizes))
    for k in keys:
        cells = []
        for n in sizes:
            ms, mb, size = results[n][k]
            flag = " ⚠" if ms > 500 or mb > 30 else ""
            cells.append(f"{ms:,.0f} / {mb:.1f} / {size / 1024:,.0f}{flag}")
        print(f"| {k} | " + " | ".join(cells) + " |")
    json.dump({str(n): r for n, r in results.items()}, open(base / f"server-{label or 'run'}.json", "w"))
