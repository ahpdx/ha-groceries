"""Configuration: HA add-on options (/data/options.json) or environment variables for local runs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LOCATION = ""   # chosen on the Settings page
DEFAULT_REDIRECT = "http://localhost:8000/callback"
HA_SUPERVISOR_PROXY = "172.30.32.2"


@dataclass
class Config:
    client_id: str
    client_secret: str
    location_id: str
    data_dir: Path
    redirect_uri: str
    trusted_proxy: str | None    # only accept requests from this address (HA Ingress); None = any
    host: str
    port: int


def load() -> Config:
    data_dir = Path(os.environ.get("GROCER_DATA_DIR", "/data"))
    opts: dict = {}
    options_file = data_dir / "options.json"
    if options_file.is_file():
        opts = json.loads(options_file.read_text())
    in_addon = options_file.is_file() and bool(os.environ.get("SUPERVISOR_TOKEN"))
    return Config(
        client_id=opts.get("client_id") or os.environ.get("KROGER_CLIENT_ID", ""),
        client_secret=opts.get("client_secret") or os.environ.get("KROGER_CLIENT_SECRET", ""),
        location_id=str(opts.get("location_id") or os.environ.get("GROCER_LOCATION_ID", DEFAULT_LOCATION)),
        data_dir=data_dir,
        redirect_uri=opts.get("redirect_uri") or os.environ.get("KROGER_REDIRECT_URI", DEFAULT_REDIRECT),
        trusted_proxy=os.environ.get("GROCER_TRUSTED_PROXY") or (HA_SUPERVISOR_PROXY if in_addon else None),
        host=os.environ.get("GROCER_HOST", "0.0.0.0" if in_addon else "127.0.0.1"),
        port=int(os.environ.get("GROCER_PORT", "8099")),
    )
