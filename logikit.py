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
# keyboard shortcuts
#
# Options+ encodes a shortcut as four ___-separated fields:
#   ControlOrCommand+Shift+KeyT___1033___Cmd+Shift+T___<mac fields>
# where the mac fields are joined by SEP:
#   mac-<virtual keycode> | <modifier mask> | <character> | <keyboard layout>
# Fields 2 and 4 just record which layout was active when the key was made.
# The mask is the Cocoa modifier flag OR'd with the device-dependent bit for
# the left-hand copy of each modifier. Verified by reproducing every shortcut
# already present in a live config byte-for-byte.
# --------------------------------------------------------------------------

SEP = "#\u00a4%&+?"

VK = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26,
    "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38,
    "k": 40, "n": 45, "m": 46,
}

# Punctuation uses a named spec rather than Key<c>. Only Period is confirmed
# against real data; the rest follow the same obvious naming.
PUNCT = {
    ".": ("Period", 47), ",": ("Comma", 43), "/": ("Slash", 44),
    "\\": ("Backslash", 42), ";": ("Semicolon", 41), "'": ("Quote", 39),
    "[": ("BracketLeft", 33), "]": ("BracketRight", 30), "-": ("Minus", 27),
    "=": ("Equal", 24), "`": ("Backquote", 50),
}

NAMED = {
    "Space": (49, " "), "Tab": (48, "\t"), "Return": (36, ""),
    "Escape": (53, ""), "Delete": (51, ""), "ArrowUp": (126, ""),
    "ArrowDown": (125, ""), "ArrowLeft": (123, ""), "ArrowRight": (124, ""),
}

MODS = {  # canonical -> (cocoa flag, left-hand device bit)
    "ControlOrCommand": (0x100000, 0x08), "Alt": (0x80000, 0x20),
    "Control": (0x40000, 0x01), "Shift": (0x20000, 0x02),
}
MOD_ORDER = ["ControlOrCommand", "Alt", "Control", "Shift"]
MOD_ALIAS = {
    "cmd": "ControlOrCommand", "command": "ControlOrCommand",
    "ctrl": "Control", "control": "Control", "shift": "Shift",
    "opt": "Alt", "option": "Alt", "alt": "Alt",
}
MOD_DISPLAY = {"ControlOrCommand": "Cmd", "Alt": "Opt",
               "Control": "Ctrl", "Shift": "Shift"}


def mac_layout():
    """Current keyboard layout id, e.g. com.apple.keylayout.US."""
    try:
        out = subprocess.run(
            ["defaults", "read", os.path.expanduser(
                "~/Library/Preferences/com.apple.HIToolbox.plist"),
             "AppleSelectedInputSources"],
            capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'"KeyboardLayout Name" = "?([^";]+)"?;', out)
        if m:
            return "com.apple.keylayout." + m.group(1).strip().replace(
                "U.S.", "US").replace(" ", "")
    except Exception:
        pass
    return "com.apple.keylayout.US"


def encode_shortcut(spec, hkl=1033, layout=None):
    """'Cmd+Shift+G' -> the keyboardKey string Options+ expects."""
    layout = layout or mac_layout()
    bits = [b for b in spec.split("+") if b]
    key = bits[-1]
    mods = []
    for b in bits[:-1]:
        canon = MOD_ALIAS.get(b.strip().lower())
        if not canon:
            die(f"unknown modifier {b!r} in {spec!r}")
        if canon not in mods:
            mods.append(canon)
    mods = [m for m in MOD_ORDER if m in mods]

    named = next((n for n in NAMED if n.lower() == key.lower()), None)
    if named:
        vk, ch = NAMED[named]
        name, disp = named, named
    elif key in PUNCT:
        name, vk = PUNCT[key]
        ch, disp = key, key
    elif key.lower() in VK:
        vk = VK[key.lower()]
        ch = key.lower()
        name, disp = f"Key{key.upper()}", key.upper()
    else:
        die(f"unsupported key {key!r} in {spec!r}")

    mask = 0
    for m in mods:
        flag, dev = MODS[m]
        mask |= flag | dev
    if named and named.startswith("Arrow"):
        mask |= 0x800000 | 0x200000     # function + numeric-pad, as macOS reports

    f1 = "+".join(mods + [name])
    f3 = "+".join([MOD_DISPLAY[m] for m in mods] + [disp])
    f4 = SEP.join([f"mac-{vk}", str(mask), ch, layout])
    return f"{f1}___{hkl}___{f3}___{f4}"


