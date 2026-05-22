# Deploying hl-agent on a DigitalOcean droplet

A complete step-by-step for running the bot 24/7 on a $6/mo droplet behind
nginx + basic auth. Total setup time: **~30-40 minutes** if you've never
done this before.

The end state:
- Backend (FastAPI + scheduler + auto-TP/SL monitor) runs as a systemd
  service on the droplet, restarts on crash, survives reboots
- Nginx in front handles HTTP (and optionally HTTPS) with basic auth
- You access the dashboard from your laptop's browser pointing at the
  droplet's IP
- Local laptop only needs to be on when *you* want to look at the dashboard

---

## Part 1 — Create the droplet

1. Go to <https://cloud.digitalocean.com> → **Droplets → Create**.
2. **Choose an image:** Ubuntu 24.04 LTS x64.
3. **Choose a size:** Basic → Regular SSD → **$6/mo (1 GB / 1 vCPU)**. The
   $4 / 512 MB tier is too tight (see RAM math in README).
4. **Choose a region:** pick one closest to where you live (latency to
   Hyperliquid's API is similar from most regions; matters more for your
   own dashboard latency).
5. **Authentication:** select **SSH Key** and add your local public key.
   If you don't have one yet, on macOS:
   ```sh
   ssh-keygen -t ed25519 -C "you@example.com"
   cat ~/.ssh/id_ed25519.pub      # copy this into DigitalOcean
   ```
6. **Hostname:** `hl-agent` (anything; just for your reference).
7. Click **Create Droplet.** Wait ~30 seconds for the IPv4 to appear.
   Note the IP — call it `<DROPLET_IP>` below.

---

## Part 2 — Initial server setup (one-time)

SSH in as root:

```sh
ssh root@<DROPLET_IP>
```

### 2.1 — Update + install system deps

```sh
apt update && apt upgrade -y
apt install -y python3.12 python3.12-venv python3-pip git nginx \
                apache2-utils ufw fail2ban
```

### 2.2 — Create a non-root user (don't run the bot as root)

```sh
adduser --disabled-password --gecos "" hlagent
usermod -aG sudo hlagent
mkdir -p /home/hlagent/.ssh
cp /root/.ssh/authorized_keys /home/hlagent/.ssh/
chown -R hlagent:hlagent /home/hlagent/.ssh
chmod 700 /home/hlagent/.ssh
chmod 600 /home/hlagent/.ssh/authorized_keys
```

### 2.3 — Add a 1 GB swap file (insurance against OOM)

```sh
fallocate -l 1G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
swapon --show          # confirm it's active
```

### 2.4 — Configure UFW firewall

```sh
ufw allow OpenSSH
ufw allow 'Nginx Full'   # allows 80 and 443
ufw --force enable
ufw status               # should show 22, 80, 443 allowed
```

### 2.5 — Disable root SSH login (recommended)

```sh
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh
```

Log out and test that you can SSH in as the new user:

```sh
exit
ssh hlagent@<DROPLET_IP>
```

If that works, proceed. (If not, debug before locking yourself out.)

---

## Part 3 — Install the bot

All remaining commands run **as the `hlagent` user**.

### 3.1 — Clone the repo

```sh
cd ~
git clone <YOUR-REPO-URL> hyperliquid    # or scp the directory up
cd hyperliquid
```

If you don't have a remote yet, from your **laptop**:

```sh
cd /Users/josealvarosacramento/work/hyperliquid
rsync -avz --exclude='.venv' --exclude='data' --exclude='web/node_modules' \
      --exclude='web/.next' --exclude='__pycache__' --exclude='*.pyc' \
      . hlagent@<DROPLET_IP>:~/hyperliquid/
```

### 3.2 — Python venv + install

```sh
cd ~/hyperliquid
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
deactivate
```

### 3.3 — Configure `.env`

**Do NOT commit secrets.** Create the file on the droplet directly:

```sh
nano ~/hyperliquid/.env
```

Paste your **mainnet** values:

```
HL_AGENT_PRIVATE_KEY=0x...        # mainnet agent key
HL_ACCOUNT_ADDRESS=0x...          # your mainnet wallet address
ANTHROPIC_API_KEY=sk-ant-...
```

Save (`Ctrl-O`, `Enter`, `Ctrl-X`) and lock down permissions:

```sh
chmod 600 ~/hyperliquid/.env
```

### 3.4 — Confirm `config.yaml`

```sh
cat ~/hyperliquid/config.yaml | grep -E 'network|model|assets|leverage'
```

Verify `network: mainnet`, the model/assets look right, and your safer-mainnet
risk caps are in place. Edit with `nano` if needed.

### 3.5 — Make sure `data/` directory exists

```sh
mkdir -p ~/hyperliquid/data
```

### 3.6 — Sanity-check that the bot can start

```sh
cd ~/hyperliquid
source .venv/bin/activate
python -m hl_agent.server     # ← Ctrl-C after you see the startup logs
```

You should see:
```
position leverage set to 5x (cross=True): {'BTC': 'ok', 'ETH': 'ok'}
server scheduler started: every 15 min on mainnet, ...
auto take-profit: every 60s at +1.50% gain ...
auto stop-loss:   every 60s at -1.50% loss ...
Uvicorn running on http://127.0.0.1:8000
```

