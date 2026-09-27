# homelayout

(disclosure: completely vibecoded to avoid spending several lifetimes tapping and dragging a bunch of little squares on a rectangle)

Dump and apply a Samsung One UI home-screen layout over ADB, without root.

The phone's layout lives in the launcher's private database, which you can't reach without root. So
`homelayout` does what you'd do by hand: it long-presses icons and drags them, flipping pages by
hovering at the screen edge. It reads the screen with
[uiautomator2](https://github.com/openatx/uiautomator2) after every drop to check that the move worked.

```
./homelayout.py dump -o layout.txt       # current layout -> text file
$EDITOR layout.txt                       # rearrange the lines
./homelayout.py apply layout.txt -n      # show the planned moves, change nothing
./homelayout.py apply layout.txt         # ask, then execute (-y skips the question)
```

## Requirements

- `adb` with the phone connected and USB debugging on
- [`uv`](https://github.com/astral-sh/uv). The script is a PEP 723 `uv run --script`, so uv
  fetches `uiautomator2` on the first run
- the phone unlocked on the home screen. Don't touch it during `apply`

On the first connection, uiautomator2 pushes its small server to `/data/local/tmp` on the phone.

## Layout file

```
# page 1 (2 widget(s): their cells are skipped)
Maps
Camera
.
Calculator

# page 2
Authenticator #1
Bank
Authenticator #2
```

- One line per icon label, in reading order: left to right, then top to bottom.
- A blank line starts the next page.
- `.` is an empty cell, used to leave a gap. Trailing gaps can be left out. A page that is just `.`
  is an empty page.
- Lines starting with `#` are comments.
- Cells covered by widgets are skipped, so page 1 starts below a clock or weather row.
- When two icons share a label, `dump` numbers them `Label #1`, `Label #2`, … in the reading order
  of the phone's *current* layout. Take a fresh `dump` before editing.
- The dock is listed in a comment but never moved.

## What `apply` does

1. **Survey.** Goes through every page and records which label sits in which cell. It reads the grid
   geometry from the launcher's `wsCellLayout` and widget positions from `AppWidgetHostView` nodes.
2. **Check the file.** Every icon on the home screen must be in the file, and every icon in the file
   must already be on the home screen. It never removes, uninstalls or adds from the app drawer.
   Mismatches are listed, with "did you mean" suggestions for near-miss labels.
3. **Plan.** Repeatedly moves any icon whose target cell is free, picking the move with the fewest
   page flips first. When icons block each other in a cycle (A wants B's cell, and B wants A's), it
   parks one of them in a spare cell. That's the same trick compilers use to turn simultaneous
   register copies into a sequence.
4. **Execute.** Before each move it compares the source page with what it expects. After the drop it
   compares the whole target page, so a reshuffled neighbour is caught immediately.
5. **Pages.** Pages the file needs beyond the phone's are created by dragging past the last page.
   Pages past the end of the file are deleted once they're empty, with the edit-mode remove button.
   A page holding a widget is never deleted.
6. **Final survey.** Confirms the phone matches the file.

## When something goes wrong

Any surprise stops the run with **exit code 3**. Surprises include being on the wrong page, an icon
not where it should be, or neighbours shifting. The run prints what it saw next to what it expected
and saves `homelayout-checkpoint.png`. Nothing further happens until you run it again, and `apply` is
safe to re-run: it surveys from scratch.

| exit | meaning |
|---|---|
| 0 | done, or nothing to do |
| 1 | bad input: layout file problems, phone locked, no device or several devices |
| 3 | checkpoint: look at the screenshot |

## Options

| option | |
|---|---|
| `-s, --serial` | adb serial (default: `$ANDROID_SERIAL`, or the only connected device) |
| `--grid 4x5` | override the detected home grid |
| `dump -o FILE` | write to FILE instead of stdout |
| `apply -n` | dry run: print the moves only |
| `apply -y` | don't ask before executing |

## Limits

- Tested on one phone: Galaxy A04e, One UI 6.1 (Android 14), 4×5 grid. Other One UI versions may
  name things differently in the view hierarchy. Other launchers won't work without changes.
- About 12 s per move. Most of that is the launcher's own timing: the long-press, the edge hover per
  page flip and the drop animation. A screen read takes about 0.7 s.
- Icons are identified by label only, since the launcher doesn't expose package names in the
  hierarchy.
- Folders are untested. They'll probably be read and dragged like icons, but their contents aren't
  managed.