def make_send_text_action(label, text, use_clipboard=True):
    """A profileAction that sends a block of text, optionally via clipboard."""
    name = f"$@Generic___@ProfileAction___{guid()}"
    return name, {
        "$type": "Loupedeck.Service.ApplicationProfileCommand, "
                 "LoupedeckService",
        "isCommand": True,
        "name": name,
        "templateActionName": "$@Generic___@SendText",
        "actionParameters": {
            "$type": "Loupedeck.ActionEditorActionParameters, PluginApi",
            "parameters": {
                "$type": "Loupedeck.StringDictionaryNoCase, PluginApi",
                "text": text,
                "useClipboard": "true" if use_clipboard else "false",
            },
            "count": 1,
        },
        "displayName": label,
        "description": "Send a block of text",
        "groupName": "",
        "superGroupName": "@macro",
        "isProfileAction": True,
        "isMultiState": False,
        "isResetCommand": False,
        "adjustmentName": None,
        "states": None,
    }


def make_shortcut_action(label, spec):
    """A profileAction that fires a keyboard shortcut."""
    name = f"$@Generic___@ProfileAction___{guid()}"
    return name, {
        "$type": "Loupedeck.Service.ApplicationProfileCommand, "
                 "LoupedeckService",
        "isCommand": True,
        "name": name,
        "templateActionName": "$@Generic___@KeyboardKey",
        "actionParameters": {
            "$type": "Loupedeck.ActionEditorActionParameters, PluginApi",
            "parameters": {
                "$type": "Loupedeck.StringDictionaryNoCase, PluginApi",
                "keyboardKey": encode_shortcut(spec),
            },
            "count": 1,
        },
        "displayName": label,
        "description": "Activate a keyboard shortcut with a single press or "
                       "hold down for continuous use like a keyboard key",
        "groupName": "",
        "superGroupName": "@macro",
        "isProfileAction": True,
        "isMultiState": False,
        "isResetCommand": False,
        "adjustmentName": None,
        "states": None,
    }




# --------------------------------------------------------------------------
# actions
#
# Options+ inlines most actions as "$@Generic___@Verb___argument".  A macro's
# action list can mix these freely, which is how a multi-step chain works.
# (Hand-built profiles also contain bare-GUID references to profile actions;
# those dangle as soon as the referenced action is deleted, so logikit always
# inlines instead.)
# --------------------------------------------------------------------------

SYS_ACTIONS = [
    "MediaPlayPause", "MediaNextTrack", "MediaPrevTrack", "VolumeMute",
    "VolumeUp", "VolumeDown", "BrightnessUp", "BrightnessDown", "Finder",
]


def encode_step(step):
    """One step of a chain -> the string Options+ stores."""
    step = step.strip()
    low = step.lower()
    if low.startswith("app:"):
        return f"$@Generic___@ExecuteApplication___{step[4:].strip()}||"
    if low.startswith("text:"):
        # \n in a keyfile means a real newline in the typed text
        return "$@Generic___@TypeText___" + step[5:].replace("\\n", "\n")
    if low.startswith("wait:"):
        ms = step[5:].strip()
        if not ms.isdigit():
            die(f"wait: needs milliseconds, got {ms!r}")
        return f"$@Generic___@Sleep___{ms}"
    if low.startswith("sys:"):
        return f"$DefaultMac___{step[4:].strip()}"
    if step.startswith("http"):
        return f"$@Generic___@OpenUrl___{step}"
    return f"$@Generic___@KeyboardKey___{encode_shortcut(step)}"


def is_shortcut(target):
    t = target.strip()
    return not (t.startswith("http") or ":" in t.split("+")[0][:6])


# --------------------------------------------------------------------------
# glyphs
#
# 32x32 SVGs drawn in white and tinted at render time by the icon's imageColor,
# so one glyph works in any colour. Keep paths simple - they are rasterised at
# roughly 60px on the key.
# --------------------------------------------------------------------------

def _svg(body):
    return ('<svg width="32" height="32" viewBox="0 0 32 32" '
            'xmlns="http://www.w3.org/2000/svg">' + body + '</svg>')


