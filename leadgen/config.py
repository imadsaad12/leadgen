"""Application settings (environment / .env), all prefixed LEADGEN_."""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEADGEN_", env_file=".env", extra="ignore")

    home: Path = Path("~/.leadgen")
    db_path: Path | None = None
    out_dir: Path | None = None

    host: str = "127.0.0.1"
    port: int = 8080

    # LinkedIn MCP sidecar (vendored, patched fork)
    mcp_bin: str = "mcp-server-linkedin"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_path: str = "/mcp"
    mcp_tool_timeout_s: float = 180.0

    # LLM assist via the `claude` CLI (subscription login; no API key is used)
    claude_bin: str = "claude"
    claude_model: str = "sonnet"
    claude_effort: str = "low"
    claude_timeout_s: float = 180.0

    @field_validator("home", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()

    @property
    def database(self) -> Path:
        return (self.db_path or (self.home / "leadgen.db")).expanduser()

    @property
    def outputs(self) -> Path:
        return (self.out_dir or (self.home / "out")).expanduser()

    @property
    def claude_cwd(self) -> Path:
        return self.home / "claude-cwd"

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}{self.mcp_path}"

    def ensure_dirs(self) -> None:
        for p in (self.home, self.outputs, self.claude_cwd):
            p.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
