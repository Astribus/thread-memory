def main() -> None:
    """Local stdio MCP server (`thread`). Imported lazily so thread-admin stays light."""
    from thread.server import main as run
    run()


__all__ = ["main"]
