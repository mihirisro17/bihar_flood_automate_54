import os
import re
import sys
import time
import signal
import base64
import logging
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime, timedelta
from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError
import paramiko

import config_loader

# Google API Imports
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Rich Imports
from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.align import Align
from rich.spinner import Spinner
from rich.progress_bar import ProgressBar
from rich.box import ROUNDED, DOUBLE

# ==========================================
# CONFIGURATION
# ==========================================
# REPO_DIR is derived from this file's own location, so the exact same
# code runs unchanged on the PC and on the compute server -- no
# per-machine path edits needed.
REPO_DIR = config_loader.get_repo_dir()
OPERATION_FILE = os.path.join(REPO_DIR, 'operation.txt')
ZIP_DIR = REPO_DIR

# This script runs on the COMPUTE server (192.168.2.54). It has no
# browser, so it must never attempt the interactive Gmail OAuth flow --
# that would hang forever waiting to open a browser that doesn't exist.
# Instead, every loop it pulls a fresh token.json from your PC's
# credentials_http_server.py (see sync_credentials_from_pc() below). If
# that ever fails and the local token can't be silently refreshed
# either, it alerts and skips Gmail for that loop rather than crashing.
ALLOW_INTERACTIVE_AUTH = False

# The .137 upload target's host/port/user/remote-dir, your PC's LAN
# host/port for credential pulls, and the loop interval all live in
# config.json (committed to git, non-secret). They're reloaded fresh
# every loop from reload_runtime_config() below, so editing config.json
# on the PC + `git push` takes effect on this server's very next
# `git pull` -- no server login, no restart.
_config = config_loader.load_config(REPO_DIR)
SFTP_HOST = _config["sftp_upload"]["host"]
SFTP_PORT = _config["sftp_upload"]["port"]
SFTP_USER = _config["sftp_upload"]["user"]
SFTP_REMOTE_DIR = _config["sftp_upload"]["remote_dir"]
INTERVAL_MINUTES = _config["interval_minutes"]

# The SFTP password and the PC-credentials-server auth token are
# secrets: they live only in secrets.local.json, which is gitignored
# and never committed. Must be created once, by hand, on this machine
# (see secrets.local.json.example).
try:
    _secrets = config_loader.load_secrets(REPO_DIR)
    SFTP_PASS = _secrets["sftp_upload_password"]
except FileNotFoundError as e:
    # Fail loudly at startup rather than silently using a placeholder --
    # SFTP upload can't work without this.
    raise SystemExit(str(e))

# Gmail API Scopes
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
TOKEN_PATH = os.path.join(REPO_DIR, 'token.json')
CREDS_PATH = os.path.join(REPO_DIR, 'credentials.json')


def reload_runtime_config():
    """Re-reads config.json and secrets.local.json into the module-level
    settings used elsewhere. Called once per main loop iteration, right
    after `git pull`, so PC-side edits show up here without a restart."""
    global SFTP_HOST, SFTP_PORT, SFTP_USER, SFTP_REMOTE_DIR, INTERVAL_MINUTES, SFTP_PASS

    cfg = config_loader.load_config(REPO_DIR)
    SFTP_HOST = cfg["sftp_upload"]["host"]
    SFTP_PORT = cfg["sftp_upload"]["port"]
    SFTP_USER = cfg["sftp_upload"]["user"]
    SFTP_REMOTE_DIR = cfg["sftp_upload"]["remote_dir"]
    INTERVAL_MINUTES = cfg["interval_minutes"]

    try:
        SFTP_PASS = config_loader.load_secrets(REPO_DIR)["sftp_upload_password"]
    except FileNotFoundError:
        # Keep the last-known-good password in memory rather than crashing
        # mid-run; upload_new_zips() will surface an auth error if it's stale.
        pass


# ==========================================
# LOGGING -> feeds the dashboard's log panel
# ==========================================
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
log_file_path = os.path.join(REPO_DIR, 'automation.log')

LOG_VIEW = deque(maxlen=60)      
ALERTS = deque(maxlen=6)         
FILE_ACTIVITY = deque(maxlen=8)  

LEVEL_STYLE = {
    "DEBUG": "grey50",
    "INFO": "bright_cyan",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}

class DashboardLogHandler(logging.Handler):
    def emit(self, record):
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        style = LEVEL_STYLE.get(record.levelname, "white")
        line = Text()
        line.append(f"{ts} ", style="grey58")
        line.append(f"{record.levelname:<8} ", style=style)
        line.append(record.getMessage(), style=style if record.levelno >= logging.WARNING else "grey85")
        LOG_VIEW.append(line)

