#!/usr/bin/env python3
"""
MissionEff Custom Builder - DMM field.json generator.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import messagebox, filedialog


APP_TITLE = "MissionEff Custom Builder"
TEMPLATE_BASENAME = "MisssionEff_Template_and_Example"
DEFAULT_OUTPUT_NAME_PREFIX = "MissionEff_Custom"

DEFAULT_CRIMSON_DESERT_DIR = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Crimson Desert")
TARGET_GAME_FILE_NAME = "skill.pabgb"

HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


@dataclass(frozen=True)
class LearnedIntent:
    entry: str
    key: int | None
    field: str
    old_hex: str
    new_hex_template: str
    byte_offset: int
    old_value: int
    template_value: int
    template_multiplier: float
    tier_name: str | None


@dataclass(frozen=True)
class BuildResult:
    output_path: Path
    template_multiplier: float
    requested_multiplier: float
    learned_count: int
    verified_game: bool | None
    warnings: list[str]


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def internals_dir() -> Path:
    return app_root() / "_internals"


def output_dir() -> Path:
    return app_root() / "output"


def ensure_layout() -> None:
    internals_dir().mkdir(parents=True, exist_ok=True)
    output_dir().mkdir(parents=True, exist_ok=True)

    template = find_template_path()
    if template is None:
        placeholder = internals_dir() / f"{TEMPLATE_BASENAME}.field.json"
        placeholder.write_text(
            "{\n"
            '  "modinfo": {\n'
            '    "title": "PLACE YOUR KNOWN-GOOD x2 TEMPLATE HERE",\n'
            '    "note": "Replace this placeholder with MissionEff x2 field.json."\n'
            "  },\n"
            '  "format": 3,\n'
            '  "target": "skill.pabgb",\n'
            '  "intents": []\n'
            "}\n",
            encoding="utf-8",
        )


def find_template_path() -> Path | None:
    d = internals_dir()
    if not d.exists():
        return None

    candidates: list[Path] = []

    for p in d.iterdir():
        if not p.is_file():
            continue

        lower = p.name.lower()
        stem = p.stem.lower()

        if lower.endswith(".json") and TEMPLATE_BASENAME.lower() in stem:
            candidates.append(p)
        elif lower.endswith(".field.json") and TEMPLATE_BASENAME.lower() in lower:
            candidates.append(p)

    if not candidates:
        for p in d.glob("*.json"):
            if "template" in p.name.lower() and "example" in p.name.lower():
                candidates.append(p)

    return sorted(candidates)[0] if candidates else None


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


def diff_one_4byte_value(old: bytes, new: bytes) -> tuple[int, bytes, bytes]:
    if len(old) != len(new):
        raise ValueError(f"old/new blob length mismatch: {len(old)} != {len(new)}")

    changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]

    if not changed:
        raise ValueError("old/new are identical; no patch was found.")

    start = changed[0]
    end = changed[-1]

    if end - start + 1 > 4:
        raise ValueError(f"Patch changed more than 4 bytes: byte range {start}..{end}")

    if start + 4 > len(old):
        raise ValueError(f"Cannot read 4-byte value at blob offset {start}")

    old4 = old[start:start + 4]
    new4 = new[start:start + 4]

    if any(i < start or i >= start + 4 for i in changed):
        raise ValueError(f"Changed bytes are not contained in one 4-byte value: {changed}")

    return start, old4, new4


def tier_from_entry(entry: str) -> str | None:
    m = re.search(r"_(I+)$", entry.strip())
    if not m:
        return None

    count = m.group(1).count("I")
    if count <= 0:
        return None

    return "Tier " + ("I" * count)


def percent_from_value(value: int) -> float:
    return value / 1000.0


def fmt_num(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.4f}".rstrip("0").rstrip(".")


def learn_from_template(template_obj: dict[str, Any]) -> list[LearnedIntent]:
    if not isinstance(template_obj, dict):
        raise ValueError("Template root must be a JSON object.")

    if template_obj.get("format") != 3:
        raise ValueError("Template must be DMM Format 3.")

    if Path(str(template_obj.get("target", ""))).name.lower() != TARGET_GAME_FILE_NAME:
        raise ValueError("Template target must be skill.pabgb.")

    intents = template_obj.get("intents")
    if not isinstance(intents, list) or not intents:
        raise ValueError("Template must contain a non-empty intents list.")

    learned: list[LearnedIntent] = []

    for idx, intent in enumerate(intents):
        if not isinstance(intent, dict):
            raise ValueError(f"Intent #{idx} is not an object.")

        entry = str(intent.get("entry", "")).strip()
        field = str(intent.get("field", "")).strip()

        if not entry:
            raise ValueError(f"Intent #{idx} is missing entry.")

        if field != "_buff_data_raw":
            raise ValueError(f"{entry}: expected field '_buff_data_raw', got {field!r}")

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
            LearnedIntent(
                entry=entry,
                key=intent.get("key") if isinstance(intent.get("key"), int) else None,
                field=field,
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


def verify_template_multipliers(learned: list[LearnedIntent]) -> None:
    by_tier: dict[str, list[LearnedIntent]] = {}

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


def make_new_blob_hex(item: LearnedIntent, requested_multiplier: float) -> tuple[str, int]:
    old_blob = bytearray.fromhex(item.old_hex)

    new_value_float = item.old_value * requested_multiplier
    new_value = int(round(new_value_float))

    if not math.isclose(new_value_float, new_value, rel_tol=0, abs_tol=1e-6):
        raise ValueError(
            f"{item.entry}: multiplier produces non-integer value: "
            f"{item.old_value} * {requested_multiplier} = {new_value_float}"
        )

    if new_value < 0 or new_value > 0xFFFFFFFF:
        raise ValueError(f"{item.entry}: generated value is outside unsigned 4-byte range: {new_value}")

    current_old = int.from_bytes(old_blob[item.byte_offset:item.byte_offset + 4], "little")

    if current_old != item.old_value:
        raise ValueError(
            f"{item.entry}: old value check failed at blob offset {item.byte_offset}. "
            f"Found {current_old}, expected {item.old_value}."
        )

    old_blob[item.byte_offset:item.byte_offset + 4] = new_value.to_bytes(4, "little", signed=False)
    return old_blob.hex(), new_value


def build_field_json(multiplier: float) -> BuildResult:
    ensure_layout()

    template_path = find_template_path()
    if template_path is None:
        raise FileNotFoundError(f"Template not found in {internals_dir()}")

    template = read_json(template_path)
    learned = learn_from_template(template)

    output_intents: list[dict[str, Any]] = []
    generated_values: dict[str, int] = {}

    for item in learned:
        new_hex, new_value = make_new_blob_hex(item, multiplier)

        old_blob = bytes.fromhex(item.old_hex)
        new_blob = bytes.fromhex(new_hex)

        off, old4, new4 = diff_one_4byte_value(old_blob, new_blob)

        if off != item.byte_offset:
            raise ValueError(f"{item.entry}: output patch offset moved from {item.byte_offset} to {off}.")

        if int.from_bytes(old4, "little") != item.old_value:
            raise ValueError(f"{item.entry}: output old 4-byte value mismatch.")

        if int.from_bytes(new4, "little") != new_value:
            raise ValueError(f"{item.entry}: output new 4-byte value mismatch.")

        if item.key is not None:
            intent = {
                "entry": item.entry,
                "key": item.key,
                "field": item.field,
                "old": item.old_hex,
                "new": new_hex,
            }
        else:
            intent = {
                "entry": item.entry,
                "field": item.field,
                "old": item.old_hex,
                "new": new_hex,
            }

        output_intents.append(intent)
        generated_values[item.entry] = new_value

    verify_generated_tiers(learned, generated_values, multiplier)

    out_obj = {
        "modinfo": {
            "description": "Custom DMM Mission Efficiency patch generated from a verified template.",
            "version": "1.0",
            "author": "Generated by MissionEff Custom Builder",
            "title": f"{DEFAULT_OUTPUT_NAME_PREFIX}_x{fmt_num(multiplier)}",
            "note": (
                "Generated from MisssionEff_Template_and_Example. "
                "Apply only one MissionEff preset at a time. "
                "Requires latest DMM from Nexus."
            ),
        },
        "format": 3,
        "target": TARGET_GAME_FILE_NAME,
        "intents": output_intents,
    }

    safe_mult = fmt_num(multiplier).replace(".", "_")
    out_path = output_dir() / f"{DEFAULT_OUTPUT_NAME_PREFIX}_x{safe_mult}_field.json"

    game_check = verify_against_installed_game(learned)
    write_json(out_path, out_obj)

    return BuildResult(
        output_path=out_path,
        template_multiplier=learned[0].template_multiplier,
        requested_multiplier=multiplier,
        learned_count=len(learned),
        verified_game=game_check,
        warnings=[],
    )


def verify_generated_tiers(
    learned: list[LearnedIntent],
    generated_values: dict[str, int],
    multiplier: float,
) -> None:
    for item in learned:
        got = generated_values[item.entry]
        expected = int(round(item.old_value * multiplier))

        if got != expected:
            raise ValueError(f"{item.entry}: generated {got}, expected {expected}.")

    by_tier: dict[str, list[LearnedIntent]] = {}

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
    """
    Extract skill.pabgb from Crimson Desert PAZ/PAMT layout.

    Expected layout:
      Crimson Desert/
        0008/
          0.pamt
          0.paz
          1.paz
          2.paz

    Returns extracted skill.pabgb path, or None if unavailable.
    """
    try:
        import struct
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
        candidate_dirs = [
            game_root / "0008",
            game_root,
        ]

        for paz_dir in candidate_dirs:
            if not paz_dir.exists():
                continue

            pamt_files = sorted(paz_dir.glob("*.pamt"))

            for pamt_path in pamt_files:
                entries = parse_pamt_local(pamt_path, paz_dir)

                matches = [
                    e for e in entries
                    if e["path"].replace("\\", "/").lower().endswith("skill.pabgb")
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
                out_path = tmp / "skill.pabgb"
                out_path.write_bytes(raw)
                return out_path

    except Exception:
        return None

    return None
    return None


def verify_against_installed_game(learned: list[LearnedIntent]) -> bool | None:
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


def preview_lines(multiplier_text: str) -> list[str]:
    try:
        multiplier = parse_multiplier(multiplier_text)
        template_path = find_template_path()

        if template_path is None:
            return ["No template found."]

        template = read_json(template_path)
        learned = learn_from_template(template)

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

    lines: list[str] = []

    for tier in ["Tier I", "Tier II", "Tier III"]:
        if tier in tier_values:
            lines.append(f"{tier} = {fmt_num(percent_from_value(tier_values[tier]))}%")

    for entry, value in non_tier:
        lines.append(f"{entry} = {fmt_num(percent_from_value(value))}%")

    return lines or ["No valid template intents found."]


def parse_multiplier(text: str) -> float:
    s = text.strip().lower().replace("x", "")

    if not s:
        raise ValueError("Enter a multiplier.")

    value = float(s)

    if value <= 0:
        raise ValueError("Multiplier must be greater than zero.")

    return value


class MissionEffApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        ensure_layout()

        self.title(APP_TITLE)
        self.geometry("620x305")
        self.resizable(False, False)

        self.status_var = tk.StringVar(value="Ready.")
        self.preview_var = tk.StringVar(value="")

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

        fmt = tk.LabelFrame(self, text="Output Format")
        fmt.pack(fill="x", padx=pad, pady=6)

        tk.Label(
            fmt,
            text="Field.JSON  (Please download latest version of DMM from Nexus)",
        ).pack(anchor="w", padx=10, pady=8)

        preview = tk.LabelFrame(self, text="Preview")
        preview.pack(fill="x", padx=pad, pady=6)

        self.preview_label = tk.Label(
            preview,
            textvariable=self.preview_var,
            justify="left",
            anchor="nw",
            height=4,
        )
        self.preview_label.pack(fill="x", padx=14, pady=6)

        buttons = tk.Frame(self)
        buttons.pack(fill="x", padx=pad, pady=(4, 4))

        tk.Button(
            buttons,
            text="Build field.json",
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
            "2. Click Build field.json.\n"
            "3. Put the generated field.json into the latest version of DMM from Nexus.\n\n"
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

    def _build_clicked(self) -> None:
        try:
            multiplier = parse_multiplier(self._get_multiplier_text())
            result = build_field_json(multiplier)

            if result.verified_game is True:
                game_note = "Skill game file verified. field.json appears compatible with the current game version."
            elif result.verified_game is False:
                game_note = (
                    "Skill game file was found, but template old blobs did not match. "
                    "field.json may not be compatible with the current game version."
                )
            else:
                game_note = "Game file verification skipped. Could not extract or verify skill.pabgb from the local game files."

            self.status_var.set(f"Build complete: {result.output_path.name}")

            messagebox.showinfo(
                "Build Complete",
                f"Built:\n{result.output_path}\n\n"
                f"Template multiplier detected: x{fmt_num(result.template_multiplier)}\n"
                f"Requested multiplier: x{fmt_num(result.requested_multiplier)}\n"
                f"Entries generated: {result.learned_count}\n\n"
                f"{game_note}",
            )

        except Exception as e:
            self.status_var.set("Build failed.")
            messagebox.showerror("Build Failed", f"{e}\n\nDetails:\n{traceback.format_exc(limit=4)}")

    def _open_output(self) -> None:
        output_dir().mkdir(parents=True, exist_ok=True)

        if sys.platform == "win32":
            os.startfile(str(output_dir()))
        else:
            filedialog.askdirectory(initialdir=output_dir())


def main() -> None:
    ensure_layout()
    app = MissionEffApp()
    app.mainloop()


if __name__ == "__main__":
    main()