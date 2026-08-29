#!/usr/bin/env python3
"""logikit - profile-as-code for the Logitech MX Creative Console.

Options+ stores every profile as plain JSON under
    ~/Library/Application Support/Logi/LogiPluginService/Applications/
This tool edits that store directly, so it can do the things the official
app refuses to: duplicate a profile, bulk-rewrite URLs, add keys in one shot.

Every write takes a timestamped backup first and restarts the plugin service
so the changes are picked up.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone

ROOT = os.path.expanduser(
    "~/Library/Application Support/Logi/LogiPluginService"
)
APPS = os.path.join(ROOT, "Applications")
BACKUPS = os.path.join(ROOT, "Applications.Backups")
SERVICE_APP = "/Applications/Utilities/LogiPluginService.app"

GUID_RE = re.compile(r"[0-9A-F]{32}")
DEVICE_NAMES = {
    "Loupedeck70": "MX Creative Keypad",
    "Loupedeck71": "MX Creative Dialpad",
    "Loupedeck72": "Actions Ring",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def guid():
    return uuid.uuid4().hex.upper()


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def read_json(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4)


def argb(spec, default=0xFF000000):
    """'#RRGGBB' / '#AARRGGBB' / '' -> ARGB uint32."""
    if not spec:
        return default
    s = spec.lstrip("#")
    if len(s) == 6:
        s = "FF" + s
    if len(s) != 8:
        die(f"bad colour {spec!r}, expected #RRGGBB or #AARRGGBB")
    return int(s, 16)


# --------------------------------------------------------------------------
# service control
# --------------------------------------------------------------------------

def service_running():
    return subprocess.run(
        ["pgrep", "-f", "LogiPluginService"],
        capture_output=True,
    ).returncode == 0


def stop_service():
    if not service_running():
        return False
    subprocess.run(
        ["osascript", "-e", 'quit app "LogiPluginService"'],
        capture_output=True,
    )
    for _ in range(20):
        if not service_running():
            return True
        time.sleep(0.25)
    subprocess.run(["pkill", "-f", "LogiPluginService"], capture_output=True)
    time.sleep(1)
    return True


def start_service():
    subprocess.run(["open", "-a", SERVICE_APP], capture_output=True)


def backup(tag="logikit"):
    os.makedirs(BACKUPS, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(BACKUPS, f"{tag}_{stamp}.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _, files in os.walk(APPS):
            for name in files:
                full = os.path.join(base, name)
                zf.write(full, os.path.relpath(full, APPS))
    return path


class Session:
    """Backup + stop service around a batch of writes, then restart."""

    def __init__(self, args):
        self.dry = args.dry_run
        self.restart = not args.no_restart
        self.was_running = False

    def __enter__(self):
        if self.dry:
            print("(dry run - nothing will be written)")
            return self
        print(f"backup -> {backup()}")
        if self.restart:
            self.was_running = stop_service()
            if self.was_running:
                print("stopped LogiPluginService")
        return self

    def __exit__(self, *exc):
        if not self.dry and self.was_running:
            start_service()
            print("restarted LogiPluginService")
        return False


# --------------------------------------------------------------------------
# profile discovery
# --------------------------------------------------------------------------

def all_profiles():
    out = []
    if not os.path.isdir(APPS):
        die(f"no Options+ profile store at {APPS}")
    for device in sorted(os.listdir(APPS)):
        ddir = os.path.join(APPS, device)
        if not os.path.isdir(ddir):
            continue
        for app in sorted(os.listdir(ddir)):
            pdir = os.path.join(ddir, app, "Profiles")
            if not os.path.isdir(pdir):
                continue
            for pid in sorted(os.listdir(pdir)):
                info = os.path.join(pdir, pid, "ProfileInfo.json")
                if not os.path.isfile(info):
                    continue
                data = read_json(info)
                out.append({
                    "device": device,
                    "app": app,
                    "id": pid,
                    "dir": os.path.join(pdir, pid),
                    "info": info,
                    "name": data.get("displayName") or pid,
                    "data": data,
                })
    return out


def bound_controls(data):
    """Yield (mode, workspace, page, control) for every control."""
    for lm in (data.get("layout") or {}).get("layoutModes") or []:
        for ws in lm.get("workspaces") or []:
            for pg in ws.get("pressPages") or []:
                for ctl in pg.get("controls") or []:
                    yield lm, ws, pg, ctl


def resolve(ref):
    """Match 'device/app/id' or any unique substring of that or the name."""
    profiles = all_profiles()
    needle = ref.lower()
    hits = []
    for p in profiles:
        key = f"{p['device']}/{p['app']}/{p['id']}".lower()
        if needle in key or needle in p["name"].lower():
            hits.append(p)
    # An exact name or id wins over substring matches.
    exact = [h for h in hits
             if h["name"].lower() == needle or h["id"].lower() == needle]
    if exact:
        hits = exact

    if not hits:
        die(f"no profile matching {ref!r} (try: logikit.py list)")
    if len(hits) > 1:
        lines = "\n".join(
            f"  {h['device']}/{h['app']}/{h['id']}  \"{h['name']}\""
            for h in hits
        )
        die(f"{ref!r} is ambiguous, matches:\n{lines}")
    return hits[0]


# --------------------------------------------------------------------------
# icons
# --------------------------------------------------------------------------

def make_icon(label, bg="#000000", fg="#FFFFFF", svg_path=None, font_size=5):
    items = []
    if svg_path:
        with open(svg_path, "rb") as fh:
            blob = base64.b64encode(fh.read()).decode()
        items.append({
            "image": blob,
            "imageFileName": None,
            "imageColor": argb(fg, 0xFFFFFFFF),
            "imageRotation": "None",
            "isVisible": True,
            "itemType": "Image",
            "area": {"x": 17, "y": 0, "width": 65, "height": 65},
        })
    if label:
        area = ({"x": 0, "y": 65, "width": 100, "height": 30} if svg_path
                else {"x": 0, "y": 30, "width": 100, "height": 40})
        items.append({
            "text": label,
            "originalText": None,
            "textColor": argb(fg, 0xFFFFFFFF),
            "fontSize": font_size,
            "fontName": "Brown Logitech Pan Light",
            "isVisible": True,
            "itemType": "Text",
            "area": area,
        })
    return {"backgroundColor": argb(bg), "items": items}


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_list(args):
    for p in all_profiles():
        if args.device and args.device.lower() not in p["device"].lower():
            continue
        if args.app and args.app.lower() not in p["app"].lower():
            continue
        d = p["data"]
        macros = len(d.get("macroCommands") or [])
        acts = len(d.get("profileActions") or [])
        keys = sum(
            1 for *_, c in bound_controls(d)
            if c.get("pressAction") or c.get("rotateAction")
        )
        dev = DEVICE_NAMES.get(p["device"], p["device"])
        print(f'{p["device"]}/{p["app"]}/{p["id"]}')
        print(f'    "{p["name"]}"  [{dev}]  '
              f'macros={macros} actions={acts} keys={keys}')


def cmd_show(args):
    p = resolve(args.profile)
    d = p["data"]
    print(f'{p["name"]}  ({p["device"]}/{p["app"]}/{p["id"]})')

    by_name = {}
    for m in d.get("macroCommands") or []:
        by_name[f'$@Generic___@Macro___{m["name"]}'] = (
            m["displayName"], " ; ".join(m.get("actions") or [])
        )
    for a in d.get("profileActions") or []:
        params = (a.get("actionParameters") or {}).get("parameters") or {}
        detail = ", ".join(
            f"{k}={v}" for k, v in params.items() if not k.startswith("$")
        )
        by_name[a["name"]] = (a.get("displayName") or a["name"], detail)

    for _, ws, pg, ctl in bound_controls(d):
        act = ctl.get("pressAction") or ctl.get("rotateAction")
        if not act:
            continue
        label, detail = by_name.get(act, (act.split("___")[-1], act))
        kind = "press" if ctl.get("pressAction") else "rotate"
        print(f'  [{ws["displayName"]} / {pg["displayName"]}] '
              f'{kind} {ctl["controlId"]}: {label}')
        if detail:
            print(f'        {detail[:100]}')


def cmd_duplicate(args):
    src = resolve(args.profile)
    raw = open(src["info"], encoding="utf-8-sig").read()

    # Every identifier in a profile is a self-contained 32-hex GUID, so a
    # consistent remap gives a clean, independent copy.
    mapping = {g: guid() for g in set(GUID_RE.findall(raw))}
    new_id = mapping.get(src["id"]) or guid()
    mapping[src["id"]] = new_id

    for old, new in mapping.items():
        raw = raw.replace(old, new)
    data = json.loads(raw)
    data["name"] = new_id
    data["displayName"] = args.name
    data["lastModifiedTimeUtc"] = utc_now()

    target_app = args.to_app or src["app"]
    dest = os.path.join(APPS, src["device"], target_app, "Profiles", new_id)

    print(f'duplicate "{src["name"]}" -> "{args.name}"')
    print(f'  {src["device"]}/{target_app}/{new_id}')
    print(f'  remapped {len(mapping)} identifiers')

    if args.dry_run:
        return
    with Session(args):
        if not os.path.isdir(os.path.dirname(dest)):
            die(f"target app {target_app!r} has no Profiles dir on "
                f"{src['device']} - create it once in Options+ first")
        os.makedirs(dest)
        write_json(os.path.join(dest, "ProfileInfo.json"), data)

        icons = os.path.join(src["dir"], "ActionIcons")
        if os.path.isdir(icons):
            dest_icons = os.path.join(dest, "ActionIcons")
            os.makedirs(dest_icons)
            for name in os.listdir(icons):
                new_name = name
                for old, new in mapping.items():
                    new_name = new_name.replace(old, new)
                shutil.copy2(os.path.join(icons, name),
                             os.path.join(dest_icons, new_name))
            print(f'  copied {len(os.listdir(dest_icons))} icons')


def cmd_add_url(args):
    p = resolve(args.profile)
    data = p["data"]
    mid = guid()
    action = f"$@Generic___@OpenUrl___{args.url}"
    ref = f"$@Generic___@Macro___{mid}"

    modes = [lm.get("modeName") or "main"
             for lm in (data.get("layout") or {}).get("layoutModes") or []]
    data.setdefault("macroCommands", []).append({
        "$type": "Loupedeck.Service.ApplicationProfileMacroCommand, "
                 "LoupedeckService",
        "isCommand": True,
        "name": mid,
        "displayName": args.label,
        "description": "",
        "groupName": "",
        "superGroupName": "@macro",
        "supportedOs": "All",
        "supportedModes": modes or ["main"],
        "showAsSingleAction": True,
        "actionEditorCommands": [],
        "isMultiState": False,
        "actions": [action],
    })

    slot = None
    for _, ws, pg, ctl in bound_controls(data):
        if args.key is not None:
            if ctl["controlId"] == args.key:
                slot = (ws, pg, ctl)
                break
        elif not ctl.get("pressAction") and not ctl.get("rotateAction"):
            slot = (ws, pg, ctl)
            break
    if slot is None:
        die("no free key found (use --key N to overwrite a specific one)")

    ws, pg, ctl = slot
    old = ctl.get("pressAction")
    ctl["pressAction"] = ref
    data["lastModifiedTimeUtc"] = utc_now()

    print(f'add "{args.label}" -> {args.url}')
    print(f'  {p["name"]}: key {ctl["controlId"]} '
          f'({ws["displayName"]} / {pg["displayName"]})'
          + (f' [replacing {old}]' if old else ''))

    if args.dry_run:
        return
    with Session(args):
        write_json(p["info"], data)
        icons = os.path.join(p["dir"], "ActionIcons")
        os.makedirs(icons, exist_ok=True)
        write_json(os.path.join(icons, f"{ref}.ict"),
                   make_icon(args.label, args.bg, args.fg, args.svg))


def cmd_set_url(args):
    changed = []
    targets = ([resolve(args.profile)] if args.profile
               else all_profiles())
    for p in targets:
        data = p["data"]
        hits = 0
        for m in data.get("macroCommands") or []:
            acts = m.get("actions") or []
            for i, a in enumerate(acts):
                if args.find in a:
                    acts[i] = a.replace(args.find, args.replace)
                    hits += 1
        if hits:
            changed.append((p, data, hits))
            print(f'{p["device"]}/{p["app"]}  "{p["name"]}": {hits} action(s)')

    if not changed:
        print(f"no actions contain {args.find!r}")
        return
    if args.dry_run:
        return
    with Session(args):
        for p, data, _ in changed:
            write_json(p["info"], data)


def cmd_icon(args):
    p = resolve(args.profile)
    data = p["data"]
    target = None
    for m in data.get("macroCommands") or []:
        if args.label.lower() in m["displayName"].lower():
            target = f'$@Generic___@Macro___{m["name"]}'
            break
    if not target:
        die(f"no macro matching {args.label!r} in \"{p['name']}\"")
    print(f'restyle "{args.label}" in "{p["name"]}"')
    if args.dry_run:
        return
    with Session(args):
        icons = os.path.join(p["dir"], "ActionIcons")
        os.makedirs(icons, exist_ok=True)
        write_json(os.path.join(icons, f"{target}.ict"),
                   make_icon(args.new_label or args.label,
                             args.bg, args.fg, args.svg))


def cmd_unbind(args):
    p = resolve(args.profile)
    data = p["data"]

    slot = None
    for _, ws, pg, ctl in bound_controls(data):
        if ctl["controlId"] == args.key:
            slot = (ws, pg, ctl)
            break
    if slot is None:
        die(f"no control {args.key} in \"{p['name']}\"")

    ws, pg, ctl = slot
    ref = ctl.get("pressAction") or ctl.get("rotateAction")
    if not ref:
        print(f'key {args.key} is already empty')
        return
    ctl["pressAction"] = None
    ctl["rotateAction"] = None

    # Drop the macro too, unless another key still points at it.
    still_used = any(
        c.get("pressAction") == ref or c.get("rotateAction") == ref
        for *_, c in bound_controls(data)
    )
    dropped = None
    if not still_used and ref.startswith("$@Generic___@Macro___"):
        mid = ref.rsplit("___", 1)[-1]
        macros = data.get("macroCommands") or []
        for i, m in enumerate(macros):
            if m["name"] == mid:
                dropped = macros.pop(i)["displayName"]
                break

    data["lastModifiedTimeUtc"] = utc_now()
    print(f'unbind key {args.key} from "{p["name"]}" '
          f'({ws["displayName"]} / {pg["displayName"]})')
    if dropped:
        print(f'  also removed now-unused macro "{dropped}"')
    elif still_used:
        print(f'  macro kept, another key still uses it')

    if args.dry_run:
        return
    with Session(args):
        write_json(p["info"], data)
        if dropped:
            icon = os.path.join(p["dir"], "ActionIcons", f"{ref}.ict")
            if os.path.isfile(icon):
                os.remove(icon)


def blank_page(model, name, display):
    """Clone the shape of an existing page so control count stays device-correct."""
    return {
        "$type": model["$type"],
        "name": name,
        "displayName": display,
        "description": None,
        "controls": [
            {
                "$type": c["$type"],
                "controlId": c["controlId"],
                "pressAction": None,
                "rotateAction": None,
            }
            for c in model["controls"]
        ],
    }


def first_workspace(data):
    for lm in (data.get("layout") or {}).get("layoutModes") or []:
        for ws in lm.get("workspaces") or []:
            return lm, ws
    die("profile has no workspace")


def cmd_add_page(args):
    p = resolve(args.profile)
    data = p["data"]
    _, ws = first_workspace(data)
    pages = ws["pressPages"]
    page = blank_page(pages[0], guid(), args.name or f"Page ({len(pages) + 1})")
    pages.append(page)
    data["lastModifiedTimeUtc"] = utc_now()
    print(f'add page "{page["displayName"]}" '
          f'({len(page["controls"])} keys) to "{p["name"]}"')
    if args.dry_run:
        return
    with Session(args):
        write_json(p["info"], data)


def parse_keyfile(path):
    """Lines of 'Label | URL | #bg'. '---' starts a new page. # comments."""
    pages, cur = [], []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("---"):
                if cur:
                    pages.append(cur)
                cur = []
                continue
            bits = [b.strip() for b in line.split("|")]
            if len(bits) < 2:
                die(f"{path}:{n}: expected 'Label | URL [| #bg]'")
            cur.append({
                "label": bits[0],
                "url": bits[1],
                "bg": bits[2] if len(bits) > 2 else "#000000",
                "fg": bits[3] if len(bits) > 3 else "#FFFFFF",
            })
    if cur:
        pages.append(cur)
    return pages


