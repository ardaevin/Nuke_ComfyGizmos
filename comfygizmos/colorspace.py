"""Colorspace name in one OCIO config -> the same colorspace's name in another.

Works across ACES 1.0-1.2 ("ACES - ACEScg", "Utility - sRGB - Texture"), ACES 1.3 / 2.0 (the OCIO studio and cg
configs: "ACEScg", "sRGB Encoded Rec.709 (sRGB)") and nuke-default ("linear", "sRGB", "rec709"), in either
direction - Nuke and ComfyUI may run different configs. Exact or punctuation-insensitive matches win, then the
alias groups below, then a few well-known spaces recognised by their core token."""
import re

# Equivalent names: each OCIO 2 studio-config colorspace with its aliases, which include the ACES 1.x names.
# Taken from studio-config-latest and Nuke 17's studio configs v2.2.0 (ACES 1.3) and v3.0.0 (ACES 2.0).
_GROUPS = [
    ("ACES2065-1", "aces2065_1", "aces", "ACES - ACES2065-1", "lin_ap0", "lin_ap0_scene"),
    ("ACEScc", "ACES - ACEScc", "acescc_ap1"),
    ("ACEScct", "ACES - ACEScct", "acescct_ap1"),
    ("ACEScg", "ACES - ACEScg", "lin_ap1", "lin_ap1_scene"),
    ("ADX10", "Input - ADX - ADX10"),
    ("ADX16", "Input - ADX - ADX16"),
    ("ARRI LogC3 (EI800)", "arri_logc3_ei800", "logc3ei800_alexawide", "Input - ARRI - V3 LogC (EI800) - Wide Gamut",
     "AlexaV3LogC"),
    ("Linear ARRI Wide Gamut 3", "lin_arri_wide_gamut_3", "lin_alexawide", "Input - ARRI - Linear - ALEXA Wide Gamut"),
    ("ARRI LogC4", "arri_logc4", "ARRILogC4"),
    ("Linear ARRI Wide Gamut 4", "lin_arri_wide_gamut_4", "lin_awg4"),
    ("CanonLog2 CinemaGamut D55", "canonlog2_cinemagamut_d55", "canonlog2_cgamutday",
     "Input - Canon - Canon-Log2 - Cinema Gamut Daylight"),
    ("CanonLog3 CinemaGamut D55", "canonlog3_cinemagamut_d55", "canonlog3_cgamutday",
     "Input - Canon - Canon-Log3 - Cinema Gamut Daylight"),
    ("Linear CinemaGamut D55", "lin_cinemagamut_d55", "lin_canoncgamutday",
     "Input - Canon - Linear - Canon Cinema Gamut Daylight"),
    ("V-Log V-Gamut", "vlog_vgamut", "Input - Panasonic - V-Log - V-Gamut"),
    ("Linear V-Gamut", "lin_vgamut", "Input - Panasonic - Linear - V-Gamut"),
    ("Log3G10 REDWideGamutRGB", "log3g10_redwidegamutrgb", "rl3g10_rwg", "Input - RED - REDLog3G10 - REDWideGamutRGB",
     "Log3G10"),
    ("Linear REDWideGamutRGB", "lin_redwidegamutrgb", "lin_rwg", "Input - RED - Linear - REDWideGamutRGB"),
    ("S-Log3 S-Gamut3", "slog3_sgamut3", "Input - Sony - S-Log3 - S-Gamut3", "SLog3"),
    ("S-Log3 S-Gamut3.Cine", "slog3_sgamut3cine", "slog3_sgamutcine", "Input - Sony - S-Log3 - S-Gamut3.Cine"),
    ("S-Log3 Venice S-Gamut3", "slog3_venice_sgamut3", "Input - Sony - S-Log3 - Venice S-Gamut3"),
    ("S-Log3 Venice S-Gamut3.Cine", "slog3_venice_sgamut3cine", "slog3_venice_sgamutcine",
     "Input - Sony - S-Log3 - Venice S-Gamut3.Cine"),
    ("Linear S-Gamut3", "lin_sgamut3", "Input - Sony - Linear - S-Gamut3"),
    ("Linear S-Gamut3.Cine", "lin_sgamut3cine", "Input - Sony - Linear - S-Gamut3.Cine"),
    ("Linear Venice S-Gamut3", "lin_venice_sgamut3", "Input - Sony - Linear - Venice S-Gamut3"),
    ("Linear Venice S-Gamut3.Cine", "lin_venice_sgamut3cine", "Input - Sony - Linear - Venice S-Gamut3.Cine"),
    ("sRGB Encoded Rec.709 (sRGB)", "srgb_encoded_rec709_srgb", "srgb_texture", "srgb_rec709_scene",
     "Utility - sRGB - Texture", "Input - Generic - sRGB - Texture", "sRGB - Texture", "srgb_tx"),
    ("Gamma 1.8 Encoded Rec.709", "g18_encoded_rec709", "g18_rec709", "Utility - Gamma 1.8 - Rec.709 - Texture",
     "Gamma 1.8 Rec.709 - Texture", "g18_rec709_tx", "g18_rec709_scene", "Gamma1.8"),
    ("Gamma 2.2 Encoded Rec.709", "g22_encoded_rec709", "g22_rec709", "Utility - Gamma 2.2 - Rec.709 - Texture",
     "Gamma 2.2 Rec.709 - Texture", "g22_rec709_tx", "g22_rec709_scene", "Gamma2.2"),
    ("Gamma 2.4 Encoded Rec.709", "g24_encoded_rec709", "g24_rec709", "rec709_display", "Utility - Rec.709 - Display",
     "Gamma 2.4 Rec.709 - Texture", "g24_rec709_tx", "g24_rec709_scene", "Gamma2.4"),
    ("Camera Rec.709", "camera_rec709", "rec709_camera", "Utility - Rec.709 - Camera"),
    ("sRGB Encoded P3-D65", "srgb_encoded_p3d65", "srgb_p3d65", "srgb_displayp3", "srgb_p3d65_scene",
     "sRGB Encoded P3-D65 - Texture", "srgb_encoded_p3d65_tx"),
    ("sRGB Encoded AP1", "srgb_encoded_ap1", "srgb_ap1", "srgb_ap1_scene", "sRGB Encoded AP1 - Texture",
     "srgb_encoded_ap1_tx"),
    ("Linear AdobeRGB", "lin_adobergb", "lin_adobergb_scene", "Utility - Linear - Adobe RGB"),
    ("Linear P3-D65", "lin_p3d65", "lin_displayp3", "lin_p3d65_scene", "Utility - Linear - P3-D65", "Linear Display P3"),
    ("Linear Rec.2020", "lin_rec2020", "lin_rec2020_scene", "Utility - Linear - Rec.2020"),
    ("Linear Rec.709 (sRGB)", "lin_rec709_srgb", "lin_rec709", "lin_rec709_scene", "lin_srgb",
     "Utility - Linear - sRGB", "Utility - Linear - Rec.709"),
    ("Raw", "Utility - Raw", "none"),
    # display-referred: ACES 1.x 'Output - ...' include the ACES output transform (highlight roll-off). The OCIO 2
    # configs' display colorspaces are the right counterpart by name, but a plain colorspace conversion to them
    # uses default_view_transform ('Un-tone-mapped' in the ACES 1.3 / 2.0 studio configs): no roll-off there
    ("sRGB - Display", "srgb_display", "srgb_rec709_display", "Output - sRGB", "out_srgb"),
    ("Rec.1886 Rec.709 - Display", "rec1886_rec709_display", "g24_rec709_display", "Output - Rec.709", "out_rec709"),
]

