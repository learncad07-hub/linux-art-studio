import os
from dataclasses import dataclass

ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "assets", "icons")


@dataclass(frozen=True)
class AppEntry:
    app_id: str
    name: str
    category: str
    icon_text: str
    icon_path: str = None
    # Only set for Flatpak-backed apps: the real Flathub app id, used for
    # flatpak install/uninstall/info calls. app_id above stays short and
    # stable for internal page routing regardless of this.
    flatpak_id: str = None
    # Only used by the simple single-app Flatpak pages (FlatpakAppPage);
    # the multi-version pages (Unreal/Blender) and DaVinci Resolve write
    # their own description directly, see window.py.
    description: str = ""


APPS = [
    AppEntry(
        app_id="ue", name="Unreal Engine", category="Game Engines", icon_text="UE",
        icon_path=os.path.join(ICON_DIR, "unrealengine.svg"),
    ),
    AppEntry(
        app_id="blender", name="Blender", category="3D", icon_text="BL",
        icon_path=os.path.join(ICON_DIR, "blender.svg"),
    ),
    AppEntry(
        app_id="dr", name="DaVinci Resolve", category="Video", icon_text="DR",
        icon_path=os.path.join(ICON_DIR, "davinciresolve.png"),
    ),
    AppEntry(
        app_id="eam", name="Epic Asset Manager", category="Game Engines", icon_text="EA",
        icon_path=os.path.join(ICON_DIR, "epicassetmanager.png"),
        flatpak_id="io.github.achetagames.epic_asset_manager",
        description="Unofficial Epic Games client. Download Marketplace assets, install "
                     "official Unreal Engine builds, and manage your Unreal projects",
    ),
    AppEntry(
        app_id="ams", name="Asset Manager Studio", category="Game Engines", icon_text="AM",
        icon_path=os.path.join(ICON_DIR, "assetmanagerstudio.png"),
        flatpak_id="studio.assetmanager.ams",
        description="Open-source asset manager for game-dev marketplaces",
    ),

    # --- 3D ---
    AppEntry(
        app_id="freecad", name="FreeCAD", category="3D", icon_text="FC",
        icon_path=os.path.join(ICON_DIR, "freecad.png"),
        flatpak_id="org.freecad.FreeCAD",
        description="Free, open-source parametric 3D CAD modeler for engineering and product design",
    ),
    AppEntry(
        app_id="meshlab", name="MeshLab", category="3D", icon_text="ML",
        icon_path=os.path.join(ICON_DIR, "meshlab.png"),
        flatpak_id="net.meshlab.MeshLab",
        description="Open-source system for processing and editing 3D triangular meshes",
    ),
    AppEntry(
        app_id="prusaslicer", name="PrusaSlicer", category="3D", icon_text="PS",
        icon_path=os.path.join(ICON_DIR, "prusaslicer.png"),
        flatpak_id="com.prusa3d.PrusaSlicer",
        description="Open-source slicer for preparing 3D models for FDM/SLA printing",
    ),

    # --- Video ---
    AppEntry(
        app_id="kdenlive", name="Kdenlive", category="Video", icon_text="KD",
        icon_path=os.path.join(ICON_DIR, "kdenlive.png"),
        flatpak_id="org.kde.kdenlive",
        description="Free, open-source non-linear video editor built on the MLT framework",
    ),
    AppEntry(
        app_id="shotcut", name="Shotcut", category="Video", icon_text="SC",
        icon_path=os.path.join(ICON_DIR, "shotcut.png"),
        flatpak_id="org.shotcut.Shotcut",
        description="Free, open-source, cross-platform non-linear video editor",
    ),
    AppEntry(
        app_id="obsstudio", name="OBS Studio", category="Video", icon_text="OBS",
        icon_path=os.path.join(ICON_DIR, "obsstudio.png"),
        flatpak_id="com.obsproject.Studio",
        description="Free, open-source software for screen recording and live streaming",
    ),
    AppEntry(
        app_id="natron", name="Natron", category="Video", icon_text="NT",
        icon_path=os.path.join(ICON_DIR, "natron.png"),
        flatpak_id="fr.natron.Natron",
        description="Free, open-source node-based video compositor, in the style of Nuke",
    ),

    # --- Image & Illustration ---
    AppEntry(
        app_id="gimp", name="GIMP", category="Image & Illustration", icon_text="GP",
        icon_path=os.path.join(ICON_DIR, "gimp.png"),
        flatpak_id="org.gimp.GIMP",
        description="Free, open-source raster image editor, the standard Photoshop alternative on Linux",
    ),
    AppEntry(
        app_id="inkscape", name="Inkscape", category="Image & Illustration", icon_text="IK",
        icon_path=os.path.join(ICON_DIR, "inkscape.png"),
        flatpak_id="org.inkscape.Inkscape",
        description="Free, open-source vector graphics editor, the standard Illustrator alternative on Linux",
    ),
    AppEntry(
        app_id="krita", name="Krita", category="Image & Illustration", icon_text="KR",
        icon_path=os.path.join(ICON_DIR, "krita.png"),
        flatpak_id="org.kde.krita",
        description="Free, open-source digital painting application for illustration and concept art",
    ),
    AppEntry(
        app_id="darktable", name="darktable", category="Image & Illustration", icon_text="DT",
        icon_path=os.path.join(ICON_DIR, "darktable.png"),
        flatpak_id="org.darktable.Darktable",
        description="Free, open-source RAW photo workflow application and non-destructive editor",
    ),
    AppEntry(
        app_id="rawtherapee", name="RawTherapee", category="Image & Illustration", icon_text="RT",
        icon_path=os.path.join(ICON_DIR, "rawtherapee.png"),
        flatpak_id="com.rawtherapee.RawTherapee",
        description="Free, open-source RAW photo processing application",
    ),
    AppEntry(
        app_id="scribus", name="Scribus", category="Image & Illustration", icon_text="SB",
        icon_path=os.path.join(ICON_DIR, "scribus.png"),
        flatpak_id="net.scribus.Scribus",
        description="Free, open-source page layout and desktop publishing application",
    ),

    # --- Audio ---
    AppEntry(
        app_id="ardour", name="Ardour", category="Audio", icon_text="AR",
        icon_path=os.path.join(ICON_DIR, "ardour.png"),
        flatpak_id="org.ardour.Ardour",
        description="Free, open-source digital audio workstation for recording, editing, and mixing",
    ),
    AppEntry(
        app_id="audacity", name="Audacity", category="Audio", icon_text="AU",
        icon_path=os.path.join(ICON_DIR, "audacity.png"),
        flatpak_id="org.audacityteam.Audacity",
        description="Free, open-source, easy-to-use audio editor and recorder",
    ),
]

CATEGORY_ICONS = {
    "Game Engines": "applications-games-symbolic",
    "3D": "draw-cuboid-symbolic",
    "Video": "video-x-generic-symbolic",
    "Image & Illustration": "applications-graphics-symbolic",
    "Audio": "audio-x-generic-symbolic",
}

CATEGORY_ORDER = [
    "Game Engines",
    "3D",
    "Video",
    "Image & Illustration",
    "Audio",
]


def apps_by_category():
    by_category = {category: [] for category in CATEGORY_ORDER}
    for app in APPS:
        by_category.setdefault(app.category, []).append(app)
    return by_category