file_handler = logging.FileHandler(log_file_path)
# Added %Y-%m-%d so the trim function can read the date
file_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"))

dash_handler = DashboardLogHandler()

# Under systemd (no terminal attached), also mirror everything to
# stdout so `journalctl -u <service>` shows live activity even though
# the Rich dashboard itself isn't drawn.
handlers = [file_handler, dash_handler]
IS_INTERACTIVE = sys.stdout.isatty()
if not IS_INTERACTIVE:
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    handlers.append(stream_handler)

logging.basicConfig(level=logging.INFO, handlers=handlers)
logger = logging.getLogger("BiharFloodAuto")

console = Console()

# ==========================================
# SHARED DASHBOARD STATE
# ==========================================
STATE = {
    "started_at": datetime.now(),
    "phase": "Initializing",
    "loop_count": 0,
    "git_status": "—",
    "git_commit": "—",
    "op_status": None,
    "emails_found": 0,
    "emails_matched": 0,
    "files_downloaded": 0,
    "files_uploaded": 0,
    "total_downloaded": 0,
    "total_uploaded": 0,
    "last_success": None,
    "next_run_time": None,
    "run_start_time": None,
    "sftp_status": "idle",
    "gmail_status": "idle",
    "pc_sync_status": "idle",
}

PHASE_ORDER = ["Git Pull", "Sync Creds", "Command Check", "Fetch Emails", "SFTP Upload", "Push Logs", "Sleeping"]

def push_alert(message: str, level: str = "error"):
    ALERTS.appendleft({"time": datetime.now().strftime("%H:%M:%S"), "message": message, "level": level})

def push_file_activity(name: str, action: str, is_new: bool):
    FILE_ACTIVITY.appendleft({
        "time": datetime.now().strftime("%H:%M:%S"),
        "name": name,
        "action": action,
        "status": "NEW" if is_new else "EXISTS",
    })

# ==========================================
# PIPELINE FUNCTIONS 
# ==========================================

def trim_log_file(filepath: str, days_to_keep: int = 5):
    """Removes log entries older than the specified number of days."""
    if not os.path.exists(filepath):
        return

    cutoff_date = (datetime.now() - timedelta(days=days_to_keep)).date()
    kept_lines = []
    date_pattern = re.compile(r"^\[(\d{4}-\d{2}-\d{2})")

    with open(filepath, 'r') as f:
        for line in f:
            match = date_pattern.search(line)
            if match:
                try:
                    log_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                    if log_date >= cutoff_date:
                        kept_lines.append(line)
                except ValueError:
                    kept_lines.append(line)
            else:
                # Keep lines without dates (like stack traces) if we are already keeping recent logs
                if kept_lines:
                    kept_lines.append(line)

    file_handler.acquire()
    try:
        file_handler.stream.close()

        with open(filepath, 'w') as f:
            f.writelines(kept_lines)

        file_handler.stream = file_handler._open()
    finally:
        file_handler.release()


def sync_credentials_from_pc():
    """Pull the latest token.json / credentials.json from the PC's
    credentials_http_server.py. If the PC is off or unreachable, this
    just leaves the existing local files alone -- it never blocks the
    rest of the loop or raises."""
    cfg = config_loader.load_config(REPO_DIR)
    pc_cfg = cfg.get("pc_credentials_server", {})
    host = pc_cfg.get("host")
    port = pc_cfg.get("port", 8765)

    if not host:
        STATE["pc_sync_status"] = "not configured"
        return

    try:
        auth_token = config_loader.load_secrets(REPO_DIR).get("pc_credentials_auth_token")
    except FileNotFoundError:
        auth_token = None

    if not auth_token:
        STATE["pc_sync_status"] = "no auth token configured"
        return

    headers = {"Authorization": f"Bearer {auth_token}"}
    targets = {"token.json": TOKEN_PATH, "credentials.json": CREDS_PATH}
    pulled_any = False

    for filename, local_path in targets.items():
        url = f"http://{host}:{port}/{filename}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = resp.read()
            with open(local_path, 'wb') as f:
                f.write(data)
            pulled_any = True
        except urllib.error.HTTPError as e:
            if e.code == 401:
                logger.error("[-] PC credentials server rejected our auth token (401).")
                push_alert("PC credentials server auth token mismatch — check secrets.local.json on both machines.", "error")
                STATE["pc_sync_status"] = "auth error"
                return
            logger.warning(f"[-] PC credentials server returned HTTP {e.code} for {filename} at {url}")
            STATE["pc_sync_status"] = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # PC is off or unreachable -- expected sometimes, not critical,
            # but log it visibly so it's not a silent mystery.
            STATE["pc_sync_status"] = "PC unreachable (using cached files)"
            logger.info(f"[-] Could not reach PC credentials server at {url} ({e}). Using existing local files.")
            return
        except Exception as e:
            logger.warning(f"[-] Unexpected error pulling {filename} from PC ({url}): {e}")
            STATE["pc_sync_status"] = "error"
            return

    if pulled_any:
        STATE["pc_sync_status"] = "synced"
        logger.debug("Pulled latest token.json/credentials.json from PC.")


