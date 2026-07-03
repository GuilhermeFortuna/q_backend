# MT5 Wine data gateway — operator guide (WO186)

Run the MetaTrader 5 terminal + the WO183 HTTP data gateway under **Wine** on Linux so
the backend serves same-day-fresh B3 futures (`WIN$` / `WDO$`) while you work on Linux —
without touching live execution. Background/design: `q_frontend/docs/design/mt5-remote-gateway.md`.

The gateway is **read-only market data only**. Live trading stays native-MT5-on-Windows.

## Pinned versions

Everything is pinned; upgrades are a deliberate operator action (edit the pin, re-run
`setup_wine.sh`). Do not auto-update.

| Component            | Pin / source                                                                 |
| -------------------- | ---------------------------------------------------------------------------- |
| Wine                 | **whatever is installed at first setup** — recorded in the prefix marker and change-checked on every run (see risk mitigation) |
| Windows Python       | **3.11.9** (`setup_wine.sh` `PYTHON_VERSION`)                                 |
| `MetaTrader5` (pip)  | **5.0.5735** (`setup_wine.sh` `MT5_PIP_VERSION`)                              |
| MT5 terminal build   | provided by the MetaQuotes/broker installer; recorded after first login (`/v1/health` → `terminal_build`) |

## Prerequisites

- `wine` (64-bit) installed system-wide. Fedora: `sudo dnf install wine`.
- `winetricks` is **not** required for the read-only gateway; install it only if the
  terminal complains about a missing component (rare).
- `curl` or `wget`, `sha256sum`, `awk` (all standard).
- Disk: budget **~3–4 GB** for the prefix (Windows Python + MT5 terminal + history cache).
- A broker account with MT5 access (credentials entered by hand, once — see below).

## One-time setup

```bash
cd q_backend
./gateway/setup_wine.sh
```

The script is idempotent (safe to re-run) and:

1. Creates a dedicated 64-bit Wine prefix at `~/.local/share/mt5-gateway-prefix`
   (override with `MT5_GATEWAY_PREFIX`). It records the current Wine version in a marker
   file and **warns loudly on later runs if the system Wine changed** — a different Wine
   can break the terminal or the `MetaTrader5` module, so the prefix is pinned on purpose.
2. Downloads + installs Windows Python `3.11.9` into the prefix (`C:\Python311`), printing
   each installer's SHA-256 so you can verify it against the official source.
3. `pip install MetaTrader5==5.0.5735` inside that Wine Python (numpy comes along as a
   dependency — nothing else is needed).
4. Installs the MetaTrader 5 terminal into the prefix.
5. Prints the launch commands and where to log in.

The script **never** stores, prompts for, or logs broker credentials.

### First login (manual, once)

Start the terminal and log in to your broker account in its GUI:

```bash
WINEPREFIX="$HOME/.local/share/mt5-gateway-prefix" \
  wine "$HOME/.local/share/mt5-gateway-prefix/drive_c/Program Files/MetaTrader 5/terminal64.exe"
```

Log in, confirm `WIN$` / `WDO$` appear in Market Watch, and leave the terminal running.
The login persists inside the prefix; you won't need to repeat it unless you reset the prefix.

## Running the gateway

### Manually (foreground, for a first smoke test)

```bash
WINEPREFIX="$HOME/.local/share/mt5-gateway-prefix" \
  wine "C:\Python311\python.exe" \
  "$(WINEPREFIX="$HOME/.local/share/mt5-gateway-prefix" winepath -w "$PWD/gateway/mt5_gateway.py")" \
  --host 127.0.0.1 --port 18812
```

Then, from another shell:

```bash
curl -s http://127.0.0.1:18812/v1/health
# {"status": "ok", "schema_version": "1.0", "mt5_connected": true, "terminal_build": <N>}
```

### On boot (systemd user units — the production trigger)

The systemd **user** units in `gateway/systemd/` are the production trigger for the
gateway. Enabling `mt5-gateway.service` is what puts it live.

```bash
mkdir -p ~/.config/systemd/user ~/.config/mt5-gateway
cp gateway/systemd/mt5-terminal.service ~/.config/systemd/user/
cp gateway/systemd/mt5-gateway.service  ~/.config/systemd/user/
cp gateway/systemd/mt5-gateway.env.example ~/.config/mt5-gateway/mt5-gateway.env
$EDITOR ~/.config/mt5-gateway/mt5-gateway.env    # set Q_BACKEND_DIR, prefix, python path

systemctl --user daemon-reload
systemctl --user enable --now mt5-gateway.service   # Requires= pulls in the terminal too
loginctl enable-linger "$USER"                      # start on boot without an active login
```

`mt5-gateway.service` is ordered `After=mt5-terminal.service` with a health-wait loop and
`Restart=on-failure`. Check it with:

```bash
systemctl --user status mt5-gateway.service
journalctl --user -u mt5-gateway.service -f
```

