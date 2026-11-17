#!/usr/bin/env python3
"""
XBLA Installer - get the game files out of your own Xbox Live Arcade package, ready for a
recompiled PC port (ReXGlue ports such as Trials HD Recomp or Guardian Heroes Recomp), or just to
extract any Xbox 360 STFS package (LIVE / CON / PIRS).

- Pick your game package (Content/0000000000000000/<TitleID>/000D0000/<long name>).
- It recognises the game from the package, extracts everything into the folder you choose
  (into its "assets" folder if you pick a port's folder), and checks every block of the package.
- For ports it knows (games.json), it checks default.xex is the exact version the port needs,
  and can apply a title update (000B0000 package or default.xexp) with xextool.

Disc images: original Xbox and Xbox 360 ISOs (XGD1/2/3) are extracted too.

Window: just run it (Windows: XBLA-Installer.exe). Command line:
  xbla_installer.py --package <file> [--dest <folder>] [--update <TU or .xexp>] [--xextool <exe>]
  xbla_installer.py --extract-only --package <file> [--dest <folder>]   (just extract, any game)
  xbla_installer.py --info <package, disc image or default.xex>   (which game, which version)
"""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading

VERSION = "1.1.2"


def here():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def bundled(name):
    """A data file shipped inside the exe (PyInstaller) or next to the script."""
    base = getattr(sys, '_MEIPASS', here())
    return os.path.join(base, name)


sys.path.insert(0, here())
sys.path.insert(0, getattr(sys, '_MEIPASS', here()))
from stfs import STFS  # noqa: E402
import xdvdfs  # noqa: E402


# ---------------------------------------------------------------------------------------------
# Game profiles. Built in from games.json; a games.json next to the program adds to / overrides
# them, so anyone can add their own port without rebuilding.

def load_profiles():
    profiles = {}
    for path in (bundled('games.json'), os.path.join(here(), 'games.json')):
        try:
            with open(path, encoding='utf-8') as f:
                for g in json.load(f)['games']:
                    profiles[int(g['title_id'], 16)] = g
        except (OSError, ValueError, KeyError):
            pass
    return profiles


PROFILES = load_profiles()


def vstr(v):
    return f"{v >> 28}.{(v >> 24) & 15}.{(v >> 8) & 0xFFFF}.{v & 255}" if v is not None else "?"


def xex_info(path):
    """(title_id, version, media_id) from the XEX header (plain text, no decryption)."""
    with open(path, 'rb') as f:
        return xex_header_info(f.read(0x10000))


def xex_header_info(d):
    if d[:4] != b'XEX2':
        return None, None, None
    n = struct.unpack('>I', d[20:24])[0]
    for i in range(n):
        key, val = struct.unpack('>II', d[24 + i * 8:32 + i * 8])
        if key == 0x00040006:
            mid, ver, _base, tid = struct.unpack('>IIII', d[val:val + 16])
            return tid, ver, mid
    return None, None, None


def package_info(path):
    """(magic, title_id, content_type, display name, version, base version) from an STFS header."""
    with open(path, 'rb') as f:
        h = f.read(0x1800)
    if h[:4] not in (b'CON ', b'LIVE', b'PIRS'):
        return None
    name = h[0x411:0x411 + 0x80].decode('utf-16-be', 'replace').split('\0')[0].strip()
    ver, base = struct.unpack('>II', h[0x358:0x360])
    return (h[:4].decode().strip(), struct.unpack('>I', h[0x360:0x364])[0],
            struct.unpack('>I', h[0x344:0x348])[0], name, ver, base)


def iso_info(path):
    """(layout, file count, (title, version, media) of default.xex or None) for a disc image."""
    try:
        x = xdvdfs.XDVDFS(path)
    except (ValueError, OSError):
        return None
    ents = list(x.entries())
    xinfo = None
    for p, is_dir, sec, sz in ents:
        if not is_dir and p.lower() == 'default.xex':
            x.f.seek(x.base + sec * xdvdfs.SECTOR)
            xinfo = xex_header_info(x.f.read(min(sz, 0x10000)))
    return x.layout, sum(1 for e in ents if not e[1]), xinfo


