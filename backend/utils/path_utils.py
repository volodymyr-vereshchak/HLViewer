from pathlib import Path


def resolve_stored_path(stored_path: str) -> Path:
    """Normalize a stored path to an OS-native absolute Path.

    Handles:
    - Windows backslashes (\\backend\\...)  →  treated as relative to cwd
    - Relative paths (backend/data/...)     →  resolved from cwd
    - Absolute Unix paths (/app/backend/..) →  used as-is

    cwd is /app in Docker, or the project root when running locally.
    """
    if "\\" in stored_path:
        # Windows-style path: strip drive/leading sep, treat as relative to cwd
        normalized = stored_path.replace("\\", "/").lstrip("/")
        return Path.cwd() / normalized

    p = Path(stored_path)
    if p.is_absolute():
        return p
    return Path.cwd() / p
