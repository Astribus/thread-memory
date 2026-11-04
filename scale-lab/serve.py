import sys, uvicorn
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from thread.home import build_app
from thread.brain import Brain
uvicorn.run(build_app(Brain(Path(sys.argv[1]))), host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
