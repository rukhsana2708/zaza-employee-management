"""Production package privacy audit for ZaZaWorkAgent (run by build.ps1).

Inspects the BUILT artifact, not the intentions in the spec:

1. Opens ``ZaZaWorkAgent.exe`` with PyInstaller's own archive reader and
   lists every Python module actually packaged (the PYZ archive).
2. Lists every file next to the executable (DLLs, .pyd extensions, data).
3. Fails if any packaged module is an upstream DeskMate package, the central
   server, the developer CLI, or a prohibited capture library; and if any
   non-standard-library module is outside the justified allowlist.
4. Scans the SOURCE of every packaged ZaZa module for Win32/library calls
   that would implement screenshots, OCR, clipboard, audio, webcam, key
   identities/typed text, accessibility-tree content, browser URLs, or
   disabling TLS verification.

Exit code 0 = pass. A report is written next to the artifacts.

Usage: python installer/audit_package.py dist/ZaZaWorkAgent [--report dist/package-audit.txt]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_config import (  # noqa: E402
    DESKMATE_PROHIBITED,
    FIRST_PARTY_ALLOWED,
    PROHIBITED_SOURCE_PATTERNS,
    ROOT,
    STDLIB_EXCEPTIONS,
    THIRD_PARTY_ALLOWED,
    THIRD_PARTY_PROHIBITED,
    is_under,
    stdlib_names,
)

PROHIBITED_FILE_HINTS = ("tesseract", "opencv", "onnxruntime", "openvino", "portaudio", "libsndfile", "torch",
                         "ffmpeg", "mss", "pil", "_imaging", "uiautomation", "comtypes", "pywin32", "win32clipboard")


@dataclass
class AuditResult:
    modules: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def packaged_modules(exe: Path) -> list[str]:
    """Module names in the PYZ archive embedded in the executable."""
    from PyInstaller.archive.readers import CArchiveReader  # noqa: PLC0415

    carchive = CArchiveReader(str(exe))
    names: set[str] = set()
    for name, entry in carchive.toc.items():
        if entry[-1] == "z":  # an embedded PYZ archive of Python modules
            names.update(carchive.open_embedded_archive(name).toc.keys())
    return sorted(names)


def check_modules(modules: list[str], result: AuditResult) -> None:
    stdlib = stdlib_names()
    for mod in modules:
        top = mod.split(".")[0]
        if top == "deskmate":
            if is_under(mod, DESKMATE_PROHIBITED):
                result.failures.append(f"prohibited upstream module packaged: {mod}")
            elif not (mod in FIRST_PARTY_ALLOWED or is_under(mod, ("deskmate.zaza",))):
                result.failures.append(f"unexpected first-party module packaged: {mod}")
        elif top in THIRD_PARTY_PROHIBITED or is_under(mod, THIRD_PARTY_PROHIBITED):
            result.failures.append(f"prohibited library packaged: {mod}")
        elif top not in stdlib and top not in THIRD_PARTY_ALLOWED:
            result.failures.append(f"unexpected library packaged (not allowlisted): {mod}")
    present = {m.split(".")[0] for m in modules}
    for name, why in STDLIB_EXCEPTIONS.items():
        if name in present:
            result.notes.append(f"allowed exception: {name} — {why}")
    for name, why in THIRD_PARTY_ALLOWED.items():
        if name in present:
            result.notes.append(f"allowed library: {name} — {why}")


def check_files(bundle: Path, result: AuditResult) -> None:
    for path in sorted(bundle.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(bundle).as_posix()
        result.files.append(rel)
        lower = rel.lower()
        parts = re.split(r"[\\/._-]", lower)
        if "deskmate" in lower and not lower.startswith("_internal/build_flavor"):
            result.failures.append(f"file with upstream branding next to the executable: {rel}")
        for hint in PROHIBITED_FILE_HINTS:
            if hint in parts or lower.startswith(hint) or f"/{hint}" in lower:
                result.failures.append(f"prohibited library file packaged: {rel}")


def check_sources(modules: list[str], result: AuditResult) -> None:
    patterns = [(re.compile(p), why) for p, why in PROHIBITED_SOURCE_PATTERNS.items()]
    scanned = 0
    for mod in modules:
        if mod.split(".")[0] != "deskmate":
            continue
        rel = Path(*mod.split("."))
        candidates = [ROOT / rel.with_suffix(".py"), ROOT / rel / "__init__.py"]
        source = next((c for c in candidates if c.exists()), None)
        if source is None:
            result.failures.append(f"packaged module without reviewable source: {mod}")
            continue
        scanned += 1
        text = source.read_text(encoding="utf-8")
        for regex, why in patterns:
            for match in regex.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                result.failures.append(f"{source.relative_to(ROOT)}:{line}: {why} ({match.group(0)!r})")
    result.notes.append(f"source-scanned {scanned} shipped ZaZa module(s) for prohibited capture calls")


def audit(bundle: Path, modules: list[str] | None = None) -> AuditResult:
    result = AuditResult()
    exe = bundle / "ZaZaWorkAgent.exe"
    if modules is None:
        if not exe.exists():
            result.failures.append(f"missing executable: {exe}")
            return result
        modules = packaged_modules(exe)
    result.modules = modules
    if "deskmate.zaza.workagent" not in modules:
        result.failures.append("the ZaZa agent entry point (deskmate.zaza.workagent) is not packaged")
    check_modules(modules, result)
    if bundle.exists():
        check_files(bundle, result)
    check_sources(modules, result)
    return result


def report_text(result: AuditResult) -> str:
    lines = ["ZaZa Work Agent — production package privacy audit", "",
             f"RESULT: {'PASS' if result.ok else 'FAIL'}", ""]
    if result.failures:
        lines += ["Failures:", *[f"  - {f}" for f in result.failures], ""]
    lines += ["Notes:", *[f"  - {n}" for n in result.notes], "",
              f"Packaged Python modules ({len(result.modules)}):",
              *[f"  {m}" for m in result.modules if m.startswith("deskmate")],
              f"  ... plus {sum(not m.startswith('deskmate') for m in result.modules)} standard-library / allowlisted modules",
              "", f"Files next to the executable ({len(result.files)}):", *[f"  {f}" for f in result.files]]
    if result.ok:
        lines += ["", "Conclusion: no executable code path in the shipped ZaZa Work Agent provides screenshots, "
                      "screen recording, OCR, clipboard capture, keylogging/typed text/key identities, "
                      "microphone, webcam, accessibility-tree content capture, browser URLs/history, or a way to "
                      "disable TLS verification."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    result = audit(args.bundle)
    text = report_text(result)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    print(text)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
