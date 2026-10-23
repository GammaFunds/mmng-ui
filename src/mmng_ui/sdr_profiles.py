"""Local SDR profile data and atomic configuration persistence."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from mmng_ui.sdr import SDRConfig

_MAX_NAME_LENGTH = 64
_NAME_TYPE_ERROR = 'Profile name must be text'
_NAME_CONTROL_ERROR = 'Profile name must not contain controls'
_NAME_LENGTH_ERROR = 'Profile name must contain 1-64 characters'
_CONFIG_TYPE_ERROR = 'Profile configuration must be SDRConfig'
_PROFILES_TYPE_ERROR = 'Profiles must be a tuple of FrequencyProfile objects'
_DUPLICATE_NAME_ERROR = 'Profile names must be unique (case insensitive)'
_SELECTION_ERROR = 'Selected profile does not exist'
_DUPLICATE_KEY_ERROR = 'Duplicate JSON object key'
_FIELDS_ERROR = 'Invalid profile storage fields'
_VERSION_ERROR = 'Unsupported profile storage version'
_PROFILES_ARRAY_ERROR = 'Profiles must be a JSON array'
_DESTINATION_ERROR = 'Profile destination must be a regular file, not a symlink'
_REGULAR_ERROR = 'Profile destination must be a regular file'


class ProfileStoreError(ValueError):
    """Profile storage failed, possibly after the replacement commit point."""

    def __init__(self, message: str, *, committed: bool = False) -> None:
        super().__init__(message)
        self.committed = committed


@dataclass(frozen=True)
class FrequencyProfile:
    name: str
    config: SDRConfig

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError(_NAME_TYPE_ERROR)
        if any(unicodedata.category(char).startswith('C') for char in self.name):
            raise ValueError(_NAME_CONTROL_ERROR)
        name = self.name.strip()
        if not 1 <= len(name) <= _MAX_NAME_LENGTH:
            raise ValueError(_NAME_LENGTH_ERROR)
        if not isinstance(self.config, SDRConfig):
            raise TypeError(_CONFIG_TYPE_ERROR)
        object.__setattr__(self, 'name', name)


@dataclass(frozen=True)
class ProfileCatalog:
    profiles: tuple[FrequencyProfile, ...] = ()
    selected_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.profiles, tuple) or any(not isinstance(p, FrequencyProfile) for p in self.profiles):
            raise TypeError(_PROFILES_TYPE_ERROR)
        names = [profile.name for profile in self.profiles]
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError(_DUPLICATE_NAME_ERROR)
        if self.selected_name is not None and (
            not isinstance(self.selected_name, str) or self.selected_name not in names
        ):
            raise ValueError(_SELECTION_ERROR)

    @property
    def selected(self) -> FrequencyProfile | None:
        return next((profile for profile in self.profiles if profile.name == self.selected_name), None)


def default_profile_path() -> Path:
    root = os.environ.get('XDG_CONFIG_HOME', '')
    base = Path(root) if root and Path(root).is_absolute() else Path.home() / '.config'
    return base / 'mmng-ui' / 'sdr-profiles.json'


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_DUPLICATE_KEY_ERROR)
        result[key] = value
    return result


def _keys(value: object, keys: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(_FIELDS_ERROR)


def _decode(raw: str) -> ProfileCatalog:
    data = json.loads(raw, object_pairs_hook=_unique_object)
    _keys(data, {'version', 'selected_name', 'profiles'})
    if type(data['version']) is not int or data['version'] != 1:
        raise ValueError(_VERSION_ERROR)
    if not isinstance(data['profiles'], list):
        raise TypeError(_PROFILES_ARRAY_ERROR)
    profiles = []
    for item in data['profiles']:
        _keys(item, {'name', 'frequency_hz', 'device_index', 'gain_db', 'ppm_correction'})
        config = SDRConfig(item['frequency_hz'], item['gain_db'], item['device_index'], item['ppm_correction'])
        profiles.append(FrequencyProfile(item['name'], config))
    return ProfileCatalog(tuple(profiles), data['selected_name'])


def _check_destination(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(_DESTINATION_ERROR)


def _require_regular(mode: int) -> None:
    if not stat.S_ISREG(mode):
        raise ValueError(_REGULAR_ERROR)


def load_profiles(path: Path) -> ProfileCatalog:
    try:
        _check_destination(path)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return ProfileCatalog()
        with os.fdopen(descriptor, encoding='utf-8') as stream:
            _require_regular(os.fstat(stream.fileno()).st_mode)
            return _decode(stream.read())
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        message = f'Cannot load SDR profiles: {exc}'
        raise ProfileStoreError(message) from exc


def save_profiles(path: Path, catalog: ProfileCatalog) -> None:
    temporary = None
    descriptor = None
    committed = False
    try:
        # Refuse to overwrite malformed or unsupported existing storage.
        load_profiles(path)
        data = {
            'version': 1,
            'selected_name': catalog.selected_name,
            'profiles': [
                {
                    'name': profile.name,
                    'frequency_hz': profile.config.frequency_hz,
                    'device_index': profile.config.device_index,
                    'gain_db': profile.config.gain_db,
                    'ppm_correction': profile.config.ppm_correction,
                }
                for profile in catalog.profiles
            ],
        }
        content = json.dumps(data, allow_nan=False, indent=2) + '\n'
        _decode(content)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.sdr-profiles-', dir=path.parent)
        stream = os.fdopen(descriptor, 'w', encoding='utf-8')
        descriptor = None
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _check_destination(path)
        os.replace(temporary, path)
        committed = True
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        cleanup_details = ''
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as close_error:
                cleanup_details += f'; temporary descriptor cleanup failed: {close_error}'
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError as cleanup_error:
                cleanup_details += f'; temporary cleanup failed: {cleanup_error}; retained temporary file: {temporary}'
        detail = 'Saved SDR profiles; durability uncertain' if committed else 'Cannot save SDR profiles'
        message = f'{detail}: {exc}{cleanup_details}'
        raise ProfileStoreError(message, committed=committed) from exc
