"""Stage exact reviewed installers into an EXISTING draft release; never publish."""
import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = 'brysonisbell/tarca-desktop-releases'
SOURCE_REPOSITORY = 'brysonisbell/tarca'
ROOT = pathlib.Path(__file__).resolve().parents[1]
UA = 'Tarca-reviewed-preview-staging'
MAX_BYTES = 1_500_000_000


def checksum(path):
    result = hashlib.sha256()
    with pathlib.Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def expected_names(version):
    return [f'Tarca-{version}-mac-arm64.dmg', f'Tarca-{version}-mac-x64.dmg',
            f'Tarca-{version}-win-x64.exe', f'Tarca-{version}-win-arm64.exe']


def validate_manifest(value, version):
    if not re.fullmatch(r'0\.\d+\.\d+', version):
        raise RuntimeError('Invalid preview version')
    if (value.get('schemaVersion') != 1 or value.get('version') != version
            or value.get('sourceRepository') != SOURCE_REPOSITORY
            or value.get('destinationRepository') != REPOSITORY
            or value.get('tag') != f'v{version}-preview'
            or type(value.get('draftReleaseId')) is not int or value['draftReleaseId'] <= 0
            or type(value.get('sourceRunId')) is not int or value['sourceRunId'] <= 0
            or not re.fullmatch('[a-f0-9]{40}', str(value.get('sourceSha', '')))):
        raise RuntimeError('Incomplete or unexpected release identity')
    archives = value.get('archives', [])
    if len(archives) != 2 or {item.get('label') for item in archives} != {'mac', 'windows'}:
        raise RuntimeError('Exactly two source archives are required')
    for item in archives:
        if (type(item.get('id')) is not int or item['id'] <= 0 or type(item.get('bytes')) is not int
                or not 0 < item['bytes'] <= MAX_BYTES or not re.fullmatch('[a-f0-9]{64}', str(item.get('sha256', '')))):
            raise RuntimeError('Incomplete archive verification evidence')
    if len({item['id'] for item in archives}) != 2:
        raise RuntimeError('Archive identities must be distinct')
    installers = value.get('installers', [])
    if len(installers) != 4 or {item.get('name') for item in installers} != set(expected_names(version)):
        raise RuntimeError('Only the four architecture-specific installers may be staged')
    for item in installers:
        label = 'mac' if '-mac-' in item['name'] else 'windows'
        archive = next(entry for entry in archives if entry['label'] == label)
        if (item.get('sourceArtifactId') != archive['id'] or item.get('sourceSha') != value['sourceSha']
                or item.get('sourceRunId') != value['sourceRunId'] or type(item.get('bytes')) is not int
                or not 0 < item['bytes'] <= MAX_BYTES or not re.fullmatch('[a-f0-9]{64}', str(item.get('sha256', '')))):
            raise RuntimeError('Installer evidence does not match the reviewed build')
    return value