If you do, **Ctrl-C** to stop, deactivate the venv (`deactivate`), and move on.

---

## Part 4 — Install the systemd service

This makes the bot run as a managed service: auto-restart on crash, auto-start
on reboot, integrated logging via `journalctl`.

```sh
sudo cp ~/hyperliquid/deploy/hl-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hl-agent      # auto-start on boot
sudo systemctl start hl-agent
sudo systemctl status hl-agent      # confirm it's running ("active (running)")
```

View logs live:

```sh
sudo journalctl -u hl-agent -f
```

You should see the same startup output as before, then cycles firing.
Press `Ctrl-C` to stop following (the service keeps running).

---

## Part 5 — Install nginx with basic auth

The API has no built-in auth — anyone who finds the droplet's IP could
pause your bot or change risk limits. Nginx + basic auth fixes that.

### 5.1 — Create basic-auth credentials

Pick a username (e.g., `admin`) and a strong password:

```sh
sudo htpasswd -c /etc/nginx/.htpasswd admin
# enter password twice when prompted
```

### 5.2 — Install the site config

```sh
sudo cp ~/hyperliquid/deploy/nginx-hl-agent.conf /etc/nginx/sites-available/hl-agent

# Replace the placeholder hostname with your droplet's IP (or domain)
sudo sed -i "s/your-droplet-ip-or-domain/<DROPLET_IP>/" /etc/nginx/sites-available/hl-agent

sudo ln -s /etc/nginx/sites-available/hl-agent /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default     # remove the default placeholder

sudo nginx -t                                # syntax check
sudo systemctl reload nginx
```

### 5.3 — Verify

From your **laptop**:

```sh
curl http://<DROPLET_IP>/api/health
# Should return JSON with network=mainnet, build hash, etc.

curl http://<DROPLET_IP>/api/account
# Should return 401 Unauthorized (auth required)

curl -u admin:<your-password> http://<DROPLET_IP>/api/account
# Should return JSON with equity, positions, etc.
```

---

## Part 6 — Optional: HTTPS via Let's Encrypt

Only needed if you've pointed a real domain at the droplet. If you're using
the bare IP address, skip this part.

```sh
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
# Follow the prompts. Certbot will edit your nginx config to enable HTTPS
# and set up automatic renewal.
```

---

## Part 7 — Connect your local dashboard

Back on your **laptop**, point Next.js at the droplet's API.

```sh
cd /Users/josealvarosacramento/work/hyperliquid/web
echo "NEXT_PUBLIC_API_URL=http://<DROPLET_IP>" > .env.local
npm run dev
```

Open <http://localhost:3000>. The dashboard now reads from the droplet.

You'll be prompted for the basic-auth credentials when the dashboard makes
its first API request. Save them in your password manager.

**Note:** basic auth + HTTP means your credentials travel in cleartext. For
real security, set up HTTPS (Part 6) with a domain.

---

## Operations cheat sheet

All from the droplet (ssh in as `hlagent`):

```sh
# Status
sudo systemctl status hl-agent
sudo journalctl -u hl-agent -n 100         # last 100 log lines
sudo journalctl -u hl-agent -f             # follow in real time

# Restart (e.g., after editing code or .env)
sudo systemctl restart hl-agent

# Update code from your laptop:
#   on laptop: rsync up new files (same command as 3.1)
#   on droplet: sudo systemctl restart hl-agent

# Inspect SQLite directly
sqlite3 ~/hyperliquid/data/hl_agent.db
> SELECT COUNT(*) FROM decisions;
> .quit

# Edit config.yaml and apply
nano ~/hyperliquid/config.yaml
sudo systemctl restart hl-agent

# Disable the bot temporarily (without uninstalling)
sudo systemctl stop hl-agent
# ... resume with `sudo systemctl start hl-agent`
```

---

## When something goes wrong

| Symptom | Check |
|---|---|
| `sudo systemctl status hl-agent` shows "failed" | `sudo journalctl -u hl-agent -n 50` for the traceback |
| Bot starts but no cycles | Check `/api/health` last_error; check `.env` has valid keys; check `journalctl` for "BadRequestError" (Anthropic credits) |
| `502 Bad Gateway` from nginx | The Python process isn't running on `127.0.0.1:8000` — restart hl-agent |
| `OOM killed` in `dmesg` | RAM exhausted — increase swap or upgrade to 2 GB droplet |
| Dashboard shows "Network Error" | Check that you've added basic-auth creds; check CORS in `nginx-hl-agent.conf`; verify droplet firewall allows port 80 |
| Slow `/api/account` responses | Hyperliquid API latency — usually transient. Check their status page if persistent. |

---

## Total monthly cost (steady state)

| Item | $/mo |
|---|---|
| DigitalOcean droplet (1 GB) | $6 |
| Anthropic API (Sonnet 4.6, current usage) | ~$70 (full month at $2.40/day) |
| Hyperliquid trading fees | varies with activity |
| **Total** | **~$76 + fees** |

If the bot is reliably positive on PnL, this is rounding error. If not, the
ongoing fixed cost is your real exposure.
