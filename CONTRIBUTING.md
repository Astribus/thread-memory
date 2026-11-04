# Contributing

Thread is a personal project and isn't actively maintained, but issues and small pull requests are welcome.

1. `uv sync`
2. Make the change, with a test where it makes sense.
3. `uv run pytest -q` must pass.
4. Never point tests or scripts at a real `~/thread-brain`; use `tmp_path` or `scale-lab/gen.py`.