def push_logs_to_git(repo_dir: str = REPO_DIR, log_filename: str = 'automation.log'):
    """Commits and pushes the trimmed automation log to GitHub without hanging on prompts."""
    STATE["git_status"] = "pushing logs"
    try:
        repo = Repo(repo_dir)
        
        if repo.is_dirty(untracked_files=True) or log_filename in repo.untracked_files:
            repo.git.add(log_filename)
            
            if repo.index.diff("HEAD"):
                commit_msg = f"Auto-update: Automation logs ({datetime.now().strftime('%Y-%m-%d')})"
                repo.index.commit(commit_msg)
                
                origin = repo.remotes.origin
                
                # Forcing GIT_TERMINAL_PROMPT=0 prevents Git from hanging to ask for passwords
                # It will instantly throw a GitCommandError instead, allowing the loop to continue.
                origin.push(env={"GIT_TERMINAL_PROMPT": "0"})
                
                logger.info(f"Successfully pushed {log_filename} to Git.")
                STATE["git_status"] = "logs pushed"
            else:
                STATE["git_status"] = "logs up to date"
        else:
            STATE["git_status"] = "logs up to date"
            
    except GitCommandError as e:
        logger.error(f"Git push failed (Authentication required): {e.stderr.strip()}")
        STATE["git_status"] = "push auth failed"
        push_alert("Git push blocked. Configure SSH keys or Git credentials.", "error")
    except Exception as e:
        logger.error(f"Failed to push logs to Git: {e}")
        STATE["git_status"] = "push failed"
        

def run_git_pull(repo_dir: str = REPO_DIR) -> bool:
    logger.info("Executing 'git pull' to check for updates...")
    state_changed = False

    try:
        repo = Repo(repo_dir)

        if repo.bare:
            logger.error(f"Repository at {repo_dir} is bare. Cannot perform pull.")
            STATE["git_status"] = "bare repo error"
            push_alert(f"Repository at {repo_dir} is bare — cannot pull.", "critical")
            return False

        origin = repo.remotes.origin
        old_commit = repo.head.commit

        logger.debug(f"Pulling from remote: {origin.url}...")
        origin.pull(env={"GIT_TERMINAL_PROMPT": "0"})
        new_commit = repo.head.commit
        STATE["git_commit"] = new_commit.hexsha[:7]

        if old_commit == new_commit:
            logger.info("Repository is already up to date.")
            STATE["git_status"] = "up to date"
        else:
            logger.info(f"Git pull downloaded new changes. HEAD moved from {old_commit.hexsha[:7]} to {new_commit.hexsha[:7]}.")
            state_changed = True
            STATE["git_status"] = "updated"

        missing_zips = []
        for diff in repo.index.diff(None):
            if diff.change_type == 'D' and diff.a_path.endswith('.zip'):
                missing_zips.append(diff.a_path)

        if missing_zips:
            logger.warning(f"Detected {len(missing_zips)} missing .zip file(s) that exist in the repository:")
            for zip_file in missing_zips:
                logger.info(f" -> Restoring missing file: {zip_file}")
                repo.git.checkout('--', zip_file)
                push_file_activity(os.path.basename(zip_file), "Restored", is_new=False)

            logger.info("All missing .zip files have been successfully restored.")
            state_changed = True
            STATE["git_status"] = f"restored {len(missing_zips)} file(s)"
        else:
            logger.debug("No locally tracked .zip files are missing.")

        return state_changed

    except InvalidGitRepositoryError:
        logger.critical(f"The directory '{repo_dir}' is not a valid git repository.")
        STATE["git_status"] = "invalid repo"
        push_alert(f"'{repo_dir}' is not a valid git repository.", "critical")
    except GitCommandError as e:
        logger.error(f"Git pull failed with status code {e.status}.")
        logger.debug(f"Git error stderr: {e.stderr.strip()}")
        STATE["git_status"] = "pull failed"
        push_alert(f"git pull failed (exit {e.status}) — likely needs SSH deploy key auth, see setup guide.", "error")
    except Exception as e:
        logger.exception(f"An unexpected error occurred during git operations: {e}")
        STATE["git_status"] = "error"
        push_alert(f"Git error: {e}", "error")

    return False

