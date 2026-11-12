"""Start the installer window with the Python that ships inside Nuke (install.bat / install.command call this).

Nuke's python finds its own Qt (PySide2 / PySide6) only once its site-packages, Qt plugins and DLL folder are
set up the way Nuke itself does it; this does that, then opens installer_gui."""
import glob
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
exe_dir = os.path.dirname(os.path.realpath(sys.executable))


def _first(patterns):
    for pattern in patterns:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


# Windows: <Nuke>/python.exe, <Nuke>/pythonextensions/site-packages, <Nuke>/qtplugins
# macOS / Linux: the same folders somewhere above the python executable
roots = [exe_dir, os.path.dirname(exe_dir), os.path.dirname(os.path.dirname(exe_dir))]
site = _first([os.path.join(r, "pythonextensions", "site-packages") for r in roots] +
              [os.path.join(r, "*", "pythonextensions", "site-packages") for r in roots])
plugins = _first([os.path.join(r, "qtplugins") for r in roots] + [os.path.join(r, "*", "qtplugins") for r in roots] +
                 [os.path.join(r, "PlugIns") for r in roots])
if site and site not in sys.path:
    sys.path.insert(0, site)
if plugins:
    os.environ.setdefault("QT_PLUGIN_PATH", plugins)
if hasattr(os, "add_dll_directory"):
    os.add_dll_directory(exe_dir)
sys.path.insert(0, here)

try:
    import installer_gui
except ImportError as e:
    sys.exit("Couldn't load Qt from this Python ({}): {}\nRun install.bat / install.command, which use the Python "
             "inside Nuke, or start installer_gui.py with a Python that has PySide2 or PySide6.".format(sys.executable, e))
installer_gui.main()
