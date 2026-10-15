"""Colorspace mapping against the live ComfyUI list: python tests/test_colorspace.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "comfygizmos"))
import client, colorspace  # noqa: E402

available = client.colorspaces(client.DEFAULT_HOST)
cases = {
    "default (ACES - ACEScg)": "ACES - ACEScg",          # ACES 1.2 (same names)
    "ACES - ACES2065-1": "ACES - ACES2065-1",
    "ACEScg": "ACES - ACEScg",                           # ACES 1.3 / 2.0 names
    "ACES2065-1": "ACES - ACES2065-1",
    "sRGB Encoded Rec.709 (sRGB)": "Utility - sRGB - Texture",
    "Linear Rec.709 (sRGB)": "Utility - Linear - sRGB",
    "ACEScct": "ACES - ACEScct",
    "linear": "Utility - Linear - sRGB",                 # nuke-default
    "sRGB": "Utility - sRGB - Texture",
}
bad = 0
for nuke_name, want in cases.items():
    got = colorspace.to_comfy(nuke_name, available)
    ok = got == want
    bad += not ok
    print("{:4} {:32} -> {}".format("ok" if ok else "FAIL", nuke_name, got))
sys.exit(bad)