# (token found in the name, a member of the group it means) - most specific first; for names in no group
_ALIASES = [
    ("acescct", "ACEScct"),
    ("acescc", "ACEScc"),
    ("acescg", "ACEScg"),
    ("aces2065", "ACES2065-1"),
    ("srgbtexture", "sRGB - Texture"),
    ("srgbencodedrec709", "sRGB - Texture"),
    ("linearrec709", "Linear Rec.709 (sRGB)"),
    ("linearsrgb", "Linear Rec.709 (sRGB)"),
    ("scenelinear", "Linear Rec.709 (sRGB)"),   # nuke-default's scene_linear role
    ("linear", "Linear Rec.709 (sRGB)"),        # nuke-default "linear" = linear Rec.709 primaries
    ("rec709", "Camera Rec.709"),               # nuke-default "rec709" = Rec.709 OETF
    ("srgb", "sRGB - Texture"),                 # nuke-default "sRGB"
]


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


_GROUPS_NORM = [(g, {_norm(x) for x in g}) for g in _GROUPS]


def _group(name):
    """(names, normalised names) of the alias group holding name; just name itself when it is in none."""
    n = _norm(name)
    for g, norms in _GROUPS_NORM:
        if n in norms:
            return g, norms
    return (name,), {n}


def equivalents(name):
    """name first, then its other names in other configs - to try one by one on a Nuke Read / Write."""
    name = unwrap_default(name)
    return [name] + [x for x in _group(name)[0] if x != name]


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
    norms = _group(name)[1]
    hits = [a for a in available if _norm(a) in norms]
    if not [a for a in hits if not _is_role(a)]:
        n = _norm(name)
        for needle, target in _ALIASES:
            if needle in n:
                target_norms = _group(target)[1]
                hits += [a for a in available if _norm(a) in target_norms]
                break
    return _best(hits)


# ACES 1.x 'Output - <display>' colorspaces are the ACES output transform (with its highlight roll-off). ACES 1.3 /
# 2.0 configs have that transform only as a display + view; a plain colorspace conversion to their display
# colorspace is un-tone-mapped. Display, then its views in order of preference: the ACES 1.0 view matches the
# ACES 1.x 'Output - ...' colorspaces exactly; ACES 2.0 configs only have their own (harder-rolling) curve.
_OUTPUT_VIEWS = {
    "Output - sRGB": ("sRGB - Display", ("ACES 1.0 - SDR Video", "ACES 2.0 - SDR 100 nits (Rec.709)")),
    "Output - Rec.709": ("Rec.1886 Rec.709 - Display", ("ACES 1.0 - SDR Video", "ACES 2.0 - SDR 100 nits (Rec.709)")),
}


def is_output_name(name):
    """An ACES 1.x 'Output - <display>' name (the highlights 'roll off' value)."""
    return any(_norm(k) == _norm(unwrap_default(name)) for k in _OUTPUT_VIEWS)


def output_view(name, available, displays, views):
    """For an ACES 1.x 'Output - ...' name that ComfyUI's config doesn't have as a colorspace: the (display, view)
    of OCIO Display that does the same. None when the name isn't one, the config has it, or no view fits."""
    name = unwrap_default(name)
    key = next((k for k in _OUTPUT_VIEWS if _norm(k) == _norm(name)), None)
    if not key or key in available:
        return None
    display, preferred = _OUTPUT_VIEWS[key]
    if display not in (displays or []):
        return None
    view = next((v for v in preferred if v in (views or [])), None)
    return (display, view) if view else None
