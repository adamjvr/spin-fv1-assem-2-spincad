
#!/usr/bin/env python3
"""
fv1_to_spcdj.py

Convert a compiled Spin FV-1 program, or a SpinASM source file plus its compiled
Intel HEX output, into a modern SpinCAD Designer .spcdj patch file.

Important reality check:
    Modern SpinCAD Designer can save/load .spcdj JSON patch files.
    It can also preserve a compiled hexFile payload for a patch.
    It does not reverse raw SpinASM source back into editable graphical blocks.

So this tool does the honest useful thing:
    1. If you give it an Intel HEX file, it extracts one 128-instruction FV-1 patch.
    2. If you give it SpinASM source plus --compiled-hex, it embeds the compiled patch
       and also preserves the original assembly source as extra JSON metadata.
    3. If you give it SpinASM source plus --assembler-cmd, it runs your assembler,
       reads the generated HEX, then writes the .spcdj.
    4. If you only want an archival/source carrier file, --source-only creates a
       valid .spcdj that preserves the ASM text, but it is NOT an executable FV-1
       patch inside SpinCAD because there are no compiled instructions.

Author: rewrite from scratch for Adam / FV-1 / OpenBrain / SpinCAD workflow.
Dependencies: Python standard library only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Constants that match the practical FV-1 / SpinCAD patch model.
# ---------------------------------------------------------------------------

# The FV-1 executes at most 128 instructions per sample for one program.
# SpinCAD's hexFile field is an int[128]-style list in the Java code.
FV1_INSTRUCTIONS_PER_PATCH = 128

# Each FV-1 instruction is stored as one 32-bit word in Intel HEX exports.
BYTES_PER_FV1_INSTRUCTION = 4

# 128 instructions * 4 bytes = 512 bytes per patch/program.
BYTES_PER_FV1_PATCH = FV1_INSTRUCTIONS_PER_PATCH * BYTES_PER_FV1_INSTRUCTION

# External EEPROM banks normally contain 8 user programs. Slot 0 starts at
# address 0x0000, slot 1 starts at 0x0200, slot 2 starts at 0x0400, etc.
FV1_PATCH_SLOTS_PER_BANK = 8

# SpinCAD's current JSON writer stores formatVersion 1.
SPINCAD_JSON_FORMAT_VERSION = 1

# Current public SpinCAD release observed during rewrite. This is metadata only.
# SpinCAD warns when a file was saved by a newer build; using a recent known build
# here is better than pretending this came from some ancient legacy exporter.
DEFAULT_SPINCAD_BUILD_NUMBER = 1070


# ---------------------------------------------------------------------------
# Small helper functions.
# ---------------------------------------------------------------------------

def die(message: str, exit_code: int = 1) -> None:
    """
    Print a plain fatal error and exit.

    I keep this as a function so the rest of the script can fail loudly without
    nesting a bunch of ugly "print then sys.exit" junk everywhere.
    """
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def warn(message: str) -> None:
    """
    Print a warning without stopping the conversion.
    """
    print(f"WARNING: {message}", file=sys.stderr)


def read_text_file(path: pathlib.Path) -> str:
    """
    Read a source-ish text file in a forgiving way.

    Most SpinASM files are simple ASCII/ANSI/UTF-8 text. This tries UTF-8 with
    BOM handling first, then falls back to latin-1 so old Windows-y files do not
    explode just because one comment has a weird character in it.
    """
    raw = path.read_bytes()

    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def write_text_file(path: pathlib.Path, text: str) -> None:
    """
    Write text as UTF-8 with Unix newlines.

    SpinCAD's JSON reader is not doing anything exotic; UTF-8 is the sane choice.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def sha256_text(text: str) -> str:
    """
    Hash source text so the .spcdj can prove exactly what ASM file was embedded.
    """
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """
    Hash binary-ish data such as an input HEX file.
    """
    return hashlib.sha256(data).hexdigest()


