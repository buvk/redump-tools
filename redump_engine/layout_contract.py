"""Bind an organization approval to the exact reviewed plan and inputs."""
import hashlib
import json

from .workflow_layout import LayoutPlan


def plan_contract(plan: LayoutPlan) -> str:
    identities = []
    for path in sorted(plan.fingerprints):
        stat = path.stat() if path.exists() else None
        identities.append((str(path), [stat.st_ctime_ns, stat.st_dev, stat.st_ino] if stat else None))
    directories = []
    for folder, names in plan.directories.items():
        relevant = {path.name for path in plan.fingerprints if path.parent == folder}
        # A shared system folder may legitimately change when an earlier game
        # in this approved batch completes. Bind only this plan's entries;
        # apply() still validates the full freshly planned directory snapshot.
        directories.append((str(folder), sorted(name for name in names if name in relevant) if names is not None else None))
    data = {"identities": identities, "source": str(plan.source_folder), "folder": str(plan.folder), "manifest": str(plan.manifest),
            "moves": sorted((str(old), str(new)) for old, new in plan.moves.items()),
            "writes": sorted((str(path), hashlib.sha256(content).hexdigest()) for path, content in plan.writes.items()),
            "obsolete": sorted(map(str, plan.obsolete)), "copies": sorted(map(str, plan.copies)),
            "files": sorted((str(path), value) for path, value in plan.fingerprints.items()),
            "directories": sorted(directories)}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()
