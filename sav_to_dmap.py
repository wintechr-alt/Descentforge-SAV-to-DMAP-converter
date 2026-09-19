#!/usr/bin/env python3
"""
sav_to_dmap.py -- convert a Descent: Legends of the Dark save file into a
DescentForge .dmap file.

A Descent: Legends of the Dark save (.sav) is a JSON dump of the Unity scene
state. This script reads the tiles and interactables that are currently
revealed on the board and reconstructs them as a DescentForge map.

USAGE
-----
    python3 sav_to_dmap.py SAVE_FILE OUTPUT.dmap [options]

    --report FILE.md          write a human-readable summary of what was
                               imported, what was skipped, and why
    --tiles-catalog FILE      path to DescentForge's tiles catalog
                               (default: data/descentforge_tiles.json)
    --interactables-catalog FILE
                               path to DescentForge's interactables catalog
                               (default: data/descentforge_interactables.json)

WHAT THIS DOES
--------------
1. Reads GameSceneData.SceneEntities and matches entity names (e.g. "13A")
   against DescentForge's tiles catalog -- these are the real physical
   Descent tile IDs, so the match is exact.

2. Reconstructs each tile's integer grid position and rotation from its
   Unity WorldPos/WorldRot.

   Translation: grid cell (gx, gy) is centered at Unity world coordinate
   (worldX=gx, worldZ=gy) -- e.g. a 2-cell-wide object centered at world
   x=6.5 occupies grid cells x=6 and x=7. Verified against the save's own
   `BlockedCoordinates` list, which independently confirms exactly this
   mapping for several placed objects.

   Rotation: yaw is extracted from the WorldRot quaternion as 2*atan2(qy,qw),
   snapped to the nearest 0/90/180/270, then applied with rot_cell() below --
   the same counterclockwise convention DescentForge's own engine uses
   (confirmed directly against DescentForge_no_base64_images.html's rotCell).

3. Converts furniture/decor entities (bookshelves, chests, tables, pillars,
   staircases, tokens, etc.) into spawns.interactables using the same
   translation math, matched against the interactables catalog by name
   prefix (so quest-specific suffixes like "Bookshelf 10A" or
   "Chest 7A Fallen" still resolve to the right catalog id).

4. All placed geometry is shifted so its bounding box starts at (0, 0),
   since DescentForge's grid is 0-indexed and Unity world coordinates are
   frequently negative.

5. IMPORTANT -- disconnected rooms: a tile that physically touches another
   revealed tile (shares a real edge in the Unity scene) is reconstructed
   exactly -- this is proven by matching multiple real saves with zero
   error. But a tile reachable only through a Staircase/Door (a level
   transition to a *separate* physical structure in the game) has NO
   recoverable spatial relationship to the rest of the board: level
   designers place those structures anywhere convenient in Unity's world
   space, since gameplay teleports the hero through the stairs rather than
   relying on physical adjacency. This was confirmed empirically (checked
   against a hand-corrected reference .dmap): neither the tile's own
   coordinates nor its connecting staircase's coordinates predict where it
   belongs on a compact board.

   So: tiles that touch something already placed are grouped into
   connected components and each component is placed as one contiguous,
   internally-consistent unit. The single largest component is treated as
   the anchored "main" board. Every other (disconnected) component is
   instead laid out in a separate staging row below the main board --
   untouching, clearly out of the way -- with its likely connecting
   Staircase/Door named in the report, so it can be dragged into place by
   hand against a reference screenshot, the same way you would for any
   staircase connection. Its rotation is still the best extracted guess,
   but should be double-checked visually once placed.

6. Hero starting positions and enemies are intentionally NOT reconstructed:
   there is no reliable hero-token entity in GameSceneData, and any enemies
   present at save time are typically already-placed encounter state rather
   than a stable spawn definition. Both are left empty for the user to set.

LIMITATIONS
-----------
- Only entities currently revealed on the board (IsVisible == true in the
  save) are included. Tiles/objects behind not-yet-opened doors are skipped.
- board.blocked (obstacle cells layered on top of a tile's floor) is left
  empty. The save's BlockedCoordinates data is reconstructible, but this
  field's expected shape in DescentForge's own loader isn't confirmed --
  shipping a guessed structure risked breaking map loads entirely, so it's
  left for the user to add by hand if needed.
- A handful of quest-specific props (elemental puzzle tokens, chain
  decorations, etc.) have no corresponding catalog entry and are emitted
  with their raw in-save name as "type" rather than a catalog id -- these
  are flagged in the report and should be checked by hand.
- Interactable rotation is extracted with the same formula used for tiles
  (verified correct for tile placement -- see quat_yaw_deg). Tile geometry
  itself does not depend on this value being right for interactables (an
  interactable's occupied cells are a simple axis-aligned box regardless of
  rotation), but the cosmetic facing direction of a rotated interactable's
  icon (e.g. which way a Staircase's arrow points) has not been
  independently confirmed against DescentForge's own renderer and may need
  a manual nudge after import for a small number of objects.
"""
import argparse
import datetime
import json
import math
import os
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TILES_CATALOG = os.path.join(SCRIPT_DIR, "data", "descentforge_tiles.json")
DEFAULT_INTERACTABLES_CATALOG = os.path.join(SCRIPT_DIR, "data", "descentforge_interactables.json")


