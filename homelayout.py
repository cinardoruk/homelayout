#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["uiautomator2>=3.2"]
# ///
"""homelayout — dump or apply a Samsung One UI home-screen layout over ADB.

    homelayout.py dump [-o FILE]           print (or save) the current layout
    homelayout.py apply FILE [-n] [-y]     rearrange the phone until it matches FILE

Layout file
    one line per icon label, in reading order (left to right, top to bottom)
    a blank line starts the next page
    "."        an empty cell, to leave a gap (trailing gaps can be left out);
               a page that is just "." is an empty page
    "# ..."    comment
    cells covered by widgets are skipped, so page 1 starts below e.g. the clock row
    duplicate labels get a suffix from `dump` ("Authenticator #1", "Authenticator #2"),
    numbered in reading order of the phone's current layout
    the dock is left alone

apply never removes or installs anything: every icon on the home screen must be in FILE
and every icon in FILE must already be on the home screen. It adds pages when FILE has more
than the phone and deletes the (then empty) pages past the end of FILE.

Anything unexpected (wrong page, an icon not where it should be, neighbours reshuffled)
stops the run with exit code 3 and leaves homelayout-checkpoint.png to look at.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import NoReturn

import uiautomator2 as u2

LONG_PRESS = 1.0     # hold before the launcher turns a press into a drag
FLIP_HOVER = 1.3     # time at the screen edge for the launcher to flip one page
SETTLE = 1.0         # wait for swipe / drop animations
CHECKPOINT_PNG = 'homelayout-checkpoint.png'

Box = tuple[int, int, int, int]
Cell = tuple[int, int]          # (col, row), 0-based from the top-left
Pos = tuple[int, int, int]      # (page, col, row), page 1-based
Move = tuple[str, Pos, Pos]     # (icon id, from, to)


class Checkpoint(Exception):
    pass


# ------------------------------------------------------------------ reading the screen
def bounds(node: ET.Element) -> Box:
    x1, y1, x2, y2 = map(int, re.findall(r'\d+', node.get('bounds', '')))
    return x1, y1, x2, y2


def rid(node: ET.Element) -> str:
    return node.get('resource-id', '').rsplit('/', 1)[-1]


@dataclass
class Screen:
    page: int | None = None               # from the page indicator
    total: int | None = None
    grid_box: Box | None = None           # wsCellLayout of the centred page
    icons: list[tuple[str, Box]] = field(default_factory=list)
    widgets: list[Box] = field(default_factory=list)
    dock: list[str] = field(default_factory=list)
    edit_mode: bool = False
    delete_button: Box | None = None      # edit mode: "remove page" button of the centred page


def parse_screen(xml: str, screen_w: int) -> Screen:
    root = ET.fromstring(xml)
    s = Screen()
    nodes = list(root.iter('node'))
    for n in nodes:
        m = re.match(r'Page (\d+) of (\d+) Selected', n.get('content-desc', ''))
        if m:
            s.page, s.total = int(m[1]), int(m[2])
    layouts = [n for n in nodes if rid(n) == 'wsCellLayout']
    if layouts:
        centre = max(layouts, key=lambda n: bounds(n)[2] - bounds(n)[0])
        s.grid_box = bounds(centre)
        for n in centre.iter('node'):
            if rid(n) == 'icon' and n.get('text'):
                s.icons.append((n.get('text', ''), bounds(n)))
            elif n.get('class', '').endswith('AppWidgetHostView'):
                s.widgets.append(bounds(n))
    for n in nodes:
        if rid(n) == 'hotseat_layout':
            s.dock = [c.get('text', '') for c in n.iter('node') if rid(c) == 'icon' and c.get('text')]
    buttons = [bounds(n) for n in nodes if rid(n) == 'delete_page_layout']
    s.edit_mode = bool(buttons)
    if buttons:
        s.delete_button = min(buttons, key=lambda b: abs((b[0] + b[2]) / 2 - screen_w / 2))
    return s


@dataclass
class Grid:
    x0: int
    y0: int
    cw: float
    ch: float
    cols: int
    rows: int

    @classmethod
    def detect(cls, s: Screen, cols: int | None, rows: int | None) -> Grid:
        assert s.grid_box is not None
        x1, y1, x2, y2 = s.grid_box
        if cols is None or rows is None:
            if not s.icons:
                raise SystemExit('cannot detect the grid from an empty page; pass --grid COLSxROWS')
            cols = cols or round((x2 - x1) / statistics.median(b[2] - b[0] for _, b in s.icons))
            rows = rows or round((y2 - y1) / statistics.median(b[3] - b[1] for _, b in s.icons))
        return cls(x1, y1, (x2 - x1) / cols, (y2 - y1) / rows, cols, rows)

    def cell_of(self, b: Box) -> Cell:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        c = int((cx - self.x0) // self.cw)
        r = int((cy - self.y0) // self.ch)
        return min(max(c, 0), self.cols - 1), min(max(r, 0), self.rows - 1)

    def cells_under(self, b: Box) -> set[Cell]:
        return {(c, r) for c in range(self.cols) for r in range(self.rows)
                if b[0] <= self.x0 + (c + .5) * self.cw <= b[2]
                and b[1] <= self.y0 + (r + .5) * self.ch <= b[3]}

    def reading_order(self) -> list[Cell]:
        return [(c, r) for r in range(self.rows) for c in range(self.cols)]

    def touch_point(self, c: int, r: int) -> tuple[int, int]:
        # aim at the icon image, a bit above the cell centre where the label is
        return int(self.x0 + (c + .5) * self.cw), int(self.y0 + (r + .5) * self.ch - .13 * self.ch)


def grid_str(cells: dict[Cell, str], cols: int, rows: int) -> str:
    return '\n'.join(' | '.join(f'{cells.get((c, r), "."):14.14s}' for c in range(cols))
                     for r in range(rows))


# ------------------------------------------------------------------ the phone
@dataclass
class Survey:
    pages: list[dict[Cell, str]]          # index 0 = page 1; cell -> label
    blocked: list[set[Cell]]              # cells covered by widgets
    widgets: list[int]                    # widget count per page
    dock: list[str]


class Phone:
    def __init__(self, serial: str, grid: tuple[int, int] | None):
        self.d = u2.connect(serial)
        self.w, self.h = self.d.window_size()
        self.grid_override = grid
        self.grid: Grid | None = None

    # -- basics
    def screen(self) -> Screen:
        return parse_screen(self.d.dump_hierarchy(), self.w)

    def ensure_ready(self) -> None:
        if not self.d.info.get('screenOn'):
            self.d.screen_on()
            time.sleep(SETTLE)
        if 'isKeyguardShowing=true' in self.d.shell('dumpsys window').output:
            raise SystemExit('the phone is locked: unlock it and run again')
        self.d.press('home')
        time.sleep(SETTLE)

    def cells(self, s: Screen) -> dict[Cell, str]:
        assert self.grid is not None
        out: dict[Cell, str] = {}
        for label, b in s.icons:
            c = self.grid.cell_of(b)
            if c in out:
                self.checkpoint(f'two icons in cell {c}: {out[c]!r} and {label!r}')
            out[c] = label
        return out

    def checkpoint(self, msg: str, seen: dict[Cell, str] | None = None,
                   want: dict[Cell, str] | None = None) -> NoReturn:
        self.d.screenshot(CHECKPOINT_PNG)
        print(f'\n*** CHECKPOINT: {msg}', flush=True)
        g = self.grid
        if g and seen is not None:
            print('--- on screen:\n' + grid_str(seen, g.cols, g.rows))
        if g and want is not None:
            print('--- expected:\n' + grid_str(want, g.cols, g.rows))
        print(f'*** screenshot: {os.path.abspath(CHECKPOINT_PNG)}', flush=True)
        raise Checkpoint(msg)

    def swipe(self, direction: int) -> None:
        a, b, y = int(.86 * self.w), int(.14 * self.w), int(.56 * self.h)
        if direction > 0:
            self.d.swipe(a, y, b, y, .25)
        else:
            self.d.swipe(b, y, a, y, .25)
        time.sleep(SETTLE)

    def go_to(self, page: int) -> Screen:
        for _ in range(3):
            s = self.screen()
            if s.page is None:
                self.checkpoint(f'no page indicator on screen (going to page {page})')
            if s.page == page:
                return s
            for _ in range(abs(page - s.page)):
                self.swipe(1 if page > s.page else -1)
        self.checkpoint(f'could not reach page {page}')

    # -- survey
    def survey(self) -> Survey:
        self.ensure_ready()
        first = self.go_to(1)
        assert first.total is not None
        screens = [first] + [self.go_to(p) for p in range(2, first.total + 1)]
        if self.grid is None:
            with_icons = next((s for s in screens if s.icons), screens[0])
            cols, rows = self.grid_override or (None, None)
            self.grid = Grid.detect(with_icons, cols, rows)
        g = self.grid
        blocked = [set().union(*(g.cells_under(b) for b in s.widgets)) for s in screens]
        return Survey([self.cells(s) for s in screens], blocked,
                      [len(s.widgets) for s in screens], first.dock)

    # -- the one primitive: pick up (c,r) on page n, drop on (a,b) on page m
    def drag(self, src: Cell, dst: Cell, flips: int) -> None:
        assert self.grid is not None
        x, y = self.grid.touch_point(*src)
        tx, ty = self.grid.touch_point(*dst)
        t = self.d.touch
        t.down(x, y)
        time.sleep(LONG_PRESS)
        t.move(x + 15, y + 15)                 # a nudge turns the long-press menu into a drag
        time.sleep(.15)
        cx, cy = x + 30, y + 30
        t.move(cx, cy)
        time.sleep(.15)
        for _ in range(abs(flips)):            # hover at the edge: one page flip each time
            t.move(self.w - 10 if flips > 0 else 10, cy)
            time.sleep(FLIP_HOVER)
            cx = self.w // 2
            t.move(cx, cy)
            time.sleep(.4)
        for i in range(1, 7):                  # glide to the target cell
            t.move(cx + (tx - cx) * i // 6, cy + (ty - cy) * i // 6)
            time.sleep(.08)
        time.sleep(.6)
        t.up(tx, ty)
        time.sleep(SETTLE)

    def move(self, label: str, src: Pos, dst: Pos,
             src_before: dict[Cell, str], dst_after: dict[Cell, str]) -> None:
        n, c, r = src
        m, a, b = dst
        s = self.go_to(n)
        seen = self.cells(s)
        if seen != src_before:
            self.checkpoint(f'page {n} is not as expected before moving {label!r}', seen, src_before)
        self.drag((c, r), (a, b), m - n)
        s = self.screen()
        if s.page != m:
            self.checkpoint(f'after moving {label!r}: expected page {m}, on page {s.page}')
        seen = self.cells(s)
        if seen != dst_after:
            self.checkpoint(f'page {m} is not as expected after moving {label!r}', seen, dst_after)

    # -- pages
    def delete_page(self, page: int) -> None:
        assert self.grid is not None
        s = self.go_to(page)
        if s.icons or s.widgets:
            self.checkpoint(f'refusing to delete page {page}: it is not empty', self.cells(s))
        x, y = self.grid.touch_point(0, 0)
        t = self.d.touch
        t.down(x, y)
        time.sleep(LONG_PRESS)
        t.up(x, y)
        time.sleep(SETTLE)
        s = self.screen()
        if not s.edit_mode or s.delete_button is None or s.page != page or s.icons:
            self.checkpoint(f'edit mode for page {page} did not look right')
        assert s.total is not None
        before = s.total
        b = s.delete_button
        self.d.click((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
        time.sleep(SETTLE)
        self.d.press('home')
        time.sleep(SETTLE)
        s = self.screen()
        if s.total != before - 1:
            self.checkpoint(f'page {page} was not deleted ({before} pages before, {s.total} now)')


# ------------------------------------------------------------------ layout files
def assign_ids(pages: list[dict[Cell, str]]) -> dict[Pos, str]:
    """Label per position; duplicate labels become 'Label #k' in reading order."""
    counts = Counter(label for p in pages for label in p.values())
    seen: Counter[str] = Counter()
    ids: dict[Pos, str] = {}
    for pi, cells in enumerate(pages, 1):
        for c, r in sorted(cells, key=lambda cr: (cr[1], cr[0])):
            label = cells[(c, r)]
            if counts[label] > 1:
                seen[label] += 1
                ids[(pi, c, r)] = f'{label} #{seen[label]}'
            else:
                ids[(pi, c, r)] = label
    return ids


def render(sv: Survey, g: Grid, model: str) -> str:
    ids = assign_ids(sv.pages)
    out = [f'# homelayout dump: {model}, {datetime.now():%Y-%m-%d %H:%M}',
           '# one line per icon in reading order; blank line = next page; "." = empty cell',
           f'# dock (not managed): {", ".join(sv.dock)}', '']
    for pi in range(1, len(sv.pages) + 1):
        free = [cr for cr in g.reading_order() if cr not in sv.blocked[pi - 1]]
        lines = [ids.get((pi, c, r), '.') for c, r in free]
        while len(lines) > 1 and lines[-1] == '.':
            lines.pop()
        header = f'# page {pi}'
        if sv.widgets[pi - 1]:
            header += f' ({sv.widgets[pi - 1]} widget(s): their cells are skipped)'
        out += [header] + (lines or ['.']) + ['']
    return '\n'.join(out)


def parse_layout(text: str) -> list[list[str | None]]:
    pages: list[list[str | None]] = []
    cur: list[str | None] = []
    for raw in text.splitlines() + ['']:
        line = raw.strip()
        if line.startswith('#'):
            continue
        if not line:
            if cur:
                pages.append(cur)
                cur = []
            continue
        cur.append(None if line == '.' else line)
    return pages


def resolve_target(layout: list[list[str | None]], sv: Survey, g: Grid,
                   current: dict[str, Pos]) -> dict[str, Pos]:
    target: dict[str, Pos] = {}
    errors: list[str] = []
    for pi, entries in enumerate(layout, 1):
        blocked = sv.blocked[pi - 1] if pi <= len(sv.pages) else set()
        free = [cr for cr in g.reading_order() if cr not in blocked]
        while entries and entries[-1] is None:           # trailing gaps mean nothing
            entries = entries[:-1]
        if len(entries) > len(free):
            errors.append(f'page {pi} lists {len(entries)} cells but only {len(free)} are free')
            continue
        if pi > len(sv.pages) and not any(entries):
            errors.append(f'page {pi} is a new page and cannot be empty')
        for entry, (c, r) in zip(entries, free):
            if entry is None:
                continue
            if entry in target:
                errors.append(f'{entry!r} is listed twice')
            target[entry] = (pi, c, r)
    unknown = [i for i in target if i not in current]
    unlisted = [i for i in current if i not in target]
    for i in unknown:
        close = difflib.get_close_matches(i, list(current), n=2)
        hint = f' (did you mean {" or ".join(map(repr, close))}?)' if close else ''
        errors.append(f'{i!r} is not on the home screen{hint}')
    for i in unlisted:
        errors.append(f'{i!r} is on the home screen but not in the file')
    if errors:
        raise SystemExit('layout file problems:\n  ' + '\n  '.join(errors))
    return target


# ------------------------------------------------------------------ planning
def plan(current: dict[str, Pos], target: dict[str, Pos], blocked: list[set[Cell]],
         g: Grid) -> list[Move]:
    """Greedy: move any icon whose target cell is free (cheapest page distance first);
    when every remaining icon is blocked by another, park one blocker in a spare cell."""
    where = dict(current)
    occ = {p: i for i, p in where.items()}
    targets = set(target.values())
    total = len(blocked)
    page = 1
    moves: list[Move] = []

    def cost(i: str, dst: Pos) -> tuple[int, int]:
        return abs(page - where[i][0]) + abs(where[i][0] - dst[0]), where[i][0]

    def do(i: str, dst: Pos) -> None:
        nonlocal page, total
        moves.append((i, where[i], dst))
        del occ[where[i]]
        occ[dst] = i
        where[i] = dst
        page = dst[0]
        total = max(total, dst[0])

    while True:
        pending = [i for i in target if where[i] != target[i]]
        if not pending:
            return moves
        ready = [i for i in pending if target[i] not in occ and target[i][0] <= total + 1]
        if ready:
            i = min(ready, key=lambda i: cost(i, target[i]))
            do(i, target[i])
            continue
        stuck = [i for i in pending if target[i] in occ]
        if not stuck:
            raise SystemExit('planner stuck: new pages must be filled in order')
        i = min(stuck, key=lambda i: cost(i, target[i]))
        blocker = occ[target[i]]
        spare = [(p, c, r) for p in range(1, total + 1) for c, r in g.reading_order()
                 if (p, c, r) not in occ and (p, c, r) not in targets
                 and (p > len(blocked) or (c, r) not in blocked[p - 1])]
        if spare:
            home = target[blocker][0]
            park = min(spare, key=lambda x: (abs(x[0] - home), abs(x[0] - where[blocker][0]), x))
        else:
            park = (total + 1, 0, 0)                   # make a scratch page, pruned at the end
        do(blocker, park)


# ------------------------------------------------------------------ commands
def cmd_dump(phone: Phone, out: str | None) -> None:
    sv = phone.survey()
    assert phone.grid is not None
    text = render(sv, phone.grid, phone.d.info.get('productName', 'phone'))
    if out:
        with open(out, 'w') as f:
            f.write(text)
        print(f'wrote {out}: {len(sv.pages)} pages, {sum(map(len, sv.pages))} icons')
    else:
        print(text, end='')


def cmd_apply(phone: Phone, path: str, dry_run: bool, yes: bool) -> None:
    with open(path) as f:
        layout = parse_layout(f.read())
    sv = phone.survey()
    g = phone.grid
    assert g is not None
    ids = assign_ids(sv.pages)
    current = {i: p for p, i in ids.items()}
    raw = {i: sv.pages[p[0] - 1][(p[1], p[2])] for i, p in current.items()}
    target = resolve_target(layout, sv, g, current)
    moves = plan(current, target, sv.blocked, g)
    keep = len(layout)
    pages_after = max([len(sv.pages)] + [d[0] for _, _, d in moves])   # moves may add pages
    prune = list(range(keep + 1, pages_after + 1))
    print(f'{len(moves)} moves, {len(prune)} page(s) to delete afterwards')
    for k, (i, s, d) in enumerate(moves, 1):
        print(f'  {k:3d}. {i:26.26s} p{s[0]}({s[1]},{s[2]}) -> p{d[0]}({d[1]},{d[2]})')
    widget_pages = [p for p in prune if p <= len(sv.pages) and sv.widgets[p - 1]]
    if widget_pages:
        raise SystemExit(f'pages {widget_pages} hold widgets but are past the end of the file')
    if dry_run or (not moves and not prune):
        return
    if not yes:
        if not sys.stdin.isatty():
            raise SystemExit('not a terminal: pass --yes to execute')
        if input(f'execute {len(moves)} moves? [y/N] ').strip().lower() != 'y':
            return

    state = {p: raw[i] for i, p in current.items()}     # position -> label, as the phone should be

    def page_view(p: int) -> dict[Cell, str]:
        return {(c, r): lab for (pp, c, r), lab in state.items() if pp == p}

    t0 = time.time()
    for k, (i, s, d) in enumerate(moves, 1):
        before = page_view(s[0])
        state[d] = state.pop(s)
        phone.move(i, s, d, before, page_view(d[0]))
        print(f'[{k}/{len(moves)}] ok  {i}', flush=True)
    for p in reversed(prune):
        phone.delete_page(p)
        print(f'deleted empty page {p}', flush=True)

    final = phone.survey()
    want = [page_view(p) for p in range(1, keep + 1)]
    if final.pages != want:
        phone.checkpoint('final layout differs from the file')
    phone.d.press('home')
    print(f'done in {time.time() - t0:.0f}s: phone matches {path}')


def pick_serial(arg: str | None) -> str:
    if arg or os.environ.get('ANDROID_SERIAL'):
        return arg or os.environ['ANDROID_SERIAL']
    out = subprocess.run(['adb', 'devices'], capture_output=True, text=True).stdout
    devs = [line.split()[0] for line in out.splitlines()[1:]
            if len(line.split()) >= 2 and line.split()[1] == 'device']
    if len(devs) != 1:
        raise SystemExit(f'need exactly one ready device, found {devs or "none"}; use --serial')
    return devs[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-s', '--serial', help='adb serial (default: $ANDROID_SERIAL or the only device)')
    ap.add_argument('--grid', help='home grid as COLSxROWS, e.g. 4x5 (default: detect)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('dump', help='write the current layout')
    d.add_argument('-o', '--output', help='file to write (default: stdout)')
    a = sub.add_parser('apply', help='rearrange the phone to match a layout file')
    a.add_argument('file')
    a.add_argument('-n', '--dry-run', action='store_true', help='print the moves, change nothing')
    a.add_argument('-y', '--yes', action='store_true', help='do not ask before executing')
    args = ap.parse_args()

    grid = None
    if args.grid:
        m = re.fullmatch(r'(\d+)x(\d+)', args.grid)
        if not m:
            ap.error('--grid must look like 4x5')
        grid = int(m[1]), int(m[2])
    phone = Phone(pick_serial(args.serial), grid)
    try:
        if args.cmd == 'dump':
            cmd_dump(phone, args.output)
        else:
            cmd_apply(phone, args.file, args.dry_run, args.yes)
    except Checkpoint:
        sys.exit(3)


if __name__ == '__main__':
    main()