def check_operation_status():
    if not os.path.exists(OPERATION_FILE):
        logger.warning(f"[-] {OPERATION_FILE} not found. Defaulting to 'off'.")
        STATE["op_status"] = False
        push_alert("operation.txt not found — defaulting to OFF.", "warning")
        return False

    with open(OPERATION_FILE, 'r') as file:
        status = file.read().strip().lower()

    STATE["op_status"] = (status == 'on')
    return status == 'on'

def fetch_zips_via_api():
    os.makedirs(ZIP_DIR, exist_ok=True)
    STATE["gmail_status"] = "authenticating"
    creds = None

    try:
        if os.path.exists(TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError as e:
                    logger.error(f"[-] Gmail token refresh failed: {e}")
                    push_alert(
                        "Gmail token expired/revoked. Make sure "
                        "credentials_http_server.py is running on your PC "
                        "with a freshly-logged-in token.json -- it'll be "
                        "pulled automatically on the next loop.",
                        "critical",
                    )
                    STATE["gmail_status"] = "token expired"
                    creds = None

            if not creds:
                if not os.path.exists(CREDS_PATH):
                    logger.error(f"[-] {CREDS_PATH} not found.")
                    push_alert(
                        "credentials.json missing. Run "
                        "credentials_http_server.py on your PC so it can "
                        "be pulled here.",
                        "critical",
                    )
                    STATE["gmail_status"] = "missing credentials"
                    return

                if not ALLOW_INTERACTIVE_AUTH:
                    # This machine has no browser -- never attempt the
                    # interactive flow here, it would just hang. Wait for
                    # a valid token.json to be pulled from the PC instead.
                    logger.error("[-] No valid Gmail token and interactive auth is disabled on this machine.")
                    push_alert(
                        "Gmail needs re-authentication. On your PC, run "
                        "'python credentials_http_server.py' (it has a "
                        "browser) -- the next loop here will pull the "
                        "fresh token automatically.",
                        "critical",
                    )
                    STATE["gmail_status"] = "needs re-auth on PC"
                    return

                # Only reachable if ALLOW_INTERACTIVE_AUTH=True is set
                # explicitly, never on this headless compute server.
                flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(TOKEN_PATH, 'w') as token:
                token.write(creds.to_json())
    except Exception as e:
        logger.exception(f"Gmail authentication failed: {e}")
        push_alert(f"Gmail authentication failed: {e}", "critical")
        STATE["gmail_status"] = "auth error"
        return

    try:
        STATE["gmail_status"] = "searching"
        service = build('gmail', 'v1', credentials=creds)
        logger.info("Searching Gmail for UNREAD emails with 'Inundation Probability'...")

        results = service.users().messages().list(userId='me', q='subject:"Inundation Probability" is:unread').execute()
        messages = results.get('messages', [])
        STATE["emails_found"] = len(messages)

        if not messages:
            logger.info("No new matching emails found in Gmail.")
            STATE["gmail_status"] = "idle"
            STATE["emails_matched"] = 0
            STATE["files_downloaded"] = 0
            return

        subject_pattern = re.compile(
            r"inundation probability\s*(?:for\s*)?\d{2}-\d{2}-\d{4}\s*and\s*\d{2}-\d{2}-\d{4}",
            re.IGNORECASE
        )

        matched = 0
        downloaded_this_run = 0

        for msg in messages:
            msg_data = service.users().messages().get(userId='me', id=msg['id']).execute()
            payload = msg_data.get('payload', {})
            headers = payload.get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "")

            if subject_pattern.search(subject):
                matched += 1
                logger.info(f"[MATCH] Found email with Subject: '{subject}'")
                parts = payload.get('parts', [])

                for part in parts:
                    filename = part.get('filename')
                    if filename and filename.endswith('.zip'):
                        clean_filename = re.sub(r'\s*\(\d+\)', '', filename)
                        file_path = os.path.join(ZIP_DIR, clean_filename)
                        already_existed = os.path.exists(file_path)

                        STATE["gmail_status"] = f"downloading {clean_filename}"
                        logger.info(f"   -> Downloading: {filename} (Saving as {clean_filename})")
                        attachment_id = part['body'].get('attachmentId')
                        attachment = service.users().messages().attachments().get(
                            userId='me', messageId=msg['id'], id=attachment_id
                        ).execute()

                        file_data = base64.urlsafe_b64decode(attachment['data'])
                        with open(file_path, 'wb') as f:
                            f.write(file_data)

                        logger.info(f"   -> Saved successfully to {file_path}")
                        push_file_activity(clean_filename, "Downloaded", is_new=not already_existed)
                        downloaded_this_run += 1

                service.users().messages().modify(
                    userId='me', id=msg['id'], body={'removeLabelIds': ['UNREAD']}
                ).execute()
                logger.info("   -> Marked email as read.")
            else:
                logger.debug(f"[IGNORED] Subject didn't match full regex pattern: '{subject}'")

        STATE["emails_matched"] = matched
        STATE["files_downloaded"] = downloaded_this_run
        STATE["total_downloaded"] += downloaded_this_run
        STATE["gmail_status"] = "idle"
        if downloaded_this_run:
            STATE["last_success"] = f"Downloaded {downloaded_this_run} zip(s) via Gmail"

    except Exception as e:
        logger.exception(f"An error occurred while interacting with Gmail API: {e}")
        STATE["gmail_status"] = "error"
        push_alert(f"Gmail API error: {e}", "error")

