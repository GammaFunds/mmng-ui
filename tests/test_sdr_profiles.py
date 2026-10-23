import json
import os
import stat
from dataclasses import replace

import pytest

import mmng_ui.sdr_profiles as store
from mmng_ui.sdr import SDRConfig
from mmng_ui.sdr_profiles import (
    FrequencyProfile,
    ProfileCatalog,
    ProfileStoreError,
    default_profile_path,
    load_profiles,
    save_profiles,
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)


def catalog():
    return ProfileCatalog((FrequencyProfile('Example', SDRConfig(123456789, 17.5, 2, -12)),), 'Example')


def test_round_trip_permissions_and_explicit_privacy_schema():
    path = default_profile_path()
    assert load_profiles(path) == ProfileCatalog()
    assert not path.parent.exists()
    save_profiles(path, catalog())
    assert load_profiles(path) == catalog()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text()) == {
        'version': 1,
        'selected_name': 'Example',
        'profiles': [
            {
                'name': 'Example',
                'frequency_hz': 123456789,
                'gain_db': 17.5,
                'device_index': 2,
                'ppm_correction': -12,
            }
        ],
    }
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize('xdg', [None, '', 'relative', '/absolute/config'])
def test_xdg_path(tmp_path, monkeypatch, xdg):
    if xdg is not None:
        monkeypatch.setenv('XDG_CONFIG_HOME', xdg)
    root = '/absolute/config' if xdg == '/absolute/config' else str(tmp_path / '.config')
    assert str(default_profile_path()) == root + '/mmng-ui/sdr-profiles.json'


@pytest.mark.parametrize('name', ['', '  ', 'x' * 65, 'x\ny', 'x\x7fy', 'x\u0085y', 12])
def test_invalid_names(name):
    with pytest.raises((ValueError, TypeError)):
        FrequencyProfile(name, SDRConfig(1, 0))


def test_names_duplicates_and_selection():
    profile = FrequencyProfile('  Example  ', SDRConfig(1, 0))
    assert profile.name == 'Example'
    with pytest.raises(ValueError, match='unique'):
        ProfileCatalog((profile, replace(profile, name='EXAMPLE')))
    with pytest.raises(ValueError, match='Selected'):
        ProfileCatalog((profile,), 'missing')
    assert len(ProfileCatalog((profile, replace(profile, name='Other'))).profiles) == 2


@pytest.mark.parametrize(
    'mutation',
    [
        lambda data: data.update(version=2),
        lambda data: data.update(version=True),
        lambda data: data.update(extra='decoded-message-sentinel'),
        lambda data: data.update(selected_name='missing'),
        lambda data: data.update(profiles={}),
        lambda data: data['profiles'][0].update(executable='sh'),
        lambda data: data['profiles'][0].pop('ppm_correction'),
        lambda data: data['profiles'][0].update(frequency_hz=True),
        lambda data: data['profiles'][0].update(gain_db=float('nan')),
        lambda data: data['profiles'].append(dict(data['profiles'][0], name='EXAMPLE')),
    ],
)
def test_schema_rejection_preserves_existing_bytes(tmp_path, mutation):
    path = tmp_path / 'profiles.json'
    save_profiles(path, catalog())
    data = json.loads(path.read_text())
    mutation(data)
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    with pytest.raises(ProfileStoreError):
        load_profiles(path)
    with pytest.raises(ProfileStoreError):
        save_profiles(path, catalog())
    assert path.read_bytes() == original


@pytest.mark.parametrize('raw', ['{', '{"version":1,"version":1}', '[]', 'null'])
def test_bad_json(tmp_path, raw):
    path = tmp_path / 'profiles.json'
    path.write_text(raw)
    with pytest.raises(ProfileStoreError):
        load_profiles(path)


@pytest.mark.parametrize('kind', ['symlink', 'dangling', 'directory', 'fifo'])
def test_reject_unsafe_destination(tmp_path, kind):
    path = tmp_path / 'profiles.json'
    target = tmp_path / 'target'
    if kind == 'symlink':
        save_profiles(target, catalog())
        path.symlink_to(target)
    elif kind == 'dangling':
        path.symlink_to(target)
    elif kind == 'directory':
        path.mkdir()
    else:
        os.mkfifo(path)
    for operation in (lambda: load_profiles(path), lambda: save_profiles(path, catalog())):
        with pytest.raises(ProfileStoreError):
            operation()


