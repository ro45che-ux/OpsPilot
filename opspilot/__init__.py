

def _load_dotenv() -> None:
    """Read KEY=value lines from <repo>/.env (git-ignored) without overriding the real environment."""
    import os
    from pathlib import Path
    f = Path(__file__).resolve().parent.parent / ".env"
    if not f.is_file():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
