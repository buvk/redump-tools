# redump-tools

A Python/PySide6 desktop app and command-line tool for organizing, converting, and checking disc-image collections.

## Supported formats

| System | Original images | Compressed images | Tool |
| --- | --- | --- | --- |
| PlayStation / PlayStation 2 | ISO or BIN/CUE | CHD | chdman |
| Dreamcast | Redump BIN/CUE | CHD | chdman |
| Xbox | Original ISO | XISO | extract-xiso |
| GameCube / Wii | ISO | RVZ | DolphinTool |

CHD and RVZ can be restored to original image formats. Xbox XISO conversion removes disc data; it cannot recreate a byte-identical Redump original ISO.

## Install and start

Requires Python 3.11 or newer. The desktop dependency uses Qt 6.8–6.11; Qt 6.12 is excluded after a Python 3.11 shutdown crash in CI. From the project folder on Windows:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[desktop]"
.venv\Scripts\python.exe -m redump_engine.desktop
```

You can also double-click `redump-tools.pyw`. The launcher finds the checkout's local `.venv` if the current Python environment lacks Qt.

An installed package provides `redump-tools-ui` for the desktop and `redump-tools` for the CLI. `redump-tools-cli.py` and `python -m redump_engine` run the CLI from a checkout.

Configure the executable locations in **Tools > Settings > Tools**. In a checkout, the initial locations are `tools/chdman.exe`, `tools/extract-xiso.exe`, and `tools/DolphinTool.exe`. The executables are not included in the repository.

## Library

1. Use **File > Change library folder** to choose a collection or system folder.
2. Enable **File > Scan subfolders** when games are in nested folders.
3. Select games, then use **Actions** or the right-click menu.

The systems sidebar, search field, and format filter narrow the library. Search matches game titles and filenames. **Refresh** and **F5** rescan without closing Settings. The library refreshes as files are processed.

- **Game** shows the recorded game title when available, with a filename-based fallback.
- **Filename** shows the exact filename and extension. It is hidden by default; enable it under **View > Columns**.
- **View > Group multi-disc games** groups related discs under a set row. Expand or collapse sets individually or together.
- Set rows aggregate file sizes, original sizes, space saved, and verification state. Current size counts all stored images; original size counts each disc once across formats. Unknown original sizes are shown explicitly. Verification labels distinguish DAT matches, integrity checks, and mixed checks.
- Columns are resizable. **View > Fit columns to contents** / **Ctrl++** fits the active Library or Results table and keeps the last column filling the available width. Double-click a header divider to fit one column.
- Double-click a disc, or choose **View disc details** from its context menu, for paths, original checksums, associated files, and verification scope.

Library column visibility, widths, sorting, grouping, filters, and window layout are remembered.

## Conversion and restoration

Source cleanup retains BIN tracks referenced by other CUE files in the surrounding system or collection. Folder renaming is refused if it would break those shared references. Updating a disc record retains alternate-image associations when the original payload sizes and checksums are unchanged.

Overwriting detaches the selected output before replacement, so other hard-linked files remain unchanged. Overwrite can be combined with official Redump naming for CHD, RVZ, and XISO. Empty old folders are removed after a successful rename and rollback-backup cleanup.

Each conversion/restore action opens a batch review. Options apply to that batch only:

- **Keep original files** starts enabled.
- **Overwrite existing output files** starts disabled. It replaces destination files, never the input image.
- **Use official Redump names** starts disabled. A unique complete DAT match is required before changing the output filename and game folder together.

File lists use compact names, with enough folder context to distinguish duplicates. **Show full paths** switches to complete paths without changing the description of the operation.

CHD conversion checks the created CHD's integrity. RVZ conversion performs an ISO round trip and compares original payload checksums. Xbox conversion records original ISO checksums and checks that the XISO can be listed; those original checksums do not verify the XISO contents.

Conversion and CHD restoration check input content before committing changes, including same-size edits with preserved timestamps. Failures restore registered outputs, manifests, and playlists. Copy-based moves validate copied bytes before deleting the source.

CHD restoration prepares outputs in a temporary folder on the same drive. Allow space for restored files and backups of overwritten outputs. If the source CHD is removed, local and system-level playlists are updated to the restored ISO or CUE, preserving custom names, ordering, comments, and formatting.

**Stop after current game** lets the active operation finish. **Continue batch** cancels a pending stop request.

## Verification

Verification history tracks file identity and modification/change times. On Windows it uses the filesystem change timestamp, so same-size edits that restore the modification time still invalidate previous results. Refreshing history does not read image contents. Malformed manifests are rejected without stopping background verification.

These checks have different scopes:

| Action | What it establishes |
| --- | --- |
| Verify manifest against Redump DAT | Recorded original payload checksums match a unique complete DAT entry; image bytes are not re-read. |
| Verify original files against Redump DAT | Existing original ISO/BIN files are rehashed, compared with the manifest, and matched against the DAT. |
| Check image integrity | chdman or DolphinTool finds no image integrity problems; this does not establish a DAT match. |
| Verify contents against Redump DAT | Actual compressed-image contents match Redump. CHD payloads are restored temporarily and hashed; DolphinTool reads Nintendo images directly. |

Complete DAT matching preserves repeated tracks and checks payload counts, recorded sizes, available checksums, and system scope. A naming/layout warning is distinct from a checksum failure.

Independent verification proofs are retained per image. A later integrity check does not erase a valid DAT match. The library displays the strongest valid proof, while valid failures or warnings remain visible. If a DAT changes, an independent integrity proof can remain valid. If the image changes, its old proofs expire.

Results carry fingerprints of the files and DATs actually checked. Late results are not cached against changed inputs. Original-file checks also fingerprint declared payloads in subfolders.

## Manifests and organization

Consolidation refuses conflicting existing disc records before changing any files. Compatible records retain their image associations when sidecars are combined into the canonical manifest.

Naming repair preserves custom playlist filenames and updates their image and parent-playlist references. Track moves are refused when they would break another CUE file outside the selected game records.

Manifest creation checks source identity and content throughout processing. If an input changes or publication fails, previous manifests are restored and the source images are kept.

**Manifests > Create missing manifests** records original payload sizes and checksums. For CHD/RVZ inputs, this can require temporary restoration and sufficient free space. Existing records are preserved. Nested BIN paths are stored relative to the game folder.

**Fix names and folders** prepares a review of file moves, renames, manifest updates, and CUE/M3U reference changes. It requires approval before applying changes. Show full paths changes only the displayed paths.

Alternate images sharing the same game manifest are organized in one transaction. Unrelated game records stay in the source manifest. Approved execution is bound to the reviewed effects and relevant input fingerprints; changed plans require another review.

Metadata updates and rollback use atomic replacement, leaving unselected hard-linked copies unchanged. Rollback restores registered changes in reverse order. If another file occupies a moved original's old path, rollback preserves both files and reports where the original remains rather than deleting it.

## Results

Results has **Game / item**, **System**, **Operation**, **Status**, and **Details** columns. Search includes the new context columns. Click headers to sort; use status filters to focus on failures, warnings, or skipped items.

The lower panel has always-available **Details** and **Log** tabs. **Copy** copies the active tab, briefly shows **Copied**, and is disabled for empty content. Result context menus provide **Open folder**. Naming warnings can provide **Fix names and folders**.

**Export CSV** exports all result rows, including complete diagnostics, regardless of the active filter. **Retry failed** is enabled only when failed results can be retried; it opens a fresh action review. The completion summary shows counts and elapsed time.

Automatic manifest rechecks run after organization/conversion. An individual error appears in Results and Log without stopping checks for other games.

## Settings and data

**Tools > Settings** contains appearance, DAT/cuesheet status and downloads, and executable locations. **OK** saves edits. **Cancel**, Escape, and closing the dialog discard edits; appearance previews revert on cancellation.

Windows application data lives under `%LOCALAPPDATA%\redump-tools`:

```text
redump-tools/
  settings.json
  dats/
  cuesheets/
  cache/
    verification.json
