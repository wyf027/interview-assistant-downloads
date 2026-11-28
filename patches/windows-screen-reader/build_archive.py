"""Apply the Windows reader patch to the original beta.2 Electron archive."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def asset_entry(archive_tree, name):
    entry = archive_tree
    for part in name.split("/"):
        entry = entry["files"][part]
    return entry


def build(original, output):
    package = Path(__file__).resolve().parent
    assets = json.loads((package / "assets.json").read_text(encoding="utf-8"))
    if original.resolve() == output.resolve():
        raise ValueError("Choose a separate output archive.")
    if output.exists():
        raise FileExistsError(output)
    with original.open("rb") as source, tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        work = Path(temporary)
        _, header_size, _, json_size = struct.unpack("<4I", source.read(16))
        tree = json.loads(source.read(json_size))
        payload = header_size + 8
        extra_offset = original.stat().st_size - payload
        for name, hashes in assets.items():
            entry = asset_entry(tree, name)
            source.seek(payload + int(entry["offset"]))
            baseline = source.read(entry["size"])
            if digest(baseline) != hashes["baseline_sha256"]:
                raise ValueError(f"Unexpected original asset: {name}")
            file = work / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(baseline)
        subprocess.run(["git", "-c", "apply.whitespace=nowarn", "apply", "--", str(package / "app.patch")],
                       cwd=work, check=True)
        replacements = []
        for name, hashes in assets.items():
            data = (work / name).read_bytes()
            if digest(data) != hashes["patched_sha256"]:
                raise ValueError(f"Unexpected patched asset: {name}")
            entry = asset_entry(tree, name)
            integrity = entry["integrity"]
            block_size = integrity["blockSize"]
            integrity.update(hash=digest(data), blocks=[
                digest(data[start:start + block_size]) for start in range(0, len(data), block_size)
            ])
            entry.update(size=len(data), offset=str(extra_offset))
            replacements.append(data)
            extra_offset += len(data)
        encoded = json.dumps(tree, ensure_ascii=False, separators=(",", ":")).encode()
        padding = (-len(encoded)) % 4
        staged = work / "app.asar"
        with staged.open("wb") as target:
            target.write(struct.pack("<4I", 4, len(encoded) + padding + 8, len(encoded) + padding + 4, len(encoded)))
            target.write(encoded + bytes(padding))
            source.seek(payload)
            shutil.copyfileobj(source, target)
            for data in replacements:
                target.write(data)
        staged.rename(output.absolute())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path, help="Original Windows beta.2 resources/app.asar")
    parser.add_argument("output", type=Path, help="New archive path in an existing writable directory")
    args = parser.parse_args()
    build(args.original, args.output)