def upload_new_zips():
    STATE["sftp_status"] = "connecting"
    if not os.path.exists(ZIP_DIR):
        logger.info("ZIP dir not found. Exiting the execution phase.")
        STATE["sftp_status"] = "idle"
        return

    zip_files = [f for f in os.listdir(ZIP_DIR) if f.endswith('.zip')]
    if not zip_files:
        logger.info("No .zip files found to upload.")
        STATE["sftp_status"] = "idle"
        STATE["files_uploaded"] = 0
        return

    try:
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=SFTP_USER, password=SFTP_PASS)
        sftp = paramiko.SFTPClient.from_transport(transport)
        STATE["sftp_status"] = "connected"

        try:
            sftp.stat(SFTP_REMOTE_DIR)
        except IOError:
            logger.info(f"[-] Remote directory {SFTP_REMOTE_DIR} not found. Creating it...")
            sftp.mkdir(SFTP_REMOTE_DIR)

        uploads_count = 0
        for zip_name in zip_files:
            local_path = os.path.join(ZIP_DIR, zip_name)
            remote_path = f"{SFTP_REMOTE_DIR.rstrip('/')}/{zip_name}"

            already_on_server = True
            try:
                sftp.stat(remote_path)
            except IOError:
                already_on_server = False

            STATE["sftp_status"] = f"uploading {zip_name}"
            action = "Overwriting" if already_on_server else "Uploading"
            logger.info(f"[+] {action}: {zip_name} -> server...")
            sftp.put(local_path, remote_path)
            push_file_activity(zip_name, "Uploaded", is_new=not already_on_server)
            uploads_count += 1

        STATE["files_uploaded"] = uploads_count
        STATE["total_uploaded"] += uploads_count

        if uploads_count > 0:
            logger.info(f"[+] Successfully copied {uploads_count} file(s).")
            STATE["last_success"] = f"Uploaded {uploads_count} zip(s) via SFTP"
        else:
            logger.info("[*] No zip files were processed.")

        sftp.close()
        transport.close()
        STATE["sftp_status"] = "idle"

    except paramiko.AuthenticationException:
        logger.error("[-] Authentication failed. Check your SFTP password.")
        STATE["sftp_status"] = "auth failed"
        push_alert("SFTP authentication failed — check secrets.local.json.", "critical")
    except Exception as e:
        logger.error(f"[-] SFTP Copy failed: {e}")
        STATE["sftp_status"] = "error"
        push_alert(f"SFTP copy failed: {e}", "error")

# ==========================================
# DASHBOARD RENDERING (only used in interactive mode)
# ==========================================

def _blink(a: str, b: str, period: float = 0.6) -> str:
    return a if int(time.time() / period) % 2 == 0 else b

def _dot(color: str) -> Text:
    return Text("●", style=color)

def _bool_badge(val):
    if val is True:
        return Text(" ● ON  ", style="bold white on green3")
    if val is False:
        return Text(" ● OFF ", style="bold white on red3")
    return Text(" ●  —  ", style="bold white on grey37")

