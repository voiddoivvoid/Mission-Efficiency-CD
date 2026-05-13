#!/usr/bin/env python3
"""
MissionEff Custom Builder.

Builds both supported Mission Efficiency custom mod formats:
  - Legacy byte-patch .json using patches[*].changes[*].offset/original/patched
  - Format 3 field JSON using skill.pabgb _buff_data_raw old/new blobs

Bundled x2 reference templates are loaded from:
  _internal/_internals

User workflow:
  1. Enter multiplier.
  2. Select output format.
  3. Build.
"""

from __future__ import annotations

import json
import math
import os
import re
import struct
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import tkinter as tk
from tkinter import filedialog, messagebox


APP_TITLE = "MissionEff Custom Builder"
DEFAULT_OUTPUT_NAME_PREFIX = "MissionEff_Custom"

DEFAULT_CRIMSON_DESERT_DIR = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert")
TARGET_GAME_FILE_NAME = "skill.pabgb"

HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

FormatKind = Literal["legacy", "field"]


@dataclass(frozen=True)
class LearnedPatch:
    kind: FormatKind
    entry: str
    label: str
    key: int | None
    field: str | None
    offset: int | None
    old_hex: str
    new_hex_template: str
    byte_offset: int
    old_value: int
    template_value: int
    template_multiplier: float
    tier_name: str | None


@dataclass(frozen=True)
class TemplateInfo:
    kind: FormatKind
    path: Path
    obj: dict[str, Any]
    learned: list[LearnedPatch]


@dataclass(frozen=True)
class OutputFileResult:
    kind: FormatKind
    output_path: Path
    learned_count: int
    template_multiplier: float
    requested_multiplier: float
    verified_game: bool | None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BuildResult:
    requested_multiplier: float
    outputs: list[OutputFileResult]


# ──────────────────────────────────────────────────────────────────────────────
# Paths / layout
# ──────────────────────────────────────────────────────────────────────────────


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def internals_dir() -> Path:
    # PyInstaller one-folder builds already use _internal for runtime files.
    # MissionEff's bundled templates/tools live inside _internal/_internals
    # to keep the top-level app folder clean.
    return app_root() / "_internal" / "_internals"


def output_dir() -> Path:
    return app_root() / "output"


def ensure_layout() -> None:
    # The release build bundles templates in _internal/_internals.
    # Do not create a top-level _internals folder or user-facing README JSON.
    internals_dir().mkdir(parents=True, exist_ok=True)
    output_dir().mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# JSON / hex helpers
