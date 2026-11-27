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
QT_PATCHES = 'b6ab1d68dcb0ddff1f03628c110efc24db123c87'
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


def key(group=None):
    files = ('Telegram/build/prepare/prepare.py', 'Telegram/build/prepare/msvc.py',
             'Telegram/build/qt_version.py', 'ci/telegramita/windows.ps1')
    prepare, msvc, qt, runner = (
        (SOURCE / name).read_bytes().replace(b'\r\n', b'\n') for name in files
    )
    dependency_runner, separator, _ = runner.partition(b'\n$sourceRevision =')
    if separator and group in ('base', 'media', 'rtc', 'rust'):
        qt_start = prepare.index(b"stage('qt_' + qt")
        qt_end = prepare.index(b"stage('tg_owt'", qt_start)
        non_qt_prepare = (prepare[:qt_start] + prepare[qt_end:]).replace(
            f'git checkout {QT_PATCHES}'.encode(),
            b'git checkout 8a28b8433af70771c3d101c776ad837a94bd47ec',
            1,
        )
        recipe = hashlib.sha256(non_qt_prepare + msvc + qt + dependency_runner).hexdigest()
        if recipe == '079ac57872d60ab1317c04ba0cce2c46a83199de1539084c87892f17d5114fb5':
            return '0b7d5e5523e6a51dbb244a13' if group == 'base' else 'faafc07a977dd676a37e2efe'
    digest = hashlib.sha256(b'win64-v143-14.44.35207-sdk-10.0.26100.0-qt-complete-prefix-v1')
    for data in (prepare, msvc, qt, dependency_runner):
        digest.update(data)
    cmake_revision = subprocess.check_output(['git', '-C', str(SOURCE), 'ls-files', '-s', '--', 'cmake'])
    digest.update(cmake_revision.split()[1])
    if group not in (None, 'qt'):
        digest.update(runner)
        digest.update(Path(__file__).read_bytes().replace(b'\r\n', b'\n'))
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
    return f'telegramita-x64-{key(group)}-{group}.json'


def ready_group(group, index, work, archive, cached=None):
    entry = index.get(marker(group))
    if entry is None:
        return False
    if not isinstance(entry.get('digest'), str) or not entry['digest'].startswith('sha256:'):
        raise ValueError(f'No manifest digest for {group}')
    manifest_path = work / f'{group}-manifest.json'
    download(entry, manifest_path, entry['digest'][7:])
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['key'] != key(group) or manifest['group'] != group:
        raise ValueError('Incompatible cache manifest')
    if group == 'qt' and manifest.get('qt_patches') != QT_PATCHES:
        raise ValueError('Unexpected Qt patch revision in cache manifest')
    if cached is not None and cached.exists():
        if cached.stat().st_size != manifest['bytes'] or checksum(cached) != manifest['sha256']:
            raise ValueError(f'Existing WD cache differs: {cached}')
        return True
    with archive.open('xb') as out:
        for part in manifest['parts']:
            name = part['name']
            if not name.startswith(f'telegramita-x64-{key(group)}-{group}-'):
                raise ValueError('Unexpected cache fragment')
            temp = work / 'fragment'
            download(index[name], temp, part['sha256'])
            with temp.open('rb') as stream:
                shutil.copyfileobj(stream, out)
            temp.unlink()
    if archive.stat().st_size != manifest['bytes'] or checksum(archive) != manifest['sha256']:
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


def selected_files(path, preserve_installation=False):
    if not path.exists():
        raise ValueError(f'Expected cache output missing: {path}')
    if path.is_file():
        yield path
        return
    for location, folders, names in os.walk(path):
        yield Path(location)
        if not preserve_installation:
            folders[:] = [d for d in folders if d.lower() not in SKIP_DIRS]
        for name in names:
            item = Path(location) / name
            is_intermediate = name.lower() in {'.git', 'cmakecache.txt'} or item.suffix.lower() in SKIP_EXTENSIONS
            if preserve_installation or not is_intermediate:
                yield item