# ----------------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------------

def rot_cell(x, y, r):
    """Rotate a local (x, y) cell offset by r degrees (0/90/180/270),
    counterclockwise, about the origin."""
    r = ((r or 0) % 360 + 360) % 360
    if r == 90:
        return (-y, x)
    if r == 180:
        return (-x, -y)
    if r == 270:
        return (y, -x)
    return (x, y)


def quat_yaw_deg(q):
    """Extract yaw (rotation about Y) from a Unity quaternion assumed
    pure-Y, snapped to the nearest multiple of 90. Matches the standard
    counterclockwise rot_cell() convention above -- confirmed by
    reconstructing multiple real saves with zero tile overlaps and correct
    room-to-room connectivity."""
    yaw = math.degrees(2 * math.atan2(q["y"], q["w"])) % 360
    return int(round(yaw / 90.0) * 90) % 360


def level_from_y(world_y):
    """Buckets a world Y (elevation) into an integer board level. Descent
    boards typically use one world-Y step per level; 0.55 sits between the
    observed 0.0 (level 0) and ~1.1 (level 1) values."""
    return 0 if world_y < 0.55 else 1


def reconstruct_tile_placement(entity, tile_def):
    rot = quat_yaw_deg(entity["WorldRot"])
    cells = tile_def["cells"]
    rotated = [rot_cell(cx, cy, rot) for cx, cy in cells]
    xs = [c[0] for c in rotated]
    ys = [c[1] for c in rotated]
    bbox_cx = (min(xs) + max(xs)) / 2
    bbox_cy = (min(ys) + max(ys)) / 2
    anchor_x = entity["WorldPos"]["x"] - bbox_cx
    anchor_y = entity["WorldPos"]["z"] - bbox_cy
    ax, ay = round(anchor_x), round(anchor_y)
    off_x, off_y = abs(anchor_x - ax), abs(anchor_y - ay)
    level = level_from_y(entity["WorldPos"]["y"])
    return {
        "tile": tile_def["id"],
        "pos": [ax, ay],
        "rot": rot,
        "level": level,
    }, max(off_x, off_y)


def tile_absolute_cells(tile_entry, tiles_catalog):
    tdef = tiles_catalog[tile_entry["tile"]]
    ax, ay = tile_entry["pos"]
    return [(ax + rx, ay + ry) for rx, ry in
            (rot_cell(cx, cy, tile_entry["rot"]) for cx, cy in tdef["cells"])]


