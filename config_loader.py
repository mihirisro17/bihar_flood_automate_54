"""
Shared config/secrets loader for the Bihar Flood Automation pipeline.

Two machines are involved:

  Your PC          -> runs credentials_http_server.py. Keeps token.json
                       valid (refreshes it, or opens a browser to log in
                       when needed) and serves it + credentials.json over
                       a small HTTP server on your LAN.

  192.168.2.54      -> the "compute server". Runs dash.py as a systemd
                       service. Every loop, it PULLS token.json /
                       credentials.json from your PC's HTTP server, then
                       does its normal job: check Gmail, download zips,
                       SFTP-upload them to 192.168.2.137.

  192.168.2.137     -> unrelated storage target. Just where finished
                       .zip files get uploaded. Nothing here talks to it
                       except the SFTP upload step, unchanged from before.

Two small files live alongside the scripts, in REPO_DIR:

  config.json            -> COMMITTED to git (public repo, non-secret).
                             Holds: the .137 upload target's host/port/
                             user/remote-dir, your PC's LAN host/port for
                             the credentials server, and the loop interval.

  secrets.local.json      -> NEVER committed (must stay in .gitignore).
                             Holds the .137 SFTP password and the shared
                             auth token used between the PC's credentials
                             server and dash.py's pull requests. Created
                             once, by hand, on each machine that needs it.
"""

import os
import json

DEFAULT_CONFIG = {
    "sftp_upload": {
        "host": "192.168.2.137",
        "port": 22,
        "user": "sac",
        "remote_dir": "/home/sac/Documents/new_bihar/bihar_flood_Zip/",
    },
    "pc_credentials_server": {
        "host": "192.168.3.202",
        "port": 8765,
    },
    "interval_minutes": 15,
}


def get_repo_dir() -> str:
    """Folder this script lives in. Works unchanged on the PC and the
    compute server, so REPO_DIR never needs to be hand-edited per machine."""
    return os.path.dirname(os.path.abspath(__file__))


def load_config(repo_dir: str) -> dict:
    """Re-reads config.json every call (cheap) so a `git pull` that just
    updated it takes effect on the very next loop, no restart needed."""
    path = os.path.join(repo_dir, "config.json")
    if not os.path.exists(path):
        return DEFAULT_CONFIG
    try:
        with open(path, "r") as f:
            data = json.load(f)
        merged = {**DEFAULT_CONFIG, **data}
        merged["sftp_upload"] = {**DEFAULT_CONFIG["sftp_upload"], **data.get("sftp_upload", {})}
        merged["pc_credentials_server"] = {
            **DEFAULT_CONFIG["pc_credentials_server"],
            **data.get("pc_credentials_server", {}),
        }
        return merged
    except Exception:
        return DEFAULT_CONFIG


def load_secrets(repo_dir: str) -> dict:
    """Reads secrets.local.json (keys: sftp_upload_password,
    pc_credentials_auth_token). Per-machine, gitignored, never synced
    automatically. See secrets.local.json.example."""
    path = os.path.join(repo_dir, "secrets.local.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} not found.\n"
            f"Copy secrets.local.json.example -> secrets.local.json and "
            f"fill in the real values on this machine. This file must "
            f"never be committed to git."
        )
    with open(path, "r") as f:
        return json.load(f)