GLYPHS = {
    "calendar": _svg(
        '<path fill="#fff" d="M10 2a1.5 1.5 0 0 1 1.5 1.5V5h9V3.5a1.5 1.5 0 '
        '0 1 3 0V5H25a4 4 0 0 1 4 4v16a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V9a4 4 0 '
        '0 1 4-4h1.5V3.5A1.5 1.5 0 0 1 10 2Zm16 11H6v12a1 1 0 0 0 1 1h18a1 1 '
        '0 0 0 1-1V13Z"/><rect x="9" y="16" width="4" height="4" rx="1" '
        'fill="#fff"/><rect x="15" y="16" width="4" height="4" rx="1" '
        'fill="#fff"/><rect x="21" y="16" width="4" height="4" rx="1" '
        'fill="#fff"/>'),
    "cap": _svg(
        '<path fill="#fff" d="M16 3 1 11l4 2.1V21a1.5 1.5 0 0 0 3 0v-6.3l8 '
        '4.3 15-8L16 3Z"/><path fill="#fff" d="M9 17.6V22c0 2.2 3.1 4 7 4s7-'
        '1.8 7-4v-4.4l-7 3.7-7-3.7Z"/>'),
    "book": _svg(
        '<path fill="#fff" d="M4 5.5A2.5 2.5 0 0 1 6.5 3H14a3 3 0 0 1 3 3v20a3'
        ' 3 0 0 0-3-3H6.5A2.5 2.5 0 0 1 4 20.5v-15Zm24 0A2.5 2.5 0 0 0 25.5 '
        '3H18a3 3 0 0 0-3 3v20a3 3 0 0 1 3-3h7.5a2.5 2.5 0 0 0 2.5-2.5v-15Z"/>'),
    "code": _svg(
        '<path fill="#fff" d="M11.7 8.3a1.5 1.5 0 0 1 0 2.1L6.1 16l5.6 5.6a1.5'
        ' 1.5 0 1 1-2.1 2.1l-6.7-6.6a1.5 1.5 0 0 1 0-2.2l6.7-6.6a1.5 1.5 0 0 '
        '1 2.1 0Zm8.6 0a1.5 1.5 0 0 1 2.1 0l6.7 6.6a1.5 1.5 0 0 1 0 2.2l-6.7 '
        '6.6a1.5 1.5 0 1 1-2.1-2.1l5.6-5.6-5.6-5.6a1.5 1.5 0 0 1 0-2.1Z"/>'),
    "globe": _svg(
        '<path fill="#fff" d="M16 2a14 14 0 1 0 0 28 14 14 0 0 0 0-28Zm10.9 '
        '12.5h-5.4a21 21 0 0 0-2-8.2 11 11 0 0 1 7.4 8.2ZM16 5.2c1.2 1.7 2.3 '
        '4.9 2.5 9.3h-5c.2-4.4 1.3-7.6 2.5-9.3Zm-3.5.9a21 21 0 0 0-2 8.4H5.1a'
        '11 11 0 0 1 7.4-8.4Zm-7.4 11.4h5.4a21 21 0 0 0 2 8.4 11 11 0 0 1-7.4'
        '-8.4ZM16 26.8c-1.2-1.7-2.3-4.9-2.5-9.3h5c-.2 4.4-1.3 7.6-2.5 9.3Zm3.'
        '5-.9a21 21 0 0 0 2-8.4h5.4a11 11 0 0 1-7.4 8.4Z"/>'),
    "home": _svg(
        '<path fill="#fff" d="M15 3.2a1.5 1.5 0 0 1 2 0l12 10.6a1.5 1.5 0 0 1'
        '-2 2.2l-.5-.4V26a3 3 0 0 1-3 3h-5v-8h-5v8H8a3 3 0 0 1-3-3V15.6l-.5.4'
        'a1.5 1.5 0 1 1-2-2.2L15 3.2Z"/>'),
    "doc": _svg(
        '<path fill="#fff" d="M7 4a3 3 0 0 1 3-3h8.5L27 9.5V28a3 3 0 0 1-3 3H1'
        '0a3 3 0 0 1-3-3V4Zm11 .8V10h5.2L18 4.8ZM11.5 15h9a1.5 1.5 0 0 1 0 3h-'
        '9a1.5 1.5 0 0 1 0-3Zm0 6h9a1.5 1.5 0 0 1 0 3h-9a1.5 1.5 0 0 1 0-3Z"/>'),
    "chart": _svg(
        '<path fill="#fff" d="M5 3a1.5 1.5 0 0 1 1.5 1.5V25a1.5 1.5 0 0 0 1.5 '
        '1.5h20a1.5 1.5 0 0 1 0 3H8a4.5 4.5 0 0 1-4.5-4.5V4.5A1.5 1.5 0 0 1 5 '
        '3Zm6 16a1.5 1.5 0 0 1 1.5 1.5V22a1.5 1.5 0 0 1-3 0v-1.5A1.5 1.5 0 0 1'
        ' 11 19Zm6-8a1.5 1.5 0 0 1 1.5 1.5V22a1.5 1.5 0 0 1-3 0V12.5A1.5 1.5 0'
        ' 0 1 17 11Zm6 4a1.5 1.5 0 0 1 1.5 1.5V22a1.5 1.5 0 0 1-3 0v-5.5A1.5 '
        '1.5 0 0 1 23 15Z"/>'),
    "chat": _svg(
        '<path fill="#fff" d="M16 3c7.7 0 14 4.9 14 11s-6.3 11-14 11c-1.3 0-2.'
        '6-.1-3.8-.4l-6.6 3.3A1 1 0 0 1 4.2 27l1.3-4.6C3.3 20.4 2 17.4 2 14 2 '
        '7.9 8.3 3 16 3Z"/>'),
    "search": _svg(
        '<path fill="#fff" d="M14 2a12 12 0 0 1 9.5 19.4l6.1 6a1.5 1.5 0 0 1-2'
        '.1 2.2l-6.1-6.1A12 12 0 1 1 14 2Zm0 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z"'
        '/>'),
    "star": _svg(
        '<path fill="#fff" d="m16 2 4.2 8.6 9.5 1.4-6.9 6.7 1.7 9.4L16 23.7l-8'
        '.5 4.4 1.7-9.4L2.3 12l9.5-1.4L16 2Z"/>'),
    "clock": _svg(
        '<path fill="#fff" d="M16 2a14 14 0 1 0 0 28 14 14 0 0 0 0-28Zm1.5 6.5'
        'v7.9l5.3 3.1a1.5 1.5 0 1 1-1.5 2.6l-6-3.5a1.5 1.5 0 0 1-.8-1.3V8.5a1.'
        '5 1.5 0 0 1 3 0Z"/>'),
    "folder": _svg(
        '<path fill="#fff" d="M3 7a3 3 0 0 1 3-3h6.2a3 3 0 0 1 2.1.9L16.6 7H26'
        'a3 3 0 0 1 3 3v14a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3V7Z"/>'),
    "play": _svg(
        '<path fill="#fff" d="M11 5.9c0-1.5 1.7-2.5 3-1.7l13 10.1a2 2 0 0 1 0 '
        '3.4l-13 10.1c-1.3.8-3-.2-3-1.7V5.9Z"/>'),
}


