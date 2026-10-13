# agent-channel

A local channel **for agents**: it lets an agent call the APIs of major overseas AI models; wiring the browser in is an optional extra.

It is built on the **sing-box** kernel. It opens a proxy port on `127.0.0.1:20849` locally, and can optionally point the Windows system proxy at it.

**This tool does not provide, bundle or recommend any nodes or subscriptions.** You need to bring your own working node links or subscription URL.

- A single sing-box 1.14.3 kernel, zero local compilation (the official prebuilt binaries are used)
- Python 3.10+, **zero runtime dependencies** (standard library only)
- Two shells sharing one kernel: a CLI (for agents and scripts) and a local web panel (for humans)
- Zero privileges: it only writes the HKCU system proxy — no drivers, no routing table changes
- All three ports listen on `127.0.0.1` only: proxy `20849`, panel `18888`

> **What it is** : a **connector**. It does exactly four things — work over loopback, be startable by an agent in one call, install the official prebuilt kernel, and verify integrity. Whatever sing-box or an existing client already does, it does not redo.

---

## 1. What it does

**It wires the channel to your agent, and to your browser.**

- **For agents**: a local HTTP API plus a CLI. An agent or script can query status, start/stop the channel, benchmark, switch the outbound, import nodes and probe the landing location — all locally, with no node configuration ever entering the conversation.
- **For humans**: a panel at `http://127.0.0.1:18888` — node sources, outbound selection, benchmarking, kernel updates and the system-proxy switch all live on one page.
- **Verifiable landing**: while the channel runs, the panel probes geolocation **through the channel's real exit**, using city and weather to indirectly verify where the exit IP physically is (see §5).

### How it plugs in, and what it does not cover

The channel opens a mixed port (socks5 + http) on `127.0.0.1:20849` and can optionally point the system proxy at it. It **does not do TUN or transparent proxying and never takes over traffic globally**, and it listens on loopback only — no sharing with other machines on your LAN.

So **programs that ignore the system proxy** (many CLIs, Node / Python toolchains, runtimes that manage their own networking) have to be pointed at it explicitly:

```bash
export HTTP_PROXY=http://127.0.0.1:20849
export HTTPS_PROXY=http://127.0.0.1:20849
```

Two limits worth knowing up front: an HTTP proxy only carries TCP, so QUIC / HTTP3 traffic will not go through this channel; and if the program is force-killed or the machine loses power, the next launch will close the stale system proxy, but it will **not restore your previous proxy settings** (the original values only ever lived in the process memory).

## 2. Protocol support

Protocol stack covered by the kernel:

- **With a common URI form**: `vless` (including Reality), `vmess`, `trojan`, `shadowsocks`, `hysteria2`, `tuic`
- **JSON only**: `naive`, `ssh`, `anytls`, `shadowtls`, `hysteria` (v1) — these have **no common URI form**, and the tool does not invent a private one for them; they are expressed as sing-box JSON

Nodes can be loaded in three shapes, all handled by the parser:

| Shape | Entry point | Notes |
| :--- | :--- | :--- |
| **Single node link** | Panel "single node" / `set-node` | `vless://` `vmess://` `trojan://` `ss://` `hysteria2://` `tuic://` |
| **Subscription URL** | Panel "subscription" / `set-sub` | Fetched, then nodes are extracted |
| **sing-box JSON** | Panel "import JSON" | Bare single outbound, `{"outbounds":[…]}`, or a naiveproxy client config |

Two things stated plainly:

- **Import is not enable**: importing only fills the `json` slot's candidate pool; it **does not change the current source**. To actually use those nodes, select a source explicitly.
- **`naive` and its companion library**: the official Windows and Linux kernels use the purego build, which needs `libcronet.dll` / `libcronet.so` sitting next to it at runtime — this tool unpacks them from the official archive along with the kernel (without it, the kernel fails outright while initializing a naive outbound). **macOS has no such file and `naive` still works there**: the official darwin kernel links cronet statically into the binary with CGO, which is why its archive contains only `sing-box` and `LICENSE`. That is a difference in linking, not a missing feature.

## 3. Sources and outbound selection: three slots, kept independent

**The three source slots (`node` / `sub` / `json`) are independent: candidates are assembled only inside their own slot, and never pooled across slots.**