def render_header():
    banner = Text(justify="center")
    banner.append("🌊 ", style="bold cyan")
    banner.append("BIHAR FLOOD AUTOMATION", style="bold white")
    banner.append("  DASHBOARD", style=_blink("bold bright_cyan", "bold cyan"))

    uptime = datetime.now() - STATE["started_at"]
    uptime_str = str(timedelta(seconds=int(uptime.total_seconds())))
    sub = Text(justify="center")
    sub.append(f"Loop #{STATE['loop_count']}", style="bold yellow")
    sub.append("   •   ", style="grey50")
    sub.append(f"Uptime {uptime_str}", style="grey70")
    sub.append("   •   ", style="grey50")
    sub.append(datetime.now().strftime("%A, %d %b %Y  %H:%M:%S"), style="bright_white")

    alert_count = len(ALERTS)
    if alert_count:
        sub.append("   •   ", style="grey50")
        sub.append(f"🔴 {alert_count} alert(s)", style=_blink("bold red", "red"))

    return Panel(Group(Align.center(banner), Align.center(sub)), box=DOUBLE,
                 style="on grey11", border_style=_blink("bright_cyan", "cyan"))

def render_pipeline():
    table = Table.grid(padding=(0, 1))
    table.add_column()
    current_idx = PHASE_ORDER.index(STATE["phase"]) if STATE["phase"] in PHASE_ORDER else -1

    for i, phase in enumerate(PHASE_ORDER):
        if i < current_idx:
            row = Text()
            row.append("✔ ", style="bold green")
            row.append(phase, style="green")
            table.add_row(row)
        elif i == current_idx:
            spinner = Spinner("dots", text=Text(f" {phase}...", style="bold black on yellow"), style="bold black on yellow")
            table.add_row(spinner)
        else:
            row = Text()
            row.append("○ ", style="grey42")
            row.append(phase, style="grey42")
            table.add_row(row)

    return Panel(table, title="[bold]⚙ Pipeline[/]", border_style="cyan", box=ROUNDED, padding=(1, 2))

def render_status():
    table = Table(box=None, show_header=False, expand=True, pad_edge=False, padding=(0, 1))
    table.add_column("k", style="bold grey70", width=14)
    table.add_column("v")

    table.add_row("Operation", _bool_badge(STATE["op_status"]))

    git_dot = _dot("green" if STATE["git_status"] in ("up to date", "updated", "logs pushed", "logs up to date") else "yellow")
    table.add_row("Git", Text.assemble(git_dot, f" {STATE['git_status']} ", (f"({STATE['git_commit']})", "grey58")))

    pc = STATE["pc_sync_status"]
    pc_color = "red" if ("error" in pc or "mismatch" in pc) else ("yellow" if pc not in ("synced", "idle") else "green")
    table.add_row("PC Sync", Text.assemble(_dot(pc_color), f" {pc}"))

    gm = STATE["gmail_status"]
    gmail_color = "red" if ("error" in gm or "expired" in gm or "missing" in gm or "re-auth" in gm) else ("yellow" if gm != "idle" else "green")
    table.add_row("Gmail", Text.assemble(_dot(gmail_color), f" {gm}"))

    sf = STATE["sftp_status"]
    sftp_color = "red" if ("error" in sf or "fail" in sf) else ("yellow" if sf != "idle" else "green")
    table.add_row("SFTP", Text.assemble(_dot(sftp_color), f" {sf}"))

    next_run = STATE["next_run_time"]
    start = STATE["run_start_time"]
    if next_run and start:
        total = (next_run - start).total_seconds()
        remaining = max((next_run - datetime.now()).total_seconds(), 0)
        elapsed_ratio = 1 - (remaining / total) if total > 0 else 1
        mins, secs = divmod(int(remaining), 60)
        bar = ProgressBar(total=100, completed=elapsed_ratio * 100, width=None,
                           complete_style="bright_magenta", finished_style="bright_magenta")
        table.add_row("Next run", f"[bold yellow]{mins:02d}:{secs:02d}[/] remaining")
        return Panel(Group(table, Text(""), bar), title="[bold]📡 Live Status[/]", border_style="magenta", box=ROUNDED, padding=(1, 2))

    table.add_row("Next run", "—")
    return Panel(table, title="[bold]📡 Live Status[/]", border_style="magenta", box=ROUNDED, padding=(1, 2))

def render_alerts():
    if not ALERTS:
        body = Align.center(Text("✅ No active alerts — all systems normal.", style="green"), vertical="middle")
        border = "green"
    else:
        rows = []
        for a in ALERTS:
            icon = "🔴" if a["level"] == "critical" else ("🟠" if a["level"] == "error" else "🟡")
            style = "bold red" if a["level"] == "critical" else ("red" if a["level"] == "error" else "yellow")
            line = Text()
            line.append(f"{icon} {a['time']}  ", style="grey58")
            line.append(a["message"], style=style)
            rows.append(line)
        body = Group(*rows)
        border = _blink("bold red", "red")

    return Panel(body, title="[bold]🚨 Alerts[/]", border_style=border, box=ROUNDED, padding=(1, 2))