# ----------------------------------------------------------------------------
# Decor / interactable placement
# ----------------------------------------------------------------------------

# Map .sav SceneEntity name -> interactables-catalog id. Different quests
# name decor differently -- some use a bare name ("Bookshelf", "Chest (1)"),
# others suffix the owning tile and a variant ("Bookshelf 10A",
# "Chest 7A Fallen", "Door LOCKED"). Matched by PREFIX (checked longest-first
# so e.g. "pillar_short" wins over "pillar"), not exact string, so any such
# suffix is tolerated automatically.
DECOR_PREFIX_TO_CATALOG_ID = [
    ("pillar_short", "PillarShort"),
    ("pillar short", "PillarShort"),
    ("stone table", "StoneTable"),
    ("round table", "RoundTable"),
    ("small table", "StoneTable"),  # bare "Small Table" props (no DT-wrapper
                                     # suffix) render the same as a stone table
    ("bookshelf", "Shelf"),
    ("chest", "Chest"),
    ("door", "Door"),
    ("archway", "Archway"),
    ("staircase", "Staircase"),
    ("front steps", "Staircase"),
    ("cauldron", "Cauldron"),
    ("terrain_cauldron", "Cauldron"),
    ("reward cauldron", "Cauldron"),
    ("lectern", "Lectern"),
    ("altar", "Lectern"),  # the in-save "Altar" prop is the same physical
                            # object as the catalog's Lectern
    ("well", "Well"),
    ("spiked barricade", "Barricade"),
    ("barricade", "Barricade"),
    ("interact token", "InteractToken"),
    ("exploration token", "SightToken"),
    ("pillar", "Pillar"),  # after pillar_short/pillar short so those match first
]

PILLAR_LABEL_RE = re.compile(r"\bp[1-9]\b")

# Entities that are logic/scripting wrappers or alt-skin sub-props, not
# separately placed physical objects -- skip entirely. Prefix match.
DECOR_SKIP_PREFIXES = (
    "dt ", "pre logic", "post logic", "initalizer", "@", "quest",
    "worldmapdt", "tutorialdt", "large table preview dt",
    "large table interact dt", "small table preview dt",
    "small table interact dt", "underlay-", "searchevent",
)

# Exact-match skips (NOT prefixes -- "books" as a prefix would also eat
# "bookshelf", which is a real separate object to keep).
DECOR_SKIP_EXACT = {"books", "bottles", "baubles"}

# Recognisable-but-unmapped: kept as a raw/custom type (flagged in the
# report) rather than silently guessing a catalog id or dropping them.
DECOR_UNMAPPED_PREFIXES = ("chains",)

ELEMENTAL_STONE_NAMES = {
    "anemos stone", "aquos stone", "ignos stone", "lumos stone",
    "mortos stone", "terros stone", "umbros stone", "vigos stone",
}


def strip_instance_suffix(name):
    return re.sub(r"\s*\(\d+\)\s*$", "", name).strip()


def match_decor_catalog_id(raw_key):
    """Longest-substring match against DECOR_PREFIX_TO_CATALOG_ID.

    Different quests put the owning tile's name in different places --
    "Bookshelf 10A" (suffix) vs "17B Spiked Barricade" (prefix) -- so this
    searches for each known keyword phrase ANYWHERE in the name rather than
    only at the start. A short generic "P1"/"P2"/"P3" label (a pillar
    instance number, seen with the same near-zero-but-negative floor
    y-offset as other confirmed short pillars) is treated as a PillarShort.
    """
    if PILLAR_LABEL_RE.search(raw_key):
        return "PillarShort"
    best = None
    for phrase, catalog_id in DECOR_PREFIX_TO_CATALOG_ID:
        if phrase in raw_key and (best is None or len(phrase) > len(best[0])):
            best = (phrase, catalog_id)
    return best[1] if best else None