def publish(group, work, upload):
    if group == 'qt':
        patches = ROOT / 'Libraries/win64/patches'
        revision = subprocess.check_output(
            ['git', '-C', str(patches), 'rev-parse', 'HEAD'], text=True
        ).strip()
        if revision != QT_PATCHES:
            raise ValueError(f'Expected Qt patches {QT_PATCHES}, got {revision}')
        print(f'QT PATCHES PASS {revision}', flush=True)
    archive = work / f'{group}.zip'
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=1, strict_timestamps=False) as output:
        for path in roots(group):
            for item in selected_files(path, preserve_installation=group == "qt"):
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
            name = f'telegramita-x64-{key(group)}-{group}-{run}-{count:03d}.zip.part'
            parts.append(send(fragment, name, upload, work))
            fragment.unlink()
            count += 1
    manifest = work / 'manifest.json'
    metadata = {'group': group, 'key': key(group), 'sha256': checksum(archive),
                'bytes': archive.stat().st_size, 'parts': parts}
    if group == 'qt':
        metadata['qt_patches'] = QT_PATCHES
    manifest.write_text(json.dumps(metadata), encoding='utf-8')
    send(manifest, marker(group), upload, work)
    print(f'CACHE SAVE {group} {archive.stat().st_size} bytes {key(group)}', flush=True)


def verify_sources(repo, expected):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])

    head = git('rev-parse', 'HEAD').strip().decode('ascii')
    if head != expected:
        raise ValueError(f'{repo}: expected {expected}, found {head}')
    count = 0
    for entry in git('ls-tree', '-rz', 'HEAD').split(b'\0'):
        if not entry:
            continue
        object_info, file_path = entry.split(b'\t', 1)
        mode, _, revision = object_info.split()
        if mode == b'160000':
            submodule = repo / os.fsdecode(file_path)
            if not (submodule / '.git').exists():
                module_config = git('config', '--blob', 'HEAD:.gitmodules', '--null', '--list')
                settings = {
                    key: value for setting in module_config.split(b'\0') if setting
                    for key, _, value in [setting.partition(b'\n')]
                }
                omitted = any(key.endswith(b'.path') and value == file_path
                              and settings.get(key.removesuffix(b'.path') + b'.update') == b'none'
                              for key, value in settings.items())
                if omitted:
                    print(f'Upstream update=none: {submodule} at {revision.decode("ascii")}')
                    continue
                raise ValueError(f'{submodule}: submodule has not been initialized')
            count += 1 + verify_sources(submodule, revision.decode('ascii'))
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('inputs', 'save', 'export', 'key', 'stages', 'verify-sources'))
    parser.add_argument('group', choices=(*STAGES, 'app'), nargs='?', default='app')
    parser.add_argument('--destination', type=Path)
    options = parser.parse_args()
    if options.action == 'verify-sources':
        count = verify_sources(SOURCE, os.environ['GITHUB_SHA'])
        if not count:
            raise ValueError('No pinned submodules were found')
        print(f'Verified root at {os.environ["GITHUB_SHA"]} and {count} pinned recursive submodules')
        return
    if options.action == 'key':
        print(key())
        return
    if options.action == 'stages':
        stages = STAGES[options.group]
        if options.group == 'qt':
            stages = ['patches', *stages]
        print(' '.join(stages))
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
                print(f'CACHE HIT {group} {key(group)}', flush=True)
                return
            for prerequisite in PREREQUISITES[group]:
                archive = work / f'{prerequisite}.zip'
                if not ready_group(prerequisite, index, work, archive):
                    raise ValueError(f'Missing required {prerequisite} cache')
                unpack(archive)
                if prerequisite == 'base':
                    msys = ROOT / 'ThirdParty/msys64'
                    for name in ('tmp', 'dev/shm', 'dev/mqueue'):
                        (msys / name).mkdir(parents=True, exist_ok=True)
                archive.unlink()
                print(f'CACHE RESTORE PASS {prerequisite} {key(prerequisite)}', flush=True)
            if group != 'app':
                print(f'CACHE MISS {group}', flush=True)
        else:
            for item in STAGES if group == 'app' else (group,):
                archive = work / f'{item}.zip'
                output = options.destination / f'{key(item)}-{item}.zip'
                if not ready_group(item, index, work, archive, cached=output):
                    raise ValueError(f'Missing final {item} cache')
                if archive.exists():
                    os.link(archive, output)
                print(f'WD CACHE PASS {output} {output.stat().st_size}', flush=True)


if __name__ == '__main__':
    main()
