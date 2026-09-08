# Bihar Flood Automation — full setup (PC + 192.168.2.54 as a service)

## The picture

```
YOUR PC                          192.168.2.54 (compute server)         192.168.2.137
--------                         -----------------------------          -------------
credentials_http_server.py  --pull-->  dash.py (systemd service)  --SFTP-->  zip storage
  - keeps token.json valid           - git pull (code+config)
  - serves it over HTTP              - pulls creds from PC
  - protected by a shared token      - fetches Gmail, downloads zips
                                      - SFTP-uploads zips to .137
                                      - pushes automation.log to git
```

- **Code + config.json + logs** → git (public repo), synced automatically like before.
- **Secrets** (`token.json`, `credentials.json`, passwords) → never in git. The
  compute server *pulls* them from your PC over HTTP every loop.
- **dash.py** on `.54` runs as a **systemd service** — starts on boot, restarts
  itself if it crashes, and you view it with `journalctl` instead of watching
  it live (there's no terminal for a service to draw into).

---

## Part 1 — One-time setup on your PC

**1. Put these files next to your existing `dash.py`, `credentials.json` in the repo folder:**
`config_loader.py`, `config.json`, `secrets.local.json.example`, `credentials_http_server.py`, `dash.py` (replace the old one), `bihar-flood-dashboard.service`, `gitignore_additions.txt`

**2. Your PC's LAN IP is `192.168.2.202`** (already reachable from `192.168.2.54` on the same subnet). If it ever changes, re-check with `ip addr show | grep "inet 192.168"` (Linux/macOS) or `ipconfig` (Windows) and update `config.json` accordingly.

**3. `config.json`** already has `pc_credentials_server.host` set to `192.168.2.202` — just double-check it matches:
```json
{
  "sftp_upload": {
    "host": "192.168.2.137",
    "port": 22,
    "user": "sac",
    "remote_dir": "/home/sac/Documents/new_bihar/bihar_flood_Zip/"
  },
  "pc_credentials_server": {
    "host": "192.168.2.202",
    "port": 8765
  },
  "interval_minutes": 15
}
```
If your PC's IP changes often (DHCP), either set a static/reserved IP for it in
your router, or use a local hostname if your network supports it.

**4. Create your secrets file:**
```bash
cp secrets.local.json.example secrets.local.json
```
Edit it:
```json
{
  "sftp_upload_password": "sac@123",
  "pc_credentials_auth_token": "paste-a-long-random-string-here"
}
```
Generate a random token (don't reuse a real password):
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

**5. Update `.gitignore` and push the non-secret files:**
```bash
cd /path/to/your/repo
cat gitignore_additions.txt >> .gitignore
git add .gitignore config.json config_loader.py dash.py credentials_http_server.py bihar-flood-dashboard.service
git status   # confirm secrets.local.json / token.json / credentials.json are NOT listed
git commit -m "Move dash.py to server as a service; pull creds from PC over HTTP"
git push
```

**6. Open the port on your PC's firewall** (Linux example, adjust for your OS/firewall):
```bash
sudo ufw allow 8765/tcp
```

**7. Start the credentials server:**
```bash
python3 credentials_http_server.py
```
First run: a browser opens, log into the Gmail account, done. It then prints
something like:
```
Serving token.json / credentials.json on 0.0.0.0:8765
Keep this running so 192.168.2.54 can pull fresh credentials.
```
Leave this running (or restart it whenever you expect the server may need a
credential refresh — it's fine for it to not be running all the time; see
Part 4).

---

## Part 2 — One-time setup on 192.168.2.54

**8. SSH in and clone the repo:**
```bash
ssh sac@192.168.2.54
cd /home/sac/Documents/python_script/
git clone <your-repo-url> bihar_flood_automate
cd bihar_flood_automate
```

**9. Install dependencies:**
```bash
python3 -m pip install gitpython paramiko google-auth google-auth-oauthlib google-api-python-client rich
```
(If `python3 -m pip` isn't available, use `pip3 install ...` instead.)

**10. Create the secrets file here too** (same SFTP password, same auth token as the PC):
```bash
cp secrets.local.json.example secrets.local.json
nano secrets.local.json
```
```json
{
  "sftp_upload_password": "sac@123",
  "pc_credentials_auth_token": "the-exact-same-random-string-from-step-4"
}
```

**11. Create `operation.txt` if it isn't already tracked in git:**
```bash
echo "on" > operation.txt
```

**12. Test it once manually before making it a service:**
```bash
python3 dash.py
```
Since this is an SSH session (a terminal), you'll see the full live dashboard.
Check that "PC Sync" shows `synced` and Gmail/SFTP look healthy, then `Ctrl+C`
to stop it.

**13. Install the systemd service:**
```bash
sudo cp bihar-flood-dashboard.service /etc/systemd/system/
sudo nano /etc/systemd/system/bihar-flood-dashboard.service
```
Check/adjust these two lines match your real setup:
```
WorkingDirectory=/home/sac/Documents/python_script/bihar_flood_automate
ExecStart=/usr/bin/python3 /home/sac/Documents/python_script/bihar_flood_automate/dash.py
```
Confirm the python3 path:
```bash
which python3
```
If it's different from `/usr/bin/python3`, update `ExecStart` accordingly.

**14. Enable and start it:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable bihar-flood-dashboard
sudo systemctl start bihar-flood-dashboard
```

**15. Check it's running:**
```bash
sudo systemctl status bihar-flood-dashboard
```
You should see `active (running)`.

**16. Watch live logs (this replaces the visual dashboard under systemd):**
```bash
journalctl -u bihar-flood-dashboard -f
```
You'll see the same log lines the Rich dashboard's log panel would have shown
(git pull results, PC sync status, Gmail/SFTP activity), just as plain text.
`automation.log` in the repo folder (and pushed to git) has the same history.

---

## Part 3 — Verify the whole chain end to end

1. On the PC, confirm `credentials_http_server.py` is running and shows no errors.
2. On `.54`, `journalctl -u bihar-flood-dashboard -f` — within one loop you
   should see a line indicating credentials were pulled/synced, then Gmail
   search, then SFTP upload to `.137`.
3. Check `.137` for the uploaded zip(s) landing in the expected folder.
4. Edit `config.json` on the PC (e.g. bump `interval_minutes`), `git push`.
   Within one loop cycle, `.54`'s service should be using the new interval —
   confirm via the journal logs, no restart needed.

---

## Day-to-day operation (no more logging into `.54` for routine changes)

- **Change SFTP host/port/user/remote dir for `.137`, or the loop interval**
  → edit `config.json` on your PC, `git push`. Picked up automatically.
- **Gmail token expires or gets revoked** → the service logs a
  "needs re-auth on PC" alert. On your PC, make sure
  `credentials_http_server.py` is running (start it if it isn't) — the next
  loop on `.54` will pull the fresh token automatically. If the browser login
  didn't happen yet, `credentials_http_server.py` will pop it open on your PC.
- **SFTP password to `.137` or the shared auth token changes** → update
  `secrets.local.json` by hand on both machines once. These aren't
  auto-synced by design, since committing them anywhere (even as an
  encrypted blob) adds risk for something that changes rarely.
- **Restart the service after a manual change:**
  ```bash
  sudo systemctl restart bihar-flood-dashboard
  ```
- **Stop/start it:**
  ```bash
  sudo systemctl stop bihar-flood-dashboard
  sudo systemctl start bihar-flood-dashboard
  ```
- **View recent logs any time:**
  ```bash
  journalctl -u bihar-flood-dashboard -n 200 --no-pager
  ```

## Why some design choices were made

- **The Rich live dashboard only renders when there's an actual terminal.**
  `dash.py` checks this automatically (`sys.stdout.isatty()`). Under systemd
  there's no terminal, so it falls back to plain log lines instead — trying
  to force the full-screen UI into a service's log stream would either error
  out or flood `journalctl` with garbage many times a second.
- **The compute server never opens a browser for Gmail login** —
  `ALLOW_INTERACTIVE_AUTH = False` is hardcoded in `dash.py`. Only
  `credentials_http_server.py`, run on your PC where a browser exists, does
  that.
- **The HTTP credential pull is protected by a bearer token**, not left open,
  since it's serving a live Gmail credential over your LAN.