def is_quest_logic_entity(raw_key):
    """Catches quest-script/logic nodes generically across different quests
    (e.g. "Golem 1 Defeat DT", "Wight Death Logic") rather than hardcoding
    one quest's specific names."""
    if raw_key.endswith(" dt"):
        return True
    for marker in ("defeat", " logic", "activation", "tactic selection", "objective updated"):
        if marker in raw_key:
            return True
    return False


def catalog_wh(catalog_id, interactables_catalog):
    for e in interactables_catalog:
        if e["id"] == catalog_id:
            return e.get("w", 1), e.get("h", 1)
    return 1, 1


def interactable_footprint(pos, w, h, rot):
    """Cell footprint of a placed interactable. Unlike tiles, interactables
    do not use a rotated coordinate transform: rot only swaps w/h at
    90/270, and the occupied cells are always the plain axis-aligned
    rectangle extending in the positive x/y direction from pos (pos is the
    rectangle's own top-left/min corner)."""
    rot = ((rot or 0) % 360 + 360) % 360
    cw, ch = (h, w) if rot in (90, 270) else (w, h)
    px, py = pos
    return [[px + dx, py + dy] for dx in range(cw) for dy in range(ch)]


def default_loot():
    return {"mode": "none", "items": [], "traits": [], "points": 0}


def reconstruct_decor_placement(entity, catalog_id, interactables_catalog):
    rot = quat_yaw_deg(entity["WorldRot"])
    w, h = catalog_wh(catalog_id, interactables_catalog) if catalog_id else (1, 1)
    eff_w, eff_h = (h, w) if rot in (90, 270) else (w, h)
    anchor_x = entity["WorldPos"]["x"] - (eff_w - 1) / 2
    anchor_z = entity["WorldPos"]["z"] - (eff_h - 1) / 2
    ax, az = round(anchor_x), round(anchor_z)
    level = level_from_y(entity["WorldPos"]["y"])
    return {
        "type": catalog_id or strip_instance_suffix(entity["Name"]),
        "pos": [ax, az],
        "level": level,
        "rot": rot,
        "name": entity["Name"],
        "text": "",
        "behavior": "official",
        "uses": "",
        "freeAction": False,
        "loot": default_loot(),
        "cells": interactable_footprint([ax, az], w, h, rot),
    }


# ----------------------------------------------------------------------------
# Main conversion
# ----------------------------------------------------------------------------

