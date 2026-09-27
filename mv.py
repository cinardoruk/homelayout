"""Rearrange One UI home-screen icons over ADB, one drag at a time.

Coordinates: page n is 1-based (as shown by the page dots); cells are (col, row),
0-based from the top-left of the 4x5 grid. Page 1 row 0 is the weather+clock widgets.

    python3 mv.py survey          # print every page as a grid
    python3 mv.py plan            # print the move list, change nothing
    python3 mv.py run             # execute the move list, verify after every drop
    python3 mv.py move n c r m a b

Any surprise (wrong page, icon not where expected, neighbours reshuffled) calls
checkpoint(): it saves checkpoint.png, prints what it saw, and exits with code 3 so
Claude can look at the screenshot before anything else happens.
"""
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

DEV = 'YOUR_DEVICE_SERIAL'            # from `adb devices`
COLS = [108, 275, 442, 609]           # cell centres (normal mode, 720x1600)
ROWS = [232, 456, 680, 904, 1128]

TARGET_PAGES = {                      # example layout: put your own here
    1: ['Maps', 'Camera', 'Gallery', 'Calculator',
        'Clock', 'Notes', None, None],   # None = keep the cell free
    2: ['Phone', 'Messages', 'Contacts', 'Settings'],
    3: ['My Files', 'Play Store'],
}


class Checkpoint(Exception):
    pass


# ---------------------------------------------------------------- device I/O
def sh(cmd):
    return subprocess.run(['adb', '-s', DEV, 'shell', cmd], capture_output=True, text=True).stdout


def screenshot(path):
    data = subprocess.run(['adb', '-s', DEV, 'exec-out', 'screencap -p'], capture_output=True).stdout
    with open(path, 'wb') as f:
        f.write(data)


def dump():
    """Return (current_page, total_pages, {label: (col,row)}) for the visible home page."""
    xml = ''
    for _ in range(5):
        sh('uiautomator dump /sdcard/ui.xml >/dev/null 2>&1')
        xml = subprocess.run(['adb', '-s', DEV, 'exec-out', 'cat /sdcard/ui.xml'],
                             capture_output=True, text=True).stdout
        if xml.startswith('<?xml'):
            break
        time.sleep(0.5)
    page = total = None
    grid = {}
    for n in ET.fromstring(xml).iter('node'):
        desc = n.get('content-desc', '')
        m = re.match(r'Page (\d+) of (\d+) Selected', desc)
        if m:
            page, total = int(m[1]), int(m[2])
        if n.get('resource-id', '').endswith('/icon') and n.get('text'):
            x1, y1, x2, y2 = map(int, re.findall(r'\d+', n.get('bounds', '')))
            if y1 < 1260:                                  # skip the dock
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                c = min(range(4), key=lambda i: abs(COLS[i] - cx))
                r = min(range(5), key=lambda i: abs(ROWS[i] - cy))
                grid[n.get('text')] = (c, r)
    return page, total, grid


def grid_str(grid):
    g = [['.'] * 4 for _ in range(5)]
    for k, (c, r) in grid.items():
        g[r][c] = k[:14]
    return '\n'.join(' | '.join(f'{v:14s}' for v in row) for row in g)


def checkpoint(msg, grid=None):
    screenshot('checkpoint.png')
    print(f'\n*** CHECKPOINT: {msg}')
    if grid is not None:
        print(grid_str(grid))
    print('*** screenshot saved to checkpoint.png — waiting for Claude to look at it')
    raise Checkpoint(msg)


# ---------------------------------------------------------------- navigation
_cur = None


def go_to(page):
    """Swipe to `page` and return its grid."""
    global _cur
    if _cur is None:
        _cur, _, _ = dump()
    for _ in range(abs(page - _cur)):
        if page > _cur:
            sh('input swipe 620 900 100 900 250')
        else:
            sh('input swipe 100 900 620 900 250')
        time.sleep(1.0)
    p, _, grid = dump()
    if p is None:
        checkpoint(f'no page indicator while going to page {page}', grid)
        raise AssertionError  # unreachable; checkpoint() raises
    if p != page:                                      # a swipe didn't register; retry once
        for _ in range(abs(page - p)):
            sh('input swipe 620 900 100 900 250' if page > p else 'input swipe 100 900 620 900 250')
            time.sleep(1.0)
        p, _, grid = dump()
    _cur = p
    if p != page:
        checkpoint(f'wanted page {page}, on page {p}', grid)
    return grid


def icon_xy(c, r):
    return COLS[c], ROWS[r] - 30                       # aim at the icon image, not the label