- **`active_source`**: the currently active source, `node`/`sub`/`json` (empty string = nothing configured yet). **You select it explicitly; it is no longer inferred from the input shape.**
- **`outbound_selections`**: each slot keeps its own outbound choice — `{"mode": "" | "auto" | "pinned", "node_key": …}`. The three slots never interfere, and switching back and forth does not force a re-selection.
- **`node_key`**: the node key when `pinned`, e.g. `vless|203.0.113.10|443` (`type|server|port`). The panel and the backend share one generator for it.

Semantics that matter:

- **Unselected by default**: with two or more nodes in a slot, you must choose explicitly before enabling. **The program does not decide your exit IP for you.** Enabling while unselected returns a clear "please select an outbound first".
- **Exactly one node** (a single node link, or a JSON file with one node): the panel shows a read-only "will use: <node>" and does not offer "auto". One element leaves nothing to optimise.
- **Two or more nodes**: the choice expands to "**auto / pin a node**".
  - **Pin a node**: the exit IP is fixed to the one you picked. If it is slow or dead, **it will not silently switch to another** — it fails and says so.
  - **Auto**: inside that slot only, nodes are ranked by latency and each is given a real forwarding check; the first one that works is used. The exit IP can change.
- **A pinned node disappearing** (removed from the subscription or the JSON) returns "the selected node is no longer valid" — **it does not quietly pick another**.
- **Enable follows the request**: enabling resolves the source and address given in the request; if the request names no source it is **rejected outright** rather than falling back to whatever `config.json` already had (persisting is a record of the result, not the source of truth).

## 4. Benchmarking and landing detection

Benchmarking measures only **the slot you are currently viewing**. For each node, in order:

1. **TCP handshake latency**: UDP-only protocols (`hysteria2`/`tuic`) have no TCP listener and are skipped;
2. **Real forwarding check**: a temporary kernel makes an actual request to an overseas probe address to confirm data can round-trip — **a reachable port is not enough**. Some nodes accept a connection and then fail the handshake; latency alone will fool you.
3. **Landing IP probe**: once forwarding passes, the node's **real landing IP** is read back through the temporary kernel;
4. **Relay detection**: if the landing IP differs from the IP the node's declared server resolves to, it is marked "relay".

Example output (illustrative data):

```text
Node A  [vless 203.0.113.10:443]  TCP 173ms  forwarding OK  landing 203.0.113.10
Node B  [vmess 198.51.100.7:443]  TCP 85ms   forwarding OK  landing 192.0.2.5 relay
```

A node marked "relay" is tunnelled or CDN-fronted — **it can benchmark fast while landing somewhere else**. You can see that at a glance instead of finding out by connecting.

Latency is cached in-process (it is lost on restart). Every entry in the outbound dropdown shows its latency directly (`—` when unmeasured), and the "auto" entry shows "fastest in this batch: <node> X ms".

## 5. Landing location: city and weather as evidence of the exit IP

While the channel runs, the panel probes geolocation once **through the channel's real exit**:

```text
Landing: Tokyo · Japan (example ISP) | 22°C overcast
```

The probe is sent through the node's exit, so the geolocation service sees the **node's landing IP**, not your machine's public IP. City and ISP come from IP geolocation; the weather comes from Open-Meteo. **City plus weather is therefore indirect evidence of where the exit IP physically is** — the tool bundles no IP-geolocation database and never looks up your local location.

## 6. Getting started

### Windows: Installer (recommended)