```

The desktop manages the DAT/cuesheet folders; they have no folder pickers. Tool executable locations remain configurable. Bundled checkout tool paths are saved relative to the app folder. Custom tool locations stay absolute.

Use **Tools > Refresh DATs and cuesheets**, or the button in Settings, to download both together for the selected systems. Empty, malformed, incompatible, or failed downloads retain the previous archive. Timestamps describe downloads made by the app and use abbreviated timezones.

Settings and verification history have separate load/save APIs and timers. Deleting `settings.json` while the app is closed resets preferences without deleting verification history or game files. Deleting `cache/verification.json` forgets remembered checks. Parsed DAT indexes are disposable caches beside the DAT downloads.

Verification history shares repeated DAT fingerprints on disk. Changes to verification rules can require a fresh check. Custom settings paths used by tests have independent sibling cache files.

## Command line

Deletion honors `--dry-run` before or after `delete`, reporting planned deletions without removing files. The explicit file list and `--confirm-delete` are still required.

```powershell
python redump-tools-cli.py --help
python redump-tools-cli.py update --systems psx ps2 dc xbox gamecube wii
python redump-tools-cli.py convert --game-dir "E:\Games\Sony - PlayStation 2\Game" --keep-sources
python redump-tools-cli.py extract --game-dir "E:\Games\Sony - PlayStation\Game" --keep-sources
python redump-tools-cli.py verify --game-dir "E:\Games\Sony - PlayStation\Game"
python redump-tools-cli.py verify --rehash --game-dir "E:\Games\Sony - PlayStation\Game"
python redump-tools-cli.py verify --chd-integrity --game-dir "E:\Games\Sony - PlayStation\Game"
python redump-tools-cli.py chd-verify --game-dir "E:\Games\Sony - PlayStation\Game"
python redump-tools-cli.py rvz --game-dir "E:\Games\Nintendo - GameCube"
python redump-tools-cli.py rvz-extract --game-dir "E:\Games\Nintendo - Nintendo Wii"
python redump-tools-cli.py nintendo-verify --game-dir "E:\Games\Nintendo - GameCube"
python redump-tools-cli.py nintendo-integrity --game-dir "E:\Games\Nintendo - Nintendo Wii"
python redump-tools-cli.py organize --game-dir "E:\Games\Microsoft - Xbox" --dry-run
```

**CLI retention differs from the desktop defaults:** use `--keep-sources` with CHD `convert` and `extract` to retain inputs. Xbox/RVZ commands retain sources unless `--delete-source` is passed. Check command help before running cleanup operations.

The CLI also accepts explicit `--dats`, `--cuesheets`, `--chdman`, and `--dolphin-tool` locations. `--dry-run` previews supported modifying workflows. Permanent deletion requires an explicit file selection and `--confirm-delete`.

## Development

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"
.venv\Scripts\python.exe -m ruff check . --no-cache
.venv\Scripts\python.exe -m pyright
.venv\Scripts\python.exe -m unittest discover -s tests
```