def cmd_glyphs(args):
    for name in sorted(GLYPHS):
        print(f"  {name}")


# --------------------------------------------------------------------------
# icons

# --------------------------------------------------------------------------

def make_icon(label, bg="#000000", fg="#FFFFFF", svg_path=None, font_size=5,
              glyph=None):
    items = []
    art = None
    if glyph:
        if glyph not in GLYPHS:
            die(f"unknown glyph {glyph!r} (try: logikit glyphs)")
        art = GLYPHS[glyph].encode()
    elif svg_path:
        with open(svg_path, "rb") as fh:
            art = fh.read()
    if art:
        blob = base64.b64encode(art).decode()
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
        area = ({"x": 0, "y": 65, "width": 100, "height": 30} if art
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
    action = encode_step(args.url)
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
                   make_icon(args.label, args.bg, args.fg, args.svg,
                             glyph=args.glyph))


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
                             args.bg, args.fg, args.svg,
                             glyph=args.glyph))


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
            target = bits[1]
            cur.append({
                "label": bits[0],
                "target": target,
                "steps": [x for x in target.split(">>") if x.strip()],
                "bg": bits[2] if len(bits) > 2 else "#000000",
                "fg": bits[3] if len(bits) > 3 else "#FFFFFF",
                "glyph": bits[4] if len(bits) > 4 else None,
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
        data["profileActions"] = []

    macros = data.setdefault("macroCommands", [])
    actions = data.setdefault("profileActions", [])
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
            steps = item["steps"]
            # A lone keyboard shortcut is a profile action; everything else
            # (a URL, an app, or any chain) becomes a macro.
            one = steps[0].strip()
            if len(steps) == 1 and one.lower().startswith("send:"):
                ref, action = make_send_text_action(
                    item["label"], one[5:].replace("\\n", "\n"))
                actions.append(action)
                page["controls"][slot]["pressAction"] = ref
                icons[ref] = make_icon(item["label"], item["bg"], item["fg"],
                                       glyph=item["glyph"])
                placed += 1
                continue
            if len(steps) == 1 and is_shortcut(steps[0]):
                ref, action = make_shortcut_action(item["label"],
                                                   steps[0].strip())
                actions.append(action)
                page["controls"][slot]["pressAction"] = ref
                icons[ref] = make_icon(item["label"], item["bg"], item["fg"],
                                       glyph=item["glyph"])
                placed += 1
                continue
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
                "actions": [encode_step(x) for x in steps],
            })
            page["controls"][slot]["pressAction"] = ref
            icons[ref] = make_icon(item["label"], item["bg"], item["fg"],
                                   glyph=item["glyph"])
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
                if "@Macro___" in f or "@ProfileAction___" in f:
                    os.remove(os.path.join(idir, f))
        for ref, icon in icons.items():
            write_json(os.path.join(idir, f"{ref}.ict"), icon)


