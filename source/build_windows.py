"""Build the standalone Windows x64 executable using Python 3.13.

Inputs are the Windows AMD64 CPython embeddable ZIP and the Windows AMD64
PyInstaller wheel. The bootloader uses the PyInstaller CArchive format.
"""
import argparse
import importlib.util
import io
import marshal
from pathlib import Path
import struct
import sys
import zipfile
import zlib
from pe_icon import with_icon


def build_executable(target, bootloader, files, interpreter_name):
    bootloader = with_icon(bootloader, Path(__file__).with_name("icon.ico").read_bytes())
    target.parent.mkdir(parents=True, exist_ok=True)
    toc = bytearray()
    with target.open("wb") as executable:
        executable.write(bootloader)
        start = executable.tell()
        for name, kind, data in files:
            offset = executable.tell() - start
            packed = zlib.compress(data, 6) if kind != b"z" else data
            executable.write(packed)
            name_bytes = name.encode("ascii") + b"\0"
            entry_length = (18 + len(name_bytes) + 15) // 16 * 16
            toc.extend(struct.pack(f"!IIIIBc{entry_length-18}s", entry_length,
                                   offset, len(packed), len(data), kind != b"z", kind, name_bytes))
        toc_offset = executable.tell() - start
        executable.write(toc)
        cookie_format = "!8sIIII64s"
        archive_length = executable.tell() - start + struct.calcsize(cookie_format)
        version = sys.version_info.major * 100 + sys.version_info.minor
        executable.write(struct.pack(cookie_format, b"MEI\x0c\x0b\x0a\x0b\x0e",
                                     archive_length, toc_offset, len(toc), version, interpreter_name.encode("ascii")))


def app_files():
    directory = Path(__file__).parent
    script = compile((directory / "novel.py").read_bytes(), "novel.py", "exec")
    return [
        ("interface.html", b"x", (directory / "interface.html").read_bytes()),
        ("providers.py", b"x", (directory / "providers.py").read_bytes()),
        ("projects.py", b"x", (directory / "projects.py").read_bytes()),
        ("exports.py", b"x", (directory / "exports.py").read_bytes()),
        ("icon.ico", b"x", (directory / "icon.ico").read_bytes()),
        ("PYZ-00.pyz", b"z", b"PYZ\0" + importlib.util.MAGIC_NUMBER + struct.pack("!i", 12) + marshal.dumps([])),
        ("novel", b"s", marshal.dumps(script)),
    ]


def build_windows(embed_zip, wheel, target):
    if sys.version_info[:2] != (3, 13):
        raise SystemExit("Run this build with CPython 3.13.")
    with zipfile.ZipFile(wheel) as package, zipfile.ZipFile(embed_zip) as runtime:
        bootloader = package.read("PyInstaller/bootloader/Windows-64bit-intel/run.exe")
        stdlib = runtime.read("python313.zip")
        with zipfile.ZipFile(io.BytesIO(stdlib)) as library:
            if library.read("encodings/__init__.pyc")[:4] != importlib.util.MAGIC_NUMBER:
                raise SystemExit("The Windows runtime has a different Python bytecode version.")
        files = [(name, b"b", runtime.read(name)) for name in runtime.namelist()
                 if name.endswith((".pyd", ".dll"))]
        files.extend([
            ("base_library.zip", b"x", stdlib),
            ("CPython-LICENSE.txt", b"x", runtime.read("LICENSE.txt")),
            ("PyInstaller-COPYING.txt", b"x",
             package.read(next(name for name in package.namelist() if name.endswith("/licenses/COPYING.txt")))),
        ])
        build_executable(target, bootloader, files + app_files(), "python313.dll")


def read_archive(path):
    data = path.read_bytes()
    cookie_size = struct.calcsize("!8sIIII64s")
    magic, size, offset, toc_size, version, library = struct.unpack("!8sIIII64s", data[-cookie_size:])
    if magic != b"MEI\x0c\x0b\x0a\x0b\x0e" or version != sys.version_info.major * 100 + sys.version_info.minor:
        raise ValueError("Executable runtime does not match this build interpreter")
    start = len(data) - size
    toc = data[start + offset:start + offset + toc_size]
    entries = []
    while toc:
        length, position, packed_size, raw_size, compressed, kind = struct.unpack("!IIIIBc", toc[:18])
        if length < 18 or length > len(toc):
            raise ValueError("Invalid executable archive")
        name = toc[18:length].rstrip(b"\0").decode("ascii")
        content = data[start + position:start + position + packed_size]
        if compressed:
            content = zlib.decompress(content)
        if len(content) != raw_size:
            raise ValueError("Invalid executable archive data")
        entries.append((name, kind, content))
        toc = toc[length:]
    return data[:start], entries, library.rstrip(b"\0").decode("ascii")


def rebuild_existing(existing, target):
    """Reuse the bundled Windows interpreter and bootloader, replacing all app code."""
    bootloader, entries, library = read_archive(existing)
    if not bootloader.startswith(b"MZ") or library != "python313.dll":
        raise ValueError("Expected Windows x64 Python 3.13 runtime")
    replacements = app_files()
    names = {name for name, _, _ in replacements}
    runtime = [entry for entry in entries if entry[0] not in names]
    with zipfile.ZipFile(io.BytesIO(next(data for name, _, data in runtime if name == "base_library.zip"))) as stdlib:
        if stdlib.read("encodings/__init__.pyc")[:4] != importlib.util.MAGIC_NUMBER:
            raise ValueError("Runtime bytecode does not match")
    build_executable(target, bootloader, runtime + replacements, library)
    _, rebuilt, _ = read_archive(target)
    if rebuilt != runtime + replacements:
        raise ValueError("Post-build archive validation failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--reuse-runtime", action="store_true", help="Pass existing EXE and output EXE")
    args = parser.parse_args()
    if args.reuse_runtime and len(args.inputs) == 2:
        rebuild_existing(*args.inputs)
    elif not args.reuse_runtime and len(args.inputs) == 3:
        build_windows(*args.inputs)
    else:
        parser.error("Use EMBED_ZIP WHEEL OUTPUT, or --reuse-runtime EXISTING_EXE OUTPUT")
