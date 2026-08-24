"""Launch the optional MinerU MCP without creating its output directory at startup."""

from pathlib import Path

from mineru_open_mcp import config


def lazy_ensure_output_dir(output_dir: str | None = None) -> Path:
    """Return the output path; the extraction tool creates it when saving."""
    return Path(output_dir or config.DEFAULT_OUTPUT_DIR)


def main() -> None:
    """Patch the optional server's eager startup behavior, then launch it."""
    config.ensure_output_dir = lazy_ensure_output_dir
    from mineru_open_mcp.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
