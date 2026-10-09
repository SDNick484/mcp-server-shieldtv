# ADB tools: review and proposal (design only)

Status: **design only, nothing here is implemented.** It reviews the three ADB tools that exist
and proposes typed, read-only additions. Every command below would need a real output sample
from your Shield before a parser is written. The [HARDWARE_VALIDATION.md](../HARDWARE_VALIDATION.md)
ADB step captures these samples.

## Why ADB is handled differently

The remote protocol can press allow-listed keys and open allow-listed links, and nothing else.
ADB is a shell on the Shield as the `shell` user. That user can start any activity, change
settings, install packages, read logs, and type text. A model that can reach a shell, through
its own mistake or a prompt injection in something it read, can do all of that. So the rule in
CLAUDE.md is absolute: **no tool argument ever reaches a shell command.** The rest of this
document follows from that rule.

## What exists today (reviewed)

| Tool | ADB call | Arguments | Annotations | Verdict |
| --- | --- | --- | --- | --- |
| `get_now_playing` | `dumpsys media_session; echo __UPTIME__; cat /proc/uptime` | none | read-only | Keep. Verified on hardware (S-DUMPSYS-MEDIA). |
| `get_remotes` | `dumpsys bluetooth_manager; echo __INPUT__; dumpsys input` | none | read-only | Keep. Verified (S-STUCK-REMOTES). |
| `reboot_shield` | the ADB protocol's `reboot:` service (not a shell) | none | destructive | Keep, but see gap 1. |

The guard holds. `run_command` refuses anything outside the `COMMANDS` constant, and
`test_adb_tools_take_no_arguments` fails if an ADB tool grows a parameter. Reviewing them
turned up three gaps.

1. **The opt-in is all or nothing.** `"adb": true` enables the read-only tools and
   `reboot_shield` together. Someone who only wants "what's playing" shouldn't also hand the
   model a reboot. *Proposal:* replace it with
   `"adb": {"read": true, "reboot": false}`, and keep accepting `"adb": true` as
   `{"read": true, "reboot": true}` so existing configs don't change. A tool that isn't
   enabled isn't registered at all, so it never appears in `tools/list` and the model can't
   call it.
2. **The device on the other end isn't verified.** ADB over TCP authenticates *us* to the
   Shield, not the Shield to us. If the Shield's DHCP address goes to another Android device
   that also has network debugging on and trusts our key, that's unlikely but possible, and the
   tools would read or reboot the wrong device. The remote protocol now checks the MAC in the
   Shield's certificate (S-CERT-MAC). *Proposal:* `adb-setup` saves `getprop ro.serialno`, and
   every ADB call first runs that constant and refuses on a mismatch, at the cost of one extra
   round trip. It's an assumption to confirm: does `ro.serialno` stay stable across reboots and
   updates?
3. **Output size is unbounded.** `dumpsys input` on a device with many input devices is large,
   and the parsers hold all of it. *Proposal:* cap what's read at 1 MB and say so when the cap
   cuts something.

## Proposed read-only tools

Each tool is one constant command, takes no arguments, returns a TypedDict (so it publishes an
`outputSchema`), and is marked `read_only_hint=True`. Each parser gets a fixture captured from
a real Shield (`tests/fixtures/adb/<command>.txt`), the same way `test_adb.py` feeds real
`dumpsys media_session` output today. The S-* ids would go into `assumptions.py` as
`simulator-only` until the fixtures exist.

| Tool | Constant command | Returns | Why the model needs it | Assumption to confirm |
| --- | --- | --- | --- | --- |
| `get_device_info` | `getprop ro.product.model; getprop ro.build.version.release; getprop ro.build.display.id; getprop ro.serialno` | model, Android version, Shield Experience build, serial | "Is my Shield up to date?" Also feeds gap 2 | S-GETPROP-KEYS: these four properties exist on a Shield |
| `get_installed_apps` | `pm list packages -3` | third-party package names | Shows which apps can be added to `apps` in config.json (today: open the app, then read `current_app_package`) | S-PM-LIST: output is `package:<name>` lines |
| `get_storage` | `df -k /data` | total, used, free (bytes) | "Why won't this app update?" | S-DF-FORMAT: busybox/toybox `df -k` column layout |
| `get_hdmi_status` | `dumpsys hdmi_control` | CEC enabled, active source, devices on the bus (logical address, name, vendor) | "Why is there no picture/sound?", the receiver/TV side of a movie-night setup | S-HDMI-DUMPSYS: the section names and fields (varies by Android version; low confidence) |

Considered and rejected, with the reason:

| Idea | Why not |
| --- | --- |
| Find which app opens a link (`cmd package query-activities ... -d <link>`) | The link would reach the shell. It's useful, but it's for the *owner* adding an app, not for the model, so it belongs in the CLI: `mcp-server-shieldtv apps check <name>`, and only for links already in config.json. |
| Screenshots (`screencap`) | Shows whatever is on screen (messages, accounts), large, and of little use to a model that can't act on pixels reliably. |
| `logcat` | Logs carry tokens and account details. |
| `input text`, `am start`, `settings put`, `pm install/uninstall` | These change the device and take free-form strings, which is exactly what the rule forbids. |
| Volume via `media volume` | Volume belongs to whatever HDMI-CEC routes it to (S-CEC-VOLUME); `send_key` already sends volume keys the same way the remote does. |

## Opt-in and annotations

```json
{ "adb": { "read": true, "reboot": false } }
```

- `read` registers `get_now_playing`, `get_remotes` and the proposed read-only tools.
- `reboot` registers `reboot_shield` (`destructive_hint=True`, so clients ask first).
- `adb-setup` asks which of the two to enable, defaulting to read only.

## Tests that would come with it

- The existing constant-command guard grows with `COMMANDS`, and a test asserts that every
  ADB tool still has an empty `inputSchema.properties`.
- A parser test per command, fed captured output, plus truncated and empty output.
- A test that a tool not enabled by the opt-in is absent from `tools/list`, not just refused.
- A serial-mismatch test (gap 2): `FakeDevice` answers a different `ro.serialno`, and the tool
  refuses without running its command.