def safe_patch_display_name(input_path: pathlib.Path, override: Optional[str]) -> str:
    """
    Pick a human-readable patch name.

    The SpinCAD comment block has a "fileName" field, and SpinCAD patch objects
    also have patchFileName. Those are file-ish strings, not a separate musical
    title field, so the output filename is still the main identity.
    """
    if override:
        return override.strip()
    return input_path.stem.strip() or "Imported FV-1 Program"


def to_java_signed_int32(value: int) -> int:
    """
    Convert an unsigned 32-bit word into Java's signed int range.

    SpinCAD's Java side stores hexFile as int[128]. Java ints are signed. If an
    FV-1 instruction word has bit 31 set, Java naturally stores that as a negative
    number. JSON can represent either positive or negative numbers, but emitting
    signed values mirrors what SpinCAD itself tends to produce after loading HEX
    and saving JSON.
    """
    value &= 0xFFFFFFFF
    if value >= 0x80000000:
        return value - 0x100000000
    return value


def from_java_signed_int32(value: int) -> int:
    """
    Convert a Java-style signed int back to an unsigned 32-bit word.

    Used only for sanity checks and potential future formatting.
    """
    return value & 0xFFFFFFFF


def looks_like_intel_hex_text(text: str) -> bool:
    """
    Heuristically decide whether some text is Intel HEX.

    Intel HEX records begin with ':' and contain ASCII hex. A real file can have
    blank lines, but the useful records should start with ':'.
    """
    useful_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not useful_lines:
        return False
    colon_lines = sum(1 for line in useful_lines if line.startswith(":"))
    return colon_lines >= max(1, len(useful_lines) // 2)


def classify_input(path: pathlib.Path, explicit_kind: Optional[str]) -> str:
    """
    Decide whether the input is assembly source or Intel HEX.

    The command-line --input-kind option can override this. Without it, extension
    and file content are both considered because FV-1 files in the wild are not
    always named consistently.
    """
    if explicit_kind:
        return explicit_kind

    ext = path.suffix.lower()
    if ext in {".hex", ".ihex", ".ihx"}:
        return "hex"

    if ext in {".asm", ".spn", ".spinasm", ".txt"}:
        # A .txt could still be Intel HEX, so inspect content.
        text = read_text_file(path)
        return "hex" if looks_like_intel_hex_text(text) else "asm"

    # Unknown extension: inspect as text if possible.
    try:
        text = read_text_file(path)
    except Exception:
        return "hex"

    return "hex" if looks_like_intel_hex_text(text) else "asm"


# ---------------------------------------------------------------------------
# Intel HEX parsing.
# ---------------------------------------------------------------------------

class IntelHexError(ValueError):
    """
    Raised when an Intel HEX file is malformed.
    """


def parse_intel_hex_record(line: str, line_number: int) -> Tuple[int, int, int, bytes]:
    """
    Parse one Intel HEX record.

    Record layout:
        :LLAAAATT[DD...]CC

    Where:
        LL      = byte count
        AAAA    = 16-bit address
        TT      = record type
        DD      = data bytes
        CC      = checksum

    The checksum rule is simple: sum all bytes after ':' including checksum;
    the low 8 bits must equal zero.
    """
    line = line.strip()

    if not line:
        raise IntelHexError(f"Line {line_number}: empty line is not a record")

    if not line.startswith(":"):
        raise IntelHexError(f"Line {line_number}: Intel HEX record must start with ':'")

    hex_part = line[1:]

    if len(hex_part) < 10:
        raise IntelHexError(f"Line {line_number}: record is too short")

    if len(hex_part) % 2 != 0:
        raise IntelHexError(f"Line {line_number}: record has an odd number of hex digits")

    if not re.fullmatch(r"[0-9A-Fa-f]+", hex_part):
        raise IntelHexError(f"Line {line_number}: record contains non-hex characters")

    try:
        record_bytes = bytes.fromhex(hex_part)
    except ValueError as exc:
        raise IntelHexError(f"Line {line_number}: cannot decode hex bytes") from exc

    byte_count = record_bytes[0]
    expected_length = 1 + 2 + 1 + byte_count + 1

    if len(record_bytes) != expected_length:
        raise IntelHexError(
            f"Line {line_number}: byte count says {byte_count} data bytes, "
            f"but record length does not match"
        )

    address = (record_bytes[1] << 8) | record_bytes[2]
    record_type = record_bytes[3]
    data = record_bytes[4:4 + byte_count]
    checksum = record_bytes[-1]

    if (sum(record_bytes) & 0xFF) != 0:
        raise IntelHexError(
            f"Line {line_number}: bad Intel HEX checksum "
            f"(checksum byte was 0x{checksum:02X})"
        )

    return byte_count, address, record_type, data


def parse_intel_hex(text: str) -> Dict[int, int]:
    """
    Parse an Intel HEX file into an address -> byte dictionary.

    This supports the common record types:
        00 = data
        01 = EOF
        02 = extended segment address
        04 = extended linear address

    FV-1 exports are usually tiny and simple, but supporting the address-extension
    records costs almost nothing and makes the parser less brittle.
    """
    memory: Dict[int, int] = {}
    upper_address_base = 0
    saw_eof = False

    for line_number, original_line in enumerate(text.splitlines(), start=1):
        line = original_line.strip()

        # Be forgiving about blank lines at the end of copied files.
        if not line:
            continue

        byte_count, address, record_type, data = parse_intel_hex_record(line, line_number)

        if record_type == 0x00:
            absolute_address = upper_address_base + address

            for offset, byte_value in enumerate(data):
                addr = absolute_address + offset

                if addr in memory:
                    warn(
                        f"Line {line_number}: address 0x{addr:08X} appears more than once; "
                        f"later byte overwrote earlier byte"
                    )

                memory[addr] = byte_value

        elif record_type == 0x01:
            saw_eof = True
            break

        elif record_type == 0x02:
            if byte_count != 2:
                raise IntelHexError(
                    f"Line {line_number}: extended segment address record must contain 2 bytes"
                )
            upper_address_base = (((data[0] << 8) | data[1]) << 4)

        elif record_type == 0x04:
            if byte_count != 2:
                raise IntelHexError(
                    f"Line {line_number}: extended linear address record must contain 2 bytes"
                )
            upper_address_base = (((data[0] << 8) | data[1]) << 16)

        else:
            # Start linear address / start segment address are irrelevant for FV-1 data.
            warn(f"Line {line_number}: ignoring Intel HEX record type 0x{record_type:02X}")

    if not saw_eof:
        warn("No Intel HEX EOF record was found. Continuing because data records were readable.")

    if not memory:
        raise IntelHexError("Intel HEX file contained no data bytes")

    return memory


def extract_fv1_patch_words_from_memory(memory: Dict[int, int], slot: int) -> List[int]:
    """
    Extract one FV-1 patch slot from parsed Intel HEX memory.

    Slot math:
        slot 0 = byte addresses 0x0000 through 0x01FF
        slot 1 = byte addresses 0x0200 through 0x03FF
        slot 2 = byte addresses 0x0400 through 0x05FF
        ...
        slot 7 = byte addresses 0x0E00 through 0x0FFF

    Every four bytes are combined into one 32-bit FV-1 instruction word.
    """
    if slot < 0 or slot >= FV1_PATCH_SLOTS_PER_BANK:
        die(f"--slot must be 0 through 7, got {slot}")

    base = slot * BYTES_PER_FV1_PATCH
    words: List[int] = []

    for instruction_index in range(FV1_INSTRUCTIONS_PER_PATCH):
        addr = base + (instruction_index * BYTES_PER_FV1_INSTRUCTION)
        byte_addresses = [addr + i for i in range(BYTES_PER_FV1_INSTRUCTION)]
        present = [a in memory for a in byte_addresses]

        if not any(present):
            # Missing trailing addresses are normal when a HEX file does not pad all 128
            # instruction locations. SpinCAD's own saver pads out to 128, so we do too.
            word = 0

        elif all(present):
            raw_bytes = bytes(memory[a] for a in byte_addresses)
            word = int.from_bytes(raw_bytes, byteorder="big", signed=False)

        else:
            missing = [f"0x{a:04X}" for a in byte_addresses if a not in memory]
            raise IntelHexError(
                f"Instruction {instruction_index} in slot {slot} is only partially present; "
                f"missing bytes at {', '.join(missing)}"
            )

        words.append(to_java_signed_int32(word))

    return words


def parse_hex_file_to_fv1_words(hex_path: pathlib.Path, slot: int) -> List[int]:
    """
    Read an Intel HEX file and return exactly 128 Java-style int32 words.
    """
    text = read_text_file(hex_path)
    memory = parse_intel_hex(text)
    return extract_fv1_patch_words_from_memory(memory, slot=slot)


# ---------------------------------------------------------------------------
# Optional external assembler support.
# ---------------------------------------------------------------------------

def run_external_assembler(
    source_path: pathlib.Path,
    assembler_command_template: str,
    temp_dir: pathlib.Path,
) -> pathlib.Path:
    """
    Run a user-supplied assembler command and return the expected HEX path.

    The tool intentionally does not pretend to be SpinASM/asfv1. FV-1 assembly
    can include macros, equates, memory declarations, includes, labels, and syntax
    details that belong in a real assembler. Re-implementing that badly would
    recreate the exact kind of fake converter we are replacing.

    The command template supports:
        {input}   quoted path to the ASM/SPN source
        {output}  quoted path where the assembler should write Intel HEX

    Example shape:
        --assembler-cmd "YOUR_ASSEMBLER {input} {output}"

    Because this is a local operator command, shell=True is used intentionally
    so Wine commands, batch wrappers, and old Windows tools can be driven without
    this script trying to understand every possible assembler argument style.
    """
    generated_hex = temp_dir / (source_path.stem + ".hex")

    substitutions = {
        "input": shlex.quote(str(source_path)),
        "output": shlex.quote(str(generated_hex)),
    }

    try:
        command = assembler_command_template.format(**substitutions)
    except KeyError as exc:
        die(f"Unknown placeholder in --assembler-cmd: {exc}")

    print(f"Running assembler command:\n  {command}")

    result = subprocess.run(
        command,
        shell=True,
        cwd=str(source_path.parent),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if result.stdout.strip():
        print(result.stdout.rstrip())

    if result.stderr.strip():
        print(result.stderr.rstrip(), file=sys.stderr)

    if result.returncode != 0:
        die(f"Assembler command failed with exit code {result.returncode}")

    if not generated_hex.exists():
        die(
            "Assembler command finished, but the expected HEX file was not created:\n"
            f"  {generated_hex}\n"
            "Use the {output} placeholder in --assembler-cmd so this script knows where to read."
        )

    return generated_hex


# ---------------------------------------------------------------------------
# SpinASM source handling.
# ---------------------------------------------------------------------------

def summarize_asm_source(source_text: str) -> Dict[str, object]:
    """
    Create lightweight diagnostics about a SpinASM source file.

    This is not a full assembler and does not try to understand every instruction.
    It only gives useful warnings like "this seems huge" or "this is empty."
    """
    lines = source_text.splitlines()

    nonempty_lines = [line for line in lines if line.strip()]

    # Treat ';' as the usual FV-1 assembly comment marker.
    codeish_lines = []
    for line in lines:
        stripped = line.strip()

        if not stripped:
            continue

        if stripped.startswith(";"):
            continue

        codeish_lines.append(stripped)

    return {
        "lineCount": len(lines),
        "nonemptyLineCount": len(nonempty_lines),
        "codeishLineCount": len(codeish_lines),
        "sha256": sha256_text(source_text),
    }


def leading_comment_lines_for_spincad(source_text: str, input_path: pathlib.Path) -> List[str]:
    """
    Build the 5-line SpinCAD comment block.

    SpinCAD's native comment block is tiny: five lines. The full ASM source is
    preserved separately in custom JSON metadata, but these five lines are what
    SpinCAD itself knows how to display in its old comment block machinery.
    """
    comments: List[str] = []

    comments.append(f"Imported source: {input_path.name}")
    comments.append("POT0: ")
    comments.append("POT1: ")
    comments.append("POT2: ")

    # Try to lift one useful leading source comment into the visible block.
    for line in source_text.splitlines():
        stripped = line.strip()

        if not stripped:
            continue

        if stripped.startswith(";"):
            clean = stripped.lstrip(";").strip()
            if clean:
                comments.append(clean[:120])
                break

        # Stop scanning once real source appears.
        if not stripped.startswith(";"):
            break

    while len(comments) < 5:
        comments.append("")

    return comments[:5]


def make_empty_source_only_notice(source_text: str, input_path: pathlib.Path) -> Dict[str, object]:
    """
    Metadata used when --source-only is requested.

    This is deliberately explicit so six months later nobody opens the JSON and
    thinks the source magically became an executable SpinCAD graph.
    """
    return {
        "mode": "source-only",
        "warning": (
            "This file preserves raw SpinASM/FV-1 assembly source as metadata only. "
            "SpinCAD Designer does not reverse raw SpinASM into editable graphical blocks, "
            "and no compiled hexFile payload was embedded."
        ),
        "sourceFile": input_path.name,
        "sourceSha256": sha256_text(source_text),
    }


# ---------------------------------------------------------------------------
# SpinCAD .spcdj JSON generation.
# ---------------------------------------------------------------------------

def build_spcdj_patch(
    *,
    output_path: pathlib.Path,
    patch_name: str,
    input_path: pathlib.Path,
    source_text: Optional[str],
    source_kind: str,
    hex_words: Optional[Sequence[int]],
    slot: int,
    build_number: int,
    generator_note: str,
) -> Dict[str, object]:
    """
    Build the Python dictionary that will be serialized to .spcdj JSON.

    This follows the fields used by SpinCADJsonSerializer:
        formatVersion
        buildNumber
        type
        patchFileName
        comments
        potValues
        isHexFile / hexFile, when compiled instructions exist

    It also adds custom metadata fields. SpinCAD's reader ignores unknown top-level
    fields, but humans and future scripts can use them to recover the original
    assembly source and conversion details.
    """
    if source_text is not None:
        comment_lines = leading_comment_lines_for_spincad(source_text, input_path)
    else:
        comment_lines = [
            f"Imported HEX: {input_path.name}",
            "POT0: ",
            "POT1: ",
            "POT2: ",
            "",
        ]

    root: Dict[str, object] = {
        "formatVersion": SPINCAD_JSON_FORMAT_VERSION,
        "buildNumber": build_number,
        "type": "patch",
        "patchFileName": output_path.name,
        "comments": {
            "fileName": output_path.name,
            "version": generator_note,
            "lines": comment_lines,
        },
        "potValues": [0.0, 0.0, 0.0],
        "generator": {
            "tool": "fv1_to_spcdj.py",
            "createdUtc": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "inputFile": str(input_path),
            "inputKind": source_kind,
            "patchName": patch_name,
            "slot": slot,
            "note": generator_note,
        },
    }

    if hex_words is not None:
        if len(hex_words) != FV1_INSTRUCTIONS_PER_PATCH:
            die(
                f"Internal error: hex_words must contain {FV1_INSTRUCTIONS_PER_PATCH} "
                f"instructions, got {len(hex_words)}"
            )

        root["isHexFile"] = True
        root["hexFile"] = list(hex_words)

    if source_text is not None:
        source_summary = summarize_asm_source(source_text)

        root["sourceAsm"] = {
            "fileName": input_path.name,
            "sha256": source_summary["sha256"],
            "lineCount": source_summary["lineCount"],
            "nonemptyLineCount": source_summary["nonemptyLineCount"],
            "codeishLineCount": source_summary["codeishLineCount"],
            "text": source_text,
        }

        if hex_words is None:
            root["sourceOnlyNotice"] = make_empty_source_only_notice(source_text, input_path)

    return root


def write_spcdj(path: pathlib.Path, data: Dict[str, object]) -> None:
    """
    Serialize the patch dictionary as stable, readable JSON.

    SpinCAD's own writer uses a hand-rolled JSON writer. Standard JSON is fine as
    long as the same key structure exists.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    json_text = json.dumps(
        data,
        indent=2,
        ensure_ascii=False,
    )

    write_text_file(path, json_text + "\n")


# ---------------------------------------------------------------------------
# Main CLI flow.
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """
    Define the command-line interface.

    The defaults are intentionally conservative. The script refuses to pretend
    that raw assembly became a working SpinCAD patch unless you provide compiled
    HEX or explicitly request --source-only archival behavior.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Convert FV-1 Intel HEX, or FV-1 SpinASM source plus compiled HEX, "
            "into a modern SpinCAD .spcdj patch file."
        )
    )

    parser.add_argument(
        "input",
        help="Input file: .hex/.ihex/.ihx compiled FV-1 HEX, or .asm/.spn SpinASM source.",
    )

    parser.add_argument(
        "output",
        help="Output .spcdj file. Modern SpinCAD uses .spcdj; legacy .spcd is not XML.",
    )

    parser.add_argument(
        "--input-kind",
        choices=["asm", "hex"],
        default=None,
        help="Override input auto-detection.",
    )

    parser.add_argument(
        "--compiled-hex",
        default=None,
        help=(
            "Compiled Intel HEX file to pair with an ASM/SPN source input. "
            "Use this when you already assembled the source elsewhere."
        ),
    )

    parser.add_argument(
        "--assembler-cmd",
        default=None,
        help=(
            "Optional shell command template used to assemble ASM/SPN to HEX. "
            "Use {input} for the quoted source path and {output} for the quoted HEX path."
        ),
    )

    parser.add_argument(
        "--slot",
        type=int,
        default=0,
        help=(
            "FV-1 bank slot to extract from HEX, 0-7. Slot N starts at byte address N*0x200. "
            "Default: 0."
        ),
    )

    parser.add_argument(
        "--patch-name",
        default=None,
        help="Human-readable patch name stored in generator metadata.",
    )

    parser.add_argument(
        "--build-number",
        type=int,
        default=DEFAULT_SPINCAD_BUILD_NUMBER,
        help=f"SpinCAD buildNumber metadata. Default: {DEFAULT_SPINCAD_BUILD_NUMBER}.",
    )

    parser.add_argument(
        "--source-only",
        action="store_true",
        help=(
            "Allow ASM/SPN input without compiled HEX. Output preserves the source text in "
            "custom JSON metadata, but the patch will not be executable/rendered by SpinCAD."
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )

    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    Program entry point.

    Keeping main() separate from the if __name__ block makes this easier to test.
    """
    args = parse_args(argv)

    input_path = pathlib.Path(args.input).expanduser().resolve()
    output_path = pathlib.Path(args.output).expanduser().resolve()

    if not input_path.exists():
        die(f"Input file does not exist: {input_path}")

    if not input_path.is_file():
        die(f"Input path is not a file: {input_path}")

    if output_path.exists() and not args.force:
        die(f"Output file already exists. Use --force to overwrite: {output_path}")

    if output_path.suffix.lower() != ".spcdj":
        warn(
            "Output file does not end in .spcdj. Modern SpinCAD patch JSON normally uses .spcdj. "
            "The file will still be written as JSON."
        )

    if args.slot < 0 or args.slot >= FV1_PATCH_SLOTS_PER_BANK:
        die("--slot must be 0 through 7")

    input_kind = classify_input(input_path, args.input_kind)
    patch_name = safe_patch_display_name(input_path, args.patch_name)

    source_text: Optional[str] = None
    hex_words: Optional[List[int]] = None
    hex_source_path: Optional[pathlib.Path] = None

    if input_kind == "hex":
        hex_source_path = input_path
        hex_words = parse_hex_file_to_fv1_words(hex_source_path, slot=args.slot)

    elif input_kind == "asm":
        source_text = read_text_file(input_path)

        if not source_text.strip():
            die(f"ASM/SPN source file is empty: {input_path}")

        source_summary = summarize_asm_source(source_text)

        if int(source_summary["codeishLineCount"]) > FV1_INSTRUCTIONS_PER_PATCH * 2:
            warn(
                "ASM source has a lot of non-comment lines. That may be fine because macros, "
                "equates, and memory declarations are counted here, but the final FV-1 program "
                "still has a hard 128-instruction patch payload."
            )

        if args.compiled_hex and args.assembler_cmd:
            die("Use either --compiled-hex or --assembler-cmd, not both.")

        if args.compiled_hex:
            hex_source_path = pathlib.Path(args.compiled_hex).expanduser().resolve()

            if not hex_source_path.exists():
                die(f"--compiled-hex file does not exist: {hex_source_path}")

            hex_words = parse_hex_file_to_fv1_words(hex_source_path, slot=args.slot)

        elif args.assembler_cmd:
            with tempfile.TemporaryDirectory(prefix="fv1_to_spcdj_") as temp_dir_name:
                temp_dir = pathlib.Path(temp_dir_name)
                generated_hex_path = run_external_assembler(
                    source_path=input_path,
                    assembler_command_template=args.assembler_cmd,
                    temp_dir=temp_dir,
                )
                hex_words = parse_hex_file_to_fv1_words(generated_hex_path, slot=args.slot)
                hex_source_path = generated_hex_path

        else:
            if not args.source_only:
                die(
                    "Raw ASM/SPN source cannot become a working SpinCAD patch by itself.\n"
                    "Provide --compiled-hex path/to/program.hex, or provide --assembler-cmd, "
                    "or add --source-only if you only want to preserve the source text in .spcdj metadata."
                )

            warn(
                "--source-only selected: writing a valid .spcdj source carrier, "
                "but no compiled hexFile payload will be embedded."
            )

    else:
        die(f"Unknown input kind: {input_kind}")

    generator_note = "Generated by fv1_to_spcdj.py"

    spcdj = build_spcdj_patch(
        output_path=output_path,
        patch_name=patch_name,
        input_path=input_path,
        source_text=source_text,
        source_kind=input_kind,
        hex_words=hex_words,
        slot=args.slot,
        build_number=args.build_number,
        generator_note=generator_note,
    )

    # Add file hashes after the main structure is built so the metadata is complete
    # but still cleanly separated from SpinCAD's own known fields.
    spcdj["generator"]["inputSha256"] = sha256_bytes(input_path.read_bytes())

    if hex_source_path is not None and hex_source_path.exists():
        spcdj["generator"]["hexFile"] = str(hex_source_path)
        spcdj["generator"]["hexSha256"] = sha256_bytes(hex_source_path.read_bytes())

    write_spcdj(output_path, spcdj)

    print(f"Wrote: {output_path}")

    if hex_words is not None:
        nonzero_words = sum(1 for word in hex_words if from_java_signed_int32(int(word)) != 0)
        print(
            f"Embedded FV-1 patch slot {args.slot}: "
            f"{len(hex_words)} instruction words, {nonzero_words} non-zero words."
        )
    else:
        print("No compiled hexFile payload embedded. This is a source-only carrier file.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