@pytest.mark.parametrize('point', ['serialize', 'write', 'flush', 'fsync', 'replace'])
def test_precommit_failures_preserve_original(tmp_path, monkeypatch, point):
    path = tmp_path / 'profiles.json'
    save_profiles(path, catalog())
    original = path.read_bytes()

    def fail(*_args, **_kwargs):
        message = 'injected'
        raise OSError(message)

    if point == 'serialize':
        monkeypatch.setattr(store.json, 'dumps', fail)
    elif point == 'replace':
        monkeypatch.setattr(store.os, 'replace', fail)
    elif point == 'fsync':
        monkeypatch.setattr(store.os, 'fsync', fail)
    else:
        original_fdopen = store.os.fdopen

        class BrokenFile:
            def __init__(self, *args, **kwargs):
                self.file = original_fdopen(*args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.file.close()

            def write(self, value):
                return fail() if point == 'write' else self.file.write(value)

            def flush(self):
                return fail()

        def fdopen(fd, *args, **kwargs):
            if args and args[0] == 'w':
                return BrokenFile(fd, *args, **kwargs)
            return original_fdopen(fd, *args, **kwargs)

        monkeypatch.setattr(store.os, 'fdopen', fdopen)
    with pytest.raises(ProfileStoreError) as error:
        save_profiles(path, ProfileCatalog())
    assert not error.value.committed
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_replace_is_commit_point_and_directory_sync_uncertainty(tmp_path, monkeypatch):
    path = tmp_path / 'profiles.json'
    save_profiles(path, catalog())
    original_replace = store.os.replace
    original_sync = store.os.fsync
    observations = []

    def replace_file(source, destination):
        observations.append(load_profiles(path))
        assert stat.S_IMODE(os.stat(source).st_mode) == 0o600
        original_replace(source, destination)
        observations.append(load_profiles(path))

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            message = 'directory sync failed'
            raise OSError(message)
        original_sync(fd)

    monkeypatch.setattr(store.os, 'replace', replace_file)
    monkeypatch.setattr(store.os, 'fsync', sync)
    with pytest.raises(ProfileStoreError) as error:
        save_profiles(path, ProfileCatalog())
    assert error.value.committed
    assert observations == [catalog(), ProfileCatalog()]
    assert load_profiles(path) == ProfileCatalog()
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('operation', ['write', 'replace'])
def test_primary_save_and_unlink_failures_preserve_contract(tmp_path, monkeypatch, operation):
    path = tmp_path / 'profiles.json'
    original_catalog = catalog()
    save_profiles(path, original_catalog)
    original = path.read_bytes()
    unrelated = tmp_path / '.sdr-profiles-unrelated'
    unrelated.write_text('preserve unrelated data')
    primary = OSError(f'injected {operation} failure')
    cleanup = OSError('injected unlink failure')
    original_unlink = store.Path.unlink
    retained = []

    def fail_operation(*_args, **_kwargs):
        raise primary

    def fail_unlink(temporary, *args, **kwargs):
        if temporary.parent == tmp_path and temporary != unrelated and temporary.name.startswith('.sdr-profiles-'):
            retained.append(temporary)
            raise cleanup
        return original_unlink(temporary, *args, **kwargs)

    if operation == 'replace':
        monkeypatch.setattr(store.os, 'replace', fail_operation)
    else:
        original_fdopen = store.os.fdopen

        class BrokenWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def write(self, _value):
                raise primary

        def fdopen(fd, *args, **kwargs):
            stream = original_fdopen(fd, *args, **kwargs)
            return BrokenWriter(stream) if args and args[0] == 'w' else stream

        monkeypatch.setattr(store.os, 'fdopen', fdopen)
    monkeypatch.setattr(store.Path, 'unlink', fail_unlink)
    with pytest.raises(ProfileStoreError) as caught:
        save_profiles(path, ProfileCatalog())
    error = caught.value
    assert not error.committed
    assert error.__cause__ is primary
    assert str(primary) in str(error)
    assert str(cleanup) in str(error)
    assert len(retained) == 1
    assert retained[0].is_file()
    assert str(retained[0]) in str(error)
    assert 'retained' in str(error).lower()
    assert path.read_bytes() == original
    assert load_profiles(path) == original_catalog
    assert original_catalog.selected_name == 'Example'
    assert unrelated.read_text() == 'preserve unrelated data'
    assert set(tmp_path.iterdir()) == {path, unrelated, retained[0]}
