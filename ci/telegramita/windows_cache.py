import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

SOURCE = Path(__file__).resolve().parents[2]
ROOT = Path('D:/TBuild')
DRAFT = 459271016
API = 'https://api.github.com/repos/dimadr/tdesktop'
PART_SIZE = 512 * 1024 * 1024
STAGES = {
    'base': ['patches', 'msys64', 'python', 'NuGet', 'jom', 'gyp', 'lzma', 'zlib',
             'mozjpeg', 'openssl3', 'libwebp', 'liblcms2', 'qt6_windows7', 'tg_angle'],
    'media': ['opus', 'rnnoise', 'gas-preprocessor', 'dav1d', 'openh264', 'libavif',
              'libjxl', 'libvpx', 'nv-codec-headers', 'regex', 'ffmpeg', 'libheif', 'openal-soft'],
    'rtc': ['breakpad', 'tg_owt', 'tde2e'],
    'qt': ['qt_6.12.0'],
    'rust': ['rust', 'tlottie', 'wallet-engine', 'tdesktop_rust'],
}
TOOLS = {'msys64', 'python', 'NuGet', 'jom', 'gyp', 'rust'}
PREREQUISITES = {'base': [], 'media': ['base'], 'rtc': ['base', 'media'],
                 'qt': ['base'], 'rust': ['base'],
                 'app': ['base', 'media', 'rtc', 'qt', 'rust']}
SKIP_DIRS = {'.git', 'cmakefiles', 'debug', 'debug_x64', 'debug.win', '__pycache__'}
SKIP_EXTENSIONS = {'.o', '.obj', '.pdb', '.pch', '.ilk', '.tlog', '.ipch', '.iobj', '.ipdb'}


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def key():
    digest = hashlib.sha256(b'win64-v143-14.44.35207-sdk-10.0.26100.0-release-d-build-v1')
    for name in ('Telegram/build/prepare/prepare.py', 'Telegram/build/prepare/msvc.py',
                 'Telegram/build/qt_version.py', 'ci/telegramita/windows_cache.py',
                 'ci/telegramita/windows.ps1'):
        digest.update((SOURCE / name).read_bytes().replace(b'\r\n', b'\n'))
    return digest.hexdigest()[:24]


def curl(url, output, *args, upload=False):
    token = os.environ['GH_TOKEN']
    if not token or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~+/=' for c in token):
        raise ValueError('Invalid GitHub credential format')
    host = urllib.parse.urlparse(url).hostname
    if host not in ('api.github.com', 'uploads.github.com'):
        raise ValueError('Unexpected GitHub endpoint')
    command = ['curl', '--config', '-', '-fLsS', '--connect-timeout', '20', '--max-time', '900']
    if not upload:
        command += ['--retry', '3', '--retry-all-errors']
    subprocess.run(command + ['-o', str(output), *args, url],
                   input='header = "Authorization: Bearer ' + token + '"\n',
                   text=True, check=True)


def api(path, work):
    curl(API + path, work / 'response.json')
    return json.loads((work / 'response.json').read_text(encoding='utf-8'))


def assets(work):
    result = {}
    for page in range(1, 12):
        batch = api(f'/releases/{DRAFT}/assets?per_page=100&page={page}', work)
        result.update((entry['name'], entry) for entry in batch if entry['state'] == 'uploaded')
        if len(batch) < 100:
            return result
    raise ValueError('Draft asset limit exceeded')


def download(asset, destination, expected):
    curl(f"{API}/releases/assets/{asset['id']}", destination, '-H', 'Accept: application/octet-stream')
    actual = checksum(destination)
    if actual != expected or destination.stat().st_size != asset['size']:
        raise ValueError(f"Integrity error in {asset['name']}")


def marker(group):
    return f'telegramita-x64-{key()}-{group}.json'


def ready_group(group, index, work, archive):
    entry = index.get(marker(group))
    if entry is None:
        return False
    if not isinstance(entry.get('digest'), str) or not entry['digest'].startswith('sha256:'):
        raise ValueError(f'No manifest digest for {group}')
    manifest_path = work / f'{group}-manifest.json'
    download(entry, manifest_path, entry['digest'][7:])
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['key'] != key() or manifest['group'] != group:
        raise ValueError('Incompatible cache manifest')
    with archive.open('xb') as out:
        for part in manifest['parts']:
            name = part['name']
            if not name.startswith(f'telegramita-x64-{key()}-{group}-'):
                raise ValueError('Unexpected cache fragment')
            temp = work / 'fragment'
            download(index[name], temp, part['sha256'])
            with temp.open('rb') as stream:
                shutil.copyfileobj(stream, out)
            temp.unlink()
    if checksum(archive) != manifest['sha256']:
        raise ValueError(f'Archive checksum mismatch for {group}')
    return True


def unpack(archive):
    with zipfile.ZipFile(archive) as package:
        for name in package.namelist():
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
                raise ValueError(f'Invalid cache path {name}')
            if path.parts[0] not in ('Libraries', 'ThirdParty'):
                raise ValueError(f'Unexpected cache path {name}')
        package.extractall(ROOT)