def cmd_build(args):
    p = resolve(args.profile)
    data = p["data"]
    groups = parse_keyfile(args.file)
    _, ws = first_workspace(data)
    pages = ws["pressPages"]
    per_page = len(pages[0]["controls"])

    if args.replace:
        for pg in pages:
            for c in pg["controls"]:
                c["pressAction"] = None
        data["macroCommands"] = []

    macros = data.setdefault("macroCommands", [])
    modes = [lm.get("modeName") or "main"
             for lm in (data.get("layout") or {}).get("layoutModes") or []]
    icons = {}
    placed = 0

    for gi, group in enumerate(groups):
        if len(group) > per_page:
            die(f"group {gi + 1} has {len(group)} keys, page holds {per_page}")
        while len(pages) <= gi:
            pages.append(blank_page(pages[0], guid(),
                                    f"Page ({len(pages) + 1})"))
        page = pages[gi]
        for slot, item in enumerate(group):
            mid = guid()
            ref = f"$@Generic___@Macro___{mid}"
            macros.append({
                "$type": "Loupedeck.Service.ApplicationProfileMacroCommand, "
                         "LoupedeckService",
                "isCommand": True,
                "name": mid,
                "displayName": item["label"],
                "description": "",
                "groupName": "",
                "superGroupName": "@macro",
                "supportedOs": "All",
                "supportedModes": modes or ["main"],
                "showAsSingleAction": True,
                "actionEditorCommands": [],
                "isMultiState": False,
                "actions": [f"$@Generic___@OpenUrl___{item['url']}"],
            })
            page["controls"][slot]["pressAction"] = ref
            icons[ref] = make_icon(item["label"], item["bg"], item["fg"])
            placed += 1
        print(f'  page {gi + 1} "{page["displayName"]}": '
              + ", ".join(i["label"] for i in group))

    data["lastModifiedTimeUtc"] = utc_now()
    print(f'build "{p["name"]}": {placed} keys across {len(groups)} page(s)')

    if args.dry_run:
        return
    with Session(args):
        write_json(p["info"], data)
        idir = os.path.join(p["dir"], "ActionIcons")
        os.makedirs(idir, exist_ok=True)
        if args.replace:
            for f in os.listdir(idir):
                if "@Macro___" in f:
                    os.remove(os.path.join(idir, f))
        for ref, icon in icons.items():
            write_json(os.path.join(idir, f"{ref}.ict"), icon)


