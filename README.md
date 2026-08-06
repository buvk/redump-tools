# redump_tools workflow engine

This repository now includes a Python workflow engine for three operations:

- `extract`: CHD -> ISO or BIN/CUE
- `convert`: ISO/BIN/CUE -> CHD
- `verify`: manifest.json payload checksums -> DAT validation
- `update`: download current Redump DAT and cuesheet ZIP archives for supported systems

It follows a manifest-first strategy, then DAT-assisted heuristics, then brute-force fallback.

## Layout

- `redump_engine/metadata.py`: DAT parsing, payload-only manifest load/save, checksums, DAT matching, shared selection/payload helpers
- `redump_engine/chd_ops.py`: wrappers for `chdman info`, `extractcd`, `splitbin`, `createcd`
- `redump_engine/workflow_extract.py`: extraction decision tree and cleanup
- `redump_engine/workflow_convert.py`: conversion, manifest creation, m3u generation, cleanup
- `redump_engine/filenames.py`: DAT naming helpers and disc parsing
- `redump_engine/cuesheets.py`: trusted cue lookup and replacement from cue ZIPs
- `main.py`: CLI entrypoint

## Requirements

- Python 3.11+
- `tools/chdman.exe` present
- DAT archives or files under `dats/`
- Cuesheet archives or files under `cuesheets/`

## Usage

From workspace root:

```powershell
python main.py extract
python main.py convert
python main.py verify
python main.py update
```

Single game folder:

```powershell
python main.py extract --game-dir "Sony - PlayStation/Chrono Cross (USA)"
python main.py convert --game-dir "Sony - PlayStation/Game Name"
python main.py verify --game-dir "Sony - PlayStation/Game Name"
python main.py verify --game-dir "Sony - PlayStation/Game Name" --csv reports/verify.csv
python main.py update --systems psx ps2
python main.py update --systems psx --no-cuesheets
```

Dry-run preview:

```powershell
python main.py extract --dry-run
```

Run tests:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

## Notes

- Multi-disc detection is driven by filename suffixes like `(Disc 1)`.
- Cue replacement uses the cue repository as source of truth and rewrites the first `FILE` line to match the extracted BIN filename.
- `splitbin` support depends on your `chdman` build; the workflow falls back to `extractcd` if needed.
- DAT parsing supports `.dat`/`.xml` directly and the same formats inside ZIP archives.
- `update` currently targets Redump's direct ZIP endpoints for PSX and PS2 and stores them under stable names in `dats/` and `cuesheets/`.

## Manifest Schema

Manifests now use a payload-only shape:

```json
{
	"schema_version": 1,
	"set_type": "bin-cue",
	"title": "Game Name",
	"discs": [
		{
			"disc": 1,
			"payload_files": [
				{
					"name": "Game Name (Disc 1).bin",
					"kind": "bin",
					"size": 123,
					"sha1": "...",
					"crc32": "..."
				}
			]
		}
	]
}
```

