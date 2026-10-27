"""Workflow execution shared by explicit file selections and game folders."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field, replace
from pathlib import Path

from .actions import ActionSpec, VerificationMode
from .batch import iter_batch
from .chd_ops import Chdman
from .cuesheets import CueRepository
from .dolphin_ops import DolphinTool
from .events import EventReporter, ResultRecord
from .filenames import is_xiso
from .image_conversion import convert_image
from .layout_contract import plan_contract
from .library import is_manifest, item_for, manifest_for
from .manifest_layout import manifest_layout_warnings
from .metadata import DatIndex, load_manifest
from .systems import system_from_path
from .verification_cache import signature
from .verification_results import integrity_pass
from .workflow_chd_verify import verify_chd_contents
from .workflow_convert import ConvertWorkflow
from .workflow_extract import ExtractWorkflow
from .workflow_layout import plan_layout
from .workflow_manifest import ManifestWorkflow
from .workflow_nintendo_verify import verify_nintendo_image
from .workflow_organize import plan_organization
from .workflow_rvz import process_rvz
from .workflow_verify import VerifyResult, VerifyWorkflow
from .workflow_xiso import convert_xiso


@dataclass(frozen=True)
class ExecutionOptions:
    dry_run: bool = False
    verbose: bool = False
    keep_sources: bool = True
    rename_folder: bool = True
    extract_xiso: Path | None = None
    use_redump_names: bool = False
    dolphin_tool: Path | None = None
    approved_plans: dict[str, str] | None = None
    dats: Path | None = None


@dataclass
class ExecutionResult:
    records: list[ResultRecord] = field(default_factory=list)
    verification: VerifyResult | None = None
    moved_folder: tuple[Path, Path] | None = None
    path_changes: dict[Path, Path] = field(default_factory=dict)


class WorkflowExecutor:
    def __init__(self, action: ActionSpec, chdman: Chdman, dat_index: DatIndex | None,
                 cues: CueRepository, options: ExecutionOptions, events: EventReporter, *, selected: bool):
        self.action, self.chdman, self.options, self.selected = action, chdman, options, selected
        rename = options.rename_folder and not selected
        self.convert = ConvertWorkflow(chdman, dat_index, options.dry_run, options.verbose, options.keep_sources,
                                       rename_folder=options.use_redump_names if selected else options.rename_folder,
                                       use_redump_names=options.use_redump_names,
                                       on_warning=lambda message: events.result("Source cleanup", "Warning", message))
        self.extract = ExtractWorkflow(chdman, dat_index, cues, options.dry_run, options.verbose, options.keep_sources,
                                       rename_folder=rename, on_warning=lambda message: events.result("Notice", "Warning", message))
        self.verify = VerifyWorkflow(dat_index, options.verbose, action.verification == VerificationMode.ORIGINALS)
        self.dolphin = DolphinTool(options.dolphin_tool or Path("tools/DolphinTool.exe"), events.tool_progress)
        self.manifests = ManifestWorkflow(chdman, dat_index, cues, options.verbose, dolphin=self.dolphin)

    def _verification(self, path: Path, metadata: Path | None, source: Path | None) -> ExecutionResult:
        folder = path.parent if self.selected else path
        result = self.verify.process_game_dir(folder, metadata)
        status = {"passed": "Passed", "failed": "Failed", "skipped": "Skipped"}[result.status]
        label = "Original ISO/BIN files rehashed and match manifest + Redump DAT" if self.action.verification == VerificationMode.ORIGINALS else "Manifest matches Redump DAT"
        detail = (f"{label}: {result.matched_dat_name}. " if status == "Passed" else "") + "; ".join(result.details)
        recorded = load_manifest(result.manifest_path)
        summary = label + "." if status == "Passed" else None
        suggested_action = None
        if status == "Passed" and recorded:
            naming = manifest_layout_warnings(result.manifest_path, recorded, self.verify.dat_index)
            if naming:
                status = "Warning"
                notes = "Naming/layout needs attention:\n" + "\n".join(naming)
                detail += "\n\n" + notes
                result.details.append(notes)
                summary = "Recorded checksums match Redump DAT; naming/layout needs attention."
                suggested_action = "repair-layout"
        if recorded and any(is_xiso(Path(name)) for disc in recorded.disc_entries for name in disc.source_files):
            detail += ". This checks the original ISO manifest only; the XISO itself was not verified."
        return ExecutionResult([ResultRecord(recorded.title if recorded else path.name, status, detail, folder, reference=result.manifest_path, source=source,
                                             summary=summary, suggested_action=suggested_action, check_mode=self.action.verification.value)], result)

    def _integrity(self, path: Path, source: Path | None) -> ResultRecord:
        try:
            self.chdman.verify(path)
            return integrity_pass(path, source)
        except Exception as exc:
            return ResultRecord(path.name, "Failed", str(exc), path.parent, source=source, check_mode=VerificationMode.IMAGE_INTEGRITY)

    def execute(self, path: Path) -> ExecutionResult:
        mode = self.action.verification
        dats = self.options.dats
        if mode == VerificationMode.NONE or dats is None:
            return self._execute(path)
        targets = {path}
        if mode in {VerificationMode.MANIFEST, VerificationMode.ORIGINALS}:
            metadata = manifest_for(path) if self.selected else path / "manifest.json"
            if metadata:
                targets.add(metadata)
        before = {str(target.resolve()): signature(target, mode, dats) for target in targets}
        index = self.verify.dat_index
        if index is not None and index.source_inputs is not None:
            for inputs in before.values():
                if inputs and "dats" in inputs and inputs["dats"] != list(index.source_inputs):
                    raise ValueError("DAT files changed after loading; run the operation again")
        outcome = self._execute(path)
        bound = {}
        changed = False
        for target in targets:
            inputs = before[str(target.resolve())]
            current = signature(target, mode, dats)
            changed |= inputs is not None and inputs != current
            if inputs is not None and inputs == current:
                bound[str(target.resolve())] = inputs
        if changed:
            outcome.records = [replace(record, status="Failed", summary="Verification inputs changed; run the check again.",
                                       details=record.details + "\n\nInputs changed during verification; no result was cached.", verified_inputs={})
                               if record.status in {"Passed", "Warning"} else record for record in outcome.records]
        else:
            outcome.records = [replace(record, verified_inputs=bound) for record in outcome.records]
        return outcome

    def _execute(self, path: Path) -> ExecutionResult:
        action, options = self.action, self.options
        source = path if self.selected else None
        folder = path.parent if self.selected else path
        status = "Planned" if options.dry_run else "Completed"
        if self.selected:
            if action.id in {"repair-layout", "organize"} and not path.exists():
                return ExecutionResult([ResultRecord(path.name, "Skipped", "Manifest already consolidated; refresh the library", folder, source=source)])
            item = item_for(path)
            eligibility = "verify" if action.verification == VerificationMode.ORIGINALS else action.id
            if not item.eligible(eligibility):
                return ExecutionResult([ResultRecord(path.name, "Skipped", item.problem or f"{action.id} does not apply to {item.kind}", folder, source=source)])
        if action.verification == VerificationMode.CHD:
            files = [path] if self.selected else sorted(path.glob("*.chd"))
            return ExecutionResult([self._integrity(file, file if self.selected else None) for file in files])
        if action.id == "chd-verify":
            return ExecutionResult([verify_chd_contents(path, self.manifests)])
        if action.verification in {VerificationMode.IMAGE, VerificationMode.IMAGE_INTEGRITY}:
            return ExecutionResult([verify_nintendo_image(path, self.dolphin, self.verify.dat_index,
                                                          integrity_only=action.verification == VerificationMode.IMAGE_INTEGRITY)])
        if action.command == "verify":
            metadata = (path if is_manifest(path) else manifest_for(path)) if self.selected else None
            if self.selected and metadata is None:
                return ExecutionResult([ResultRecord(path.name, "Skipped", "No manifest found for this disc", folder, source=source)])
            return self._verification(path, metadata, source)
        if action.id in {"repair-layout", "organize"}:
            plan = (plan_organization if action.id == "organize" else plan_layout)(path, self.verify.dat_index)
            if options.approved_plans is not None and options.approved_plans.get(str(path.resolve())) != plan_contract(plan):
                raise ValueError("Organization plan changed since review; no changes applied to this game. Review the selection again.")
            if options.dry_run:
                return ExecutionResult([ResultRecord(path.name, "Planned", plan.description(), folder, source=source,
                                                     summary="Naming/layout changes planned; see full details.")])
            if not plan.changed:
                return ExecutionResult([ResultRecord(path.name, "Skipped", "Naming/layout is already correct", folder, source=source)])
            detail = plan.description()
            plan.apply()
            changes = {**plan.moves, **{old: plan.manifest for old in plan.obsolete}}
            moved = (plan.source_folder, plan.folder) if plan.source_folder != plan.folder and not plan.folder.is_relative_to(plan.source_folder) else None
            result_source = plan.moves.get(path, path) if action.id == "organize" and not is_manifest(path) else plan.manifest
            return ExecutionResult([ResultRecord(plan.title, "Completed", detail, plan.folder, reference=plan.manifest, source=result_source,
                                                 summary="Naming/layout updated; see full details.")],
                                   moved_folder=moved, path_changes=changes)
        if action.id == "convert":
            report = self.convert.process_game_dir(folder, selected_files=[path] if self.selected else None)
            if self.selected:
                detail = "Output: " + ", ".join(report["created"])
            elif report["status"] == "converted":
                detail = f"discs={report['discs']} dat_name={report['dat_name']}"
            else:
                status, detail = "Skipped", report["reason"]
            target = Path(report["game_dir"])
            moved = (folder, target) if target != folder and not options.dry_run else None
            if moved:
                detail += f". Game folder: {target}"
            output = Path(report["created"][0]) if report.get("created") else None
            record = ResultRecord(path.name, status, detail, target, reference=manifest_for(output) if output and output.exists() else None,
                                  source=output if self.selected else None,
                                  summary=action.summary_for(status))
            return ExecutionResult([record], moved_folder=moved)
        elif action.id == "extract":
            reports = self.extract.process_game_dir(folder, selected_file=path if self.selected else None,
                                                     input_manifest=manifest_for(path) if self.selected else None)
            if not self.selected:
                return ExecutionResult([ResultRecord(report.game_dir.name, status,
                                       f"strategy={report.strategy}, decision={report.decision}, extracted={len(report.extracted_files)}",
                                       report.game_dir, summary=action.summary_for(status)) for report in reports])
            detail = "Output: " + ", ".join(str(file) for report in reports for file in report.extracted_files)
        elif action.id == "manifest":
            if options.dry_run:
                status, detail = "Planned", "Would create missing manifest; CHDs may require temporary extraction. Sources kept."
            else:
                status, detail = self.manifests.process_file(path)
        elif action.id == "xiso":
            if options.extract_xiso is None:
                raise ValueError("An extract-xiso tool path is required")
            tool = options.extract_xiso
            outcome = convert_image(path, path.with_name(path.stem + ".xiso.iso"), self.manifests,
                                    lambda manifests: convert_xiso(path, tool, manifests, keep_source=True),
                                    dry_run=options.dry_run, keep_sources=options.keep_sources, force=self.chdman.force,
                                    use_redump_names=options.use_redump_names)
            return ExecutionResult([ResultRecord(path.name, outcome.status, outcome.details, outcome.output.parent, reference=manifest_for(outcome.output) if outcome.output.exists() else None, source=outcome.output,
                                                  summary=action.summary_for(outcome.status))], path_changes=outcome.changes)
        elif action.id in {"rvz", "rvz-extract"}:
            extract = action.id == "rvz-extract"
            outcome = convert_image(path, path.with_suffix(".iso" if extract else ".rvz"), self.manifests,
                                    lambda manifests: process_rvz(path, self.dolphin, manifests, extract=extract, keep_source=True),
                                    dry_run=options.dry_run, keep_sources=options.keep_sources, force=self.chdman.force,
                                    use_redump_names=options.use_redump_names)
            return ExecutionResult([ResultRecord(path.name, outcome.status, outcome.details, outcome.output.parent, reference=manifest_for(outcome.output) if outcome.output.exists() else None, source=outcome.output,
                                                  summary=action.summary_for(outcome.status))], path_changes=outcome.changes)
        else:
            raise ValueError(f"No workflow for action {action.id}")
        return ExecutionResult([ResultRecord(path.name, status, detail, folder, source=source, summary=action.summary_for(status))])


def run_batch(paths: list[Path], executor: WorkflowExecutor, events: EventReporter, *,
              stop_file: Path | None = None, csv_path: Path | None = None) -> int:
    """One error boundary, stop policy, result transport, and accounting for both scopes."""
    action, options = executor.action, executor.options
    if not executor.selected and action.id == "extract":
        paths = [path for path in paths if any(path.glob("*.chd"))]
    passed = failed = skipped = processed = errors = warnings = 0
    csv_rows: list[dict[str, str]] = []
    for path in iter_batch(paths, stop_file, options.verbose, events):
        processed += 1
        try:
            outcome = executor.execute(path)
            if outcome.path_changes:
                events.emit("paths-renamed", changes={str(old): str(new) for old, new in outcome.path_changes.items()})
                paths[:] = [outcome.path_changes.get(pending, pending) for pending in paths]
            if outcome.moved_folder:
                previous, target = outcome.moved_folder
                events.emit("folder-renamed", previous=str(previous.resolve()), folder=str(target.resolve()))
                for index, pending in enumerate(paths):
                    if pending.is_relative_to(previous):
                        paths[index] = target / pending.relative_to(previous)
        except Exception as exc:
            folder = path.parent if executor.selected else path
            record = ResultRecord(path.name, "Failed", str(exc), folder, source=path if executor.selected else None)
            verification = VerifyResult(folder, folder / "manifest.json", "failed", "", None, [f"error: {exc}"]) if action.command == "verify" else None
            outcome = ExecutionResult([record], verification)
        errors += any(record.status == "Failed" for record in outcome.records)
        for record in outcome.records:
            system = record.system
            if system is None and record.reference and is_manifest(record.reference):
                try:
                    manifest = load_manifest(record.reference)
                    system = manifest.system if manifest else None
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            context = record.source or record.reference or (record.folder / "_" if record.folder else path)
            record = replace(record, system=system or system_from_path(context), operation=record.operation or action.id)
            passed += record.status in {"Passed", "Completed", "Planned"}
            failed += record.status == "Failed"
            skipped += record.status == "Skipped"
            warnings += record.status == "Warning"
            label = "ERROR" if not executor.selected and action.command in {"convert", "extract"} and record.status == "Failed" else record.status.upper()
            print(f"[{action.command}] {label} {record.name}: {record.details}", flush=True)
            events.result(record.name, record.status, record.details, record.folder, record.reference, record.source,
                          summary=record.summary, suggested_action=record.suggested_action, check_mode=record.check_mode, verified_inputs=record.verified_inputs, system=record.system, operation=record.operation)
        if outcome.verification:
            result = outcome.verification
            csv_rows.append({"game_dir": str(result.game_dir), "status": result.status,
                             "manifest_dat_name": result.dat_name, "matched_dat_name": result.matched_dat_name or "",
                             "details": "; ".join(result.details)})
    if csv_path:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["game_dir", "status", "manifest_dat_name", "matched_dat_name", "details"])
            writer.writeheader()
            writer.writerows(csv_rows)
    if not executor.selected and action.verification == VerificationMode.CHD:
        counts = dict(checked=passed + failed, passed=passed, failed=failed)
    elif executor.selected and action.id == "manifest":
        counts = dict(processed=passed, errors=failed, skipped=skipped)
    elif not executor.selected and action.id == "extract":
        counts = dict(processed=processed, errors=errors)
    elif not executor.selected and action.id == "convert":
        counts = dict(converted=passed, errors=errors)
    else:
        counts = dict(checked=passed + failed + warnings, passed=passed, failed=failed, skipped=skipped)
    if warnings:
        counts["warnings"] = warnings
    events.summary(**counts)
    if not paths and action.id == "extract":
        print("[extract] no .chd files found in the selected directories; nothing to do")
    print(f"[{action.command}] summary: " + " ".join(f"{key}={value}" for key, value in counts.items()))
    return (1 if action.command in {"convert", "extract"} and not executor.selected else 2) if failed else 0
