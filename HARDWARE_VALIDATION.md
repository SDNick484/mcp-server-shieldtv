# Hardware validation (Shield TV)

The first hardware pass (2026-10, SHIELD Android TV, remote service 7.00) verified pairing,
`get_status`, keys, `launch_app`, `set_power`, `adb-setup`, `get_now_playing` and `get_remotes`.
Everything the `hardware-free` branch added was built away from the Shield and is **verified
against the simulator only** until you run the step that covers it. The simulator is a wire-level
fake that the real androidtvremote2 library pairs and connects with. Each step depends only on
the ones before it and names the assumptions it confirms (ids, claims and sources are in
`src/shieldtv_mcp/assumptions.py`).

Plan on about 45 minutes. `doctor` points at steps 1-3 and 6 by number, so keep the numbering if
you edit this file.

## What's new and unconfirmed

| Feature (hardware-free branch) | Status | Step |
| --- | --- | --- |
| `pair`: retry after a typo, after the TV rejects the code, after Cancel; saves name and MAC | verified against simulator only | 1 |
| `pair`: sets aside unusable old credentials | verified against simulator only | 1 |
| `doctor` (config, TCP, identity, session, ADB, mDNS) | verified against simulator only | 2 |
| Setters return ActionResult; `set_power` "unchanged"; dry run | verified against simulator only | 4 |
| `get_status`: `stale`, `as_of`, `error` while disconnected | verified against simulator only | 5 |
| Bounded connect attempts (a session that never starts) | verified against simulator only | 5 |
| Re-pairing picked up by a running server | verified against simulator only | 7 |
| Rediscovery by MAC after an address change | verified against simulator only | 8 |
| MCP resources (`shieldtv://apps`, `shieldtv://keys`) and prompts | verified against simulator only | 9 |
| Streamable HTTP + Cloudflare Access (JWT checks tested with local keys) | verified against simulator only | 10 |
| OpenRC service on Alpine (tested in a `python:3.12-alpine` container) | verified against simulator only | 10 |
| Log redaction (addresses, MACs; keys, JWTs) | verified against simulator only | 2 |

## Before you start

