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
IGCSE    | https://example.com/igcse | #12233F | #7FB4FF
New Chat | Cmd+N                     | #2B1B12 | #F0A882
Sidebar  | Cmd+Shift+G               | #1A1A1A | #FFFFFF
---
CBSE 11  | https://example.com/cbse  | #10301F | #6FE3A6
```

Modifiers are `Cmd`, `Ctrl`, `Shift` and `Opt`/`Alt`. Keys can be letters,
digits, punctuation, or `Space`, `Tab`, `Return`, `Escape`, `Delete` and
`ArrowUp`/`Down`/`Left`/`Right`.

Registering an app Options+ has never seen:

```bash
logikit add-app --bundle com.example.app --name "Example" \
                --template "Loupedeck70/com.apple.safari"
```

See `codehaven.keys` and `claude.keys` for full two-page examples.

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