## Backend configuration

Point the Linux backend at the gateway (see also `README.md` env table):

```bash
# .env (backend)
Q_MT5_GATEWAY_URL=http://127.0.0.1:18812
Q_MT5_GATEWAY_TOKEN=            # only if the gateway was started with --token / MT5_GATEWAY_TOKEN
```

Leave `data_source=auto` (native MT5 → **remote gateway** → local parquet) or set it to
`remote` to force the gateway. On `auto`, reads that go through the gateway are
**fetch-through cached** into the local parquet store, so once ingested they survive the
gateway going offline.

## Fresh-boot recap (the whole thing)

```bash
# 1. terminal + gateway come up via systemd (after enable --now + linger, this is automatic):
systemctl --user start mt5-gateway.service
# 2. verify:
curl -s http://127.0.0.1:18812/v1/health
# 3. backend on data_source=auto now serves fresh WIN$/WDO$ bars via the gateway.
```

## Troubleshooting

| Symptom                                              | Likely cause / fix                                                                                                                                                 |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Terminal won't start under Wine                     | Check `wine --version` matches the recorded pin (`~/.local/share/mt5-gateway-prefix/.gateway-setup-marker`). Try `WINEDEBUG=+err wine .../terminal64.exe`. If Wine changed, reset the prefix and re-run `setup_wine.sh`. As a fallback, run the gateway on a Windows box (below). |
| `MetaTrader5` fails to import in the Wine Python     | Version/ABI mismatch. Confirm the pinned wheel matches the Wine Python: `wine "C:\Python311\python.exe" -m pip show MetaTrader5`. Re-run `setup_wine.sh` to reinstall the pinned version; if a Wine bump broke it, revert Wine or move to a Windows VM. |
| `/v1/health` shows `"mt5_connected": false`          | The terminal isn't running or isn't logged in. Start `mt5-terminal.service` (or the terminal GUI), log in, confirm symbols in Market Watch. The gateway retries MT5 init lazily, so health flips to `true` once the terminal is up. |
| Schema-version mismatch after a repo update          | The client refuses a gateway whose `schema_version` major differs (it treats it as unavailable and degrades to `local`). Redeploy the updated `gateway/mt5_gateway.py` to the prefix/box and restart `mt5-gateway.service` so both sides speak the same `/vN/`. |
| `curl` to the port hangs / connection refused        | Gateway not running or wrong port. `systemctl --user status mt5-gateway.service`; check `MT5_GATEWAY_PORT`. Confirm nothing else owns 18812. |

## Fallback: run the gateway on a Windows box

If Wine proves flaky, the **identical** `gateway/mt5_gateway.py` runs on a Windows VM,
spare machine, or VPS with a native MT5 terminal + Python — **zero code changes**:

```bat
python mt5_gateway.py --host 0.0.0.0 --port 18812 --token <STRONG_SECRET>
```

Then point the Linux backend at it:

```bash
Q_MT5_GATEWAY_URL=http://<box-ip>:18812
Q_MT5_GATEWAY_TOKEN=<STRONG_SECRET>
```

Security posture (read before binding off-localhost): the gateway speaks **plain HTTP**.
The default bind is `127.0.0.1`. Only bind to `0.0.0.0`/a LAN IP together with the
`--token` shared secret, and only over a trusted **LAN or VPN** — **never the open
internet**. There is no TLS (out of scope for now).

## Manual end-to-end checklist (the real verification)

CI cannot exercise Wine, so this is executed **once on the target machine**. Paste each
step's observed output when reporting.

- [ ] **Health OK.** `curl -s http://127.0.0.1:18812/v1/health` →
      `{"status":"ok","schema_version":"1.0","mt5_connected":true,"terminal_build":<N>}`.
- [ ] **Ingest on Linux.** In the Storage page, ingest `WIN$` `M5` for the last week; the
      job completes and rows land in the local parquet store (served via the gateway's
      acquisition path, WO185).
- [ ] **Backtest incl. yesterday.** Run a Backtests Simulation over a range that includes
      yesterday; it uses the just-fetched fresh bars and completes without errors.
- [ ] **Degrade cleanly.** Stop the gateway (`systemctl --user stop mt5-gateway.service`);
      the backend on `auto` degrades to `local` with **no errors**, and the bars just
      ingested are still served from parquet.

## Bumping a pin

1. Edit `PYTHON_VERSION` and/or `MT5_PIP_VERSION` in `gateway/setup_wine.sh`
   (confirm a matching `win_amd64` wheel exists on
   [PyPI](https://pypi.org/project/MetaTrader5/#history) for the Python `cpXY` tag).
2. For a Wine bump: reset the prefix (`rm -rf ~/.local/share/mt5-gateway-prefix`) so the
   pin marker is rewritten, then re-run `setup_wine.sh`.
3. Re-run the E2E checklist above.
