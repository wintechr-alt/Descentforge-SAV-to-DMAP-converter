# sav-to-dmap

Convert a **Descent: Legends of the Dark** save file into a **DescentForge**
`.dmap` file.

A Descent: Legends of the Dark save (`.sav`) turns out to be a JSON dump of
the Unity scene state -- every tile and prop currently on the board, with
its real-world position and rotation. This script reads that data and
reconstructs it as a DescentForge map, so a board you've explored in-game
can be brought into DescentForge for editing, remixing, or building a new
quest around.

## What it does

- Matches revealed tile entities against DescentForge's own tile catalog
  (the physical Descent tile IDs, e.g. `13A`), and reconstructs each tile's
  grid position, rotation, and level from its Unity transform.
- Matches furniture/decor entities (bookshelves, chests, tables, pillars,
  staircases, tokens, etc.) against the interactables catalog, tolerating
  the different naming conventions different quests use for the same prop
  (e.g. `"Bookshelf"`, `"Bookshelf 10A"`, `"Bookshelf MOVED"` all resolve to
  the same catalog entry).
- Groups tiles into connected components (tiles that physically touch, the
  same way they're built in-game) and places the largest one -- the "main"
  board -- with exact, verified position and rotation.
- Any other component (a room reachable only through a staircase, which the
  game places anywhere in its own world space with no spatial relationship
  to the rest of the board -- see below) is staged in a separate row below
  the main board instead of guessing a wrong position for it.
- Normalizes the whole layout onto DescentForge's 0-indexed grid.
- Writes a human-readable import report alongside the `.dmap` describing
  what was placed, what was skipped, and anything that needs a manual look.

## What it intentionally does *not* do

- **Rooms connected only by a staircase.** This was tested directly against
  a hand-corrected reference map: a tile's Unity coordinates only predict
  its position on the board when it physically touches something else
  that's already placed. A room on the other side of a staircase is a
  separate structure the game can place anywhere in its own world space --
  there's no formula that recovers where it "should" go on a compact board,
  because that information simply isn't in the save. These rooms are staged
  separately (see above) with their likely connecting Staircase/Door named
  in the report, ready to drag into place by hand against a reference.
- **Hero starting position** and **enemies** are not reconstructed. There's
  no reliable hero-token entity in the save to pull a start position from,
  and enemies present at save time reflect in-progress encounter state
  rather than a stable spawn definition worth copying. Set these by hand
  in the output `.dmap`.
- **Unrevealed content** (tiles and props behind doors you haven't opened
  yet) is excluded -- only what's currently visible on the board is
  imported.
- **`board.blocked`** (obstacle cells layered on a tile's floor) is left
  empty. The save data needed to reconstruct it exists, but the field's
  expected shape in DescentForge's loader isn't confirmed, and a wrong
  guess here risks breaking the whole map load. Left for manual editing.

See the module docstring in `sav_to_dmap.py` for the full technical
breakdown of the coordinate math and known limitations, including an open
question around cosmetic rotation of a few interactable icons.

## Usage

```bash
python3 sav_to_dmap.py path/to/save.sav output.dmap --report report.md
```

Options:

| Flag | Description |
|---|---|
| `--report FILE.md` | Write a human-readable summary of the import |
| `--tiles-catalog FILE` | Override the tiles catalog path (default: `data/descentforge_tiles.json`) |
| `--interactables-catalog FILE` | Override the interactables catalog path (default: `data/descentforge_interactables.json`) |

No third-party dependencies -- Python 3 standard library only.

## Repository layout

```
sav_to_dmap.py          the converter
data/
  descentforge_tiles.json          tile catalog (from DescentForge)
  descentforge_interactables.json  interactables catalog (from DescentForge)
examples/
  example_output.dmap    sample converted output
  example_report.md      the import report for that same run
```

## Contributing

If you hit a save file this doesn't handle correctly -- an unrecognized
prop name, a tile that lands in the wrong place, a rotation that looks off
once loaded in DescentForge -- please open an issue with the save file (or
the relevant snippet of it) attached. The name-matching and catalog
mappings in `sav_to_dmap.py` are built to generalize across quests, but
new quests can always introduce naming conventions this hasn't seen yet.

## Disclaimer

This is an unofficial fan tool. Descent: Legends of the Dark is a trademark
of Fantasy Flight Games / Asmodee; this project is not affiliated with or
endorsed by them. It's intended for personal use converting your own save
data for use with the DescentForge community map editor.

## License

MIT for the code in `sav_to_dmap.py`. The catalog files under `data/` are
derived from DescentForge's own tile/interactable reference data; see that
project for their terms if redistributing separately.
