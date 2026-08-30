# logikit

**Profile-as-code for the Logitech MX Creative Console.**

Duplicate a profile. Rebuild a whole keypad from a text file. Bulk-rewrite
every URL at once. All the things Logi Options+ makes you do by hand, or
won't do at all.

Logi Options+ stores every profile as plain JSON under
`~/Library/Application Support/Logi/LogiPluginService/Applications/`.
`logikit` edits that store directly, so it can do the things the official app
won't: duplicate a profile, bulk-rewrite URLs, build a whole keypad from a
text file, and generate key icons.

macOS, Python 3, no dependencies.

## Install

```bash
git clone https://github.com/kunalbhayana/logikit.git
ln -s "$PWD/logikit/logikit.py" ~/.local/bin/logikit
```

## Use

```bash
logikit list                      # every profile on every device
logikit show "CodeHaven"          # what each key does

logikit duplicate "Chrome Profile" --name "Chrome - Teaching"
logikit add-url  "Chrome - Teaching" --label "IGCSE" \
                 --url https://example.com --bg "#12233F" --fg "#7FB4FF"
logikit unbind   "Chrome - Teaching" --key 6
logikit set-url  --find old.example.com --replace new.example.com
```

### Building a keypad from a file

Write the keys as text, one per line, `---` between pages. A target that
starts with `http` opens a URL; anything else is a keyboard shortcut:

```
Calendar | https://calendar.google.com | #1E3A5F | #8FC7FF | calendar
IGCSE    | https://example.com/igcse  | #12233F | #7FB4FF | code
New Chat | Cmd+N                      | #2B1B12 | #F0A882 | chat
---
CBSE 11  | https://example.com/cbse   | #10301F | #6FE3A6 | cap
```

Targets can be more than URLs and shortcuts:

| Target | Does |
|---|---|
| `https://...` | opens a URL |
| `Cmd+Shift+G` | sends a keyboard shortcut |
| `app:/Applications/Notes.app` | launches an app |
| `sys:MediaPlayPause` | a built-in system action (`logikit actions`) |
| `text:Hello\nthere` | types text (`\n` is a newline) |
| `wait:500` | pauses, for use inside a chain |
| `plugin:ZoomMeeting:Loupedeck.ZoomPlugin.ToggleChatCommand` | runs a plugin action directly |
| `open:/path/to/folder` | opens a file, folder or URL in the Finder |

Chain steps with `>>` to build a macro:

```
Open CH | app:/Applications/Google Chrome.app >> wait:900 >> Cmd+L >> text:https://codehaven.in >> Return
```

A fifth field picks a built-in icon glyph (`logikit glyphs` lists them:
calendar, cap, book, code, globe, home, doc, chart, chat, search, star,
clock, folder, play). Glyphs are drawn in white and tinted to the key's
text colour, so one glyph works in any palette. `--svg` takes your own file
instead.

Modifiers are `Cmd`, `Ctrl`, `Shift` and `Opt`/`Alt`. Keys can be letters,
digits, punctuation, or `Space`, `Tab`, `Return`, `Escape`, `Delete` and
`ArrowUp`/`Down`/`Left`/`Right`.

Registering an app Options+ has never seen:

```bash
logikit add-app --bundle com.example.app --name "Example" \
                --template "Loupedeck70/com.apple.safari"
```

See `codehaven.keys`, `claude.keys` and `general.keys` for full examples.

### Looking at a page before you trust it

```bash
logikit preview "CodeHaven" --page 1 --out page1.svg
```

Renders the page's real `.ict` files to an SVG, so you can see what is
actually on the device rather than what you meant to put there.

Then:

```bash
logikit build "CodeHaven" --file codehaven.keys --replace
```

See `codehaven.keys` for a full two-page example.

## Why

Options+ has no duplicate button. If you want a variant of a profile you
already built, you rebuild all nine keys by hand, one dialog at a time. There
is no bulk edit, so moving a domain means clicking through every key that
mentions it.

None of that is a hardware limit. The profiles are plain JSON sitting in a
directory, and the icons are base64 SVG. Once your keypad is a text file in
git, changing it is one command and you can see every change in a diff.

## Keeping private links out of git

Anything with a token in it - a Zoom join link with `?pwd=`, a private
document URL, a personal canned reply - belongs in a `*.local.keys` file.
Those are gitignored, and `logikit build` reads them like any other keyfile:

```bash
logikit build "Chrome - Daily" --file chrome-daily.local.keys --replace
```

The committed `.keys` files here are examples with public URLs only.

## Safety

Every write zips the whole profile store into `Applications.Backups/` first,
then stops and restarts `LogiPluginService` so the changes are picked up (the
service caches profiles in memory). Pass `-n` / `--dry-run` to preview any
command, or `--no-restart` when batching several edits.

To undo, restore a `logikit_*.zip` from `Applications.Backups/`, or just delete
the profile in Options+ like any other.

## How it works

Each profile is a self-contained directory: `ProfileInfo.json` holds the
layout (`workspaces` → `pressPages` → `controls`, each control mapping a
`controlId` to a `pressAction`), the macros, and the keyboard shortcuts.
`ActionIcons/*.ict` are JSON wrapping a base64 SVG plus a text layer.

Keyboard shortcuts are stored in a four-field encoding that carries the
macOS virtual keycode, a modifier mask (the Cocoa flag OR'd with the
device-dependent bit for each left-hand modifier), the character, and the
keyboard layout that was active when the key was made. `logikit` generates it
from a plain `Cmd+Shift+G` string; the encoder was validated by reproducing
every shortcut in a live config byte-for-byte.

Every identifier is an uppercase 32-hex GUID used only inside that directory,
which is why duplication works: remap every GUID consistently and you get an
independent copy. That is the one-line summary of the feature Options+ is
missing.

## Devices

| Slot | Device |
|---|---|
| `Loupedeck70` | MX Creative Keypad (9 LCD keys per page) |
| `Loupedeck71` | MX Creative Dialpad |
| `Loupedeck72` | Actions Ring |

## License

MIT - see [LICENSE](LICENSE).

Unaffiliated with Logitech. Edits your local config; back up anything you care
about.