CONTENT_TYPES = {
    0x000D0000: "Arcade game", 0x000B0000: "Title update", 0x00000002: "DLC",
    0x00080000: "Game demo", 0x00007000: "Games on Demand", 0x00004000: "Installed disc game",
    0x00009000: "Avatar item", 0x00020000: "Gamer picture", 0x00030000: "Theme",
    0x00000001: "Saved game", 0x00010000: "Gamer profile", 0x00090000: "Video",
    0x00002000: "Game trailer", 0x000F0000: "Community game", 0x00050000: "Xbox Original game",
}
GAME_TYPES = {0x000D0000, 0x00080000, 0x00007000, 0x00004000}   # packages that hold the game itself


def supported_versions(prof):
    v = prof.get('version')
    return [] if v is None else (list(v) if isinstance(v, list) else [v])


def version_verdict(tid, ver):
    """One line comparing a version with what a known port needs ('' if no port/check)."""
    prof = PROFILES.get(tid)
    if not prof or not supported_versions(prof) or ver is None:
        return ''
    if ver in supported_versions(prof):
        return f"Version {vstr(ver)}: supported by the {prof['name']} port."
    need = " or ".join(vstr(v) for v in supported_versions(prof))
    return (f"Version {vstr(ver)}: the {prof['name']} port needs {need}, "
            "so you'll also need the title update.")


def describe_package(path):
    """(info, text) for a package, a disc image or a default.xex. info is None if unusable."""
    p = package_info(path)
    if p:
        magic, tid, ctype, name, ver, base = p
        prof = PROFILES.get(tid)
        kind = CONTENT_TYPES.get(ctype, f"content type {ctype:08X}")
        text = f"{name or 'Unnamed'}  -  title {tid:08X}, {kind} ({magic})"
        if ctype in GAME_TYPES or ctype == 0x000B0000:
            text += f"\nVersion {vstr(ver)}" + (f" (base {vstr(base)})" if base != ver else "")
        if prof:
            newline = ctype not in GAME_TYPES and ctype != 0x000B0000
            text += ("\nKnown port: " if newline else "  -  known port: ") + prof['name']
            if ctype in GAME_TYPES:
                verdict = version_verdict(tid, ver)
                if verdict:
                    text += "\n" + verdict
            elif ctype == 0x00000002:
                text += " (this is add-on content, not the game itself)"
        return ('stfs',) + p, text
    i = iso_info(path)
    if i:
        layout, nfiles, xinfo = i
        text = f"Xbox disc image ({layout}), {nfiles} files"
        if xinfo and xinfo[0] is not None:
            tid, ver, mid = xinfo
            prof = PROFILES.get(tid)
            text += f"\ndefault.xex: title {tid:08X}, version {vstr(ver)}"
            if prof:
                text += f"  -  known port: {prof['name']}\n" + version_verdict(tid, ver)
        return ('iso', layout), text
    try:
        tid, ver, mid = xex_info(path)
    except OSError:
        tid = None
    if tid is not None:
        text = f"default.xex: title {tid:08X}, version {vstr(ver)}, media ID {mid:08X}"
        v = version_verdict(tid, ver)
        return None, text + ("\n" + v if v else "") + "\n(This is the program only; pick the package or disc image to install.)"
    return None, "Not an Xbox 360 package (LIVE/CON/PIRS), Xbox disc image or default.xex."


# ---------------------------------------------------------------------------------------------

def resolve_dest(dest):
    """Picked a port's folder (with its program or an assets folder)? Install into assets."""
    if os.path.basename(os.path.normpath(dest)).lower() == 'assets':
        return dest
    if os.path.isdir(os.path.join(dest, 'assets')):
        return os.path.join(dest, 'assets')
    programs = {g.get('program', '') for g in PROFILES.values()} - {''}
    for prog in programs:
        if os.path.isfile(os.path.join(dest, prog)) or os.path.isfile(os.path.join(dest, prog + '.exe')):
            return os.path.join(dest, 'assets')
    return dest