def render_stats():
    table = Table(box=ROUNDED, expand=True, border_style="grey37")
    table.add_column("Metric", style="bold grey70")
    table.add_column("This Run", justify="right", style="bold white")
    table.add_column("Total", justify="right", style="bold green")

    table.add_row("📧 Emails found", str(STATE["emails_found"]), "—")
    table.add_row("✔ Emails matched", str(STATE["emails_matched"]), "—")
    table.add_row("⬇ Downloaded", str(STATE["files_downloaded"]), str(STATE["total_downloaded"]))
    table.add_row("⬆ Uploaded", str(STATE["files_uploaded"]), str(STATE["total_uploaded"]))

    return Panel(table, title="[bold]📊 Transfer Stats[/]", border_style="green", box=ROUNDED, padding=(1, 1))

def render_file_activity():
    table = Table(box=ROUNDED, expand=True, border_style="grey37")
    table.add_column("Time", style="grey58", width=9)
    table.add_column("File", style="white", overflow="fold")
    table.add_column("Action", style="cyan", width=11)
    table.add_column("Status", justify="center", width=9)

    if not FILE_ACTIVITY:
        table.add_row("—", "No file activity yet", "—", "—")
    else:
        for f in FILE_ACTIVITY:
            badge = Text(" NEW ", style="bold white on blue") if f["status"] == "NEW" else Text(" EXISTS ", style="bold black on yellow")
            table.add_row(f["time"], f["name"], f["action"], badge)

    return Panel(table, title="[bold]📁 File Activity[/]", border_style="blue", box=ROUNDED, padding=(0, 1))

def render_logs():
    lines = list(LOG_VIEW)[-14:]
    body = Group(*lines) if lines else Text("Waiting for activity...", style="grey50")
    return Panel(body, title="[bold]📜 Live Log[/]", border_style="grey58", box=ROUNDED, padding=(0, 1))

def render_footer():
    cfg = Text(justify="center")
    cfg.append(f"Repo: {os.path.basename(REPO_DIR)}", style="grey58")
    cfg.append("   •   ", style="grey37")
    cfg.append(f"SFTP: {SFTP_HOST}:{SFTP_PORT}", style="grey58")
    cfg.append("   •   ", style="grey37")
    cfg.append(f"Interval: {INTERVAL_MINUTES}m", style="grey58")
    cfg.append("   •   ", style="grey37")
    cfg.append("Ctrl+C to stop", style="grey58")
    return Panel(Align.center(cfg), box=ROUNDED, border_style="grey37")

def build_layout() -> Layout:
    layout = Layout(name="root")

    layout.split(
        Layout(name="header", size=4),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )

    layout["body"].split_row(
        Layout(name="left", ratio=2, minimum_size=32),
        Layout(name="right", ratio=3, minimum_size=40),
    )

    layout["left"].split(
        Layout(name="pipeline", ratio=2, minimum_size=6),
        Layout(name="status", ratio=3, minimum_size=7),
        Layout(name="alerts", ratio=3, minimum_size=6),
    )

    layout["right"].split(
        Layout(name="stats", ratio=2, minimum_size=6),
        Layout(name="files", ratio=3, minimum_size=6),
        Layout(name="logs", ratio=4, minimum_size=6),
    )

    return layout

def refresh_layout(layout: Layout):
    layout["header"].update(render_header())
    layout["pipeline"].update(render_pipeline())
    layout["status"].update(render_status())
    layout["alerts"].update(render_alerts())
    layout["stats"].update(render_stats())
    layout["files"].update(render_file_activity())
    layout["logs"].update(render_logs())
    layout["footer"].update(render_footer())

# ==========================================
# SHARED PIPELINE STEPS (used by both run modes)
# ==========================================

