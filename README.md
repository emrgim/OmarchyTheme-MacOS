# OmarchyTheme-MacOS

Sync your **macOS** appearance with your **Omarchy** (Linux) theme — the same way
an **OmarchyTV** follows the desktop. OmarchyMac is a small menu-bar daemon that
speaks the **Omarchy Link** protocol (v1) as the *device* side, so your Omarchy PC
pushes its palette here with the exact same `omarchy-link sync-theme` it uses for
the TV. No polling, no cloud, LAN only.

When Omarchy switches theme, the Mac follows:

- **Appearance** → Light / Dark from the theme `mode` (applied live via System Events)
- **Highlight colour** (text selection) → the theme's **exact** accent (`AppleHighlightColor`)
- **System accent** → the nearest of macOS's fixed accent swatches (`AppleAccentColor`)
- **Desktop wallpaper** → the pushed Omarchy wallpaper
- the full 24-key `colors.toml` palette is received and kept for app-level theming

> macOS only exposes **8 fixed system accents** (plus graphite). The accent is
> therefore snapped to the nearest swatch; the **highlight** colour is set to the
> exact hex. This is the same kind of platform ceiling OmarchyTV hits on its OS.

## How it works

```
  Omarchy PC (SuperOMI)                         Mac (this)
  ┌───────────────────┐   PUT /v1/wallpaper     ┌────────────────────────┐
  │ omarchy theme set │ ─────────────────────▶  │ OmarchyMac menu-bar app │
  │  → theme-set hook │   PUT /v1/theme         │  Omarchy Link server    │
  │  → omarchy-link   │   (OTV1 signed, LAN)    │  :47800  (TV side)      │
  │     sync-theme    │ ◀───────────────────    │  → applies to macOS     │
  └───────────────────┘   SPAKE2 pairing code   └────────────────────────┘
```

The Mac runs the **TV side** of [`docs/PROTOCOL.md`](https://github.com/piermark/omarchytv):
`GET /v1/info`, SPAKE2 `POST /v1/pair/{start,confirm}`, and the authenticated
`GET /v1/status`, `GET|PUT /v1/wallpaper`, `PUT /v1/theme`. Pairing shows a
6-digit code in the menu bar; the token is derived with SPAKE2 and never crosses
the wire. Requests and responses are HMAC-signed with that token.

On the Omarchy PC you point a **second** `omarchy-link` instance at the Mac (its
own `XDG_STATE_HOME`), so the existing TV pairing is untouched and both devices
get every theme change.

## Install (on the Mac)

```sh
git clone https://github.com/emrgim/OmarchyTheme-MacOS.git ~/omarchy-mac-link
cd ~/omarchy-mac-link && ./install.sh
```

`install.sh` builds a venv (`rumps`), installs the LaunchAgent
`com.mrg.omarchymac`, and starts the menu-bar app. Click **Pair a new computer**
to show a code.

## Pair from the Omarchy PC

```sh
# a dedicated state dir keeps your OmarchyTV pairing separate
export XDG_STATE_HOME=~/.local/state/omarchy-link-mac
export XDG_CONFIG_HOME=~/.config/omarchy-link-mac
omarchy-link pair <mac-ip> --code <code-from-menu-bar>
omarchy-link sync-theme --force        # push the current theme now
```

Then add a theme-set hook so every switch follows (see `hooks/`).

## Requirements

- macOS (built and tested on Ventura 13.7, Intel), stock `python3`
- An Omarchy PC with the `omarchy-link` PC plugin on the same LAN
- One macOS **Automation** consent (System Events) on first apply — a TCC prompt
  no tool can bypass

## License

MIT