def load_manifest(version, root=ROOT):
    # Validate before interpolating a path.
    if not re.fullmatch(r'0\.\d+\.\d+', version):
        raise RuntimeError('Invalid preview version')
    return validate_manifest(json.loads((pathlib.Path(root) / 'releases' / (version + '.json')).read_text()), version)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def signed_host(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ''
        return (parsed.scheme == 'https' and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.fragment and (host.endswith('.blob.core.windows.net') or host.endswith('.githubusercontent.com')))
    except ValueError:
        return False


def unsigned_response(url):
    # No Authorization/Cookie header is ever attached to archive URLs.
    for _ in range(5):
        if not signed_host(url):
            raise RuntimeError('Unexpected archive download location')
        request = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/octet-stream'})
        try:
            return urllib.request.build_opener(NoRedirect()).open(request, timeout=120)
        except urllib.error.HTTPError as error:
            if error.code not in (301, 302, 303, 307, 308):
                raise RuntimeError(f'Archive download returned HTTP {error.code}') from None
            url = urllib.parse.urljoin(url, error.headers.get('Location', ''))
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError('Archive download failed') from None
    raise RuntimeError('Too many archive redirects')


def download_archive(item, output):
    url = os.environ.get('TARCA_PREVIEW_' + item['label'].upper() + '_ARCHIVE_URL', '')
    if not url:
        raise RuntimeError('A temporary archive download secret is missing')
    target = output / (item['label'] + '.zip')
    temporary = target.with_suffix('.part')
    size = 0
    result = hashlib.sha256()
    try:
        with unsigned_response(url) as source, temporary.open('wb') as destination:
            if source.status != 200:
                raise RuntimeError('Archive was not a complete download')
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                size += len(chunk)
                if size > item['bytes']:
                    raise RuntimeError('Archive exceeded its reviewed size')
                result.update(chunk)
                destination.write(chunk)
        if size != item['bytes'] or result.hexdigest() != item['sha256']:
            raise RuntimeError('Archive checksum or size mismatch')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def verify_installers(manifest, output):
    expected = manifest['installers']
    if {entry.name for entry in output.iterdir()} != {item['name'] for item in expected} | {'SHA256SUMS.txt'}:
        raise RuntimeError('Unexpected staging files')
    for item in expected:
        path = output / item['name']
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item['bytes'] or checksum(path) != item['sha256']:
            raise RuntimeError('Staged installer changed after review')
    sums = ''.join(next(item for item in expected if item['name'] == name)['sha256'] + '  ' + name + '\n' for name in expected_names(manifest['version']))
    if (output / 'SHA256SUMS.txt').read_text() != sums:
        raise RuntimeError('Checksum manifest changed')
    return expected + [{'name': 'SHA256SUMS.txt', 'bytes': (output / 'SHA256SUMS.txt').stat().st_size, 'sha256': checksum(output / 'SHA256SUMS.txt')}]


def extract_installers(manifest, folder):
    output = folder / 'verified'
    output.mkdir(exist_ok=False)
    for archive in manifest['archives']:
        path = folder / (archive['label'] + '.zip')
        if path.stat().st_size != archive['bytes'] or checksum(path) != archive['sha256']:
            raise RuntimeError('Source archive changed before extraction')
        wanted = [item for item in manifest['installers'] if item['sourceArtifactId'] == archive['id']]
        version = manifest['version']
        allowed = ({f'Tarca-{version}-mac-{arch}.{ext}' for arch in ['arm64', 'x64'] for ext in ['dmg', 'zip']}
                   if archive['label'] == 'mac' else {f'Tarca-{version}-win-{arch}.exe' for arch in ['arm64', 'x64']} | {f'Tarca-{version}-win.exe'})
        with zipfile.ZipFile(path) as bundle:
            entries = bundle.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or set(names) - allowed or not {item['name'] for item in wanted}.issubset(names):
                raise RuntimeError('Unexpected or incomplete archive contents')
            for entry in entries:
                mode = entry.external_attr >> 16
                if entry.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or entry.flag_bits & 1 or entry.file_size <= 0:
                    raise RuntimeError('Unsafe archive entry')
            for item in wanted:
                entry = bundle.getinfo(item['name'])
                if entry.file_size != item['bytes']:
                    raise RuntimeError('Archived installer size mismatch')
                with bundle.open(entry) as source, (output / item['name']).open('wb') as destination:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        destination.write(chunk)
    sums = ''.join(next(item for item in manifest['installers'] if item['name'] == name)['sha256'] + '  ' + name + '\n' for name in expected_names(manifest['version']))
    (output / 'SHA256SUMS.txt').write_text(sums)
    verify_installers(manifest, output)


def github_request(path, token, data=None, upload=False):
    prefix = '/repos/' + REPOSITORY + '/releases/'
    if not path.startswith(prefix):
        raise RuntimeError('Unexpected destination API path')
    host = 'uploads.github.com' if upload else 'api.github.com'
    headers = {'Authorization': 'Bearer ' + token, 'User-Agent': UA, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    if upload:
        if data is None or not re.fullmatch(re.escape(prefix) + r'\d+/assets\?name=[A-Za-z0-9.%-]+', path):
            raise RuntimeError('Only asset upload mutations are permitted')
        headers['Content-Type'] = 'application/octet-stream'
    elif data is not None:
        raise RuntimeError('Release mutations are prohibited')
    try:
        request = urllib.request.Request('https://' + host + path, data=data, method='POST' if upload else 'GET', headers=headers)
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=600 if upload else 60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Destination API returned HTTP {error.code}') from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('Destination API request failed') from None


def check_draft(manifest, token):
    release = github_request(f"/repos/{REPOSITORY}/releases/{manifest['draftReleaseId']}", token)
    if (release.get('id') != manifest['draftReleaseId'] or release.get('tag_name') != manifest['tag']
            or release.get('draft') is not True or release.get('prerelease') is not True):
        raise RuntimeError('Destination is not the reviewed existing draft preview')
    return release


def verify_remote_assets(assets, expected, complete=False):
    wanted = {item['name']: item for item in expected}
    names = [item.get('name') for item in assets]
    if len(names) != len(set(names)) or set(names) - set(wanted) or (complete and set(names) != set(wanted)):
        raise RuntimeError('Unexpected or incomplete draft assets')
    for asset in assets:
        item = wanted[asset['name']]
        if asset.get('size') != item['bytes'] or asset.get('digest') != 'sha256:' + item['sha256'] or asset.get('state') != 'uploaded':
            raise RuntimeError('An existing draft asset differs from the reviewed installer')


def upload(manifest, folder):
    expected = verify_installers(manifest, folder / 'verified')
    token = os.environ.get('GH_TOKEN', '')
    if not token:
        raise RuntimeError('Scoped distribution token is unavailable')
    release = check_draft(manifest, token)
    assets = release.get('assets', [])
    verify_remote_assets(assets, expected)
    existing = {item['name'] for item in assets}
    for item in expected:
        if item['name'] in existing:
            continue
        check_draft(manifest, token)
        path = f"/repos/{REPOSITORY}/releases/{manifest['draftReleaseId']}/assets?name=" + urllib.parse.quote(item['name'], safe='')
        result = github_request(path, token, (folder / 'verified' / item['name']).read_bytes(), upload=True)
        verify_remote_assets([result], [item], complete=True)
    release = check_draft(manifest, token)
    verify_remote_assets(release.get('assets', []), expected, complete=True)
    print(json.dumps({'staged': True, 'published': False, 'releaseId': manifest['draftReleaseId'], 'version': manifest['version'], 'sourceSha': manifest['sourceSha'], 'assets': [{'name': item['name'], 'bytes': item['bytes'], 'sha256': item['sha256']} for item in expected]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['validate', 'fetch', 'upload'])
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    manifest = load_manifest(args.version)
    folder = ROOT / 'staged' / args.version
    if args.action == 'validate':
        print(json.dumps({'valid': True, 'version': manifest['version'], 'sourceSha': manifest['sourceSha'], 'sourceRunId': manifest['sourceRunId']}))
    elif args.action == 'fetch':
        folder.mkdir(parents=True, exist_ok=False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda item: download_archive(item, folder), manifest['archives']))
        extract_installers(manifest, folder)
        print('Two archives and four installers match the reviewed manifest.', flush=True)
    else:
        upload(manifest, folder)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Signed URLs/token values/HTTP response bodies/tracebacks never enter logs.
        print(json.dumps({'error': str(error) if isinstance(error, RuntimeError) else type(error).__name__}), flush=True)
        sys.exit(1)
