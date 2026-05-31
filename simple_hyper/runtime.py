"""Runtime helpers shared by local entrypoint scripts."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def ensure_local_venv(entrypoint: str | Path) -> None:
    project_dir = Path(entrypoint).resolve().parent
    venv_dir = project_dir / ".venv"
    candidates = [
        venv_dir / "bin" / "python",
        venv_dir / "Scripts" / "python.exe",
    ]
    venv_python = next((path for path in candidates if path.exists()), None)
    if venv_python is None:
        return
    if Path(sys.prefix).resolve() == venv_dir.resolve():
        return
    os.execv(str(venv_python), [str(venv_python), str(Path(entrypoint).resolve()), *sys.argv[1:]])


def load_dotenv(path: str = ".env") -> dict[str, str]:
    env_path = Path(path)
    values: dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")

    normalized = {key.lower(): value for key, value in values.items()}
    for key in ("account_address", "secret_key"):
        for env_key in (key, key.upper()):
            env_value = os.environ.get(env_key)
            if env_value:
                normalized[key] = env_value
                break
    if not normalized.get("account_address") or not normalized.get("secret_key"):
        raise FileNotFoundError(f"Missing credentials: set {env_path} or account_address/secret_key environment variables")
    return normalized


def mask(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}" if address else ""