def run_one_loop_body():
    """The actual work done every loop iteration. Called by both the
    interactive (Rich Live) runner and the headless (systemd) runner."""
    STATE["phase"] = "Git Pull"
    run_git_pull()

    # Config.json may have just changed via that pull (new SFTP
    # host/port/interval etc). Reload before using any of it.
    reload_runtime_config()

    STATE["phase"] = "Sync Creds"
    sync_credentials_from_pc()

    STATE["phase"] = "Command Check"
    op_status = check_operation_status()

    if op_status:
        STATE["phase"] = "Fetch Emails"
        fetch_zips_via_api()

        STATE["phase"] = "SFTP Upload"
        upload_new_zips()
    else:
        logger.info("[*] Operation is OFF. Skipping Email Fetch and SFTP copy.")
        STATE["files_downloaded"] = 0
        STATE["files_uploaded"] = 0

    STATE["phase"] = "Push Logs"
    trim_log_file(log_file_path, days_to_keep=5)
    push_logs_to_git(REPO_DIR, 'automation.log')


# ==========================================
# MAIN EXECUTION LOOP -- interactive (tmux/SSH terminal)
# ==========================================

def main_interactive():
    layout = build_layout()
    refresh_layout(layout)

    with Live(layout, console=console, screen=True, refresh_per_second=8) as live:
        logger.info(f"=== Started Bihar Flood Automation Dashboard (interval: {INTERVAL_MINUTES}m) ===")

        while True:
            try:
                STATE["loop_count"] += 1

                STATE["phase"] = "Git Pull"
                refresh_layout(layout)
                run_git_pull()
                refresh_layout(layout)

                reload_runtime_config()

                STATE["phase"] = "Sync Creds"
                refresh_layout(layout)
                sync_credentials_from_pc()
                refresh_layout(layout)

                STATE["phase"] = "Command Check"
                refresh_layout(layout)
                op_status = check_operation_status()
                refresh_layout(layout)

                if op_status:
                    STATE["phase"] = "Fetch Emails"
                    refresh_layout(layout)
                    fetch_zips_via_api()
                    refresh_layout(layout)

                    STATE["phase"] = "SFTP Upload"
                    refresh_layout(layout)
                    upload_new_zips()
                    refresh_layout(layout)
                else:
                    logger.info("[*] Operation is OFF. Skipping Email Fetch and SFTP copy.")
                    STATE["files_downloaded"] = 0
                    STATE["files_uploaded"] = 0

                STATE["phase"] = "Push Logs"
                refresh_layout(layout)
                trim_log_file(log_file_path, days_to_keep=5)
                push_logs_to_git(REPO_DIR, 'automation.log')
                refresh_layout(layout)

            except Exception as e:
                logger.error(f"[-] Unexpected error in loop: {e}")
                push_alert(f"Unexpected loop error: {e}", "critical")

            STATE["phase"] = "Sleeping"
            STATE["run_start_time"] = datetime.now()
            STATE["next_run_time"] = STATE["run_start_time"] + timedelta(minutes=INTERVAL_MINUTES)
            logger.info(f"Sleeping for {INTERVAL_MINUTES} minutes. Next check at {STATE['next_run_time'].strftime('%H:%M:%S')}...")

            sleep_until = STATE["next_run_time"]
            while datetime.now() < sleep_until:
                refresh_layout(layout)
                time.sleep(0.25)


# ==========================================
# MAIN EXECUTION LOOP -- headless (systemd, no terminal)
# ==========================================

_shutdown_requested = False

def _handle_sigterm(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Received shutdown signal, will stop after this sleep tick...")

def main_headless():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    logger.info(f"=== Started Bihar Flood Automation (headless/service mode, interval: {INTERVAL_MINUTES}m) ===")

    while not _shutdown_requested:
        try:
            STATE["loop_count"] += 1
            logger.info(f"--- Loop #{STATE['loop_count']} starting ---")
            run_one_loop_body()
            logger.info(f"--- Loop #{STATE['loop_count']} complete ---")
        except Exception as e:
            logger.error(f"[-] Unexpected error in loop: {e}")
            push_alert(f"Unexpected loop error: {e}", "critical")

        if _shutdown_requested:
            break

        STATE["run_start_time"] = datetime.now()
        STATE["next_run_time"] = STATE["run_start_time"] + timedelta(minutes=INTERVAL_MINUTES)
        logger.info(f"Sleeping for {INTERVAL_MINUTES} minutes. Next check at {STATE['next_run_time'].strftime('%H:%M:%S')}...")

        sleep_until = STATE["next_run_time"]
        while datetime.now() < sleep_until and not _shutdown_requested:
            time.sleep(2)

    logger.info("Shutting down cleanly.")


if __name__ == "__main__":
    try:
        if IS_INTERACTIVE:
            main_interactive()
        else:
            main_headless()
    except KeyboardInterrupt:
        if IS_INTERACTIVE:
            console.print("\n[bold yellow]Automation stopped by user.[/]")
        else:
            logger.info("Automation stopped by user.")