# ──────────────────────────────────────────────────────────────────────────────


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def clean_hex(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a hex string.")

    s = (
        value.strip()
        .replace(" ", "")
        .replace("\n", "")
        .replace("\r", "")
        .replace("\t", "")
        .lower()
    )

    if not s or len(s) % 2:
        raise ValueError(f"Invalid hex length: {value!r}")

    if not HEX_RE.match(s):
        raise ValueError(f"Invalid hex string: {value!r}")

    return s


def parse_int_maybe_hex(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        try:
            if s.lower().startswith("0x"):
                return int(s, 16)
            return int(s, 10)
        except ValueError:
            return None
    return None


def diff_one_4byte_value(old: bytes, new: bytes) -> tuple[int, bytes, bytes]:
    """Find the single changed 4-byte scalar inside old/new bytes."""
    if len(old) != len(new):
        raise ValueError(f"old/new length mismatch: {len(old)} != {len(new)}")

    changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]

    if not changed:
        raise ValueError("old/new are identical; no patch was found.")

    start = changed[0]
    end = changed[-1]

    if end - start + 1 > 4:
        raise ValueError(f"Patch changed more than 4 bytes: byte range {start}..{end}")

    if start + 4 > len(old):
        raise ValueError(f"Cannot read 4-byte value at byte offset {start}")

    old4 = old[start:start + 4]
    new4 = new[start:start + 4]

    if any(i < start or i >= start + 4 for i in changed):
        raise ValueError(f"Changed bytes are not contained in one 4-byte value: {changed}")

    return start, old4, new4


def percent_from_value(value: int) -> float:
    return value / 1000.0


def fmt_num(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.4f}".rstrip("0").rstrip(".")


def safe_multiplier_for_filename(multiplier: float) -> str:
    return fmt_num(multiplier).replace(".", "_")


def parse_multiplier(text: str) -> float:
    s = text.strip().lower().replace("x", "")

    if not s:
        raise ValueError("Enter a multiplier.")

    value = float(s)

    if value <= 0:
        raise ValueError("Multiplier must be greater than zero.")

    return value


# ──────────────────────────────────────────────────────────────────────────────
# Label / tier helpers
# ──────────────────────────────────────────────────────────────────────────────


def tier_from_entry(entry: str) -> str | None:
    m = re.search(r"_(I+)$", entry.strip())
    if not m:
        return None

    count = m.group(1).count("I")
    if count <= 0:
        return None

    return "Tier " + ("I" * count)


def entry_from_label(label: str) -> str:
    label = label.strip()
    normalized = label.lower().replace("_", " ").strip()

    if normalized in {"superworker", "super worker"}:
        return "Skill_SuperWorker"

    m = re.match(r"^(?P<name>.+?)\s+(?P<tier>I{1,3})$", label, flags=re.IGNORECASE)
    if m:
        name = re.sub(r"\s+", "_", m.group("name").strip())
        tier = m.group("tier").upper()
        return f"Skill_{name}_{tier}"

    return "Skill_" + re.sub(r"\s+", "_", label)


def label_from_entry(entry: str) -> str:
    s = entry.strip()
    if s == "Skill_SuperWorker":
        return "Superworker"
    if s.startswith("Skill_"):
        s = s[len("Skill_"):]
    return s.replace("_", " ")


# ──────────────────────────────────────────────────────────────────────────────
# Template detection
# ──────────────────────────────────────────────────────────────────────────────


def is_format3_field_json(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("format") == 3 and (
        isinstance(obj.get("intents"), list) or isinstance(obj.get("targets"), list)
    )


def is_legacy_patch_json(obj: Any) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("patches"), list)


def iter_skill_field_intents(obj: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def is_skill_target(value: Any) -> bool:
        return Path(str(value or "skill.pabgb").replace("\\", "/")).name.lower() == TARGET_GAME_FILE_NAME

    if isinstance(obj.get("intents"), list) and is_skill_target(obj.get("target", TARGET_GAME_FILE_NAME)):
        for intent in obj["intents"]:
            if isinstance(intent, dict):
                out.append(intent)

    targets = obj.get("targets")
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, dict):
                continue
            if not is_skill_target(target.get("file", target.get("target", ""))):
                continue
            intents = target.get("intents")
            if isinstance(intents, list):
                for intent in intents:
                    if isinstance(intent, dict):
                        out.append(intent)

    return out


def iter_legacy_skill_changes(obj: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    patches = obj.get("patches")
    if not isinstance(patches, list):
        return out

    for patch in patches:
        if not isinstance(patch, dict):
            continue

        game_file = str(patch.get("game_file", patch.get("target", ""))).replace("\\", "/").lower()
        if game_file and not game_file.endswith(TARGET_GAME_FILE_NAME):
            continue

        changes = patch.get("changes")
        if not isinstance(changes, list):
            continue

        for change in changes:
            if not isinstance(change, dict):
                continue
            if "original" in change and "patched" in change:
                out.append(change)

    return out


def template_score(path: Path) -> tuple[int, str]:
    name = path.name.lower()
    score = 0
    if "x2" in name:
        score -= 50
    if "template" in name:
        score -= 30
    if "example" in name:
        score -= 20
    if "field" in name:
        score -= 5
    return score, name


def find_templates() -> dict[FormatKind, TemplateInfo]:
    ensure_layout()

    found: dict[FormatKind, TemplateInfo] = {}
    candidates: list[tuple[Path, dict[str, Any]]] = []

    for path in sorted(internals_dir().glob("*.json")):
        try:
            obj = read_json(path)
        except Exception:
            continue
        if isinstance(obj, dict):
            candidates.append((path, obj))

    field_candidates: list[TemplateInfo] = []
    legacy_candidates: list[TemplateInfo] = []

    for path, obj in candidates:
        try:
            if is_format3_field_json(obj):
                learned = learn_from_field_template(obj)
                if learned:
                    field_candidates.append(TemplateInfo("field", path, obj, learned))
            elif is_legacy_patch_json(obj):
                learned = learn_from_legacy_template(obj)
                if learned:
                    legacy_candidates.append(TemplateInfo("legacy", path, obj, learned))
        except Exception:
            # Invalid template candidates are ignored here; the chosen
            # template's errors are surfaced through build/preview.
            continue

    if field_candidates:
        field_candidates.sort(key=lambda t: template_score(t.path))
        found["field"] = field_candidates[0]

    if legacy_candidates:
        legacy_candidates.sort(key=lambda t: template_score(t.path))
        found["legacy"] = legacy_candidates[0]

    return found


# ──────────────────────────────────────────────────────────────────────────────
# Learn from templates
# ──────────────────────────────────────────────────────────────────────────────


def learn_from_field_template(template_obj: dict[str, Any]) -> list[LearnedPatch]:
    if not is_format3_field_json(template_obj):
        raise ValueError("Field template must be Format 3 JSON.")

    intents = iter_skill_field_intents(template_obj)
    if not intents:
        raise ValueError("Field template must contain skill.pabgb intents.")

    learned: list[LearnedPatch] = []

    for idx, intent in enumerate(intents):
        entry = str(intent.get("entry", "")).strip()
        field_name = str(intent.get("field", "")).strip()

        if not entry:
            raise ValueError(f"Intent #{idx} is missing entry.")

        if field_name != "_buff_data_raw":
            raise ValueError(f"{entry}: expected field '_buff_data_raw', got {field_name!r}")

        old_hex = clean_hex(intent.get("old"))
        new_hex = clean_hex(intent.get("new"))

        old_blob = bytes.fromhex(old_hex)
        new_blob = bytes.fromhex(new_hex)

        patch_offset, old4, new4 = diff_one_4byte_value(old_blob, new_blob)

        old_value = int.from_bytes(old4, "little", signed=False)
        template_value = int.from_bytes(new4, "little", signed=False)

        if old_value <= 0:
            raise ValueError(f"{entry}: old value must be greater than zero.")

        multiplier = template_value / old_value
        if multiplier <= 0:
            raise ValueError(f"{entry}: template multiplier must be positive.")

        learned.append(
            LearnedPatch(
                kind="field",
                entry=entry,
                label=label_from_entry(entry),
                key=intent.get("key") if isinstance(intent.get("key"), int) else None,
                field=field_name,
                offset=None,
                old_hex=old_hex,
                new_hex_template=new_hex,
                byte_offset=patch_offset,
                old_value=old_value,
                template_value=template_value,
                template_multiplier=multiplier,
                tier_name=tier_from_entry(entry),
            )
        )

    verify_template_multipliers(learned)
    return learned


def learn_from_legacy_template(template_obj: dict[str, Any]) -> list[LearnedPatch]:
    if not is_legacy_patch_json(template_obj):
        raise ValueError("Legacy template must contain patches[].")

    changes = iter_legacy_skill_changes(template_obj)
    if not changes:
        raise ValueError("Legacy template must contain skill.pabgb changes.")

    learned: list[LearnedPatch] = []

    for idx, change in enumerate(changes):
        label = str(change.get("label", "")).strip() or f"Change {idx + 1}"
        entry = str(change.get("entry", "")).strip() or entry_from_label(label)
        offset = parse_int_maybe_hex(change.get("offset"))
        old_hex = clean_hex(change.get("original"))
        new_hex = clean_hex(change.get("patched"))

        old_bytes = bytes.fromhex(old_hex)
        new_bytes = bytes.fromhex(new_hex)

        patch_offset, old4, new4 = diff_one_4byte_value(old_bytes, new_bytes)

        if len(old_bytes) != 4 or len(new_bytes) != 4:
            raise ValueError(
                f"{label}: legacy MissionEff change should be a 4-byte scalar; "
                f"got {len(old_bytes)} old bytes and {len(new_bytes)} new bytes."
            )

        old_value = int.from_bytes(old4, "little", signed=False)
        template_value = int.from_bytes(new4, "little", signed=False)

        if old_value <= 0:
            raise ValueError(f"{label}: old value must be greater than zero.")

        multiplier = template_value / old_value
        if multiplier <= 0:
            raise ValueError(f"{label}: template multiplier must be positive.")

        learned.append(
            LearnedPatch(
                kind="legacy",
                entry=entry,
                label=label,
                key=None,
                field=None,
                offset=offset,
                old_hex=old_hex,
                new_hex_template=new_hex,
                byte_offset=patch_offset,
                old_value=old_value,
                template_value=template_value,
                template_multiplier=multiplier,
                tier_name=tier_from_entry(entry),
            )
        )

    verify_template_multipliers(learned)
    return learned


def verify_template_multipliers(learned: list[LearnedPatch]) -> None:
    if not learned:
        raise ValueError("Template produced no learnable patches.")

    by_tier: dict[str, list[LearnedPatch]] = {}

    for item in learned:
        if item.tier_name:
            by_tier.setdefault(item.tier_name, []).append(item)

    for tier, items in sorted(by_tier.items()):
        base = items[0].template_multiplier

        for item in items:
            if not math.isclose(item.template_multiplier, base, rel_tol=0, abs_tol=1e-9):
                raise ValueError(
                    f"{tier} multiplier mismatch: {item.entry} has x{item.template_multiplier}, "
                    f"expected x{base}."
                )

    unique = sorted({round(x.template_multiplier, 10) for x in learned})
    if len(unique) > 1:
        raise ValueError(f"Template has multiple multipliers: {unique}. Use one clean baseline example.")


def generated_u32(old_value: int, requested_multiplier: float, label: str) -> int:
    new_value_float = old_value * requested_multiplier
    new_value = int(round(new_value_float))

    if not math.isclose(new_value_float, new_value, rel_tol=0, abs_tol=1e-6):
        raise ValueError(
            f"{label}: multiplier produces non-integer value: "
            f"{old_value} * {requested_multiplier} = {new_value_float}"
        )

    if new_value < 0 or new_value > 0xFFFFFFFF:
        raise ValueError(f"{label}: generated value is outside unsigned 4-byte range: {new_value}")

    return new_value


def make_new_blob_hex(item: LearnedPatch, requested_multiplier: float) -> tuple[str, int]:
    old_blob = bytearray.fromhex(item.old_hex)
    new_value = generated_u32(item.old_value, requested_multiplier, item.entry)

    current_old = int.from_bytes(old_blob[item.byte_offset:item.byte_offset + 4], "little")
    if current_old != item.old_value:
        raise ValueError(
            f"{item.entry}: old value check failed at blob offset {item.byte_offset}. "
            f"Found {current_old}, expected {item.old_value}."
        )

    old_blob[item.byte_offset:item.byte_offset + 4] = new_value.to_bytes(4, "little", signed=False)
    return old_blob.hex(), new_value


def make_new_scalar_hex(item: LearnedPatch, requested_multiplier: float) -> tuple[str, int]:
    if len(bytes.fromhex(item.old_hex)) != 4:
        raise ValueError(f"{item.label}: expected 4 original bytes.")

    new_value = generated_u32(item.old_value, requested_multiplier, item.label)
    return new_value.to_bytes(4, "little", signed=False).hex(), new_value


# ──────────────────────────────────────────────────────────────────────────────
# Build outputs
# ──────────────────────────────────────────────────────────────────────────────


def build_selected_outputs(
    multiplier: float,
    build_legacy: bool,
    build_field: bool,
) -> BuildResult:
    ensure_layout()

    if not build_legacy and not build_field:
        raise ValueError("Select at least one output format.")

    templates = find_templates()
    outputs: list[OutputFileResult] = []

    if build_legacy:
        if "legacy" not in templates:
            raise FileNotFoundError(
                f"Bundled legacy .JSON template not found in {internals_dir()}. "
                "The build package is missing MissionEff_x2.json."
            )
        outputs.append(build_legacy_json(multiplier, templates["legacy"]))

    if build_field:
        if "field" not in templates:
            raise FileNotFoundError(
                f"Bundled Field JSON template not found in {internals_dir()}. "
                "The build package is missing MissionEff_x2_field.json."
            )
        outputs.append(build_field_json(multiplier, templates["field"]))

    return BuildResult(requested_multiplier=multiplier, outputs=outputs)


def build_legacy_json(multiplier: float, template: TemplateInfo) -> OutputFileResult:
    obj = json.loads(json.dumps(template.obj))
    generated_values: dict[str, int] = {}

    changes = iter_legacy_skill_changes(obj)
    learned = template.learned

    if len(changes) != len(learned):
        raise ValueError(
            f"Legacy template changed while building: {len(changes)} changes vs {len(learned)} learned patches."
        )

    for change, item in zip(changes, learned):
        new_hex, new_value = make_new_scalar_hex(item, multiplier)
        change["patched"] = new_hex
        generated_values[item.entry] = new_value

    update_modinfo_for_output(obj, multiplier, kind="legacy")
    verify_generated_tiers(learned, generated_values, multiplier)

    safe_mult = safe_multiplier_for_filename(multiplier)
    out_path = output_dir() / f"{DEFAULT_OUTPUT_NAME_PREFIX}_x{safe_mult}.json"

    game_check = verify_legacy_against_installed_game(learned)
    write_json(out_path, obj)

    return OutputFileResult(
        kind="legacy",
        output_path=out_path,
        template_multiplier=learned[0].template_multiplier,
        requested_multiplier=multiplier,
        learned_count=len(learned),
        verified_game=game_check,
    )


def build_field_json(multiplier: float, template: TemplateInfo) -> OutputFileResult:
    obj = json.loads(json.dumps(template.obj))
    generated_values: dict[str, int] = {}

    intents = iter_skill_field_intents(obj)
    learned = template.learned

    if len(intents) != len(learned):
        raise ValueError(
            f"Field template changed while building: {len(intents)} intents vs {len(learned)} learned patches."
        )

    for intent, item in zip(intents, learned):
        new_hex, new_value = make_new_blob_hex(item, multiplier)
        intent["entry"] = item.entry
        if item.key is not None:
            intent["key"] = item.key
        intent["field"] = "_buff_data_raw"
        intent["old"] = item.old_hex
        intent["new"] = new_hex
        generated_values[item.entry] = new_value

    update_modinfo_for_output(obj, multiplier, kind="field")
    verify_generated_tiers(learned, generated_values, multiplier)

    safe_mult = safe_multiplier_for_filename(multiplier)
    out_path = output_dir() / f"{DEFAULT_OUTPUT_NAME_PREFIX}_x{safe_mult}_field.json"

    game_check = verify_field_against_installed_game(learned)
    write_json(out_path, obj)

    return OutputFileResult(
        kind="field",
        output_path=out_path,
        template_multiplier=learned[0].template_multiplier,
        requested_multiplier=multiplier,
        learned_count=len(learned),
        verified_game=game_check,
    )


def update_modinfo_for_output(obj: dict[str, Any], multiplier: float, kind: FormatKind) -> None:
    modinfo = obj.get("modinfo")
    if not isinstance(modinfo, dict):
        modinfo = {}
        obj["modinfo"] = modinfo

    title = f"{DEFAULT_OUTPUT_NAME_PREFIX}_x{fmt_num(multiplier)}"
    modinfo["title"] = title

    if kind == "legacy":
        modinfo["description"] = "Custom Mission Efficiency legacy byte-patch JSON generated from a verified template."
        modinfo["note"] = (
            "Generated by MissionEff Custom Builder. "
            "Apply only one MissionEff preset at a time. "
            "Legacy .JSON format for CDUMM/JMM-style byte patching."
        )
    else:
        modinfo["description"] = "Custom Mission Efficiency Format 3 field JSON generated from a verified template."
        modinfo["note"] = (
            "Generated by MissionEff Custom Builder. "
            "Apply only one MissionEff preset at a time. "
            "Format 3 _buff_data_raw field JSON."
        )

    modinfo.setdefault("version", "1.0")
    modinfo["author"] = "Generated by MissionEff Custom Builder"

    # Some older JSONs also use top-level display fields.
    if "title" in obj:
        obj["title"] = title
    if "name" in obj:
        obj["name"] = title


def verify_generated_tiers(
    learned: list[LearnedPatch],
    generated_values: dict[str, int],
    multiplier: float,
) -> None:
    for item in learned:
        got = generated_values[item.entry]
        expected = int(round(item.old_value * multiplier))

        if got != expected:
            raise ValueError(f"{item.entry}: generated {got}, expected {expected}.")

    by_tier: dict[str, list[LearnedPatch]] = {}

    for item in learned:
        if item.tier_name:
            by_tier.setdefault(item.tier_name, []).append(item)

    for tier, items in by_tier.items():
        expected_old = items[0].old_value
        expected_new = generated_values[items[0].entry]

        for item in items:
            if item.old_value != expected_old:
                raise ValueError(f"{tier}: {item.entry} has old value {item.old_value}, expected {expected_old}.")

            if generated_values[item.entry] != expected_new:
                raise ValueError(f"{tier}: {item.entry} generated different value than its tier.")


# ──────────────────────────────────────────────────────────────────────────────
# Optional local game verification
# ──────────────────────────────────────────────────────────────────────────────


def find_installed_skill_pabgb() -> Path | None:
    roots: list[Path] = []

    env = os.environ.get("CRIMSON_DESERT_DIR", "").strip()
    if env:
        roots.append(Path(env))

    roots.append(DEFAULT_CRIMSON_DESERT_DIR)

    for root in roots:
        if not root.exists():
            continue

        for candidate in [
            root / "0008" / "skill.pabgb",
            root / "0008" / "gamedata" / "binary__" / "client" / "bin" / "skill.pabgb",
            root / "gamedata" / "binary__" / "client" / "bin" / "skill.pabgb",
        ]:
            if candidate.exists():
                return candidate

        try:
            for p in root.rglob("skill.pabgb"):
                if p.is_file():
                    return p
        except Exception:
            pass

        extracted = try_extract_skill_with_paz_tools(root)
        if extracted and extracted.exists():
            return extracted

    return None


def try_extract_skill_with_paz_tools(game_root: Path) -> Path | None:
    """Small local fallback extractor for unencrypted LZ4 type-2 PAZ entries."""
    try:
        import lz4.block
    except Exception:
        return None

    def parse_pamt_local(pamt_path: Path, paz_dir: Path):
        data = pamt_path.read_bytes()
        pamt_stem = pamt_path.stem

        off = 0
        off += 4
        paz_count = struct.unpack_from("<I", data, off)[0]
        off += 4
        off += 8

        for i in range(paz_count):
            off += 4
            off += 4
            if i < paz_count - 1:
                off += 4

        folder_size = struct.unpack_from("<I", data, off)[0]
        off += 4
        folder_end = off + folder_size

        folder_prefix = ""
        while off < folder_end:
            parent = struct.unpack_from("<I", data, off)[0]
            slen = data[off + 4]
            name = data[off + 5:off + 5 + slen].decode("utf-8", errors="replace")

            if parent == 0xFFFFFFFF:
                folder_prefix = name

            off += 5 + slen

        node_size = struct.unpack_from("<I", data, off)[0]
        off += 4
        node_start = off

        nodes = {}
        while off < node_start + node_size:
            rel = off - node_start
            parent = struct.unpack_from("<I", data, off)[0]
            slen = data[off + 4]
            name = data[off + 5:off + 5 + slen].decode("utf-8", errors="replace")
            nodes[rel] = (parent, name)
            off += 5 + slen

        def build_path(node_ref: int) -> str:
            parts = []
            cur = node_ref

            while cur != 0xFFFFFFFF and len(parts) < 128:
                if cur not in nodes:
                    break

                parent, name = nodes[cur]
                parts.append(name)
                cur = parent

            return "".join(reversed(parts))

        folder_count = struct.unpack_from("<I", data, off)[0]
        off += 4
        off += 4
        off += folder_count * 16

        entries = []

        while off + 20 <= len(data):
            node_ref, paz_offset, comp_size, orig_size, flags = struct.unpack_from("<IIIII", data, off)
            off += 20

            paz_index = flags & 0xFF
            compression_type = (flags >> 16) & 0x0F
            node_path = build_path(node_ref)
            full_path = f"{folder_prefix}/{node_path}" if folder_prefix else node_path
            paz_num = int(pamt_stem) + paz_index
            paz_file = paz_dir / f"{paz_num}.paz"

            entries.append(
                {
                    "path": full_path,
                    "paz_file": paz_file,
                    "offset": paz_offset,
                    "comp_size": comp_size,
                    "orig_size": orig_size,
                    "compression_type": compression_type,
                    "compressed": comp_size != orig_size,
                }
            )

        return entries

    try:
        for paz_dir in [game_root / "0008", game_root]:
            if not paz_dir.exists():
                continue

            for pamt_path in sorted(paz_dir.glob("*.pamt")):
                entries = parse_pamt_local(pamt_path, paz_dir)
                matches = [
                    e for e in entries
                    if e["path"].replace("\\", "/").lower().endswith(TARGET_GAME_FILE_NAME)
                ]

                if not matches:
                    continue

                entry = matches[0]
                if not entry["paz_file"].exists():
                    continue

                with open(entry["paz_file"], "rb") as f:
                    f.seek(entry["offset"])
                    raw = f.read(entry["comp_size"] if entry["compressed"] else entry["orig_size"])

                if entry["compressed"]:
                    if entry["compression_type"] == 2:
                        raw = lz4.block.decompress(raw, uncompressed_size=entry["orig_size"])
                    else:
                        return None

                tmp = Path(tempfile.mkdtemp(prefix="missioneff_verify_"))
                out_path = tmp / TARGET_GAME_FILE_NAME
                out_path.write_bytes(raw)
                return out_path

    except Exception:
        return None

    return None


def verify_field_against_installed_game(learned: list[LearnedPatch]) -> bool | None:
    path = find_installed_skill_pabgb()
    if path is None:
        return None

    try:
        data = path.read_bytes()
        for item in learned:
            old_blob = bytes.fromhex(item.old_hex)
            if data.find(old_blob) == -1:
                return False
        return True
    except Exception:
        return None


def verify_legacy_against_installed_game(learned: list[LearnedPatch]) -> bool | None:
    path = find_installed_skill_pabgb()
    if path is None:
        return None

    try:
        data = path.read_bytes()
        for item in learned:
            if item.offset is None:
                return None
            original = bytes.fromhex(item.old_hex)
            if item.offset < 0 or item.offset + len(original) > len(data):
                return False
            if data[item.offset:item.offset + len(original)] != original:
                return False
        return True
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Preview
# ──────────────────────────────────────────────────────────────────────────────


def preview_lines(multiplier_text: str) -> list[str]:
    try:
        multiplier = parse_multiplier(multiplier_text)
        templates = find_templates()

        if "field" in templates:
            learned = templates["field"].learned
            template_kind = "field"
        elif "legacy" in templates:
            learned = templates["legacy"].learned
            template_kind = "legacy"
        else:
            return ["No bundled template found."]

    except Exception:
        return ["Tier I = ", "Tier II = ", "Tier III = "]

    tier_values: dict[str, int] = {}
    non_tier: list[tuple[str, int]] = []

    for item in learned:
        value = int(round(item.old_value * multiplier))

        if item.tier_name:
            tier_values.setdefault(item.tier_name, value)
        else:
            non_tier.append((item.entry, value))

    lines: list[str] = [f"Preview source: {template_kind} template"]

    for tier in ["Tier I", "Tier II", "Tier III"]:
        if tier in tier_values:
            lines.append(f"{tier} = {fmt_num(percent_from_value(tier_values[tier]))}%")

    for entry, value in non_tier:
        lines.append(f"{entry} = {fmt_num(percent_from_value(value))}%")

    return lines or ["No valid template patches found."]


# ──────────────────────────────────────────────────────────────────────────────
# Tk app
# ──────────────────────────────────────────────────────────────────────────────


class MissionEffApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        ensure_layout()

        self.title(APP_TITLE)
        self.minsize(680, 0)
        self.resizable(False, False)

        self.status_var = tk.StringVar(value="Ready.")
        self.preview_var = tk.StringVar(value="")
        self.build_legacy_var = tk.BooleanVar(value=True)
        self.build_field_var = tk.BooleanVar(value=True)

        self._build_ui()
        self._show_startup_message_once()
        self.after(200, self._activate_multiplier)
        self.after(250, self._refresh_preview)

    def _build_ui(self) -> None:
        pad = 10

        row = tk.Frame(self)
        row.pack(fill="x", padx=pad, pady=(pad, 4))

        tk.Label(row, text="Multiplier:").pack(side="left")

        self.multiplier_entry = tk.Spinbox(
            row,
            from_=0.01,
            to=9999,
            increment=1,
            width=18,
            state="normal",
        )
        self.multiplier_entry.delete(0, "end")
        self.multiplier_entry.insert(0, "5")
        self.multiplier_entry.pack(side="left", padx=(10, 0))
        self.multiplier_entry.bind("<KeyRelease>", lambda _e: self._refresh_preview())
        self.multiplier_entry.bind("<ButtonRelease>", lambda _e: self.after(50, self._refresh_preview))

        fmt = tk.LabelFrame(self, text="Output Formats")
        fmt.pack(fill="x", padx=pad, pady=6)

        tk.Checkbutton(
            fmt,
            text="Legacy .JSON  (offset/original/patched byte patch)",
            variable=self.build_legacy_var,
        ).pack(anchor="w", padx=10, pady=(8, 2))

        tk.Checkbutton(
            fmt,
            text="Field JSON  (Format 3 _buff_data_raw old/new blobs)",
            variable=self.build_field_var,
        ).pack(anchor="w", padx=10, pady=(2, 8))

        preview = tk.LabelFrame(self, text="Preview")
        preview.pack(fill="x", padx=pad, pady=6)

        self.preview_label = tk.Label(
            preview,
            textvariable=self.preview_var,
            justify="left",
            anchor="nw",
            width=92,
        )
        self.preview_label.pack(fill="x", padx=14, pady=6)

        buttons = tk.Frame(self)
        buttons.pack(fill="x", padx=pad, pady=(4, 4))

        tk.Button(
            buttons,
            text="Build Selected",
            width=18,
            command=self._build_clicked,
        ).pack(side="left")

        tk.Button(
            buttons,
            text="Open Output Folder",
            width=20,
            command=self._open_output,
        ).pack(side="left", padx=(8, 0))


        tk.Label(self, textvariable=self.status_var, anchor="w").pack(fill="x", padx=pad, pady=(0, 6))

    def _show_startup_message_once(self) -> None:
        msg = (
            "MissionEff Custom Builder\n\n"
            "How to use:\n"
            "1. Enter a multiplier.\n"
            "2. Select output format(s).\n"
            "3. Click Build Selected.\n\n"
            "Credits:\n"
            "- Lazorr for the PAZ unpacker.\n"
            "- cracker for the field.json format.\n"
            "- HexagonCS for the original Mission Efficiency mod."
        )

        messagebox.showinfo("Read Me", msg)

    def _activate_multiplier(self) -> None:
        try:
            self.lift()
            self.attributes("-topmost", True)
            self.after(100, lambda: self.attributes("-topmost", False))
            self.focus_force()
            self.multiplier_entry.focus_force()
            self.multiplier_entry.icursor("end")
            self.multiplier_entry.selection_range(0, "end")
        except Exception:
            pass

    def _get_multiplier_text(self) -> str:
        return self.multiplier_entry.get()

    def _refresh_preview(self) -> None:
        self.preview_var.set("\n".join(preview_lines(self._get_multiplier_text())))
        self._fit_to_content()

    def _fit_to_content(self) -> None:
        try:
            self.update_idletasks()
            self.geometry(f"680x{self.winfo_reqheight()}")
        except Exception:
            pass

    def _build_clicked(self) -> None:
        try:
            multiplier = parse_multiplier(self._get_multiplier_text())
            result = build_selected_outputs(
                multiplier=multiplier,
                build_legacy=self.build_legacy_var.get(),
                build_field=self.build_field_var.get(),
            )

            self.status_var.set(f"Build complete: {len(result.outputs)} file(s)")

            lines = ["Build complete.", ""]
            for out in result.outputs:
                if out.verified_game is True:
                    verify_note = "Verified"
                elif out.verified_game is False:
                    verify_note = "Verification warning"
                else:
                    verify_note = "Verification skipped"

                label = "Legacy .JSON" if out.kind == "legacy" else "Field JSON"
                lines.append(
                    f"{label}: x{fmt_num(out.requested_multiplier)}, "
                    f"{out.learned_count} entries, {verify_note}."
                )

            lines.append("")
            lines.append("Use Open Output Folder to view the generated file(s).")

            messagebox.showinfo("Build Complete", "\n".join(lines))

        except Exception as e:
            self.status_var.set("Build failed.")
            messagebox.showerror("Build Failed", f"{e}\n\nDetails:\n{traceback.format_exc(limit=5)}")

    def _open_output(self) -> None:
        output_dir().mkdir(parents=True, exist_ok=True)
        open_folder(output_dir())


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))
    else:
        filedialog.askdirectory(initialdir=path)


def main() -> None:
    ensure_layout()
    app = MissionEffApp()
    app.mainloop()


if __name__ == "__main__":
    main()