# ---------------------------------------------------------------- the one primitive
def move(n, c, r, m, a, b, label=None, expect_after=None):
    """Pick the icon at page n cell (c,r) and drop it at page m cell (a,b)."""
    global _cur
    grid = go_to(n)
    here = {v: k for k, v in grid.items()}
    if (c, r) not in here:
        checkpoint(f'no icon at page {n} ({c},{r})', grid)
    if label and here[(c, r)] != label:
        checkpoint(f'expected {label!r} at page {n} ({c},{r}), found {here[(c, r)]!r}', grid)
    label = here[(c, r)]

    x, y = icon_xy(c, r)
    tx, ty = icon_xy(a, b)
    cmds = [f'input motionevent DOWN {x} {y}', 'sleep 1.0',
            f'input motionevent MOVE {x + 15} {y + 15}', 'sleep 0.15',
            f'input motionevent MOVE {x + 30} {y + 30}', 'sleep 0.15']
    cy = y + 30
    edge = 710 if m > n else 10
    for _ in range(abs(m - n)):                        # hover at the edge -> launcher flips a page
        cmds += [f'input motionevent MOVE {edge} {cy}', 'sleep 1.3',
                 f'input motionevent MOVE 360 {cy}', 'sleep 0.4']
    sx, sy = (x + 30, cy) if m == n else (360, cy)
    for i in range(1, 7):                              # glide to the target cell
        cmds += [f'input motionevent MOVE {sx + (tx - sx) * i // 6} {sy + (ty - sy) * i // 6}', 'sleep 0.08']
    cmds += ['sleep 0.6', f'input motionevent UP {tx} {ty}']
    sh('; '.join(cmds))
    time.sleep(1.2)

    p, _, grid = dump()
    _cur = p
    if p != m:
        checkpoint(f'after moving {label!r}: expected page {m}, on page {p}', grid)
    if grid.get(label) != (a, b):
        checkpoint(f'{label!r} not at page {m} ({a},{b}) after drop', grid)
    if expect_after is not None and grid != expect_after:
        checkpoint(f'page {m} layout differs from expected after moving {label!r}', grid)
    print(f'ok  {label:24s} p{n}({c},{r}) -> p{m}({a},{b})', flush=True)
    return grid


# ---------------------------------------------------------------- survey + planner
def survey():
    sh('input keyevent KEYCODE_HOME')
    time.sleep(1.2)
    global _cur
    _cur, total, grid = dump()
    if total is None:
        checkpoint('could not read the page indicator', grid)
        raise AssertionError  # unreachable; checkpoint() raises
    state = {}                                          # (page, c, r) -> label
    for p in range(1, total + 1):
        for label, (c, r) in go_to(p).items():
            state[(p, c, r)] = label
    return state, total


def target_cells():
    tgt, reserved = {}, set()
    for p, labels in TARGET_PAGES.items():
        cells = [(c, r) for r in range(5) for c in range(4) if not (p == 1 and r == 0)]
        for label, (c, r) in zip(labels, cells):
            if label is None:
                reserved.add((p, c, r))
            else:
                tgt[label] = (p, c, r)
    return tgt, reserved


def plan(state, total):
    tgt, reserved = target_cells()
    where = {v: k for k, v in state.items()}
    missing = [l for l in tgt if l not in where]
    extra = [l for l in where if l not in tgt]
    if missing or extra:
        raise SystemExit(f'layout mismatch — missing on phone: {missing}, not in target: {extra}')
    occ = dict(state)                                   # cell -> label
    blocked = {(1, c, 0) for c in range(4)} | reserved   # widget row + reserved cells
    all_cells = [(p, c, r) for p in range(1, total + 1) for r in range(5) for c in range(4)]
    target_set = set(tgt.values())
    moves, cur_page = [], 1

    def do(label, dst):
        nonlocal cur_page
        src = where[label]
        moves.append((label, src, dst))
        del occ[src]
        occ[dst] = label
        where[label] = dst
        cur_page = dst[0]

    def cost(src, dst):
        return abs(cur_page - src[0]) + abs(src[0] - dst[0])

    while True:
        pending = [l for l in tgt if where[l] != tgt[l]]
        if not pending:
            return moves
        ready = [l for l in pending if tgt[l] not in occ]
        if ready:
            l = min(ready, key=lambda l: cost(where[l], tgt[l]))
            do(l, tgt[l])
            continue
        # every pending target is occupied by another pending icon: park one blocker
        l = min(pending, key=lambda l: cost(where[l], tgt[l]))
        blocker = occ[tgt[l]]
        free = [x for x in all_cells if x not in occ and x not in blocked and x not in target_set]
        home = tgt[blocker][0]
        park = min(free, key=lambda x: (abs(x[0] - home), abs(x[0] - where[blocker][0]), x))
        do(blocker, park)


def expected_page(occ, p):
    return {lab: (c, r) for (pp, c, r), lab in occ.items() if pp == p}


def run(dry=False):
    state, total = survey()
    moves = plan(state, total)
    print(f'{len(moves)} moves planned', flush=True)
    occ = dict(state)
    for i, (label, src, dst) in enumerate(moves, 1):
        del occ[src]
        occ[dst] = label
        print(f'[{i}/{len(moves)}] ', end='', flush=True)
        if dry:
            print(f'{label:24s} p{src[0]}({src[1]},{src[2]}) -> p{dst[0]}({dst[1]},{dst[2]})')
            continue
        move(*src, *dst, label=label, expect_after=expected_page(occ, dst[0]))
    if not dry:
        sh('input keyevent KEYCODE_HOME')
        print('done', flush=True)


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'plan'
    try:
        if cmd == 'survey':
            st, tot = survey()
            for p in range(1, tot + 1):
                print(f'=== PAGE {p} ===')
                print(grid_str(expected_page(st, p)))
        elif cmd == 'plan':
            run(dry=True)
        elif cmd == 'run':
            run()
        elif cmd == 'move':
            n, c, r, m, a, b = map(int, sys.argv[2:8])
            move(n, c, r, m, a, b)
    except Checkpoint:
        sys.exit(3)