def convert(sav_path, out_path, report_path=None,
            tiles_catalog_path=DEFAULT_TILES_CATALOG,
            interactables_catalog_path=DEFAULT_INTERACTABLES_CATALOG):
    tiles_catalog = json.load(open(tiles_catalog_path))
    interactables_catalog = json.load(open(interactables_catalog_path))

    # Fail fast if a hardcoded mapping doesn't literally match a real
    # catalog id, so the mapping table can never silently drift out of sync.
    valid_ids = {e["id"] for e in interactables_catalog}
    for prefix, catalog_id in DECOR_PREFIX_TO_CATALOG_ID:
        if catalog_id not in valid_ids:
            raise SystemExit(
                f"DECOR_PREFIX_TO_CATALOG_ID maps {prefix!r} -> {catalog_id!r}, "
                f"but {catalog_id!r} is not a real id in {interactables_catalog_path}. "
                f"Valid ids: {sorted(valid_ids)}"
            )

    with open(sav_path) as f:
        data = json.load(f)
    gsd = data["GameSceneData"]
    entities = gsd["SceneEntities"]

    report_lines = [
        f"# Import report -- {sav_path} -> {out_path}",
        f"Generated {datetime.datetime.now().isoformat(timespec='seconds')}",
        "",
    ]

    # ---- tiles ----
    tile_entries = []
    tile_warnings = []
    tile_names = []  # parallel list of source .sav entity Names, for decor grouping
    for e in entities:
        key = e["Name"].lower()
        if key in tiles_catalog and e.get("IsVisible"):
            placement, slop = reconstruct_tile_placement(e, tiles_catalog[key])
            tile_entries.append(placement)
            tile_names.append(e["Name"])
            if slop > 1e-4:
                tile_warnings.append(f"{e['Name']}: anchor rounded with slop {slop:.4f} (expected ~0)")

    tile_section = [f"## Tiles placed: {len(tile_entries)}\n"]
    if tile_warnings:
        tile_section.append("**Tile anchor warnings (should not normally happen):**")
        for w in tile_warnings:
            tile_section.append(f"- {w}")
        tile_section.append("")

    hidden = [e["Name"] for e in entities if e["Name"].lower() in tiles_catalog and not e.get("IsVisible")]
    if hidden:
        tile_section.append(f"Tiles present in save but not yet revealed (excluded): {hidden}\n")

    # ---- connected-component detection ----
    # A tile that physically touches another revealed tile is reconstructed
    # exactly; a tile only reachable through a Staircase/Door is a separate
    # physical structure with no recoverable spatial relationship to the
    # rest of the board (see module docstring). Group tiles by real
    # adjacency, keep the largest group's coordinates untouched as the
    # anchored "main" board, and stage every other group separately so nothing
    # gets a silently-wrong position.
    n = len(tile_entries)
    abs_cells_per_tile = [set(tile_absolute_cells(t, tiles_catalog)) for t in tile_entries]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    def touching(cells_a, cells_b):
        for (x, y) in cells_a:
            if any((x + dx, y + dy) in cells_b for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))):
                return True
        return False

    for i in range(n):
        for j in range(i + 1, n):
            if tile_entries[i]["level"] == tile_entries[j]["level"] and touching(abs_cells_per_tile[i], abs_cells_per_tile[j]):
                union(i, j)

    components = {}
    for i in range(n):
        components.setdefault(find(i), []).append(i)

    main_root = max(components, key=lambda r: sum(len(abs_cells_per_tile[i]) for i in components[r])) if components else None
    tile_shift = [(0, 0)] * n  # per-tile (dx, dy) applied on top of its naive placement

    if main_root is not None:
        main_cells = set()
        for i in components[main_root]:
            main_cells |= abs_cells_per_tile[i]
        main_min_x = min(c[0] for c in main_cells)
        main_max_y = max(c[1] for c in main_cells)

        stage_y = main_max_y + 2  # 1-row gap below the main board
        for root, members in components.items():
            if root == main_root:
                continue
            comp_cells = set()
            for i in members:
                comp_cells |= abs_cells_per_tile[i]
            comp_min_x = min(c[0] for c in comp_cells)
            comp_min_y = min(c[1] for c in comp_cells)
            comp_max_y = max(c[1] for c in comp_cells)
            dx = main_min_x - comp_min_x
            dy = stage_y - comp_min_y
            for i in members:
                tile_shift[i] = (dx, dy)
            stage_y += (comp_max_y - comp_min_y) + 2  # advance past this component + gap

            names_here = [tile_names[i] for i in members]
            connector = None
            for e in entities:
                raw = e["Name"].lower()
                if any(nm.lower() in raw for nm in names_here) and any(
                    raw.startswith(p) for p in ("staircase", "door", "archway")
                ):
                    connector = e["Name"]
                    break
            tile_section.append(
                f"## Disconnected room: {names_here} -- staged separately below the main "
                f"board (not touching anything else in the save), since its true position "
                f"can't be derived from the data (see docstring). "
                + (f"Likely connects via {connector!r} -- move it into place by hand against "
                   f"a reference screenshot; double-check its rotation too.\n"
                   if connector else "No obvious connecting Staircase/Door found by name -- "
                   "verify placement and rotation manually.\n")
            )

    for i, t in enumerate(tile_entries):
        dx, dy = tile_shift[i]
        if dx or dy:
            t["pos"] = [t["pos"][0] + dx, t["pos"][1] + dy]

    tile_shift_by_name = {tile_names[i]: tile_shift[i] for i in range(n)}
    # Pre-shift tile footprints, keyed by tile name, for the proximity
    # fallback below (decor with no name match, e.g. a generic "Altar" or
    # "Terrain_Cauldron" sitting on a disconnected tile with no tile-name
    # suffix of its own).
    tile_naive_cells_by_name = {tile_names[i]: (tile_entries[i]["level"], abs_cells_per_tile[i]) for i in range(n)}

    report_lines.extend(tile_section)

    # ---- decor / interactables ----
    interactable_entries = []
    unmapped = []
    for e in entities:
        raw_key = strip_instance_suffix(e["Name"]).lower()
        if raw_key in DECOR_SKIP_EXACT:
            continue
        if any(raw_key.startswith(p) for p in DECOR_SKIP_PREFIXES):
            continue
        if raw_key in tiles_catalog:
            continue  # already handled as a tile
        if is_quest_logic_entity(raw_key):
            continue
        if not e.get("IsVisible"):
            continue

        if raw_key in ELEMENTAL_STONE_NAMES or any(raw_key.startswith(p) for p in DECOR_UNMAPPED_PREFIXES):
            entry = reconstruct_decor_placement(e, None, interactables_catalog)
            interactable_entries.append(entry)
            unmapped.append(e["Name"])
        else:
            catalog_id = match_decor_catalog_id(raw_key)
            entry = reconstruct_decor_placement(e, catalog_id, interactables_catalog)
            interactable_entries.append(entry)
            if not catalog_id:
                # Unrecognised entity type -- surface it rather than dropping
                # it silently, so nothing goes missing without a trace.
                unmapped.append(e["Name"])

        # A decor item belonging to a staged (disconnected) tile needs the
        # SAME shift as that tile, or it'll be left floating in the main
        # board while its tile moved. Preferred match: the tile's name
        # appears in the decor entity's own name (e.g. "Bookshelf 10A" ->
        # "10A"). Fallback for generic-named decor with no such suffix
        # (e.g. a plain "Altar" or "Terrain_Cauldron"): proximity to a
        # disconnected tile's own (pre-shift) footprint. Anything matching
        # neither is assumed to belong to the main board.
        matched = False
        for tname, shift in tile_shift_by_name.items():
            if shift != (0, 0) and tname.lower() in e["Name"].lower():
                entry["pos"] = [entry["pos"][0] + shift[0], entry["pos"][1] + shift[1]]
                entry["cells"] = [[c[0] + shift[0], c[1] + shift[1]] for c in entry["cells"]]
                matched = True
                break
        if not matched:
            decor_level = level_from_y(e["WorldPos"]["y"])
            decor_pt = (round(e["WorldPos"]["x"]), round(e["WorldPos"]["z"]))
            for tname, (tlevel, tcells) in tile_naive_cells_by_name.items():
                shift = tile_shift_by_name[tname]
                if shift == (0, 0) or tlevel != decor_level:
                    continue
                if any(abs(decor_pt[0] - cx) <= 1 and abs(decor_pt[1] - cy) <= 1 for cx, cy in tcells):
                    entry["pos"] = [entry["pos"][0] + shift[0], entry["pos"][1] + shift[1]]
                    entry["cells"] = [[c[0] + shift[0], c[1] + shift[1]] for c in entry["cells"]]
                    break

    report_lines.append(f"## Interactables placed: {len(interactable_entries)}\n")
    if unmapped:
        report_lines.append(
            "**Unmapped types (no catalog id -- verify manually before "
            f"loading in DescentForge):** {unmapped}\n"
        )

    # ---- enemies / hero start ----
    report_lines.append(
        "## Enemies and hero start are not reconstructed by this script -- "
        "see the module docstring for why. Set spawns.heroStart and "
        "spawns.enemies by hand if needed.\n"
    )

    # ---- normalize onto the .dmap grid ----
    # World-space reconstruction naturally produces negative x/y (the save's
    # Unity origin isn't the board's corner), but DescentForge's grid is
    # 0-indexed. Shift everything (tiles, interactable pos+cells) by the
    # same offset so the whole layout's true occupied bounding box (not
    # just anchors -- a tile's rotated cells can extend further than its
    # own anchor) starts at (0, 0), preserving every relative position.
    all_xy = []
    for t in tile_entries:
        all_xy.extend(tile_absolute_cells(t, tiles_catalog))
    for it in interactable_entries:
        all_xy.extend((c[0], c[1]) for c in it["cells"])

    if all_xy:
        min_x = min(c[0] for c in all_xy)
        min_y = min(c[1] for c in all_xy)
    else:
        min_x = min_y = 0
    off_x, off_y = -min_x, -min_y

    if off_x or off_y:
        for t in tile_entries:
            t["pos"] = [t["pos"][0] + off_x, t["pos"][1] + off_y]
        for it in interactable_entries:
            it["pos"] = [it["pos"][0] + off_x, it["pos"][1] + off_y]
            it["cells"] = [[c[0] + off_x, c[1] + off_y] for c in it["cells"]]
        report_lines.append(
            f"## Grid normalization: shifted everything by (+{off_x}, +{off_y}) "
            "so the occupied bounding box starts at (0,0).\n"
        )

    # ---- assemble dmap ----
    all_levels = sorted(set(t["level"] for t in tile_entries)) or [0]
    all_x = [c[0] for c in all_xy] if all_xy else [0]
    all_y = [c[1] for c in all_xy] if all_xy else [0]
    grid_size = [max(all_x) + off_x + 1, max(all_y) + off_y + 1]

    dmap = {
        "format": "dmap",
        "version": 2,
        "meta": {
            "id": "sav_import",
            "name": (data.get("PartyName", "") + " (imported)").strip(),
            "author": "",
            "description": "Imported from a Descent: Legends of the Dark save file via sav_to_dmap.py",
            "difficulty": "normal",
            "heroCount": [1, 4],
            "intro": "",
            "music": {"file": "", "title": "", "when": "map", "loop": True, "url": ""},
            "created": datetime.date.today().isoformat(),
        },
        "board": {
            "gridSize": grid_size,
            "levels": max(all_levels) + 1,
            "tiles": tile_entries,
            "blocked": [],
            "floor": "flagstone",
        },
        "spawns": {
            "heroStart": [],
            "enemies": [],
            "interactables": interactable_entries,
        },
        "objectives": {
            "primary": [],
            "optional": [],
            "failConditions": [{"id": "all_heroes_defeated", "params": {}}],
        },
        "triggers": [],
    }

    with open(out_path, "w") as f:
        json.dump(dmap, f, indent=2)

    if report_path:
        with open(report_path, "w") as f:
            f.write("\n".join(report_lines))

    return dmap, report_lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sav_path", help="path to the Descent: Legends of the Dark .sav file")
    ap.add_argument("out_path", help="path to write the .dmap output file")
    ap.add_argument("--report", default=None, help="optional path to write a human-readable import report")
    ap.add_argument("--tiles-catalog", default=DEFAULT_TILES_CATALOG, help="path to descentforge_tiles.json")
    ap.add_argument("--interactables-catalog", default=DEFAULT_INTERACTABLES_CATALOG,
                     help="path to descentforge_interactables.json")
    args = ap.parse_args()

    dmap, report = convert(
        args.sav_path, args.out_path, args.report,
        tiles_catalog_path=args.tiles_catalog,
        interactables_catalog_path=args.interactables_catalog,
    )
    print(f"Wrote {args.out_path} ({len(dmap['board']['tiles'])} tiles, "
          f"{len(dmap['spawns']['interactables'])} interactables)")
    if args.report:
        print(f"Wrote {args.report}")


if __name__ == "__main__":
    main()