def cmd_add_app(args):
    tpl = resolve(args.template)
    device = tpl["device"]
    dest = os.path.join(APPS, device, args.bundle)
    if os.path.isdir(dest):
        die(f"{args.bundle} already exists on {device}")

    info = {
        "$type": "Loupedeck.Service.SupportedApplicationInfo, LoupedeckService",
        "name": args.bundle,
        "displayName": args.name,
        "description": None,
        "deviceType": device,
        "nativePluginName": None,
        "hasNativePlugin": False,
        "processOrBundleName": args.bundle,
        "modes": [{
            "$type": "Loupedeck.Service.ApplicationMode, LoupedeckService",
            "name": "main",
            "parentModeName": None,
            "displayName": "Main",
        }],
        "defaultProfileName": None,
        "isEnabled": True,
    }

    # Start from a real profile so the layout matches the device exactly,
    # then strip every binding out of the copy.
    raw = open(tpl["info"], encoding="utf-8-sig").read()
    mapping = {g: guid() for g in set(GUID_RE.findall(raw))}
    for old, new in mapping.items():
        raw = raw.replace(old, new)
    prof = json.loads(raw)
    pid = mapping.get(tpl["id"]) or guid()
    prof["name"] = pid
    prof["displayName"] = args.profile_name or f"{args.name} Profile"
    prof["applicationName"] = args.bundle
    prof["macroCommands"] = []
    prof["profileActions"] = []
    prof["lastModifiedTimeUtc"] = utc_now()
    for _, _, _, ctl in bound_controls(prof):
        ctl["pressAction"] = None
        ctl["rotateAction"] = None

    print(f'register "{args.name}" ({args.bundle}) on {device}')
    print(f'  empty profile "{prof["displayName"]}" -> {pid}')
    if args.dry_run:
        return
    with Session(args):
        os.makedirs(os.path.join(dest, "Profiles", pid))
        write_json(os.path.join(dest, "ApplicationInfo.json"), info)
        write_json(os.path.join(dest, "Profiles", pid, "ProfileInfo.json"),
                   prof)
        os.makedirs(os.path.join(dest, "Profiles", pid, "ActionIcons"))
        if args.icon and os.path.isfile(args.icon):
            shutil.copy2(args.icon,
                         os.path.join(dest, "ApplicationIcon.png"))
            print("  copied application icon")