> Installers and zip builds are downloaded from the repository [Releases](https://github.com/gambn/agent-channel/releases) page; if that release ships no binaries, build locally per §10 (`python packaging/build.py`, the kernel manifest is pinned to v1.14.3).

1. Double-click `agent-channel-0.1.1-setup.exe`;
2. Walk through the wizard (install directory, desktop shortcut, start-with-Windows are optional);
3. It launches when installation finishes; a tray icon appears and the browser opens the panel at `http://127.0.0.1:18888` (default port; if it is taken the program picks another one).

Installation **needs no administrator rights** and raises no UAC prompt.

### Windows: portable zip / single-file build

- **zip**: unpack anywhere (avoid special characters in the path) and run `agent-channel.exe`. The kernel is already bundled, so it works offline.
- **single file** (~10 MB): **no kernel included**; the first run downloads the official archive (~33 MB, ~91 MB unpacked). The UI may look unresponsive while that happens; just let it finish. **This form is not on the Releases page** — a single file of the same name differs per platform, so Releases only carries the per-platform packages; build one yourself with `--no-core` (see §10).

### macOS / Linux

Two paths — **A is recommended** (no Python-version or PATH pitfalls):

**A. Native packages** (download from the repository [Releases](https://github.com/gambn/agent-channel/releases) page; if that release ships no binaries, use B)

- **Linux `.deb` (x86_64)**: `sudo apt install ./agent-channel_0.1.1_amd64.deb`
- **Universal tar.gz (Linux / macOS)**: unpack it and run `./install.sh` — installs to `~/.local/opt/agent-channel`, symlinks the command into `~/.local/bin`, **no root needed**; `./install.sh --uninstall` removes it (your `~/.agent_channel` data is kept)
- **macOS `.dmg` (one for x64, one for arm64)**: open it and drag `agent-channel.app` into Applications
- All three ship the official prebuilt sing-box kernel: ready to use, no system Python required

> **glibc requirement on Linux**: both the `.deb` and the tar.gz need **glibc ≥ 2.31** (Ubuntu 20.04+, Debian 11+, RHEL 9+). The bundled interpreter and kernel are built in a Debian 11 environment, so older distributions fail at startup with `GLIBC_2.xx not found`; the `.deb` declares the same floor as `libc6 (>= 2.31)`.

> **macOS system requirement**: the bundle declares `LSMinimumSystemVersion` **11.0** (this release was tested on 12.7.6; 11 was not tested); the `naive` outbound additionally needs **macOS 12 or later** — that is cronet's official floor, and below 12 only that one protocol is unavailable.

> **First launch on macOS**: the bundle is ad-hoc signed and not notarized by Apple, so a copy
> downloaded through a browser will be blocked by Gatekeeper the first time ("unidentified
> developer" or "damaged"). Either path works: the **tar.gz** route strips the quarantine flag
> in `install.sh`, so it opens right away; for the **dmg** route, open the app once and use
> "Open Anyway" under System Settings → Privacy & Security (macOS 15 removed the right-click
> shortcut), or run `xattr -dr com.apple.quarantine /Applications/agent-channel.app`.
> After that it opens normally.

**B. Install from source (developer path)**

Requires **Python 3.10+**: macOS's built-in `/usr/bin/python3` is 3.9 and **does not qualify** — pick an explicit version (e.g. `python3.12`).

```bash
# 1) get the source
git clone https://github.com/gambn/agent-channel.git && cd agent-channel

# 2) create a virtualenv and install (replace python3.12 with any interpreter ≥3.10 on this machine; macOS's bundled python3 is 3.9 and won't do)
python3.12 -m venv .venv
.venv/bin/pip install .

# 3) install the official prebuilt kernel (picks the asset for this platform)
.venv/bin/agent-channel core list
.venv/bin/agent-channel core install

# 4) configure a node and start
.venv/bin/agent-channel set-sub "<your-subscription-url>"
.venv/bin/agent-channel start

# 5) open the control panel
.venv/bin/agent-channel ui
```

> Why a virtualenv rather than `pip install --user`: on macOS `--user` puts the command in `$(python3 -m site --user-base)/bin` (measured: `/Users/<you>/Library/Python/3.x/bin`), which is **not on PATH by default**, so step 3 fails with `command not found`. On top of that, both Homebrew Python on macOS and distro Python on Debian/Ubuntu refuse `--user` installs because of **PEP 668** (`--break-system-packages` gets past that refusal, but not past the PATH problem).

Three notes (all code facts):

- There is **no tray icon** off Windows; use the CLI `start` / `stop` or the buttons in the panel for start/stop and proxy restore.
- macOS may ask for "network" permission on first run; Linux's system proxy (`gsettings`) only applies to desktops that follow the GNOME proxy convention (GNOME / Cinnamon / MATE) — on KDE etc. point the proxy at `127.0.0.1:20849` manually.
- Kernel download goes through `api.github.com`; networks that cannot reach GitHub must solve that first (same as Windows).

### Configuring a node (three steps)

1. In "Node configuration", pick one input shape and fill it in: **single node link** / **subscription URL** / **import JSON**;
2. Click "Save configuration";
3. Under "**Outbound when enabling**", choose: with a single node it read-only shows which one will be used; with several, pick "auto" or click one of them. Then click "**Start channel**".

When it is up, the status card reads "running" and shows the active outbound.

### Three other actions on the panel

- **Quick test**: actually checks whether OpenAI / Gemini / Anthropic are reachable, with latency;
- **Benchmark**: measures the current source's nodes (real forwarding check + landing IP + relay mark). The results are **diagnostic only** — they feed latency numbers into the outbound dropdown;
- **Check kernel update**: reads the local version, compares once against the latest official release, and tells you if there is a newer one.

> Note: the outbound dropdown is the **only** place a node is selected. The benchmark results carry no "use" button — that would be a second entry point for the same decision and would disagree with the dropdown, so it was removed.

## 7. Tray icon (Windows)

While running, an icon sits in the system tray. **Right-click** for: **Open panel** / **Start channel** / **Stop channel** / **Quit** (quitting restores the system proxy). **Double-click** the icon to open the panel.

Each platform uses the affordance that fits it: **Windows** uses the system tray; **macOS** gets a native window (the red close button stops the channel, restores the system proxy and exits; the in-window "退出" button does the same); **Linux** is CLI + browser panel — there is no native window, so quit with the **Quit button at the top right of the panel** or Ctrl-C, and note that **closing the browser tab does not stop the process**.

## 8. Command line

```text
agent-channel status                              show current status (lightweight, read-only)
agent-channel set-sub  <subscription URL>         save a subscription
agent-channel set-node <vless://...>              save a single node (JSON fragments accepted too)
agent-channel set-json <file|->                   import sing-box JSON nodes from a file or stdin (candidates only)
agent-channel start [--source node|sub|json]      enable using the saved config (optionally pin a source slot)
agent-channel stop                                stop the channel and restore the system proxy
agent-channel test                                test connectivity to overseas AI endpoints
agent-channel benchmark --sub <URL>               benchmark (--node for one node, --json for JSON output)
agent-channel config                              show config (redacted by default, --reveal for full)
agent-channel core list                           list available official kernel versions
agent-channel core install v1.14.3                install a specific kernel version
agent-channel ui                                  open the control panel
```

There is also an unattended daemon entry point (source runs only): `python launch_service.py`. It starts the control panel but **does not open a browser** — the URL is printed to the console and you open it yourself. It also connects the channel from `config.json` and stays resident, which suits auto-start and other background use. To have the panel open on launch, use `agent-channel ui` above or the installer.

For unattended use, select an outbound in the panel once beforehand: this entry connects only from saved config, and with a pool source (two or more nodes) that has no outbound selected it prints the tiered message "N candidate nodes configured but no outbound selected yet: choose one under 'Outbound on enable' in the panel" and stops at the panel instead of picking an exit for you. That is consistent with "do not enable until an outbound is selected", but at auto-start nobody is at the panel to tell "not selected" apart from "nothing configured".

Log output is UTF-8, so non-ASCII node names render correctly.

## 9. Configuration and data model

`config.json` (on Windows: `%LOCALAPPDATA%\agent_channel\`; on macOS/Linux: `~/.agent_channel/`):

```json
{
  "active_source": "",
  "node_uri": "",
  "subscription_url": "",
  "imported_nodes": [],
  "running_source": null,
  "outbound_selections": {
    "node": {"mode": "", "node_key": null},
    "sub":  {"mode": "", "node_key": null},
    "json": {"mode": "", "node_key": null}
  }
}
```

- `active_source`: the currently active source slot, `node`/`sub`/`json` (empty string = nothing configured yet);
- `running_source`: a snapshot of the source the running channel was started from (written on a successful `enable`, cleared on stop). The status card shows `running_source ?? active_source`, so "started, then switched tabs" still shows which line is actually running;
- `imported_nodes`: imported nodes, used as candidates for the `json` slot;
- **Credentials stay in `config.json` only**: no API response and no panel ever returns `uuid` / `password` / `private_key`;
- **Old configs migrate automatically**: `mode`, `outbound_mode`/`outbound_node_key`, `outbound`/`pinned_node` and `strategy`/`custom_node` from earlier versions are all folded into the three-slot structure and the old keys are removed; if a mapping is impossible it degrades to "unselected" with a warning instead of raising.

### HTTP API (shared by the panel and agents; loopback only)

| Method | Path | Purpose |
| :--- | :--- | :--- |
| GET | `/api/status` | Channel status + system proxy + source (`source` = tab being viewed, `running_source` = the line actually running) + config warnings |
| GET | `/api/config` | Current config (redacted, no credentials; `imported_nodes` comes back as a count only) |
| GET | `/api/outbound_options` | Candidates for the given source (`?source=`) + count + current selection + per-item latency |
| GET | `/api/uuid` | Generate a v4 UUID |
| GET | `/api/geolocation` | Probe landing city / ISP / weather through the channel exit |
| GET | `/api/core_info`, `/api/check_update` | Kernel version / update check |
| POST | `/api/config` | Incremental save (writes only the keys given; never clears other fields) |
| POST | `/api/enable` | Start the channel; **`source` is required** (`node`/`sub`/`json`) and that source must have an address |
| POST | `/api/disable` | Stop the channel and restore the system proxy |
| POST | `/api/benchmark` | Benchmark the requested source's nodes (`{source}`, default `active_source`) (landing IP + relay mark) |
| POST | `/api/set_outbound` | Set the outbound `{mode, node_key?, source?}` |
| POST | `/api/import_nodes` | Import / clear nodes (writes the `json` slot candidates only, **never switches the active source**) |
| POST | `/api/sysproxy` | Toggle the system proxy |
| POST | `/api/test_ai` | Test AI endpoint connectivity (results carry `via` = `channel`/`direct` and `channel_running`) |
| POST | `/api/update_core` | Download and replace the official kernel (refuses clearly when the kernel is in use) |

**Write-endpoint contract** (every POST above):

- `Content-Type: application/json` is required, otherwise **415**; an empty body is treated as `{}`;
- the body must be a valid JSON **object**, otherwise **400** (the old behaviour fell back to an empty object and executed anyway — removed);
- request body limit is **8 MiB**, over that is **413**;
- a request carrying `Origin` must be same-origin with this machine (normalised against the `Host` header; `127.0.0.1` / `localhost` / `[::1]` are all accepted), cross-site writes are **403**. **Local callers without `Origin` (curl / CLI / tray / daemon) are unaffected.**

> What this guards against: browsers let any web page send a "simple request" to `127.0.0.1:<port>` without a CORS preflight, and the write endpoints can stop the channel, change the subscription (pointing traffic at someone else's node) or replace the kernel — reading the response is irrelevant, the side effect already happened.

### Programmatic use

```python
from agent_channel import channel_state, enable_channel, load_config

st = channel_state(load_config())
if st["state"] != "ok":
    print(st["message"])  # unconfigured / configured-but-unrecognized / unselected / pinned-node-dead
else:
    enable_channel(**st["kwargs"], auto_patch=True, set_sys_proxy=True)
# 127.0.0.1:20849 is now usable
```

`channel_state(config)` returns `{state, kwargs, candidates, message}`: only `state == "ok"` may be enabled; otherwise `message` is the one sentence shown to the user (unconfigured / configured-but-unrecognized / unselected / pinned-node-dead, the single definition point in the whole codebase). `channel_kwargs` is a compatibility shim over it (`kwargs` when `ok`, otherwise an empty dict).

## 10. Building and testing from source

```bash
pip install PyInstaller
python packaging/build.py              # onedir + zip, kernel bundled, works offline
python packaging/build.py --no-core    # slim single file, downloads the kernel on first run
python packaging/build.py --installer  # produces setup.exe (Windows only, requires Inno Setup)

python tests/run_tests.py              # full suite (custom runner, not pytest)
```

## 11. FAQ

**The browser does not open the panel after install.**
Visit `http://127.0.0.1:18888` manually. If the port is taken the program picks another one and prints the actual address on startup.

**The channel is enabled but web pages still fail.**
Run "Quick test" first. If it fails, the node itself is most likely unusable — benchmark once and switch to a node whose forwarding works, or pick another one in the outbound dropdown.

**Does it affect access to domestic sites?**
No. It only points the system proxy at the local channel. If a particular domestic site misbehaves, turn "sync as local system proxy" off on the panel.

**Can I run two instances?**
Not advisable. The local proxy port is fixed at `20849`, so two instances fight over it.

**Does it upload my data?**
No. It only forwards traffic locally. The only outbound network access falls into two kinds: **direct** (the kernel update check and fetching your configured subscription, both bypassing the system proxy) and **through the node exit** (the benchmark and landing probes you trigger yourself).

**Uninstalling**

- Installer build: uninstall from Settings → Apps, or run `unins000.exe` in the install directory (the bundled kernel is removed too);
- zip / single-file build: just delete the directory. The program only stores config and kernel under `%LOCALAPPDATA%\agent_channel\` (Windows) or `~/.agent_channel/`; delete that and nothing remains.
- If it is running, right-click the tray icon and choose Quit first.

## 12. Disclaimer

This tool is a purely technical network channel management utility. It **does not provide, bundle, or recommend any nodes or subscription material**. Users are responsible for preparing a lawful network egress and for their own use of it.

## 13. License

This project is released under the open-source [MIT License](LICENSE), Copyright (c) 2026 gambn. See the [LICENSE](LICENSE) file in the repository root for the full text.
