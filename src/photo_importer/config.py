"""Load and merge photo-importer configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_EXTENSIONS = [
    ".jpg", ".jpeg", ".heic", ".png", ".tif", ".bmp", ".gif",
    # RAW formats: Canon, Nikon, Sony, Adobe/universal, Olympus, Fujifilm,
    # Panasonic/Leica, Pentax/Samsung, Sigma, Hasselblad, Kodak, Minolta,
    # Epson, Leaf, Phase One, GoPro.
    ".cr2", ".cr3", ".crw",
    ".nef", ".nrw",
    ".arw", ".srf", ".sr2",
    ".dng",
    ".orf", ".raf", ".rw2", ".rwl",
    ".pef", ".srw",
    ".x3f", ".3fr",
    ".dcr", ".kdc", ".mrw", ".erf", ".mos", ".iiq", ".gpr", ".raw",
    ".mp4", ".mov", ".avi", ".m4v", ".qt", ".webm",
]

def _default_config_locations() -> list[Path]:
    locations = [Path("config.yaml")]
    appdata = os.environ.get("APPDATA")
    if appdata:
        # Windows convention -- checked first since it's the more idiomatic
        # location there when present.
        locations.append(Path(appdata) / "photo-importer" / "config.yaml")
    locations.append(Path.home() / ".config" / "photo-importer" / "config.yaml")
    return locations


class ConfigError(Exception):
    pass


@dataclass
class Config:
    source: str = "auto"
    local_root: str | None = None
    extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    nas_mount_point: str | None = None
    nas_remote_subpath: str = ""
    nas_smb_url: str | None = None
    nas_sync_workers: int = 2
    migrate_source_path: str | None = None

    @property
    def extension_set(self) -> set[str]:
        return {e.lower() if e.startswith(".") else f".{e.lower()}" for e in self.extensions}


def _find_config_file(explicit_path: str | None) -> Path | None:
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path
    for candidate in _default_config_locations():
        if candidate.is_file():
            return candidate
    return None


def load_config(explicit_path: str | None = None) -> Config:
    """Load config.yaml (if present) into a Config, applying defaults for missing keys."""
    config_path = _find_config_file(explicit_path)
    data = {}
    if config_path is not None:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

    nas = data.get("nas") or {}
    migrate = data.get("migrate") or {}
    return Config(
        source=data.get("source", "auto"),
        local_root=data.get("local_root"),
        extensions=data.get("extensions", list(DEFAULT_EXTENSIONS)),
        nas_mount_point=nas.get("mount_point"),
        nas_remote_subpath=nas.get("remote_subpath", ""),
        nas_smb_url=nas.get("smb_url"),
        nas_sync_workers=nas.get("sync_workers", 2),
        migrate_source_path=migrate.get("source_path"),
    )


def apply_cli_overrides(config: Config, args) -> Config:
    """Override config fields with any explicitly-passed CLI flags."""
    if getattr(args, "source", None):
        config.source = args.source
    if getattr(args, "local_root", None):
        config.local_root = args.local_root
    return config


def require_local_root(config: Config) -> str:
    if not config.local_root:
        raise ConfigError(
            "local_root is not set. Pass --local-root or set it in your config.yaml."
        )
    return os.path.expanduser(config.local_root)
