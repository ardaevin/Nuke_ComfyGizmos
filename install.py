"""Copy the comfygizmos package into ~/.nuke/python (replacing the previous copy) - the manual way.
Most people should run install.bat / install.command instead: that installer also sets up ComfyUI, the models
and menu.py.

    python install.py [target_dir]

menu.py needs this block once (Nodes > ComfyUI > one entry per workflow):

    try:
        import comfygizmos
        comfygizmos.register_menu()
    except Exception as _err:
        nuke.tprint('comfygizmos failed to load: %s' % _err)
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def main(target):
    src = os.path.join(HERE, "comfygizmos")
    dst = os.path.join(target, "comfygizmos")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=IGNORE)
    print("installed:", dst)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.nuke/python"))