- The Shield on, on the same network as this machine, and its IP address (your router's
  client list, or the Shield's own network settings)
- The TV in view: steps 1, 2 and 7 put things on screen, or must not

```sh
git clone https://github.com/SDNick484/mcp-server-shieldtv.git && cd mcp-server-shieldtv
git checkout hardware-free
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                      # expect: all passed
mkdir -p captures
# A separate config dir, so your working pairing stays untouched until you're happy:
export SHIELDTV_CONFIG_DIR=~/.config/mcp-server-shieldtv-test
```

**Tool calls** use `mcp-server-shieldtv call <tool> key=value ...`. It runs the tool through the
same MCP layer the model uses and prints the structured result. `call tools` lists the tools.
Add `--debug` (and `2> captures/<name>.log`) to record the protocol traffic. Addresses and MACs
are redacted; `--no-redact` shows them.

**Recording a result.** When a step confirms an assumption, set its `status` to
`"hardware-verified"` in `assumptions.py` with what you saw in `note`. When it contradicts one,
set `"hardware-contradicted"` and keep the output. Update the README's verification table to
match (`pytest` fails until you do), and change the feature's row above to "verified on hardware".

---

## 1. Pairing

The pairing code changed on this branch, so start here, in the fresh config directory. Make
the first two attempts fail on purpose:

```sh
mcp-server-shieldtv pair --host <shield ip>
```

1. A code appears on the TV. **Type a wrong code** that differs in the first two characters
   (e.g. swap them). Expected: `That didn't work (that code doesn't match the one on the TV).
   Starting over; a new code will appear.`, and a new code appears on the TV.
2. On the new code, **press Back (Cancel) on the TV** instead of typing. Then press Enter in the
   terminal with any 6 characters. Expected: a "didn't work" line, and a third code.
3. Type the code shown. Expected:

```
Generated a new client certificate in ~/.config/mcp-server-shieldtv-test
Pairing with <name> (<MAC>). A code will appear on the TV screen.
...
Paired and connected: NVIDIA SHIELD Android TV
Saved <name> at <ip> (MAC <MAC>). Credentials are in ... (keep them private).
```

Check `config.json` has `host`, `name` and `mac`, and that `ls -l` shows `cert.pem`, `key.pem`
and `config.json` as `-rw-------`. Write down the name and MAC: step 8 relies on the MAC.

If step 2 behaves differently (e.g. the TV keeps the old code, or the terminal hangs), capture
`mcp-server-shieldtv pair --debug --host <ip> 2> captures/pair.log`. The simulator closes the
connection on Cancel, and that's an assumption about the real Shield.

Confirms: **S-PAIRING** (again, now with the retry paths), the name/MAC part of **S-CERT-MAC**.

## 2. doctor

```sh
mcp-server-shieldtv doctor
```

Expected, ending in `OK`:

```
Shield <ip>
   ok config    paired with <name> (MAC xx:xx:xx:xx:..:..) at x.x.x.<n>; credentials load and are private
   ok tcp       ports 6466 and 6467 accept connections
   ok identity  <name>, MAC xx:xx:xx:xx:..:..
   ok session   NVIDIA SHIELD Android TV (remote service 7.00); on, <app> in front, volume ...; started in <n> ms
```

**Watch the TV while it runs.** The `identity` check opens a TLS connection to the pairing port
just to read the certificate. If a pairing dialog or code flashes up, **S-CERT-MAC's "shows
nothing on the TV" is contradicted**. Record it, because rediscovery (step 8) does the same
thing for every Android TV device it finds.

Confirms: **S-REMOTE-HANDSHAKE** (again), part of **S-CERT-MAC**.

## 3. mDNS

```sh
mcp-server-shieldtv discover
```

Expected: a line with the Shield's IP and its name. Nothing found means multicast doesn't reach
this machine (WSL2 NAT, a VLAN): that's inconclusive, so try from a machine on the same subnet.
Rediscovery (step 8) depends on this.

Confirms: **S-MDNS**.

## 4. Tools, results and dry run

```sh
mcp-server-shieldtv call get_status
mcp-server-shieldtv call launch_app app=youtube
mcp-server-shieldtv call send_key key=DPAD_DOWN repeat=2
mcp-server-shieldtv call set_power state=off
mcp-server-shieldtv call set_power state=off
mcp-server-shieldtv call set_power state=on
mcp-server-shieldtv call --dry-run launch_app app=netflix
```

Expected: `get_status` shows `name`, `stale: false`, `as_of` (a UTC time), `error: null`.
Each action prints an ActionResult (`"outcome": "done"` and `sent`, e.g.
`["app_link https://www.youtube.com"]`). The second `set_power state=off` gives
`"outcome": "unchanged"` and sends nothing. The dry run gives `"outcome": "dry_run"` and
Netflix does **not** open.

## 5. Drops, stale state, standby

Run the server the way Claude would, so it stays connected, and ask through the Inspector or
Claude. Or use one process and two terminals:

```sh
mcp-server-shieldtv serve --http --port 8712 &       # terminal 1 (loopback only, no Access needed)
npx @modelcontextprotocol/inspector                  # connect to http://127.0.0.1:8712/shieldtv/mcp
```

1. **Pull the Shield's network cable (or turn its Wi-Fi off) for a minute.** `get_status`:
   `reachable: false`, `stale: true`, the last power and app, `as_of` from before, and an `error`.
   Plug it back in: within seconds `reachable: true`, `stale: false`.
2. **Standby for a while.** `set_power state=off`, wait at least 15 minutes (overnight if you
   can), then `get_status`. Expected: `reachable: true`, `power: "standby"`. Then
   `send_key key=HOME`: the Shield wakes. If it was unreachable instead, or reconnected (the
   server log shows "Connected to Shield" again), record it for **S-SLEEP-CONNECTION**.
3. **Launch while asleep.** `set_power state=off`, then `launch_app app=youtube`. Record what
   happens: does it wake and open YouTube, or fail with "...in standby, which may be why"? The
   simulator assumes the latter. Either is fine, but write it down so the simulator can match.

Confirms: **S-SLEEP-CONNECTION**, **S-RECONNECT** (again).

## 6. ADB

Only if you use the ADB tools (in the test config dir, run `mcp-server-shieldtv adb-setup` first):

```sh
mcp-server-shieldtv call get_now_playing
mcp-server-shieldtv call get_remotes
mcp-server-shieldtv call --dry-run reboot_shield      # must NOT reboot
mcp-server-shieldtv doctor                             # now includes an `adb` line
```

For the ADB design ([docs/ADB_TOOLS.md](docs/ADB_TOOLS.md)), save these outputs. They're read-only:

```sh
adb connect <ip>:5555
for c in "getprop ro.product.model" "getprop ro.build.version.release" "getprop ro.build.display.id" \
         "getprop ro.serialno" "pm list packages -3" "df -k /data" "dumpsys hdmi_control"; do
  echo "### $c"; adb shell "$c"; done > captures/adb-proposed.txt
adb shell pm list packages | grep -i sling               # S-LIVE-TV-PACKAGES, if Sling is installed
```

Confirms: **S-LIVE-TV-PACKAGES** (if Sling is installed); provides fixtures for the proposals.

## 7. Unpaired on the TV, then re-paired while the server runs

With the server from step 5 still running:

1. On the Shield, remove this client's pairing. It's listed as `mcp-server-shieldtv` wherever
   your Shield lists paired remote apps. If you can't find it, clearing the storage of the
   "Android TV Remote Service" system app forgets every pairing (androidtvremote2's own hint:
   Settings > Apps > See all apps > Show system apps > Android TV Remote Service > Storage >
   Clear data). That also unpairs your phone's remote app.
2. Restart the Shield's network (or wait for a drop) so the server reconnects. `get_status`:
   `error` mentions "rejected our certificate"; `send_key` says to re-run `pair`. If it says
   "can't reach" instead, **S-TLS-REJECT is contradicted**: capture
   `serve --http --debug 2> captures/unpaired.log`.
3. In another terminal, `mcp-server-shieldtv pair --host <ip>` (same `SHIELDTV_CONFIG_DIR`).
   Within ~5 s of it finishing, without restarting the server: `get_status` shows
   `reachable: true`.

Confirms: **S-TLS-REJECT**; the re-pair pickup.

## 8. A new address

With the server running and `config.json` holding the `mac` from step 1:

1. Give the Shield a different IP: change its DHCP reservation and reboot it, or set a static IP
   in its network settings.
2. Watch the server log. After about 60 s unreachable: `The Shield moved from x.x.x.A to x.x.x.B;
   switching and saving the new address.` `config.json`'s `host` is the new address, and
   `get_status` works.
3. Put the address back the same way. The server follows again.

If it never moves: `discover` (step 3) must list the Shield at the new address, and
`doctor` at the new address must show the same MAC. A different MAC **contradicts
S-CERT-MAC**: record both.

Confirms: **S-CERT-MAC**, **S-MDNS** (again).

## 9. With Claude: resources and prompts

```sh
claude mcp add shieldtv -e SHIELDTV_CONFIG_DIR=$SHIELDTV_CONFIG_DIR -- $(which mcp-server-shieldtv)
```

In Claude Code, `@shieldtv:shieldtv://apps` attaches the app list (defaults marked `default`).
Running `/mcp__shieldtv__watch youtube` wakes the Shield and opens YouTube, and
`/mcp__shieldtv__remotes_not_working` runs `get_remotes` (with ADB).

## 10. As a service: Alpine LXC, HTTP, Cloudflare Access

In the LXC (Alpine 3.20+, as root):

```sh
apk add git
git clone -b hardware-free https://github.com/SDNick484/mcp-server-shieldtv.git /root/mcp-server-shieldtv
sh /root/mcp-server-shieldtv/deploy/alpine/install.sh /root/mcp-server-shieldtv
su -s /bin/sh mcp-shieldtv -c 'SHIELDTV_CONFIG_DIR=/var/lib/mcp-server-shieldtv /opt/mcp-server-shieldtv/venv/bin/mcp-server-shieldtv pair --host <ip>'
su -s /bin/sh mcp-shieldtv -c 'SHIELDTV_CONFIG_DIR=/var/lib/mcp-server-shieldtv /opt/mcp-server-shieldtv/venv/bin/mcp-server-shieldtv doctor'
rc-update add mcp-server-shieldtv default && rc-service mcp-server-shieldtv start
curl -s http://127.0.0.1:8712/healthz
```

Expected: `install.sh` finishes without compiling anything, the pairing code shows on the TV,
`doctor` passes as in step 2 (mDNS too, unless the LXC's bridge filters multicast), `healthz`
answers `{"status": "ok"}`, `ps -o user,args | grep shieldtv` shows `mcp-shieldtv`, and
`/var/lib/mcp-server-shieldtv/*.pem` are `-rw------- mcp-shieldtv`.

With cloudflared and an Access application (README: HTTP and Cloudflare Access), set
`CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` and `MCP_PUBLIC_HOSTS` in `/etc/mcp-server-shieldtv/env`,
restart, and check:

```sh
curl -si -H 'Host: <public host>' http://127.0.0.1:8712/shieldtv/mcp | head -1   # expect: 403 (no JWT)
```

Then add `https://<public host>/shieldtv/mcp` as a custom connector in Claude and call
`get_status`. `/var/log/mcp-server-shieldtv/server.log` should show addresses redacted and no JWT.

---

## Which step confirms what

| Assumption           | Step | Confidence before | Notes                                                    |
| -------------------- | ---- | ----------------- | -------------------------------------------------------- |
| `S-PAIRING`          | 1    | high              | already hardware-verified; re-checks the retry paths     |
| `S-REMOTE-HANDSHAKE` | 2    | high              | already hardware-verified                                |
| `S-TLS-REJECT`       | 7    | medium            |                                                          |
| `S-CERT-MAC`         | 1, 2, 8 | medium         | name/MAC read (1), silent on the TV (2), stable (8)      |
| `S-MDNS`             | 3, 8 | high              | no answer can be the network, not the Shield             |
| `S-RECONNECT`        | 5    | high              | already hardware-verified                                |
| `S-SLEEP-CONNECTION` | 5    | medium            | the longer the standby, the better the evidence          |
| `S-MARKET-REJECT`    | none | high              | already hardware-verified                                |
| `S-LINK-UNHANDLED`   | none | high              | already hardware-verified                                |
| `S-WAKE-ANY-KEY`     | 5    | high              | already hardware-verified                                |
| `S-CEC-VOLUME`       | 4    | high              | already hardware-verified                                |
| `S-APP-LINKS`        | 4    | high              | already hardware-verified                                |
| `S-DUMPSYS-MEDIA`    | 6    | high              | already hardware-verified                                |
| `S-LIVE-TV-PACKAGES` | 6    | medium            | the Sling package name                                   |
| `S-STUCK-REMOTES`    | 6    | high              | already hardware-verified                                |
| `S-REBOOT-TIME`      | none | high              | already hardware-verified; step 6 only dry-runs a reboot |

## What to send back if something fails

- The full `doctor` output (redacted by default), or `doctor --json`.
- For a failing call: the same call with `--debug 2> captures/<name>.log`. It's redacted too,
  and PEM keys and JWTs are always removed.
- For pairing: `pair --debug --host <ip> 2> captures/pair.log` and what the TV showed at each
  attempt.
- `captures/adb-proposed.txt` from step 6, for the ADB design.
