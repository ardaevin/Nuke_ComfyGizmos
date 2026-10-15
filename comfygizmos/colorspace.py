"""Nuke colorspace name -> the name ComfyUI-OCIO knows.

Exact or punctuation-insensitive matches win. Otherwise a few well-known
spaces are recognised by their core token, so ACES 1.3 / 2.0 names and the
nuke-default config ("linear", "sRGB", "rec709") still land on the right
ACES 1.2 name."""
import re

# (token found in the Nuke name, token to look for in ComfyUI's names) - most specific first
_ALIASES = [
    ("acescct", "acescct"),
    ("acescc", "acescc"),
    ("acescg", "acescg"),
    ("aces2065", "aces2065"),
    ("srgbtexture", "srgbtexture"),
    ("srgbencodedrec709", "srgbtexture"),
    ("linearrec709", "linearsrgb"),
    ("linearsrgb", "linearsrgb"),
    ("scenelinear", "linearsrgb"),    # nuke-default's scene_linear role
    ("linear", "linearsrgb"),         # nuke-default "linear" = linear Rec.709 primaries
    ("rec709", "rec709camera"),       # nuke-default "rec709" = Rec.709 OETF
    ("srgb", "srgbtexture"),          # nuke-default "sRGB"
]


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def unwrap_default(value):
    """'default (ACES - ACEScg)' -> 'ACES - ACEScg'."""
    m = re.match(r"^default \((.*)\)$", (value or "").strip())
    return m.group(1).strip() if m else (value or "").strip()


def _is_role(name):
    """OCIO roles / lower-case aliases ('acescg', 'srgb_texture') - valid, but the full name reads better."""
    return name == name.lower() and " " not in name


def _best(hits):
    return min(hits, key=lambda a: (_is_role(a), len(a))) if hits else None


# OCIO roles: what they point at depends on the config, so they say nothing reliable about the file
_ROLES = {"default", "scene_linear", "compositing_linear", "rendering", "reference", "data", "color_picking",
          "texture_paint", "matte_paint", "compositing_log", "color_timing", "aces_interchange", "cie_xyz_d65_interchange"}


def is_default(nuke_name):
    """Empty, 'default' / 'default (...)' or a role - the Read doesn't say what the file really is.
    What 'default' resolves to depends on the script's root LUT settings, so it is not trusted."""
    raw = (nuke_name or "").strip()
    if not raw or raw.lower().startswith("default"):
        return True
    return unwrap_default(raw).lower() in _ROLES


def to_comfy(nuke_name, available):
    """Best ComfyUI name for nuke_name, or None. Full colorspace names win over roles."""
    name = unwrap_default(nuke_name)
    if name in available and not _is_role(name):
        return name
    n = _norm(name)
    hits = [a for a in available if _norm(a) == n]
    for needle, target in _ALIASES:
        if needle in n:
            hits += [a for a in available if target in _norm(a)]
            break
    return _best(hits)