def cmd_preview(args):
    """Render a page's real .ict files to an SVG contact sheet."""
    p = resolve(args.profile)
    pages = []
    for _, _, pg, ctl in bound_controls(p["data"]):
        if not pages or pages[-1][0] is not pg:
            pages.append((pg, []))
        pages[-1][1].append(ctl)
    if not pages:
        die(f'"{p["name"]}" has no pages')
    idx = max(1, min(args.page, len(pages)))
    page, controls = pages[idx - 1]

    def rgb(v):
        return f"#{(v >> 16) & 255:02X}{(v >> 8) & 255:02X}{v & 255:02X}"

    K, GAP, PAD, TOP = 128, 12, 18, 44
    cols = 3 if len(controls) > 4 else len(controls) or 1
    rows = (len(controls) + cols - 1) // cols
    W = PAD * 2 + cols * K + (cols - 1) * GAP
    H = TOP + PAD + rows * K + (rows - 1) * GAP
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" '
           f'height="{H}" viewBox="0 0 {W} {H}">',
           f'<rect width="{W}" height="{H}" fill="#0A0B0D" rx="18"/>',
           f'<text x="{W / 2}" y="27" fill="#9BA1A8" font-size="12" '
           f'font-family="-apple-system,system-ui,sans-serif" '
           f'text-anchor="middle">{p["name"]} &#183; '
           f'{page["displayName"]}</text>']

    for i, ctl in enumerate(controls):
        r, c = divmod(i, cols)
        x, y = PAD + c * (K + GAP), TOP + r * (K + GAP)
        ref = ctl.get("pressAction")
        path = os.path.join(p["dir"], "ActionIcons", f"{ref}.ict")
        if not ref or not os.path.isfile(path):
            out.append(f'<rect x="{x}" y="{y}" width="{K}" height="{K}" '
                       f'rx="10" fill="#151719"/>')
            continue
        ic = read_json(path)
        out.append(f'<rect x="{x}" y="{y}" width="{K}" height="{K}" rx="10" '
                   f'fill="{rgb(ic["backgroundColor"])}"/>')
        for it in ic["items"]:
            a = it["area"]
            ax, ay = x + K * a["x"] / 100, y + K * a["y"] / 100
            aw, ah = K * a["width"] / 100, K * a["height"] / 100
            if it["itemType"] == "Image":
                art = base64.b64decode(it["image"]).decode()
                inner = re.sub(r"^<svg[^>]*>|</svg>$", "", art)
                inner = inner.replace("#fff", rgb(it["imageColor"]))
                side = min(aw, ah)
                out.append(
                    f'<g transform="translate({ax + (aw - side) / 2:.1f},'
                    f'{ay + (ah - side) / 2:.1f}) scale({side / 32:.3f})">'
                    f'{inner}</g>')
            else:
                txt = (it["text"].replace("&", "&amp;").replace("<", "&lt;")
                       .replace(">", "&gt;"))
                out.append(
                    f'<text x="{ax + aw / 2:.1f}" y="{ay + ah / 2 + 5:.1f}" '
                    f'fill="{rgb(it["textColor"])}" font-size="14" '
                    f'font-family="-apple-system,system-ui,sans-serif" '
                    f'text-anchor="middle">{txt}</text>')
    out.append("</svg>")

    dest = args.out or f"preview-{idx}.svg"
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print(f'wrote {dest} ({len(controls)} keys, page {idx} of {len(pages)})')


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
        p.add_argument("--glyph", help="built-in glyph name (see: glyphs)")

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
                   help="lines of 'Label | URL-or-shortcut [| #bg [| #fg]]'")
    p.add_argument("--replace", action="store_true",
                   help="clear existing keys and macros first")
    writable(p)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("add-app", help="register an app Options+ doesn't list")
    p.add_argument("--bundle", required=True, help="e.g. com.example.app")
    p.add_argument("--name", required=True, help="display name")
    p.add_argument("--template", required=True,
                   help="existing profile to copy the device layout from")
    p.add_argument("--profile-name")
    p.add_argument("--icon", help="256x256 PNG")
    writable(p)
    p.set_defaults(func=cmd_add_app)

    p = sub.add_parser("preview", help="render a page to an SVG you can look at")
    p.add_argument("profile")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--out")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("actions", help="list built-in system actions")
    p.set_defaults(func=lambda a: [print("  sys:" + n) for n in SYS_ACTIONS])

    p = sub.add_parser("glyphs", help="list built-in icon glyphs")
    p.set_defaults(func=cmd_glyphs)

    p = sub.add_parser("service", help="control LogiPluginService")
    p.add_argument("op", choices=["status", "stop", "start", "restart"])
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("backup", help="zip the profile store")
    p.set_defaults(func=cmd_backup)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
