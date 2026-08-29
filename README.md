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

Write the keys as text, one per line, `---` between pages:

```
IGCSE    | https://example.com/igcse    | #12233F | #7FB4FF
Practice | https://example.com/practice | #12233F | #7FB4FF
---
CBSE 11  | https://example.com/cbse/11  | #10301F | #6FE3A6
```

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