Automated tests use temporary fixtures and fake external tools; Qt tests run headlessly. CI runs on Windows and Linux with Python 3.11 and 3.13.

Manual smoke checks with the real tools have also passed on disposable synthetic fixtures: CD/DVD CHD and GameCube RVZ round trips preserved payload bytes, and extract-xiso handled an already-optimized image while preserving its input. These checks do not establish game bootability, full Redump Xbox ISO conversion, or byte-identical Xbox restoration. Use disposable files or read-only verification for further real-tool checks.

Core modules:

- `execution.py`, `batch.py`, `file_batch.py`: shared workflow execution and event reporting.
- `workflow_*.py`: conversion, restoration, manifests, organization, and verification.
- `file_transaction.py`, `file_io.py`, `source_guard.py`: rollback, source identity/content checks, and validated transfers.
- `payload_paths.py`, `payload_matching.py`: shared relative-path resolution and complete DAT matching.
- `organization_group.py`, `layout_contract.py`, `layout_review.py`: combined plans, approvals, and review text.
- `library_*.py`: discovery, metadata lookup, grouping, eligibility, and presentation. Manifest lookups use a per-folder index.
- `file_identity.py`, `verification_cache.py`, `verification_evidence.py`, `verification_storage.py`: filesystem fingerprints, asynchronous checks, independent proofs, and compact persistence.
- `qt_app.py`, `qt_operations.py`, `qt_background.py`, `qt_menus.py`: desktop orchestration.
- `qt_layout.py`, `qt_widgets.py`: shared spacing, dialog buttons, search rows, text views, column fitting, and clipboard feedback.
- `qt_library.py`, `qt_results.py`, `qt_settings.py`, `qt_action_review.py`, `qt_details.py`: focused UI components.

The bundled visual assets are `redump-tools.ico` and the light/dark dropdown chevrons. Executables, downloaded references, collection images, settings, caches, and build outputs are excluded from Git.