def send(path, name, upload_url, work):
    digest = checksum(path)
    try:
        curl(upload_url + '?' + urllib.parse.urlencode({'name': name}), work / 'upload.json',
             '-H', 'Content-Type: application/octet-stream', '--data-binary', '@' + str(path), upload=True)
        asset = json.loads((work / 'upload.json').read_text(encoding='utf-8'))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        asset = assets(work).get(name)
        if asset is None:
            raise
    if asset.get('digest') != 'sha256:' + digest or asset['size'] != path.stat().st_size:
        raise ValueError(f'Invalid upload response for {name}')
    return {'name': name, 'sha256': digest}


def roots(group):
    for stage in STAGES[group]:
        directory = ROOT / ('ThirdParty' if stage in TOOLS else 'Libraries/win64')
        if stage == 'qt_6.12.0':
            yield directory / 'Qt-6.12.0'
        elif stage == 'tdesktop_rust':
            yield directory / stage / 'out'
        elif stage not in ('tlottie', 'wallet-engine'):
            yield directory / stage
        yield directory / 'cache_keys' / stage
    if group in ('base', 'media', 'rtc'):
        p = ROOT / 'Libraries/win64/local'
        if p.exists():
            yield p


def selected_files(path):
    if not path.exists():
        raise ValueError(f'Expected cache output missing: {path}')
    if path.is_file():
        yield path
        return
    for location, folders, names in os.walk(path):
        folders[:] = [d for d in folders if d.lower() not in SKIP_DIRS]
        for name in names:
            item = Path(location) / name
            if name.lower() in {'.git', 'cmakecache.txt'} or item.suffix.lower() in SKIP_EXTENSIONS:
                continue
            yield item


def publish(group, work, upload):
    archive = work / f'{group}.zip'
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as output:
        for path in roots(group):
            for item in selected_files(path):
                output.write(item, item.relative_to(ROOT).as_posix())
    parts = []
    run = os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']
    with archive.open('rb') as data:
        count = 0
        while True:
            fragment = work / 'fragment'
            remaining = PART_SIZE
            with fragment.open('xb') as out:
                while remaining:
                    chunk = data.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            if fragment.stat().st_size == 0:
                fragment.unlink()
                break
            name = f'telegramita-x64-{key()}-{group}-{run}-{count:03d}.zip.part'
            parts.append(send(fragment, name, upload, work))
            fragment.unlink()
            count += 1
    manifest = work / 'manifest.json'
    manifest.write_text(json.dumps({'group': group, 'key': key(), 'sha256': checksum(archive),
                                    'bytes': archive.stat().st_size, 'parts': parts}), encoding='utf-8')
    send(manifest, marker(group), upload, work)
    print(f'CACHE SAVE {group} {archive.stat().st_size} bytes {key()}', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('inputs', 'save', 'export', 'key', 'stages'))
    parser.add_argument('group', choices=(*STAGES, 'app'), nargs='?', default='app')
    parser.add_argument('--destination', type=Path)
    options = parser.parse_args()
    if options.action == 'key':
        print(key())
        return
    if options.action == 'stages':
        print(' '.join(STAGES[options.group]))
        return
    group = options.group
    if options.action == 'export':
        if options.destination is None:
            raise ValueError('WD archive destination is required')
        options.destination.mkdir(parents=True, exist_ok=True)
        parent = options.destination
    else:
        parent = ROOT / 'scratch'
        parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent, prefix='stage-cache-') as directory:
        work = Path(directory)
        release = api(f'/releases/{DRAFT}', work)
        if not release['draft'] or release['published_at'] is not None:
            raise ValueError('Cache transport requires an unpublished draft')
        index = assets(work)
        if options.action == 'save':
            if marker(group) not in index:
                publish(group, work, release['upload_url'].split('{', 1)[0])
        elif options.action == 'inputs':
            if group != 'app' and marker(group) in index:
                print(f'CACHE HIT {group} {key()}', flush=True)
                return
            for prerequisite in PREREQUISITES[group]:
                archive = work / f'{prerequisite}.zip'
                if not ready_group(prerequisite, index, work, archive):
                    raise ValueError(f'Missing required {prerequisite} cache')
                unpack(archive)
                archive.unlink()
                print(f'CACHE RESTORE PASS {prerequisite} {key()}', flush=True)
            if group != 'app':
                print(f'CACHE MISS {group}', flush=True)
        else:
            for item in STAGES:
                archive = work / f'{item}.zip'
                if not ready_group(item, index, work, archive):
                    raise ValueError(f'Missing final {item} cache')
                output = options.destination / f'{key()}-{item}.zip'
                if output.exists():
                    if checksum(output) != checksum(archive):
                        raise ValueError(f'Existing WD cache differs: {output}')
                else:
                    os.link(archive, output)
                print(f'WD CACHE PASS {output} {output.stat().st_size}', flush=True)


if __name__ == '__main__':
    main()