def cmd_service(args):
    if args.op == "status":
        print("running" if service_running() else "stopped")
    elif args.op == "stop":
        print("stopped" if stop_service() else "was not running")
    elif args.op == "start":
        start_service()
        print("started")
    else:
        stop_service()
        start_service()
        print("restarted")


def cmd_backup(args):
    print(backup())


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="logikit",
        description="Profile-as-code for the Logitech MX Creative Console.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def writable(p):
        p.add_argument("-n", "--dry-run", action="store_true",
                       help="show what would change, write nothing")
        p.add_argument("--no-restart", action="store_true",
                       help="do not stop/start LogiPluginService")

    def styled(p):
        p.add_argument("--bg", default="#000000", help="key background colour")
        p.add_argument("--fg", default="#FFFFFF", help="label/icon colour")
        p.add_argument("--svg", help="SVG file to embed above the label")

    p = sub.add_parser("list", help="list every profile")
    p.add_argument("--device")
    p.add_argument("--app")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show a profile's key bindings")
    p.add_argument("profile")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("duplicate",
                       help="copy a profile with fresh identifiers")
    p.add_argument("profile")
    p.add_argument("--name", required=True, help="display name for the copy")
    p.add_argument("--to-app", help="copy into a different app instead")
    writable(p)
    p.set_defaults(func=cmd_duplicate)

    p = sub.add_parser("add-url", help="bind a URL to a key")
    p.add_argument("profile")
    p.add_argument("--label", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--key", type=int, help="control id (default: first free)")
    styled(p)
    writable(p)
    p.set_defaults(func=cmd_add_url)

    p = sub.add_parser("set-url", help="bulk find/replace inside URL actions")
    p.add_argument("--find", required=True)
    p.add_argument("--replace", required=True)
    p.add_argument("--profile", help="limit to one profile")
    writable(p)
    p.set_defaults(func=cmd_set_url)

    p = sub.add_parser("icon", help="restyle a key's icon")
    p.add_argument("profile")
    p.add_argument("--label", required=True, help="macro to restyle")
    p.add_argument("--new-label", help="change the visible text too")
    styled(p)
    writable(p)
    p.set_defaults(func=cmd_icon)

    p = sub.add_parser("unbind", help="clear a key and drop its macro")
    p.add_argument("profile")
    p.add_argument("--key", type=int, required=True, help="control id")
    writable(p)
    p.set_defaults(func=cmd_unbind)

    p = sub.add_parser("add-page", help="append a blank page of keys")
    p.add_argument("profile")
    p.add_argument("--name")
    writable(p)
    p.set_defaults(func=cmd_add_page)

    p = sub.add_parser("build", help="build pages of keys from a text file")
    p.add_argument("profile")
    p.add_argument("--file", required=True,
                   help="lines of 'Label | URL [| #bg [| #fg]]', --- per page")
    p.add_argument("--replace", action="store_true",
                   help="clear existing keys and macros first")
    writable(p)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("service", help="control LogiPluginService")
    p.add_argument("op", choices=["status", "stop", "start", "restart"])
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("backup", help="zip the profile store")
    p.set_defaults(func=cmd_backup)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