def extract_package(pkg, out, log):
    s = STFS(pkg)
    ents = s.entries()
    total = sum(e['size'] for e in ents if not e['is_dir']) or 1
    done, written = 0, []
    for e in ents:
        rel = s.path_of(ents, e)
        p = os.path.join(out, *rel.split('/'))
        if e['is_dir']:
            os.makedirs(p, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        remaining = e['size']
        with open(p, 'wb') as fo:
            for b in s.chain(e['start'], e['blocks'], e['consecutive']):
                fo.write(s.read_block(b)[:min(remaining, 0x1000)])
                remaining -= 0x1000
                if remaining <= 0:
                    break
        done += e['size']
        written.append(rel)
        log(f"  {rel}  ({done * 100 // total}%)", progress=True)
    return s, written


def find_xextool(given):
    if given:
        return given
    for name in ('xextool.exe', 'XexTool.exe', 'xextool'):
        for d in (here(), os.path.join(here(), 'tools')):
            if os.path.isfile(os.path.join(d, name)):
                return os.path.join(d, name)
        w = shutil.which(name)
        if w:
            return w
    return None


def apply_update(dest, update, xextool, log):
    xex = os.path.join(dest, 'default.xex')
    with tempfile.TemporaryDirectory() as tmp:
        if update.lower().endswith('.xexp'):
            xexp = update
        else:
            log("Reading the title update package...")
            _, written = extract_package(update, tmp, log)
            cands = [w for w in written if w.lower().endswith('.xexp')]
            pick = [w for w in cands if os.path.basename(w).lower() == 'default.xexp'] or cands
            if not pick:
                raise RuntimeError("That title update package has no default.xexp in it.")
            xexp = os.path.join(tmp, *pick[0].split('/'))
        tool = find_xextool(xextool)
        if not tool:
            raise RuntimeError(
                "Applying a title update needs xextool (xorloser's XexTool). Put xextool.exe "
                "next to this installer, or pick it in the XexTool box, and press Install again.")
        patched = os.path.join(tmp, 'default_patched.xex')
        cmd = [tool, '-p', xexp, '-o', patched, xex]
        if os.name != 'nt' and tool.lower().endswith('.exe') and shutil.which('wine'):
            cmd = ['wine'] + cmd
        log("Applying the title update with xextool...")
        r = subprocess.run(cmd, capture_output=True, text=True)
        for line in (r.stdout + r.stderr).splitlines()[-8:]:
            log("  xextool: " + line)
        if r.returncode != 0 or not os.path.isfile(patched):
            raise RuntimeError("xextool could not apply the update (see the lines above).")
        backup = xex + '.orig'
        if not os.path.exists(backup):
            shutil.copy2(xex, backup)
        shutil.copy2(patched, xex)
        log(f"Updated default.xex (original kept as {os.path.basename(backup)}).")


def default_extract_dir(package, info):
    """'<package folder>/<game name> (<title id>)' for Just extract with no folder picked."""
    if info[0] == 'stfs':
        label = f"{info[4] or 'Package'} ({info[2]:08X})"
    else:
        label = os.path.splitext(os.path.basename(package))[0]
    label = ''.join(c for c in label if c not in '<>:"/\\|?*').strip() or 'extracted'
    return os.path.join(os.path.dirname(os.path.abspath(package)), label)


def install(package, dest, update, xextool, log, extract_only=False):
    if not package or not os.path.isfile(package):
        raise RuntimeError("Pick your game package or disc image first.")
    info, text = describe_package(package)
    if not info:
        raise RuntimeError(text)
    if info[0] == 'stfs' and info[3] not in GAME_TYPES and not extract_only:
        log("This isn't the game itself, so it's just extracted (no game checks).")
        extract_only = True
    if extract_only:
        dest = dest or default_extract_dir(package, info)
        log(text)
        os.makedirs(dest, exist_ok=True)
        log(f"Just extracting into {dest}")
        if info[0] == 'iso':
            xdvdfs.extract(package, dest, log,
                           progress=lambda p, pc: log(f"  {p}  ({pc}%)", progress=True))
        else:
            extract_package(package, dest, log)
        log(f"Done - everything is in {dest}")
        return
    if not dest:
        raise RuntimeError("Pick where to install the files (or tick Just extract).")
    if info[0] == 'stfs' and info[3] == 0x000B0000:
        log("Note: this is a title update package; it's extracted as-is (put it in the "
            "Title update box to apply it to a game).")
    log(text)
    dest = resolve_dest(dest)
    os.makedirs(dest, exist_ok=True)
    log(f"Extracting into {dest}")
    if info[0] == 'iso':
        xdvdfs.extract(package, dest, log,
                       progress=lambda p, pc: log(f"  {p}  ({pc}%)", progress=True))
        log("Extracted every file from the disc image.")
    else:
        extract_package(package, dest, log)
        log("Extracted and verified every block of the package.")

    xex = os.path.join(dest, 'default.xex')
    if not os.path.isfile(xex):
        log("(No default.xex here - nothing to check.)")
        return
    xtid, ver, mid = xex_info(xex)
    tid = xtid if xtid is not None else info[2] if info[0] == 'stfs' else None
    prof = PROFILES.get(tid)
    log(f"default.xex: title {xtid or 0:08X}, version {vstr(ver)}, media ID {mid or 0:08X}")
    if not prof:
        if update:
            apply_update(dest, update, xextool, log)
            _, ver, _ = xex_info(xex)
            log(f"default.xex is now version {vstr(ver)}")
        log("Done. (This game isn't in the list of known ports, so the version wasn't checked.)")
        return

    missing = [n for n in prof.get('required', []) if not os.path.exists(os.path.join(dest, n))]
    if missing:
        raise RuntimeError("The package is missing " + ", ".join(missing) + ".")
    supported = supported_versions(prof)
    need = " or ".join(vstr(v) for v in supported)
    if supported and ver not in supported:
        if not update:
            raise RuntimeError(
                f"Your copy of {prof['name']} is version {vstr(ver)}; the port needs {need} "
                f"(the updated release). Pick your title update (the package from "
                f"{tid:08X}/000B0000/, or a default.xexp) and xextool, and press Install again.")
        apply_update(dest, update, xextool, log)
        _, ver, _ = xex_info(xex)
        log(f"default.xex is now version {vstr(ver)}")
        if ver not in supported:
            raise RuntimeError(f"After the update default.xex is version {vstr(ver)}, still not "
                               f"{need}. That title update is a different one from the "
                               "version the port was built from.")
    prog = prof.get('program')
    log(f"All done - {prof['name']} is ready." + (f" Start {prog} (or {prog}.exe)." if prog else ""))


# ---------------------------------------------------------------------------------------------

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext

    root = tk.Tk()
    root.title(f"XBLA Installer {VERSION}")
    root.minsize(680, 480)
    v = {k: tk.StringVar() for k in ('package', 'dest', 'update', 'xextool', 'detected')}
    v['xextool'].set(find_xextool(None) or '')
    v['detected'].set("Pick a package or disc image to see which game and version it is.")

    def on_package(*_):
        p = v['package'].get().strip()
        if os.path.isfile(p):
            v['detected'].set(describe_package(p)[1])
    v['package'].trace_add('write', on_package)

    rows = [
        ('package', "Game package (Content/.../<TitleID>/000D0000/<long name>) or disc image (.iso)", 'file'),
        ('dest', "Install into (the port's folder or its assets folder, or any folder)", 'dir'),
        ('update', "Title update - only if asked (000B0000 package or default.xexp)", 'file'),
        ('xextool', "xextool.exe - only needed with a title update", 'file'),
    ]
    r = 0
    for key, label, kind in rows:
        tk.Label(root, text=label, anchor='w').grid(row=r, column=0, columnspan=2, sticky='we',
                                                    padx=8, pady=(8, 0))
        tk.Entry(root, textvariable=v[key]).grid(row=r + 1, column=0, sticky='we', padx=8)

        def browse(key=key, kind=kind):
            p = filedialog.askdirectory() if kind == 'dir' else filedialog.askopenfilename()
            if p:
                v[key].set(p)
        tk.Button(root, text="Browse...", command=browse).grid(row=r + 1, column=1, padx=8)
        r += 2
        if key == 'package':
            tk.Label(root, textvariable=v['detected'], anchor='w', justify='left',
                     fg='#1a5fb4').grid(row=r, column=0, columnspan=2, sticky='we', padx=8)
            r += 1
    root.columnconfigure(0, weight=1)

    logbox = scrolledtext.ScrolledText(root, height=12, state='disabled')
    logbox.grid(row=r, column=0, columnspan=2, sticky='nsew', padx=8, pady=8)
    root.rowconfigure(r, weight=1)
    only = tk.BooleanVar(value=False)
    bar = tk.Frame(root)
    bar.grid(row=r + 1, column=0, columnspan=2, pady=(0, 10))
    tk.Checkbutton(bar, text="Just extract (any folder, no game checks; empty folder = next to the file)",
                   variable=only, command=lambda: btn.configure(
                       text="Extract" if only.get() else "Install")).pack(side='left', padx=8)
    btn = tk.Button(bar, text="Install", width=16)
    btn.pack(side='left', padx=8)

    def log(msg, progress=False):
        def put():
            logbox.configure(state='normal')
            if progress and logbox.get('end-2l', 'end-1c').startswith('  '):
                logbox.delete('end-2l', 'end-1c')
            logbox.insert('end', msg + '\n')
            logbox.see('end')
            logbox.configure(state='disabled')
        root.after(0, put)

    def work():
        try:
            install(v['package'].get().strip(), v['dest'].get().strip(),
                    v['update'].get().strip() or None, v['xextool'].get().strip() or None, log,
                    extract_only=only.get())
            root.after(0, lambda: messagebox.showinfo("XBLA Installer", "Finished."))
        except Exception as e:
            msg = str(e)
            log("Problem: " + msg)
            root.after(0, lambda: messagebox.showerror("XBLA Installer", msg))
        finally:
            root.after(0, lambda: btn.configure(state='normal'))

    def start():
        btn.configure(state='disabled')
        threading.Thread(target=work, daemon=True).start()
    btn.configure(command=start)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="Extract Xbox Live Arcade packages for PC ports.")
    ap.add_argument('--package')
    ap.add_argument('--dest')
    ap.add_argument('--update')
    ap.add_argument('--xextool')
    ap.add_argument('--info', help="describe a package, disc image or default.xex and exit")
    ap.add_argument('--extract-only', action='store_true',
                    help="just extract into --dest (default: next to the file), no game checks")
    ap.add_argument('--version', action='version', version=VERSION)
    a = ap.parse_args()

    if a.info:
        print(describe_package(a.info)[1])
        return
    if not a.package:
        try:
            run_gui()
            return
        except ImportError:
            print("(No window toolkit found; on Debian/Ubuntu: sudo apt install python3-tk)")
            a.package = input("Path to your game package file: ").strip().strip('"')
            a.dest = input("Install into folder: ").strip().strip('"')
            a.update = input("Title update file (Enter to skip): ").strip().strip('"') or None

    def log(msg, progress=False):
        print(msg)
    try:
        if a.extract_only:
            install(a.package, a.dest, None, None, log, extract_only=True)
        else:
            install(a.package, a.dest or os.path.join(os.getcwd(), 'extracted'), a.update,
                    a.xextool, log)
    except Exception as e:
        print("Problem: " + str(e))
        sys.exit(1)


if __name__ == '__main__':
    main()
