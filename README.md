# PhotoDedup · 图片去重工坊

**English** ｜ [简体中文](README.zh-CN.md)

A desktop tool that scans a folder tree for duplicate images and then **walks you through them one pair at a time**:
two images side by side, a recommendation on which one to keep and *why*, delete the loser with `Delete`, move on
to the next pair. Read-only until you press a key — no "clean everything" button to regret.

![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![Platform](https://img.shields.io/badge/platform-Windows-0078D4)
![License](https://img.shields.io/badge/license-MIT-green)

![Compare page](docs/screenshots/03_compare_side.png)

## Why

The same photo usually exists in a library in many shapes: the original from the camera, a compressed copy
forwarded through WeChat, a resized version for a slide, a copy with rewritten EXIF, a version rotated 90°.
Filename and file size won't find them; MD5 only catches files where *not a single byte* changed.

PhotoDedup does three things:

1. **Layered matching** — the byte layer and pixel layer are allowed to conclude; perceptual layers only propose
   candidates. The further down, the less certain.
2. **Pairwise decisions** — tournament style. `Delete` drops the non-recommended one, `Z` undoes it.
3. **A paper trail** — what was deleted, what was kept, and why, all written to a report.

## Install

### Prebuilt binary (Windows, no Python needed)

Download `PhotoDedup-1.0.0-win64.exe` from [Releases](../../releases/latest) and run it. Single file, ~103 MB,
no installer. First launch unpacks to a temp folder (2–4 s).

### From source

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python run.py
```

or `python -m photodedup` with `src` on `PYTHONPATH`.

## Usage

1. Pick a folder (drag & drop works), adjust the threshold if needed, hit **Start scan**
2. The compare page opens automatically: two images, each tagged **Keep** or **Delete**
3. Read the reason underneath. Agree → `Delete`. Disagree → press `1`/`2` to pick the loser yourself
4. Groups with 3+ images run as a tournament: the winner stays and faces the next one
5. Finish → a Markdown + CSV report is written. Deletions go to the Recycle Bin; `Z` pulls them back

### Shortcuts

| Key | Action |
|---|---|
| `Delete` | Delete the recommended one |
| `1` / `2` | Delete left / right explicitly |
| `Space` | Keep both, next pair |
| `Z` / `Ctrl+Z` | Undo last deletion |
| `F` | Cycle view: side-by-side → overlay → difference |
| `+` / `-` / `0` | Zoom in / out / reset |
| Wheel | Zoom at cursor, both images stay in sync |
| Drag | Pan, both images stay in sync |
| `Esc` | Finish and write the report |

### Three views

- **Side by side** — the default; synchronized zoom and pan
- **Overlay** — B blended over A with a slider; push it to either end to inspect one image, keep it centered to spot
  small shifts
- **Difference** — mismatches rendered as a red heatmap, so you can tell compression noise from a change of content

| Overlay | Difference |
| --- | --- |
| ![Overlay](docs/screenshots/04_compare_overlay.png) | ![Difference](docs/screenshots/05_compare_diff.png) |

| Start | Scanning | Done |
| --- | --- | --- |
| ![Start](docs/screenshots/01_start.png) | ![Progress](docs/screenshots/02_progress.png) | ![Finish](docs/screenshots/08_finish.png) |

## How it decides

Four gates, each one more expensive than the last:

| Layer | Criterion | Strength of conclusion |
|---|---|---|
| 1. Bytes | Identical MD5 | **Certain** — deleting either loses nothing |
| 2. Pixels | Full decode, byte-identical RGB | **Certain** — only container/metadata differ |
| 3. Perceptual | pHash (64-bit DCT low frequency) + dHash, with 8 rotation/mirror variants | Candidates only, **never certain** |
| 4. Geometry | ORB features + RANSAC homography + difference distribution after alignment (including **the number of significant blocks**) | Decides whether a candidate may be promoted |

Layer 4 is not optional, because **layers 2 and 3 can be fooled at the same time**. Two photos taken seconds apart
share an identical background: pHash may differ by 8 bits, ORB inlier ratio can reach 0.99 — yet they are two
different photographs. The discriminator is the *shape* of the difference:

- The same photo in another encoding → differences are scattered encoding noise, **0 significant blocks**
- A burst shot → the difference is one concentrated region where the subject moved
- Two screenshots of the same app taken minutes apart → **many small changes, ~8 blocks**

That criterion was not invented at a desk; it came from running the tool over 6,629 real screenshots. Measured
numbers and threshold calibration are in [`docs/algorithm.md`](docs/algorithm.md) (Chinese).

## Recommendation engine

Scored by "what would hurt if it were deleted":

| Dimension | Weight | Signal |
|---|---|---|
| Pixel count | 35 | Higher resolution leaves more room to zoom and re-crop |
| Effective sharpness | 20 | Laplacian variance; detects over-compression and downscaling |
| JPEG quality | 15 | Estimated from quantization tables (IJG); lossless formats get a bonus |
| Size at equal resolution | 10 | Bigger usually means lighter compression |
| Metadata completeness | 10 | EXIF / camera / GPS / ICC — flags forwarded copies |
| Capture time | 5 | Earlier is more likely the original |
| Filename | 5 | `_copy`, `(1)`, `IMG-副本`, etc. |

Two hard rules:

- **If the scores are within 6 points, `Delete` does nothing** — you must pick with `1`/`2`
- Crops, rotations and mirrors get an explicit warning instead of a silent decision

## Safety

1. Deletions go to the **Windows Recycle Bin**. `Z` asks the shell to restore the file; if that fails, you get a hard
   error, never a silent lie.
2. Alternatively, move files into `<folder>\_去重待删_<timestamp>\` keeping the original tree — restoring is a move
   back, 100 % reliable, good for reviewing a batch before purging it.
3. **At least one file per group survives** — deleting down to one ends the group.
4. Size and mtime are verified right before each delete; externally modified files are refused.
5. Failures are never silent: an error banner appears and you stay on the current pair.
6. Nothing is ever deleted automatically — every deletion is a key you pressed.

## Tuning the threshold

The default threshold of 8 is a starting point:

- Mostly camera originals plus messaging-app forwards → 8 is fine
- "They're identical but it didn't find them" → raise to 10–12; more burst shots will appear as candidates
  (labelled *same scene, different frame* rather than grouped)
- "These are different but it grouped them" → lower to 5–6, and keep that pair as a negative regression sample

## Development

```bash
.venv\Scripts\python -m pytest -q                      # 23 tests, incl. bursts / flat walls / unrelated images
.venv\Scripts\python scripts\diag_scan.py <folder>     # headless scan: groups and reasons
.venv\Scripts\python scripts\ui_preview.py --dir <folder> --out _shots   # render every page to PNG
.venv\Scripts\python scripts\make_icon.py              # regenerate the icon
```

### Build

```bash
.venv\Scripts\python scripts\build.py      # package + rename + selftest
```

Output: `dist\图片去重工坊.exe` plus an ASCII-named copy `dist\PhotoDedup-<version>-win64.exe` for releases.
The selftest deletes a temporary file, pulls it back from the Recycle Bin and verifies the content, so the shipped
binary proves that *delete → undo* works on your machine (`--selftest` can also be run standalone).

## Layout

```
src/photodedup/
    core/               UI-free engine, unit-testable
        scanner.py      recursive walk, format allowlist, skip rules
        imageio.py      decoding, EXIF, JPEG quality estimate, sharpness
        hashing.py      MD5 / pHash / dHash / rotation-mirror variants
        verify.py       ORB alignment + difference distribution + verdict
        grouping.py     MIH candidate generation + union-find
        pipeline.py     orchestration, thread pool, sqlite fingerprint cache
        recommend.py    scoring and reason generation
        session.py      tournament queue, delete, undo, reports
    ui/                 PySide6 front end
        widgets.py      image pane, info card, advice panel
        compare_page.py comparison page
        pages.py        start / progress / finish pages
        main_window.py  main window, scan worker, shortcuts
tests/                  end-to-end tests on synthetic samples
scripts/                diagnostics and packaging helpers
docs/                   algorithm notes and screenshots
```

## Known limitations

- **Heavy crops are missed** — cropping 20 % pushes the pHash distance to 24 (threshold is 8). Intentional:
  missing a duplicate is cheaper than a wrong merge.
- **Large tonal edits / big watermarks** look like "different content" — the difference region is too large to
  distinguish from a moved object, so they are excluded.
- **Flat images** (sky, white walls, scans) give ORB nothing to match, so they stay at low confidence and are not
  grouped.
- **Mirrors/flips** can't be verified by homography; they only match through hash variants and never reach "high".
- **HEIC** needs `pillow-heif` (bundled); RAW (CR3/NEF) only gets byte-level comparison.
- **GIF** only the first frame.
- A perceptual verdict is never "the same file" — glance at the picture before deleting.

## Benchmarks

| Folder | Files | Time | Result |
|---|---|---|---|
| Synthetic suite (9 variant types + 8 negative cases) | 17 | 1.0 s | 1 group of 9; negatives all clean |
| A real screenshot folder (phone captures, 2018–2025) | 6,629 | 63 s (6,625 cache hits) | 18 groups / 22 duplicates, every pair verified by eye |

## License

[MIT](LICENSE). The algorithm and the calibration process are documented in
[`docs/algorithm.md`](docs/algorithm.md) (Chinese).
