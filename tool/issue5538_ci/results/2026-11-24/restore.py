"""Verify or restore the exact evidence files listed by manifest.json."""

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_relative(name):
    path = PurePosixPath(name)
    require(not path.is_absolute() and path.parts and all(p not in {'.', '..'} for p in path.parts),
            'Unsafe archive path: ' + name)
    require('\\' not in name and ':' not in name and not any(ord(c) < 32 for c in name),
            'Nonportable archive path: ' + name)
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    require(all(not p.endswith((' ', '.')) and p.split('.')[0].rstrip(' ').upper() not in reserved for p in path.parts),
            'Windows device or normalized path alias: ' + name)
    return Path(*path.parts)


def read_verified(root, receipt):
    path = root / safe_relative(receipt['path'])
    require(not path.is_symlink() and path.resolve().is_relative_to(root), 'Archive path escaped its root')
    data = path.read_bytes()
    require(digest(data) == receipt['sha256'], 'SHA-256 mismatch: ' + receipt['path'])
    if 'bytes' in receipt:
        require(len(data) == receipt['bytes'], 'Length mismatch: ' + receipt['path'])
    return data


def verify_group(root, group):
    require(safe_relative(group['id']).as_posix() == group['id'] and '/' not in group['id'], 'Unsafe group id')
    read_verified(root, group['summary'])
    record = group['rawArchive']
    joined = b''.join(read_verified(root, part) for part in record['partsInConcatenationOrder'])
    require(len(joined) == record['bytes'] and digest(joined) == record['sha256'], 'Joined archive mismatch: ' + group['id'])
    expected = {item['path']: item for item in record['files']}
    require(len(expected) == record['fileCount'] == len(record['files']), 'Duplicate manifest file')
    payloads = {}
    with tarfile.open(fileobj=io.BytesIO(joined), mode='r:gz') as archive:
        for member in archive:
            require(member.isfile() and member.name in expected and member.name not in payloads, 'Unexpected tar member: ' + member.name)
            safe_relative(member.name)
            data = archive.extractfile(member).read()
            receipt = expected[member.name]
            require(len(data) == receipt['bytes'] and digest(data) == receipt['sha256'], 'Raw evidence mismatch: ' + member.name)
            payloads[member.name] = data
    require(payloads.keys() == expected.keys(), 'Missing raw evidence files')
    return payloads


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--verify', action='store_true')
    mode.add_argument('--output', type=Path)
    parser.add_argument('--group', action='append', help='Restore only the named manifest group; repeatable.')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest_bytes = (root / 'manifest.json').read_bytes()
    expected_manifest = (root / 'manifest.sha256').read_text().split()[0]
    require(digest(manifest_bytes) == expected_manifest, 'Manifest checksum mismatch')
    manifest = json.loads(manifest_bytes)
    require(manifest['schemaVersion'] == 1, 'Unsupported manifest schema')
    read_verified(root, manifest['corpusProvenance'])
    read_verified(root, manifest['overview'])
    for receipt in manifest['tools']:
        read_verified(root, receipt)
    selected = [g for g in manifest['groups'] if not args.group or g['id'] in args.group]
    require(not args.group or set(args.group) == {g['id'] for g in selected}, 'Unknown group requested')
    verified = {g['id']: verify_group(root, g) for g in selected}
    output = args.output.resolve() if args.output else None
    if output:
        require(not output.exists(), 'Restore output must be a new directory')
        output.mkdir(parents=True)
        for group, payloads in verified.items():
            for name, data in payloads.items():
                destination = output / group / safe_relative(name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open('xb') as target:
                    target.write(data)
    print(json.dumps({'status': 'verified' if not output else 'restored', 'groups': len(verified),
        'files': sum(len(p) for p in verified.values()),
        'bytes': sum(len(b) for p in verified.values() for b in p.values()),
        'output': str(output) if output else None}))


if __name__ == '__main__':
    main()
